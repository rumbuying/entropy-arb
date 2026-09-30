"""Historical CSV import (V2-007, spec §7.3).

Known header versions (from entropy_arb.engine — the writer is the schema):

  taker v1  ts,direction,buy_venue,sell_venue,qty,...,ok,buy_fill,
            sell_fill,buy_status,sell_status,fill_edge_usd
  maker v1  ts,hedge_dir,hedge_qty,maker_px,hedge_px,gross_edge_bps,
            net_edge_bps,fill_to_hedge_ms,n_fills,failures

Rules the spec pins down:

* incremental — each file's imported line offset is stored; only appended
  lines are read; a changed header or size/mtime identity is a NEW source
  (rotation), the old one keeps its rows;
* idempotent — event_id is src:{source}:line:{n}, so re-running an import
  inserts nothing new. Line-offset idempotence is IMPORT idempotence, NOT
  proof of trade-level dedupe: old rows carry no fill ids, so dedupe_key
  stays null and the events stay `unresolved`;
* half lines wait for the next run; bad rows are recorded with line
  numbers and reasons — never silently dropped, never counted as trades;
* mapping is explicit: the caller names the profile, the importer resolves
  the strategy through the same launch identity the runner used. Unknown
  mapping keeps strategy_id null (unresolved), never guessed from file
  names.

These legacy files are PARTIAL evidence: the taker CSV has directions and
fill quantities but no order ids, no per-leg prices actually filled, and
no fees; the maker CSV is hedge batches whose n_fills is not trustworthy
(spec §8.3). The importer says so in its coverage output instead of
pretending a complete ledger.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import time
from typing import Dict, List, Optional, Tuple

TAKER_HEADER = ["ts", "direction", "buy_venue", "sell_venue", "qty",
                "buy_limit", "sell_limit", "buy_notional", "sell_notional",
                "exp_edge_usd", "gross_edge_usd", "marginal_premium_bps",
                "midline_bps", "inv_add_bps", "ok", "buy_fill", "sell_fill",
                "buy_status", "sell_status", "fill_edge_usd"]
MAKER_HEADER = ["ts", "hedge_dir", "hedge_qty", "maker_px", "hedge_px",
                "gross_edge_bps", "net_edge_bps", "fill_to_hedge_ms",
                "n_fills", "failures"]


def _header_hash(header: List[str]) -> str:
    return hashlib.sha256(",".join(header).encode()).hexdigest()[:12]


def count_lines(path: str) -> int:
    n = 0
    with open(path, "rb") as fh:
        for _ in fh:
            n += 1
    return max(0, n - 1)          # minus header


def file_identity(path: str, header_hash: str, generation: int = 1) -> str:
    """path + schema + generation. Appends keep the identity (offsets
    accumulate); a rotation — the file now holds FEWER lines than the
    stored offset — starts the next generation (see import_csv)."""
    return f"{header_hash}-g{generation}"


def classify(path: str) -> Optional[str]:
    """'taker' | 'maker' | None by the header line."""
    try:
        with open(path, newline="", errors="replace") as fh:
            first = fh.readline()
        header = next(csv.reader([first]))
    except (OSError, StopIteration):
        return None
    if header == TAKER_HEADER:
        return "taker"
    if header == MAKER_HEADER:
        return "maker"
    return None


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class ImportReport(dict):
    pass


def import_csv(storage, *, path: str, strategy_id: Optional[str],
               batch: Optional[str] = None) -> ImportReport:
    """Import one CSV incrementally. Returns a coverage report — never a
    'success' claim about trade completeness."""
    batch = batch or f"imp-{int(time.time())}-{os.getpid()}"
    kind = classify(path)
    if kind is None:
        return ImportReport(path=path, status="unknown_header", rows=0,
                            imported=0, bad=[], note="header not recognized")
    with open(path, newline="", errors="replace") as fh:
        first = fh.readline()
    header = next(csv.reader([first]))
    hh = _header_hash(header)
    # pick up the latest active source for this path+schema; a file with
    # FEWER lines than the stored offset was truncated or rotated → next
    # generation, old rows stay under the old identity
    lines_now = count_lines(path)
    prior = [s for s in storage.list_import_sources(limit=500)
             if s["path"] == path and s["status"] == "active"
             and s["header_hash"] == hh]
    prior.sort(key=lambda s: s["id"], reverse=True)
    src = prior[0] if prior else None
    if src is not None and lines_now < src["offset_line"]:
        # fewer data lines than the stored offset → truncated / rotated
        gens = [int(s["file_identity"].rsplit("-g", 1)[1])
                for s in prior if s["file_identity"].rsplit("-g", 1)[-1]
                .isdigit()]
        src = None                       # rotation → new generation below
        next_gen = (max(gens) + 1) if gens else 1
    else:
        next_gen = None
    ident = file_identity(path, hh, next_gen or 1)
    if src is not None and src["file_identity"] != ident:
        src = None                       # generation bumped
    start_line = src["offset_line"] if src else 0
    prev_total = src["rows_total"] if src else 0
    prev_bad = src["rows_bad"] if src else 0
    prev_bad_list = (json.loads(src["bad_json"]) if src and src["bad_json"]
                     else [])

    # the source row must exist before events so event ids can reference it
    if src is None:
        src_id = storage.upsert_import_source(
            path=path, file_identity=ident, header_hash=hh,
            offset_line=0, rows_total=0, rows_bad=0, bad=None)
    else:
        src_id = src["id"]

    imported = bad_rows = total = 0
    bad: List[dict] = []
    torn = False
    with open(path, newline="", errors="replace") as fh:
        reader = csv.reader(fh)
        next(reader)                              # header
        for lineno, row in enumerate(reader, start=2):
            if lineno - 2 < start_line:           # already imported
                continue
            total += 1
            if not row or all(not c.strip() for c in row):
                continue                          # blank tail
            try:
                payload = _parse_row(kind, header, row)
                if payload is None:
                    raise ValueError("unparsable fields")
            except ValueError as e:
                if "truncated line" in str(e):
                    # torn tail — leave it for the next round: the offset
                    # stops BEFORE this line so completion gets imported
                    torn = True
                    break
                bad_rows += 1
                bad.append({"line": lineno, "reason": str(e)[:120],
                            "raw": ",".join(row)[:200]})
                continue
            except Exception as e:
                bad_rows += 1
                bad.append({"line": lineno, "reason": str(e)[:120],
                            "raw": ",".join(row)[:200]})
                continue
            event_ts = payload.pop("event_ts", None)
            # no real fill ids exist in these schemas → dedupe_key stays
            # null: line-offset idempotence is import idempotence only
            ok = storage.insert_event(
                event_id=f"src:{src_id}:line:{lineno}",
                event_type=f"{kind}_csv_row",
                import_batch=batch, source_id=src_id, source_line=lineno,
                event_ts=event_ts, payload=payload,
                strategy_id=strategy_id,
                venue=payload.get("sell_venue") or payload.get("hedge_dir"),
                unresolved=True)
            if ok:
                imported += 1
    consumed = total if not torn else max(0, total - 1)
    storage.upsert_import_source(
        path=path, file_identity=ident, header_hash=hh,
        offset_line=start_line + consumed,
        rows_total=prev_total + total,
        rows_bad=prev_bad + bad_rows,
        bad=(prev_bad_list + bad)[:50])
    return ImportReport(path=path, status="ok", kind=kind, source_id=src_id,
                        rows=total, imported=imported, bad=bad[:50],
                        unresolved=imported,
                        note="legacy CSV: no order ids / fees — partial "
                             "evidence, not a reconciled ledger")


def _parse_row(kind: str, header: List[str], row: List[str]) \
        -> Optional[dict]:
    if len(row) < len(header):
        # a torn final line (crash mid-write) — wait for the next round
        raise ValueError(f"truncated line: {len(row)} of {len(header)} "
                         "fields")
    r = dict(zip(header, row))
    ts = _float(r.get("ts"))
    if ts is None:
        raise ValueError(f"bad ts {r.get('ts')!r}")
    if kind == "taker":
        return {
            "event_ts": ts,
            "direction": r.get("direction") or None,
            "qty": _float(r.get("qty")),
            "buy_limit": _float(r.get("buy_limit")),
            "sell_limit": _float(r.get("sell_limit")),
            "buy_fill": _float(r.get("buy_fill")),
            "sell_fill": _float(r.get("sell_fill")),
            "buy_status": r.get("buy_status") or None,
            "sell_status": r.get("sell_status") or None,
            "exp_edge_usd": _float(r.get("exp_edge_usd")),
            "gross_edge_usd": _float(r.get("gross_edge_usd")),
            "fill_edge_usd": _float(r.get("fill_edge_usd")),
            "ok": r.get("ok"),
            "buy_venue": r.get("buy_venue") or None,
            "sell_venue": r.get("sell_venue") or None,
            # deliberately ABSENT (not zero): per-leg FILL prices, order
            # ids, fees, funding — the schema has no such columns
        }
    return {
        "event_ts": ts,
        "hedge_dir": r.get("hedge_dir") or None,
        "hedge_qty": _float(r.get("hedge_qty")),
        "maker_px": _float(r.get("maker_px")),
        "hedge_px": _float(r.get("hedge_px")),
        "gross_edge_bps": _float(r.get("gross_edge_bps")),
        "net_edge_bps": _float(r.get("net_edge_bps")),
        "fill_to_hedge_ms": _float(r.get("fill_to_hedge_ms")),
        "n_fills": _float(r.get("n_fills")),     # untrusted (spec §8.3)
        "failures": _float(r.get("failures")),
    }


def import_events_jsonl(storage, *, path: str, run_id: str,
                        strategy_id: Optional[str],
                        batch: Optional[str] = None) -> ImportReport:
    """Import one worker's events JSONL (V2-008) into normalized_events.

    event_id = evjsonl:{source}:line:{n} keeps import idempotence; events
    carry their own event_ts. Real fills keep the venue fill id (dedupe_key
    = f:{venue_fill_id} when present) — unlike the legacy CSVs these CAN be
    trade-deduped. A torn tail (crash mid-write) is reported as a gap."""
    from ..eventlog import read_events
    batch = batch or f"ev-{int(time.time())}-{os.getpid()}"
    events, torn_tail, bad = read_events(path)
    hh = _header_hash(["jsonl"])
    ident = file_identity(path, hh)
    prior = [s for s in storage.list_import_sources(limit=500)
             if s["path"] == path and s["status"] == "active"]
    prior.sort(key=lambda s: s["id"], reverse=True)
    src = prior[0] if prior else None
    if src is not None and len(events) < src["offset_line"]:
        src = None                        # truncated / rotated
    start = src["offset_line"] if src else 0
    src_id = (src["id"] if src is not None else
              storage.upsert_import_source(
                  path=path, file_identity=ident, header_hash=hh,
                  offset_line=0, rows_total=0, rows_bad=0, bad=None))
    imported = 0
    for i, ev in enumerate(events, start=1):
        if i <= start:
            continue
        if not isinstance(ev, dict):
            bad.append({"line": i, "reason": "not an object"})
            continue
        fee = ev.get("fee")
        payload = {k: v for k, v in ev.items()
                   if k not in ("schema_version",)}
        ok = storage.insert_event(
            event_id=f"evjsonl:{src_id}:line:{i}",
            event_type=str(ev.get("event_type") or "unknown"),
            import_batch=batch, source_id=src_id, source_line=i,
            event_ts=ev.get("event_ts"),
            payload=payload,
            strategy_id=ev.get("strategy_id") or strategy_id,
            run_id=ev.get("run_id") or run_id,
            venue=ev.get("venue"),
            instrument=ev.get("symbol") or ev.get("instrument"),
            dedupe_key=(f"f:{ev.get('venue_fill_id')}"
                        if ev.get("venue_fill_id") else None),
            unresolved=(ev.get("venue_fill_id") is None
                        and ev.get("event_type") in
                        ("maker_fill", "taker_attempt")))
        if ok:
            imported += 1
    notes = ["jsonl events carry venue fill ids when the adapter reports "
             "them; fills without ids stay unresolved"]
    if torn_tail:
        notes.append("torn tail line (writer crashed mid-write) — gap "
                     "recorded, line not imported")
    storage.upsert_import_source(
        path=path, file_identity=ident, header_hash=hh,
        offset_line=len(events),
        rows_total=(src["rows_total"] if src else 0) + imported,
        rows_bad=(src["rows_bad"] if src else 0) + len(bad),
        bad=bad[:50])
    return ImportReport(path=path, status="ok", kind="events", source_id=src_id,
                        rows=len(events) - start, imported=imported,
                        bad=bad[:50], torn_tail=torn_tail,
                        unresolved=imported, note="; ".join(notes))


def discover_csvs(profiles, profile: str, root: str) -> List[dict]:
    """Backend-managed discovery (spec §13.4): the paths the profile itself
    declares, plus the .old rotation beside them. Never client paths."""
    out: List[dict] = []
    seen = set()

    def add(path: str, kind_hint: str) -> None:
        if not path:
            return
        if not os.path.isabs(path):
            path = os.path.join(root, path)
        for p in (path, path + ".old"):
            rp = os.path.realpath(p)
            if rp in seen or not os.path.exists(rp) or rp in seen:
                continue
            seen.add(rp)
            out.append({"path": rp, "kind": classify(rp) or kind_hint,
                        "size": os.path.getsize(rp),
                        "mtime": os.path.getmtime(rp)})

    try:
        import yaml as _yaml
        with open(os.path.join(profiles.dir, f"{profile}.yaml")) as fh:
            raw = _yaml.safe_load(fh) or {}
    except Exception:
        return out
    add((raw.get("logging") or {}).get("trades_csv"), "taker")
    add((raw.get("maker") or {}).get("trades_csv"), "maker")
    return out
