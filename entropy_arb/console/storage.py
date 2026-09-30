"""Console V2 persistent storage: SQLite (stdlib only) + versioned migrations.

Phase A foundation (CONSOLE-V2-DEVELOPMENT-SPEC §2.2.3 / §7.2): the records
ops needs to survive a console restart —

  runs              one row per real worker-process lifecycle (run_id UUID);
                    strategy_id stays nullable until the phase-B mapping
  config_versions   full normalized profile snapshots (no secrets), sources
                    manual/autoband/import — written from V2-004 on
  operations        flatten/start/stop/restart + preview/lock bookkeeping
                    with per-leg results — written from V2-003 on
  audit_events      append-only operation trail, never contains secrets

Schema changes land as numbered migrations applied in one transaction each;
an interrupted upgrade leaves the previous schema (and data) intact. Only
the console process writes this database — workers never touch it.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

# Migration 1: phase-A ops tables. Later phases append to MIGRATIONS; the
# highest applied version lives in PRAGMA user_version (and _migrations).
MIGRATIONS: List[tuple] = [
    (1, """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  run_id          TEXT PRIMARY KEY,
  strategy_id     TEXT,
  worker_id       TEXT NOT NULL,
  profile         TEXT NOT NULL,
  symbol          TEXT NOT NULL,
  hedge           TEXT NOT NULL,
  base            TEXT NOT NULL DEFAULT 'hl',
  mode            TEXT NOT NULL,
  pid             INTEGER,
  proc_start_ts   REAL,
  cmdline_hash    TEXT,
  provisional     INTEGER NOT NULL DEFAULT 0,
  identity_note   TEXT,
  config_version  TEXT,
  event_path      TEXT,
  started_ts      REAL NOT NULL,
  ended_ts        REAL,
  state           TEXT NOT NULL DEFAULT 'running',
  exit_code       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_runs_profile_ts  ON runs(profile, started_ts);
CREATE INDEX IF NOT EXISTS idx_runs_strategy    ON runs(strategy_id);
CREATE TABLE IF NOT EXISTS config_versions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  version       TEXT NOT NULL UNIQUE,
  profile       TEXT NOT NULL,
  content_hash  TEXT NOT NULL,
  yaml_text     TEXT NOT NULL,
  sidecar_json  TEXT,
  source        TEXT NOT NULL DEFAULT 'manual',
  changed_by    TEXT,
  parent_version TEXT,
  created_ts    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cfgver_profile ON config_versions(profile,
                                                                created_ts);
CREATE TABLE IF NOT EXISTS operations (
  operation_id TEXT PRIMARY KEY,
  op_type      TEXT NOT NULL,
  target       TEXT NOT NULL,
  request_id   TEXT,
  status       TEXT NOT NULL DEFAULT 'queued',
  request_json TEXT,
  result_json  TEXT,
  error        TEXT,
  created_ts   REAL NOT NULL,
  started_ts   REAL,
  ended_ts     REAL
);
CREATE INDEX IF NOT EXISTS idx_operations_target ON operations(target,
                                                               created_ts);
CREATE TABLE IF NOT EXISTS audit_events (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  ts     REAL NOT NULL,
  actor  TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events(ts);
"""),
    # Migration 2 (V2-006, phase B): persistent strategy identity + the
    # strategy/run mapping. A strategy is the long-lived object a profile
    # launches into a market; runs (process lifecycles) reference it.
    (2, """
CREATE TABLE IF NOT EXISTS strategies (
  id             TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  symbol         TEXT NOT NULL,
  type           TEXT NOT NULL,            -- taker_basis | maker_hedge
  base_venue     TEXT NOT NULL,
  base_market    TEXT NOT NULL,            -- dex qualifier for hl legs
  hedge_venue    TEXT NOT NULL,
  parent_id      TEXT,
  created_ts     REAL NOT NULL,
  archived_ts    REAL
);
CREATE INDEX IF NOT EXISTS idx_strategies_symbol ON strategies(symbol,
                                                                created_ts);
CREATE TABLE IF NOT EXISTS profile_links (
  profile        TEXT NOT NULL,
  strategy_id    TEXT NOT NULL,
  launch_identity TEXT NOT NULL,           -- symbol|base|dex|hedge|type
  created_ts     REAL NOT NULL,
  PRIMARY KEY (profile, launch_identity)
);
CREATE INDEX IF NOT EXISTS idx_profile_links_strategy
  ON profile_links(strategy_id);
"""),
    # Migration 3 (V2-007): historical CSV import — per-file incremental
    # state and the normalized event log the ledger will build on.
    (3, """
CREATE TABLE IF NOT EXISTS import_sources (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  path          TEXT NOT NULL,
  file_identity TEXT NOT NULL,           -- size+mtime+header hash class
  header_hash   TEXT NOT NULL,
  offset_line   INTEGER NOT NULL DEFAULT 0,
  rows_total    INTEGER NOT NULL DEFAULT 0,
  rows_bad      INTEGER NOT NULL DEFAULT 0,
  bad_json      TEXT,
  last_import_ts REAL,
  status        TEXT NOT NULL DEFAULT 'active',
  UNIQUE(path, file_identity)
);
CREATE TABLE IF NOT EXISTS normalized_events (
  event_id      TEXT PRIMARY KEY,        -- src:{source_id}:line:{n}
  dedupe_key    TEXT,                    -- real-trade dedupe, may be null
  event_type    TEXT NOT NULL,
  strategy_id   TEXT,
  run_id        TEXT,
  account_id    TEXT,
  venue         TEXT,
  instrument    TEXT,
  event_ts      REAL,
  payload_json  TEXT NOT NULL,
  source_id     INTEGER NOT NULL,
  source_line   INTEGER NOT NULL,
  unresolved    INTEGER NOT NULL DEFAULT 1,
  import_batch  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_strategy_ts
  ON normalized_events(strategy_id, event_ts);
CREATE INDEX IF NOT EXISTS idx_events_source
  ON normalized_events(source_id, source_line);
"""),
    # Migration 4 (V2-009): inventory sub-ledger, boundary valuations and
    # reconciliation runs. The ledger is Decimal-exact; lots persist so a
    # recomputation never revises an old reconciliation silently.
    (4, """
CREATE TABLE IF NOT EXISTS inventory_lots (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id   TEXT NOT NULL,
  account_id    TEXT NOT NULL,
  instrument    TEXT NOT NULL,
  side          TEXT NOT NULL,             -- long | short
  qty           TEXT NOT NULL,             -- Decimal string, remaining
  open_px       TEXT NOT NULL,
  multiplier    TEXT NOT NULL DEFAULT '1',
  opened_event  TEXT,
  method_version TEXT NOT NULL DEFAULT 'fifo-v1',
  created_ts    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lots_scope
  ON inventory_lots(strategy_id, account_id, instrument);
CREATE TABLE IF NOT EXISTS valuations (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id   TEXT NOT NULL,
  boundary      TEXT NOT NULL,             -- start | end
  ts            REAL NOT NULL,
  mark_source   TEXT,
  payload_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_valuations ON valuations(strategy_id, ts);
CREATE TABLE IF NOT EXISTS reconciliations (
  id            TEXT PRIMARY KEY,
  strategy_id   TEXT NOT NULL,
  scope_json    TEXT NOT NULL,             -- period, accounts, instruments
  rule_version  TEXT NOT NULL,
  status        TEXT NOT NULL,             -- reconciled | estimated |
                                           -- incomplete | no_data
  net_pnl       TEXT,
  components_json TEXT,
  missing_json  TEXT,
  residual_json TEXT,
  created_ts    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recons ON reconciliations(strategy_id,
                                                         created_ts);
"""),
]


def new_run_id() -> str:
    return f"run-{uuid.uuid4().hex}"


def new_version_id(content_hash: str) -> str:
    """cfg-<uuid8>-<hash8> per spec §13.1 — unique without leaking content."""
    return f"cfg-{uuid.uuid4().hex[:8]}-{content_hash[:8]}"


def new_operation_id() -> str:
    return f"op-{uuid.uuid4().hex}"


def cmdline_hash(profile: str, symbol: str, hedge: str, base: str,
                 mode: str) -> str:
    """Stable identity of a worker spawn — identical between a fresh start
    and a later adopt of the same process (raw argv differ: absolute paths,
    extra flags). Not a secret."""
    import hashlib
    key = f"{profile}|{symbol.upper()}|{hedge}|{base}|{mode}"
    return hashlib.sha256(key.encode()).hexdigest()


class Storage:
    def __init__(self, path: str) -> None:
        self.path = path
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.migrate()

    # ------------------------------------------------------------- migrations

    def migrate(self) -> None:
        current = self.db.execute("PRAGMA user_version").fetchone()[0]
        for version, sql in MIGRATIONS:
            if version <= current:
                continue
            with self.db:                      # one transaction per migration
                self.db.executescript(sql)
                self.db.execute(f"PRAGMA user_version = {version}")
                self.db.execute(
                    "INSERT OR REPLACE INTO meta(key, value)"
                    " VALUES('last_migration', ?)", (str(version),))

    # ------------------------------------------------------------------ runs

    def create_run(self, *, run_id: str, worker_id: str, profile: str,
                   symbol: str, hedge: str, base: str, mode: str,
                   pid: Optional[int], cmdline_hash: str,
                   started_ts: float, strategy_id: Optional[str] = None,
                   provisional: bool = False, identity_note: str = "",
                   config_version: Optional[str] = None,
                   event_path: Optional[str] = None,
                   proc_start_ts: Optional[float] = None) -> None:
        self.db.execute(
            "INSERT INTO runs(run_id, strategy_id, worker_id, profile, symbol,"
            " hedge, base, mode, pid, proc_start_ts, cmdline_hash, provisional,"
            " identity_note, config_version, event_path, started_ts, state)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'running')",
            (run_id, strategy_id, worker_id, profile, symbol.upper(), hedge,
             base, mode, pid, proc_start_ts, cmdline_hash, int(provisional),
             identity_note, config_version, event_path, started_ts))
        self.db.commit()

    def finish_run(self, run_id: str, *, ended_ts: float,
                   state: str, exit_code: Optional[int]) -> None:
        self.db.execute(
            "UPDATE runs SET ended_ts=?, state=?, exit_code=?"
            " WHERE run_id=? AND ended_ts IS NULL",
            (ended_ts, state, exit_code, run_id))
        self.db.commit()

    def set_run_pid(self, run_id: str, pid: Optional[int],
                    proc_start_ts: Optional[float]) -> None:
        self.db.execute(
            "UPDATE runs SET pid=?, proc_start_ts=? WHERE run_id=?",
            (pid, proc_start_ts, run_id))
        self.db.commit()

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        r = self.db.execute("SELECT * FROM runs WHERE run_id=?",
                            (run_id,)).fetchone()
        return dict(r) if r else None

    def list_runs(self, *, limit: int = 200) -> List[Dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM runs ORDER BY started_ts DESC, run_id LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]

    def find_resumable_run(self, *, profile: str, symbol: str, hedge: str,
                           base: str, mode: str, pid: Optional[int],
                           cmdline_hash: str,
                           proc_start_ts: Optional[float] = None) \
            -> Optional[Dict[str, Any]]:
        """The persisted run of a still-alive worker this console just
        restarted beside (adopt). Matches on the full spawn identity PLUS
        pid; never on pid alone (spec §7.1). proc_start_ts tightens the
        match where /proc is available (Linux); on platforms without it the
        pid + identity pair is accepted and the caller notes the gap."""
        if pid is None:
            return None
        rows = self.db.execute(
            "SELECT * FROM runs WHERE state='running' AND profile=? AND"
            " symbol=? AND hedge=? AND base=? AND mode=? AND pid=? AND"
            " cmdline_hash=? ORDER BY started_ts DESC, run_id LIMIT 5",
            (profile, symbol.upper(), hedge, base, mode, pid,
             cmdline_hash)).fetchall()
        for r in rows:
            if proc_start_ts is not None and r["proc_start_ts"] is not None:
                if abs(r["proc_start_ts"] - proc_start_ts) > 300.0:
                    continue
            return dict(r)
        return None

    def resume_run(self, run_id: str, *, worker_id: str,
                   pid: Optional[int],
                   proc_start_ts: Optional[float] = None) -> None:
        """Adopt: same run keeps its history, only the worker id moves."""
        self.db.execute(
            "UPDATE runs SET worker_id=?, pid=?, proc_start_ts=?"
            " WHERE run_id=?", (worker_id, pid, proc_start_ts, run_id))
        self.db.commit()

    # -------------------------------------------------------- config versions

    def record_config_version(self, *, profile: str, content_hash: str,
                              yaml_text: str, sidecar: Optional[dict],
                              source: str, changed_by: str = "console",
                              parent_version: Optional[str] = None,
                              created_ts: Optional[float] = None,
                              version: Optional[str] = None) -> str:
        """version defaults to the content hash itself — one content, one
        version id, so every API surface compares the same identifier."""
        vid = version or content_hash
        self.db.execute(
            "INSERT OR IGNORE INTO config_versions(version, profile,"
            " content_hash, yaml_text, sidecar_json, source, changed_by,"
            " parent_version, created_ts) VALUES(?,?,?,?,?,?,?,?,?)",
            (vid, profile, content_hash, yaml_text,
             json.dumps(sidecar) if sidecar is not None else None,
             source, changed_by, parent_version,
             created_ts if created_ts is not None else time.time()))
        self.db.commit()
        return vid

    def latest_config_version(self, profile: str) -> Optional[Dict[str, Any]]:
        r = self.db.execute(
            "SELECT * FROM config_versions WHERE profile=?"
            " ORDER BY created_ts DESC, id DESC LIMIT 1", (profile,)).fetchone()
        return dict(r) if r else None

    def list_config_versions(self, profile: str,
                             limit: int = 50) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM config_versions WHERE profile=?"
            " ORDER BY created_ts DESC, id DESC LIMIT ?", (profile, limit))]

    def set_run_config_version(self, run_id: str,
                               config_version: Optional[str]) -> None:
        self.db.execute("UPDATE runs SET config_version=? WHERE run_id=?",
                        (config_version, run_id))
        self.db.commit()

    # ------------------------------------------------------------- strategies

    def find_strategy_by_identity(self, launch_identity: str) \
            -> Optional[Dict[str, Any]]:
        r = self.db.execute(
            "SELECT s.* FROM strategies s JOIN profile_links p"
            " ON p.strategy_id = s.id WHERE p.launch_identity=?"
            " AND s.archived_ts IS NULL ORDER BY s.created_ts DESC LIMIT 1",
            (launch_identity,)).fetchone()
        return dict(r) if r else None

    def create_strategy(self, *, name: str, symbol: str, type_: str,
                        base_venue: str, base_market: str, hedge_venue: str,
                        parent_id: Optional[str] = None,
                        ts: Optional[float] = None) -> Dict[str, Any]:
        sid = f"str-{uuid.uuid4().hex}"
        now = ts if ts is not None else time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO strategies(id, name, symbol, type, base_venue,"
                " base_market, hedge_venue, parent_id, created_ts)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (sid, name, symbol.upper(), type_, base_venue, base_market,
                 hedge_venue, parent_id, now))
        return self.get_strategy(sid)

    def link_profile(self, *, profile: str, strategy_id: str,
                     launch_identity: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO profile_links(profile, strategy_id,"
            " launch_identity, created_ts) VALUES(?,?,?,?)",
            (profile, strategy_id, launch_identity, time.time()))
        self.db.commit()

    def get_strategy(self, strategy_id: str) -> Optional[Dict[str, Any]]:
        r = self.db.execute("SELECT * FROM strategies WHERE id=?",
                            (strategy_id,)).fetchone()
        return dict(r) if r else None

    def list_strategies(self, *, include_archived: bool = True,
                        limit: int = 200) -> List[Dict[str, Any]]:
        sql = ("SELECT * FROM strategies"
               + ("" if include_archived else " WHERE archived_ts IS NULL")
               + " ORDER BY created_ts DESC, id LIMIT ?")
        return [dict(r) for r in self.db.execute(sql, (limit,))]

    def strategy_runs(self, strategy_id: str,
                      limit: int = 100) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM runs WHERE strategy_id=?"
            " ORDER BY started_ts DESC, run_id LIMIT ?",
            (strategy_id, limit))]

    # ------------------------------------------------------------ import

    def get_import_source(self, path: str, file_identity: str) \
            -> Optional[Dict[str, Any]]:
        r = self.db.execute(
            "SELECT * FROM import_sources WHERE path=? AND file_identity=?",
            (path, file_identity)).fetchone()
        return dict(r) if r else None

    def upsert_import_source(self, *, path: str, file_identity: str,
                             header_hash: str, offset_line: int,
                             rows_total: int, rows_bad: int,
                             bad: Optional[list],
                             status: str = "active") -> int:
        self.db.execute(
            "INSERT INTO import_sources(path, file_identity, header_hash,"
            " offset_line, rows_total, rows_bad, bad_json, last_import_ts,"
            " status) VALUES(?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(path, file_identity) DO UPDATE SET"
            " offset_line=excluded.offset_line,"
            " rows_total=excluded.rows_total,"
            " rows_bad=excluded.rows_bad,"
            " bad_json=excluded.bad_json,"
            " last_import_ts=excluded.last_import_ts,"
            " status=excluded.status",
            (path, file_identity, header_hash, offset_line, rows_total,
             rows_bad, json.dumps(bad) if bad else None, time.time(),
             status))
        self.db.commit()
        return self.get_import_source(path, file_identity)["id"]

    def list_import_sources(self, limit: int = 100) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM import_sources ORDER BY last_import_ts DESC,"
            " id LIMIT ?", (limit,))]

    def insert_event(self, *, event_id: str, event_type: str,
                     import_batch: str, source_id: int, source_line: int,
                     event_ts: Optional[float], payload: dict,
                     strategy_id: Optional[str] = None,
                     run_id: Optional[str] = None,
                     account_id: Optional[str] = None,
                     venue: Optional[str] = None,
                     instrument: Optional[str] = None,
                     dedupe_key: Optional[str] = None,
                     unresolved: bool = True) -> bool:
        """Idempotent by event_id (src:line). Returns True when inserted."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO normalized_events(event_id, dedupe_key,"
            " event_type, strategy_id, run_id, account_id, venue, instrument,"
            " event_ts, payload_json, source_id, source_line, unresolved,"
            " import_batch) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, dedupe_key, event_type, strategy_id, run_id,
             account_id, venue, instrument, event_ts,
             json.dumps(payload, ensure_ascii=False), source_id, source_line,
             int(unresolved), import_batch))
        self.db.commit()
        return cur.rowcount > 0

    def events_for_strategy(self, strategy_id: str, *,
                            start_ts: Optional[float] = None,
                            end_ts: Optional[float] = None,
                            limit: int = 500) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM normalized_events WHERE strategy_id=?"
        args: list = [strategy_id]
        if start_ts is not None:
            sql += " AND event_ts >= ?"
            args.append(start_ts)
        if end_ts is not None:
            sql += " AND event_ts < ?"
            args.append(end_ts)
        sql += " ORDER BY event_ts, event_id LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.db.execute(sql, args)]

    def events_page(self, strategy_id: str, *,
                    start_ts: Optional[float] = None,
                    end_ts: Optional[float] = None,
                    cursor_ts: Optional[float] = None,
                    cursor_id: Optional[str] = None,
                    limit: int = 50) -> Tuple[List[Dict[str, Any]], bool]:
        """Stable pagination (§10.1): ORDER BY event_ts, event_id with the
        opaque cursor (ts,id); returns (rows, has_more)."""
        sql = "SELECT * FROM normalized_events WHERE strategy_id=?"
        args: list = [strategy_id]
        if start_ts is not None:
            sql += " AND event_ts >= ?"
            args.append(start_ts)
        if end_ts is not None:
            sql += " AND event_ts < ?"
            args.append(end_ts)
        if cursor_ts is not None and cursor_id:
            sql += " AND (event_ts > ? OR (event_ts = ? AND event_id > ?))"
            args.extend([cursor_ts, cursor_ts, cursor_id])
        sql += " ORDER BY event_ts, event_id LIMIT ?"
        args.append(limit + 1)
        rows = [dict(r) for r in self.db.execute(sql, args)]
        has_more = len(rows) > limit
        return rows[:limit], has_more

    # -------------------------------------------------------------- ledger

    def save_reconciliation(self, *, strategy_id: str, scope: dict,
                            rule_version: str, status: str,
                            net_pnl: Optional[str],
                            components: dict, missing: list,
                            residual: dict) -> str:
        import uuid as _uuid
        rid = f"rec-{_uuid.uuid4().hex}"
        self.db.execute(
            "INSERT INTO reconciliations(id, strategy_id, scope_json,"
            " rule_version, status, net_pnl, components_json, missing_json,"
            " residual_json, created_ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (rid, strategy_id, json.dumps(scope), rule_version, status,
             net_pnl, json.dumps(components, ensure_ascii=False),
             json.dumps(missing, ensure_ascii=False),
             json.dumps(residual, ensure_ascii=False), time.time()))
        self.db.commit()
        return rid

    def list_reconciliations(self, strategy_id: str,
                             limit: int = 20) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM reconciliations WHERE strategy_id=?"
            " ORDER BY created_ts DESC, id LIMIT ?", (strategy_id, limit))]

    def save_valuation(self, *, strategy_id: str, boundary: str, ts: float,
                       mark_source: Optional[str], payload: dict) -> None:
        self.db.execute(
            "INSERT INTO valuations(strategy_id, boundary, ts, mark_source,"
            " payload_json) VALUES(?,?,?,?,?)",
            (strategy_id, boundary, ts, mark_source,
             json.dumps(payload, ensure_ascii=False)))
        self.db.commit()

    # ------------------------------------------------------------------ meta

    def meta_get(self, key: str, default=None):
        r = self.db.execute("SELECT value FROM meta WHERE key=?",
                            (key,)).fetchone()
        return r["value"] if r else default

    def meta_set(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)",
            (key, str(value)))
        self.db.commit()

    # -------------------------------------------------- credential revision
    # Internal, non-secret counter bumped on every successful secrets write.
    # Diagnostics cache entries record the revision they ran at, so the UI
    # can show "stale — credentials changed" without ever seeing a key.

    def credential_revision(self) -> int:
        return int(self.meta_get("credential_revision", "0"))

    def bump_credential_revision(self) -> int:
        rev = self.credential_revision() + 1
        self.meta_set("credential_revision", rev)
        return rev

    # ------------------------------------------------------------- operations

    def record_operation(self, *, op_type: str, target: str,
                         request_id: Optional[str] = None,
                         request: Optional[dict] = None,
                         operation_id: Optional[str] = None) -> str:
        op_id = operation_id or new_operation_id()
        self.db.execute(
            "INSERT OR IGNORE INTO operations(operation_id, op_type, target,"
            " request_id, status, request_json, created_ts)"
            " VALUES(?,?,?,?, 'queued', ?, ?)",
            (op_id, op_type, target, request_id,
             json.dumps(request) if request is not None else None,
             time.time()))
        self.db.commit()
        return op_id

    def update_operation(self, operation_id: str, *, status: str,
                         result: Optional[dict] = None,
                         error: Optional[str] = None,
                         started: bool = False) -> None:
        if started:
            self.db.execute(
                "UPDATE operations SET status=?, started_ts=?"
                " WHERE operation_id=? AND started_ts IS NULL",
                (status, time.time(), operation_id))
        else:
            self.db.execute(
                "UPDATE operations SET status=?, result_json=?, error=?,"
                " ended_ts=? WHERE operation_id=?",
                (status,
                 json.dumps(result) if result is not None
                 else None,
                 error, time.time(), operation_id))
        self.db.commit()

    def get_operation(self, operation_id: str) -> Optional[Dict[str, Any]]:
        r = self.db.execute("SELECT * FROM operations WHERE operation_id=?",
                            (operation_id,)).fetchone()
        return dict(r) if r else None

    def list_operations(self, *, op_type: Optional[str] = None,
                        target: Optional[str] = None,
                        limit: int = 20) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM operations"
        conds, args = [], []
        if op_type is not None:
            conds.append("op_type=?")
            args.append(op_type)
        if target is not None:
            conds.append("target=?")
            args.append(target)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_ts DESC, operation_id LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    # ------------------------------------------------------------------ audit

    def audit(self, actor: str, action: str, detail: str = "") -> None:
        """Append-only; detail must be value-free (callers mask)."""
        self.db.execute(
            "INSERT INTO audit_events(ts, actor, action, detail)"
            " VALUES(?,?,?,?)", (time.time(), actor, action, detail))
        self.db.commit()

    def close(self) -> None:
        try:
            self.db.commit()
            self.db.close()
        except Exception:
            pass
