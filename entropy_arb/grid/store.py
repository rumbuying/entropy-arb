"""Independent grid runtime store (spec §6, handoff §6).

The GridRuntime process is the ONLY writer of ``data/grid-runtime.sqlite``.
The legacy console database never holds trading authority data.

Hard rules implemented here:

- One StoreWriter thread owns the single write connection. Async callers
  await the commit before results are visible; close() drains accepted
  writes first (never a daemon that loses the queue).
- WAL + ``synchronous=FULL`` + foreign_keys + busy_timeout. Local disk
  only (NFS/tmp dirs are the deployment's problem, we refuse nothing but
  document it).
- Migrations run one explicit transaction per version; ``user_version``
  bumps inside the same transaction, so an interrupted upgrade rolls back
  completely. A database written by an UNKNOWN HIGHER schema refuses to
  open for trading.
- Stable unique keys live in SQL, not only in Python queries:
  ``events.event_key``, ``executions.execution_key``,
  ``order_intents.cause_key``, ``request_dedup(owner_scope, request_id)``.
  Duplicate fills with conflicting economics raise DATA_CONFLICT — never
  silently pick the newest row.
- Backup uses SQLite online backup; the manifest is read back from the
  COMPLETED copy (schema version + last committed seq), never stitched
  from the live file.
"""
from __future__ import annotations

import asyncio
import json
import queue
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import models

SCHEMA_VERSION = 6
BUSY_TIMEOUT_MS = 5000

MIGRATIONS: List[Tuple[int, str]] = [
    (1, """
-- core runtime metadata + evidence (migration 1)
CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE accounts (
  account_id         TEXT PRIMARY KEY,
  venue              TEXT NOT NULL,
  deployment         TEXT NOT NULL DEFAULT '',
  owner              TEXT NOT NULL,
  fixture            INTEGER NOT NULL DEFAULT 0,
  credential_revision TEXT NOT NULL DEFAULT '',
  created_ts         TEXT NOT NULL,
  UNIQUE(venue, deployment, owner)
);
CREATE TABLE instruments (
  market_key  TEXT PRIMARY KEY,
  rules_json  TEXT NOT NULL,
  revision    TEXT NOT NULL,
  fetched_ts  TEXT NOT NULL
);
CREATE TABLE capabilities (
  venue        TEXT NOT NULL,
  deployment   TEXT NOT NULL DEFAULT '',
  capability   TEXT NOT NULL,
  status       TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  checked_ts   TEXT NOT NULL,
  PRIMARY KEY (venue, deployment, capability)
);
CREATE TABLE events (
  seq          INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key    TEXT NOT NULL UNIQUE,
  scope        TEXT NOT NULL,
  event_type   TEXT NOT NULL,
  event_ts     TEXT,
  received_ts  TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_events_scope_seq ON events(scope, seq);
CREATE TABLE checkpoints (
  scope        TEXT PRIMARY KEY,
  applied_seq  INTEGER NOT NULL,
  updated_ts   TEXT NOT NULL
);
CREATE TABLE previews (
  preview_id   TEXT PRIMARY KEY,
  purpose      TEXT NOT NULL,
  config_hash  TEXT NOT NULL,
  account      TEXT NOT NULL,
  market       TEXT NOT NULL,
  rules_revision TEXT NOT NULL DEFAULT '',
  capability_revision TEXT NOT NULL DEFAULT '',
  risk_snapshot_id TEXT,
  quote_snapshot_id TEXT,
  plan_json    TEXT NOT NULL,
  expires_ts   REAL NOT NULL,
  consumed_by  TEXT,
  created_ts   REAL NOT NULL
);
CREATE TABLE request_dedup (
  owner_scope  TEXT NOT NULL,
  request_id   TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  result_json  TEXT NOT NULL,
  created_ts   REAL NOT NULL,
  PRIMARY KEY (owner_scope, request_id)
);
CREATE TABLE counters (
  name   TEXT PRIMARY KEY,
  value  INTEGER NOT NULL
);
CREATE TABLE scope_claims (
  scope_key  TEXT PRIMARY KEY,
  owner_type TEXT NOT NULL,
  owner_id   TEXT NOT NULL,
  state      TEXT NOT NULL,
  acquired_ts TEXT NOT NULL,
  released_ts TEXT
);
"""),
    (2, """
-- grids, versions, intents, orders, executions (migration 2)
CREATE TABLE grids (
  grid_id        TEXT PRIMARY KEY,
  account_id     TEXT NOT NULL REFERENCES accounts(account_id),
  market_key     TEXT NOT NULL,
  name           TEXT NOT NULL DEFAULT '',
  status         TEXT NOT NULL,
  desired_mode   TEXT NOT NULL,
  epoch          INTEGER NOT NULL DEFAULT 0,
  active_revision INTEGER,
  parent_grid_id TEXT,
  created_ts     TEXT NOT NULL,
  updated_ts     TEXT NOT NULL
);
-- one non-closed grid per account+market; residual positions still claim
CREATE UNIQUE INDEX idx_grids_claim
  ON grids(account_id, market_key)
  WHERE status != 'CLOSED';
CREATE TABLE grid_versions (
  grid_id    TEXT NOT NULL REFERENCES grids(grid_id),
  revision   INTEGER NOT NULL,
  config_hash TEXT NOT NULL,
  plan_hash  TEXT NOT NULL,
  config_json TEXT NOT NULL,
  plan_json  TEXT NOT NULL,
  created_ts TEXT NOT NULL,
  PRIMARY KEY (grid_id, revision)
);
CREATE TABLE grid_levels (
  grid_id     TEXT NOT NULL,
  revision    INTEGER NOT NULL,
  level_index INTEGER NOT NULL,
  price       TEXT NOT NULL,
  PRIMARY KEY (grid_id, revision, level_index),
  UNIQUE (grid_id, revision, price)
);
CREATE TABLE order_intents (
  intent_id  TEXT PRIMARY KEY,
  grid_id    TEXT NOT NULL REFERENCES grids(grid_id),
  cause_key  TEXT NOT NULL UNIQUE,
  revision   INTEGER NOT NULL,
  level_index INTEGER NOT NULL,
  side       TEXT NOT NULL CHECK (side IN ('buy','sell')),
  qty_base   TEXT NOT NULL,
  price      TEXT NOT NULL,
  policy     TEXT NOT NULL,
  reduce_only INTEGER NOT NULL DEFAULT 0,
  epoch      INTEGER NOT NULL,
  state      TEXT NOT NULL,
  client_id  TEXT,
  created_ts TEXT NOT NULL,
  updated_ts TEXT NOT NULL
);
CREATE INDEX idx_intents_grid_state ON order_intents(grid_id, state);
CREATE TABLE exchange_orders (
  account       TEXT NOT NULL,
  client_id     TEXT NOT NULL,
  exchange_order_id TEXT,
  intent_id     TEXT NOT NULL REFERENCES order_intents(intent_id),
  cumulative_qty TEXT NOT NULL DEFAULT '0',
  terminal_state TEXT,
  raw_receipt_json TEXT,
  updated_ts    TEXT NOT NULL,
  PRIMARY KEY (account, client_id)
);
CREATE TABLE dispatch_attempts (
  attempt_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  intent_id   TEXT NOT NULL REFERENCES order_intents(intent_id),
  request_hash TEXT NOT NULL,
  result      TEXT NOT NULL,
  error_code  TEXT,
  detail_json TEXT,
  created_ts  TEXT NOT NULL
);
CREATE INDEX idx_attempts_intent ON dispatch_attempts(intent_id);
CREATE TABLE executions (
  execution_key TEXT PRIMARY KEY,
  account      TEXT NOT NULL,
  market       TEXT NOT NULL,
  order_client_id TEXT,
  exchange_order_id TEXT,
  trade_id     TEXT,
  side         TEXT NOT NULL CHECK (side IN ('buy','sell')),
  qty_base     TEXT NOT NULL,
  price        TEXT NOT NULL,
  fee_amount   TEXT,
  fee_asset    TEXT,
  is_maker     INTEGER,
  exchange_ts  TEXT NOT NULL,
  received_ts  TEXT NOT NULL,
  source       TEXT NOT NULL,
  source_seq   TEXT,
  finality     TEXT NOT NULL DEFAULT 'final',
  event_seq    INTEGER
);
CREATE INDEX idx_exec_order ON executions(order_client_id);
CREATE INDEX idx_exec_market_ts ON executions(market, exchange_ts);
"""),
    (3, """
-- inventory, pairing, capital, funding, operations (migration 3)
CREATE TABLE inventory_lots (
  lot_id      TEXT PRIMARY KEY,
  grid_id     TEXT NOT NULL REFERENCES grids(grid_id),
  side        TEXT NOT NULL CHECK (side IN ('buy','sell')),
  qty_remaining TEXT NOT NULL,
  price       TEXT NOT NULL,
  source_execution_key TEXT NOT NULL REFERENCES executions(execution_key),
  open_ts     TEXT NOT NULL,
  closed_ts   TEXT
);
CREATE INDEX idx_lots_grid ON inventory_lots(grid_id, side, open_ts);
CREATE TABLE pair_allocations (
  pair_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  grid_id     TEXT NOT NULL REFERENCES grids(grid_id),
  buy_execution_key  TEXT NOT NULL REFERENCES executions(execution_key),
  sell_execution_key TEXT NOT NULL REFERENCES executions(execution_key),
  qty         TEXT NOT NULL,
  buy_fee_alloc  TEXT NOT NULL,
  sell_fee_alloc TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_pair_unique
  ON pair_allocations(grid_id, buy_execution_key, sell_execution_key);
CREATE TABLE funding_payments (
  payment_key TEXT PRIMARY KEY,
  account     TEXT NOT NULL,
  market      TEXT NOT NULL,
  amount      TEXT NOT NULL,
  asset       TEXT NOT NULL,
  ts          TEXT NOT NULL,
  source_seq  TEXT,
  source      TEXT NOT NULL
);
CREATE TABLE capital_movements (
  movement_id TEXT PRIMARY KEY,
  grid_id     TEXT NOT NULL REFERENCES grids(grid_id),
  kind        TEXT NOT NULL,
  amount      TEXT NOT NULL,
  asset       TEXT NOT NULL,
  ts          TEXT NOT NULL,
  evidence    TEXT
);
CREATE TABLE budget_reservations (
  grid_id     TEXT PRIMARY KEY REFERENCES grids(grid_id),
  reserved_amount TEXT NOT NULL,
  asset       TEXT NOT NULL,
  updated_ts  TEXT NOT NULL
);
CREATE TABLE operations (
  operation_id TEXT PRIMARY KEY,
  grid_id      TEXT NOT NULL REFERENCES grids(grid_id),
  op_type      TEXT NOT NULL,
  request_id   TEXT,
  request_hash TEXT,
  status       TEXT NOT NULL DEFAULT 'queued',
  step         TEXT NOT NULL DEFAULT '',
  request_json TEXT,
  result_json  TEXT,
  error        TEXT,
  created_ts   TEXT NOT NULL,
  started_ts   TEXT,
  ended_ts     TEXT
);
CREATE INDEX idx_operations_grid ON operations(grid_id, created_ts);
-- one in-flight mutation per grid
CREATE UNIQUE INDEX idx_operations_active
  ON operations(grid_id)
  WHERE status IN ('queued','running');
CREATE TABLE runtime_state (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""),
    (4, """
-- G16-G18: templates, automation, notifications, exports (migration 4)
CREATE TABLE drafts (
  draft_id     TEXT PRIMARY KEY,
  owner_scope  TEXT NOT NULL,
  name         TEXT NOT NULL,
  config_json  TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  revision     INTEGER NOT NULL DEFAULT 1,
  created_ts   TEXT NOT NULL,
  updated_ts   TEXT NOT NULL
);
CREATE TABLE automation_policies (
  policy_id    TEXT PRIMARY KEY,
  grid_id      TEXT NOT NULL REFERENCES grids(grid_id),
  kind         TEXT NOT NULL,
  enabled      INTEGER NOT NULL DEFAULT 0,
  limits_json  TEXT NOT NULL,
  spent_amount TEXT NOT NULL DEFAULT '0',
  asset        TEXT NOT NULL,
  created_ts   TEXT NOT NULL
);
CREATE TABLE automation_runs (
  run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
  policy_id    TEXT NOT NULL REFERENCES automation_policies(policy_id),
  trigger_key  TEXT NOT NULL,
  operation_id TEXT,
  amount       TEXT,
  created_ts   TEXT NOT NULL,
  UNIQUE(policy_id, trigger_key)
);
CREATE TABLE notification_preferences (
  scope        TEXT NOT NULL,
  event_kind   TEXT NOT NULL,
  channel_ref  TEXT NOT NULL,
  enabled      INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (scope, event_kind, channel_ref)
);
CREATE TABLE notification_outbox (
  notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key    TEXT NOT NULL UNIQUE,
  event_kind   TEXT NOT NULL,
  scope        TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'pending',
  attempts     INTEGER NOT NULL DEFAULT 0,
  next_retry_ts REAL,
  created_ts   TEXT NOT NULL
);
CREATE TABLE export_jobs (
  job_id       TEXT PRIMARY KEY,
  grid_id      TEXT NOT NULL REFERENCES grids(grid_id),
  kind         TEXT NOT NULL,
  source_seq   INTEGER NOT NULL,
  method_version TEXT NOT NULL,
  filter_json  TEXT,
  result_ref   TEXT,
  created_ts   TEXT NOT NULL
);
"""),
    (5, """
CREATE TABLE execution_owners (
  execution_key TEXT PRIMARY KEY REFERENCES executions(execution_key),
  grid_id TEXT NOT NULL REFERENCES grids(grid_id)
);
CREATE INDEX idx_execution_owner_grid ON execution_owners(grid_id);
INSERT INTO execution_owners(execution_key, grid_id)
SELECT e.execution_key, i.grid_id FROM executions e
JOIN exchange_orders o ON o.account=e.account AND o.client_id=e.order_client_id
JOIN order_intents i ON i.intent_id=o.intent_id
JOIN grids g ON g.grid_id=i.grid_id AND g.account_id=e.account AND g.market_key=e.market;
CREATE TABLE funding_owners (
  payment_key TEXT PRIMARY KEY REFERENCES funding_payments(payment_key),
  grid_id TEXT NOT NULL REFERENCES grids(grid_id)
);
CREATE TABLE refill_demands (
  grid_id TEXT NOT NULL REFERENCES grids(grid_id),
  revision INTEGER NOT NULL,
  level_index INTEGER NOT NULL,
  side TEXT NOT NULL,
  price TEXT NOT NULL,
  reduce_only INTEGER NOT NULL,
  qty TEXT NOT NULL,
  PRIMARY KEY(grid_id, revision, level_index, side)
);
ALTER TABLE automation_runs ADD COLUMN status TEXT NOT NULL DEFAULT 'reserved';
ALTER TABLE automation_runs ADD COLUMN error TEXT;
"""),
    (6, """
ALTER TABLE previews ADD COLUMN context_json TEXT;
ALTER TABLE previews ADD COLUMN config_json TEXT;
"""),
]


class StoreError(Exception):
    """Stable-code store error mapped by callers to API semantics."""

    def __init__(self, code: str, message: str,
                 status: int = 409) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


def connect(path: str, *, readonly: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro" if readonly else path,
                           uri=True, timeout=BUSY_TIMEOUT_MS / 1000.0,
                           isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = %d" % BUSY_TIMEOUT_MS)
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def schema_version_of(path: str) -> int:
    conn = connect(path, readonly=True)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _iter_statements(sql: str) -> List[str]:
    """Split a migration script into complete statements. ``executescript``
    is deliberately NOT used: it implicitly COMMITs any pending
    transaction (spec §6.3), which would break per-migration atomicity.
    The migration SQL is plain DDL: ``--`` comments are stripped first
    (they may themselves contain semicolons)."""
    lines = []
    for line in sql.splitlines():
        pos = line.find("--")
        lines.append(line[:pos] if pos >= 0 else line)
    cleaned = "\n".join(lines)
    out: List[str] = []
    buf: List[str] = []
    for ch in cleaned:
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def migrate(path: str, *, migrations: Optional[List[Tuple[int, str]]] = None,
            conn: Optional[sqlite3.Connection] = None) -> int:
    """Apply pending migrations one explicit transaction each. Returns the
    final schema version. A mid-migration failure leaves the previous
    version and DDL intact (tested)."""
    own = conn is None
    c = conn or connect(path)
    try:
        current = int(c.execute("PRAGMA user_version").fetchone()[0])
        for version, sql in (migrations or MIGRATIONS):
            if version <= current:
                continue
            if version != current + 1:
                raise StoreError(
                    "SCHEMA_GAP",
                    f"migration {version} follows {current}; sequence "
                    "must be contiguous", status=500)
            c.execute("BEGIN IMMEDIATE")
            try:
                for stmt in _iter_statements(sql):
                    c.execute(stmt)
                c.execute(f"PRAGMA user_version = {version}")
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
            current = version
        return current
    finally:
        if own:
            c.close()


class Store:
    """Async facade over the single-writer thread. Reads run on a separate
    read-only connection (WAL readers see committed state immediately)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._wconn = connect(path)
        if path == ":memory:":
            # a second connection to ":memory:" is a DIFFERENT empty db —
            # reads must share the writer connection in that case
            self._rconn = self._wconn
        else:
            self._rconn = connect(path, readonly=True)
        self._q: "queue.Queue[Any]" = queue.Queue(maxsize=4096)
        self._closed = threading.Event()
        # daemon=True ONLY so an unclosed store cannot wedge interpreter
        # exit; close() still drains every ACCEPTED write before the
        # connection drops (the no-data-loss rule is about close(), which
        # production paths always call)
        self._thread = threading.Thread(target=self._writer_loop,
                                        name="grid-store-writer", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------ lifecycle
    @classmethod
    def open(cls, path: str, *, allow_create: bool = True) -> "Store":
        import os
        if path == ":memory:":
            # migrate ON the store's own writer connection: a separate
            # connection to ":memory:" would evaporate with its schema
            s = cls(path)
            migrate(path, conn=s._wconn)
            return s
        if not os.path.exists(path):
            if not allow_create:
                raise StoreError("NOT_FOUND", f"store missing: {path}",
                                 status=404)
        else:
            v = schema_version_of(path)
            if v > SCHEMA_VERSION:
                raise StoreError(
                    "SCHEMA_TOO_NEW",
                    f"store schema v{v} > supported v{SCHEMA_VERSION}; "
                    "refusing to open for trading", status=500)
        migrate(path)
        return cls(path)

    def _writer_loop(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                self._q.task_done()
                return
            fn, fut, own_tx = item
            try:
                result = self._run(self._wconn, fn, own_tx)
                self._resolve(fut, "ok", result)
            except BaseException as e:  # propagate to the awaiting caller
                self._resolve(fut, "err", e)
            finally:
                self._q.task_done()

    @staticmethod
    def _resolve(fut: Any, kind: str, payload: Any) -> None:
        """Deliver the outcome. Asyncio futures are resolved via
        ``call_soon_threadsafe`` — a bare set_result from this thread
        queues callbacks without waking the loop's selector (deadlock).
        Queue-based sync futures take the tuple directly."""
        loop = getattr(fut, "_loop", None)
        if loop is not None and isinstance(fut, asyncio.Future):
            def _set():
                if not fut.done():
                    if kind == "ok":
                        fut.set_result(payload)
                    else:
                        fut.set_exception(payload)
            loop.call_soon_threadsafe(_set)
        else:
            fut.put((kind, payload))

    @staticmethod
    def _run(conn: sqlite3.Connection, fn: Callable, own_tx: bool) -> Any:
        if not own_tx:
            return fn(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn(conn)
            conn.execute("COMMIT")
            return result
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    async def write(self, fn: Callable[[sqlite3.Connection], Any], *,
                    tx: bool = True) -> Any:
        """Run fn(writer_conn) on the store thread; await its commit.
        fn runs inside BEGIN IMMEDIATE..COMMIT when tx=True."""
        if self._closed.is_set():
            raise StoreError("STORE_CLOSED", "store is closed",
                             status=503)
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._q.put((fn, fut, tx))
        return await fut

    def write_sync(self, fn: Callable[[sqlite3.Connection], Any], *,
                   tx: bool = True) -> Any:
        if self._closed.is_set():
            raise StoreError("STORE_CLOSED", "store is closed",
                             status=503)
        fut: "queue.Queue[Any]" = queue.Queue(maxsize=1)
        self._q.put((fn, fut, tx))
        kind, payload = fut.get()
        if kind == "err":
            raise payload
        return payload

    def read(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        """Run fn(read_conn) inline. Read-only: must not mutate."""
        return fn(self._rconn)

    def close(self, timeout: float = 30.0) -> None:
        """Stop accepting writes, drain everything already accepted, then
        close both connections and join the writer thread."""
        if self._closed.is_set():
            return
        self._closed.set()
        self._q.put(None)
        self._q.join()
        self._thread.join(timeout)
        try:
            self._wconn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        self._wconn.close()
        if self._rconn is not self._wconn:
            self._rconn.close()


# ------------------------------------------------------------- primitives

def bump_counter(conn: sqlite3.Connection, name: str, *, by: int = 1) -> int:
    """Persistent monotonic counter (client ids, epochs). Survives restart
    by construction — memory seeds are forbidden (spec §3.1)."""
    conn.execute(
        "INSERT INTO counters(name, value) VALUES(?, 0) "
        "ON CONFLICT(name) DO NOTHING", (name,))
    conn.execute("UPDATE counters SET value = value + ? WHERE name = ?",
                 (by, name))
    return int(conn.execute("SELECT value FROM counters WHERE name = ?",
                            (name,)).fetchone()[0])


def get_counter(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT value FROM counters WHERE name = ?",
                       (name,)).fetchone()
    return int(row[0]) if row else 0


def register_event(conn: sqlite3.Connection, *, event_key: str, scope: str,
                   event_type: str, payload: Dict[str, Any],
                   event_ts: Optional[str] = None,
                   received_ts: Optional[str] = None) -> Tuple[int, bool]:
    """Append-only event by stable key. Returns (seq, deduped). The row is
    never updated afterwards; processing progress lives in checkpoints."""
    import uuid
    ts = received_ts or models.utc_now_iso()
    try:
        cur = conn.execute(
            "INSERT INTO events(event_key, scope, event_type, event_ts, "
            "received_ts, payload_json, schema_version) "
            "VALUES(?,?,?,?,?,?,?)",
            (event_key, scope, event_type, event_ts, ts,
             json.dumps(payload, ensure_ascii=False, sort_keys=True),
             models.SCHEMA_VERSION))
        return int(cur.lastrowid), False
    except sqlite3.IntegrityError:
        row = conn.execute("SELECT seq FROM events WHERE event_key = ?",
                           (event_key,)).fetchone()
        if row is None:  # pragma: no cover - integrity implies the row
            raise
        return int(row[0]), True


def set_checkpoint(conn: sqlite3.Connection, scope: str, applied_seq: int) \
        -> None:
    conn.execute(
        "INSERT INTO checkpoints(scope, applied_seq, updated_ts) "
        "VALUES(?,?,?) ON CONFLICT(scope) DO UPDATE SET "
        "applied_seq = excluded.applied_seq, "
        "updated_ts = excluded.updated_ts",
        (scope, applied_seq, models.utc_now_iso()))


def get_checkpoint(conn: sqlite3.Connection, scope: str) -> int:
    row = conn.execute("SELECT applied_seq FROM checkpoints WHERE scope = ?",
                       (scope,)).fetchone()
    return int(row[0]) if row else 0


def register_request(conn: sqlite3.Connection, *, owner_scope: str,
                     request_id: str, request_hash: str,
                     result: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """Persistent idempotency: same (owner_scope, request_id) + same hash
    returns the original result; different payload raises
    IDEMPOTENCY_CONFLICT (spec §13.1)."""
    result_json = json.dumps(result, ensure_ascii=False, sort_keys=True)
    try:
        conn.execute(
            "INSERT INTO request_dedup(owner_scope, request_id, "
            "request_hash, result_json, created_ts) VALUES(?,?,?,?,?)",
            (owner_scope, request_id, request_hash, result_json, time.time()))
        return result, False
    except sqlite3.IntegrityError:
        row = conn.execute(
            "SELECT request_hash, result_json FROM request_dedup "
            "WHERE owner_scope = ? AND request_id = ?",
            (owner_scope, request_id)).fetchone()
        if row is None:  # pragma: no cover
            raise
        if row["request_hash"] != request_hash:
            raise StoreError(
                "IDEMPOTENCY_CONFLICT",
                f"request {request_id!r} was already used with a different "
                "payload", status=409)
        return json.loads(row["result_json"]), True


def insert_execution(conn: sqlite3.Connection, ev: models.ExecutionEvent) \
        -> str:
    """First-class fill insert with SQL-level dedup. Same stable key +
    identical economics -> 'duplicate'; different quantity/price/side/
    market/account -> DATA_CONFLICT (never pick a 'newest' row)."""
    try:
        conn.execute(
            "INSERT INTO executions(execution_key, account, market, "
            "order_client_id, exchange_order_id, trade_id, side, qty_base, "
            "price, fee_amount, fee_asset, is_maker, exchange_ts, "
            "received_ts, source, source_seq, finality) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ev.execution_key, ev.account, ev.market, ev.order_client_id,
             ev.exchange_order_id, ev.trade_id, ev.side, ev.qty_base,
             ev.price, ev.fee_amount, ev.fee_asset,
             None if ev.is_maker is None else int(ev.is_maker),
             ev.exchange_ts, ev.received_ts, ev.source, ev.source_seq,
             ev.finality))
        return "inserted"
    except sqlite3.IntegrityError:
        row = conn.execute(
            "SELECT account, market, side, qty_base, price FROM executions "
            "WHERE execution_key = ?", (ev.execution_key,)).fetchone()
        if row is None:  # pragma: no cover
            raise
        same = (row["account"] == ev.account and row["market"] == ev.market
                and row["side"] == ev.side
                and models.canonical_decimal_text(models.parse_decimal_text(
                    row["qty_base"], "qty")) == ev.qty_base
                and models.canonical_decimal_text(models.parse_decimal_text(
                    row["price"], "px")) == ev.price)
        if same:
            return "duplicate"
        raise StoreError(
            "DATA_CONFLICT",
            f"execution {ev.execution_key!r} already recorded with "
            "different economics", status=409)


def amend_execution_fee(conn: sqlite3.Connection, execution_key: str,
                        fee_amount: str, fee_asset: str) -> None:
    """Fee backfill is a REVISION: quantity never changes (spec §6.1)."""
    cur = conn.execute(
        "UPDATE executions SET fee_amount = ?, fee_asset = ? "
        "WHERE execution_key = ?", (fee_amount, fee_asset, execution_key))
    if cur.rowcount != 1:
        raise StoreError("NOT_FOUND", f"execution {execution_key!r} missing",
                         status=404)


def backup(path: str, dest: str, *, manifest_extra: Optional[dict] = None) \
        -> Dict[str, Any]:
    """Consistent online backup. The manifest is derived from the finished
    copy only (schema version + last committed event seq), then written
    next to it. Returns the manifest."""
    src = connect(path, readonly=True)
    try:
        dst = connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    chk = connect(dest, readonly=True)
    try:
        schema_v = int(chk.execute("PRAGMA user_version").fetchone()[0])
        row = chk.execute(
            "SELECT COALESCE(MAX(seq), 0) AS s FROM events").fetchone()
        last_seq = int(row["s"]) if row else 0
        ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        chk.close()
    if ok != "ok":
        raise StoreError("BACKUP_CORRUPT", f"integrity_check: {ok}",
                         status=500)
    manifest = {"schema_version": schema_v, "method": "sqlite_online_backup",
                "last_seq": last_seq,
                "created_ts": models.utc_now_iso(),
                "integrity": ok}
    if manifest_extra:
        manifest.update(manifest_extra)
    import os
    with open(dest + ".manifest.json", "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.sync()
    return manifest


def restore_from_backup(src: str, dest: str) -> Dict[str, Any]:
    """Copy a backup over the live path and return its manifest. Restored
    stores start in RECOVERING — callers must not resume orders directly."""
    import os
    import shutil
    for suffix in ("", "-wal", "-shm"):
        p = dest + suffix
        if os.path.exists(p):
            os.unlink(p)
    shutil.copyfile(src, dest)
    with open(src + ".manifest.json", encoding="utf-8") as fh:
        return json.load(fh)
