"""GridActor: per-grid state machine (spec §6.2, §7.3, §8.1).

The actor never does network I/O and never opens its own transactions
outside the runtime's store. The FILL TRANSACTION is the core invariant:

    insert execution (dedup) -> order cumulative -> inventory lots ->
    pairing allocations -> refill intent -> checkpoint

all inside ONE store transaction; the executor only sends the refill
after the commit. A replayed fill (same execution_key) touches nothing.

Refill rule: a fill at level L on side S creates an exit intent at the
adjacent level (buy@i -> sell@i+1; sell@i+1 -> buy@i) with the REAL
filled quantity, only when it meets min_qty/min_notional at the exit
price (dust stays visible as an unpaired lot instead of a bogus order).
Long/short exit intents are reduce_only; neutral ones are not (spec
§7.3: neutral orders may cross zero).
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional

from . import models, pairing, store
from .inventory import Inventory
from .journal import OrderJournal
from .store import Store

log = logging.getLogger("grid.actor")


class GridActor:
    def __init__(self, *, st: Store, journal: OrderJournal, grid_id: str,
                 account: models.AccountIdentity, market_key: str,
                 rules: models.InstrumentRules, direction: str,
                 levels: List[str], epoch: int, revision: int,
                 execution_policy: str = "post_only") -> None:
        self.store = st
        self.journal = journal
        self.grid_id = grid_id
        self.account = account
        self.market_key = market_key
        self.rules = rules
        self.direction = direction
        self.levels = levels
        self.epoch = epoch
        self.revision = revision
        self.execution_policy = execution_policy
        self.inv = Inventory(st, grid_id)
        self.candidates: List[Dict[str, Any]] = []   # plan's send list
        self.mark: str = "0"
        self.drain_pump = self._noop_pump            # runtime injects

    @staticmethod
    async def _noop_pump() -> None:
        return None

    def mark_price(self) -> str:
        return self.mark

    # ------------------------------------------------------------ helpers
    def exit_level(self, entry_level: int, side: str) -> Optional[int]:
        """Adjacent exit level for a refill; None at the boundary."""
        if side == "buy":
            j = entry_level + 1
        else:
            j = entry_level - 1
        if 0 <= j < len(self.levels):
            return j
        return None

    def exit_reduce_only(self, side: str) -> bool:
        return (self.direction == "long" and side == "sell") or \
            (self.direction == "short" and side == "buy")

    # ------------------------------------------------------ fill transaction
    async def apply_fill(self, ev: models.ExecutionEvent, *, recovery_only=False) -> Dict[str, Any]:
        """The one fill path. Returns an outcome dict; 'duplicate' means
        the fill was already booked and NOTHING changed."""

        def w(c):
            if recovery_only:
                grid = c.execute('SELECT status FROM grids WHERE grid_id=?', (self.grid_id,)).fetchone()
                if grid is None or grid['status'] != 'RECOVERING':
                    raise store.StoreError('RECOVERY_MODE_CHANGED', 'partial-history booking needs recovery mode')
            client_id = ev.order_client_id
            if ev.account != self.account.account_id or ev.market != self.market_key:
                raise store.StoreError("FOREIGN_EXECUTION", "account/market mismatch")
            bound = c.execute(
                "SELECT i.*,o.client_id AS bound_client_id FROM exchange_orders o JOIN order_intents i "
                "ON i.intent_id=o.intent_id WHERE o.account=? AND "
                "(o.client_id=? OR (? IS NOT NULL AND o.exchange_order_id=?))",
                (ev.account, ev.order_client_id, ev.exchange_order_id,
                 ev.exchange_order_id)).fetchone()
            if bound is None or bound["grid_id"] != self.grid_id:
                raise store.StoreError("FOREIGN_EXECUTION", "fill has no owned grid order")
            client_id = bound["bound_client_id"]
            if bound["side"] != ev.side:
                raise store.StoreError("DATA_CONFLICT", "fill side differs from owned order")
            if ev.finality != "final":
                raise store.StoreError("EXECUTION_NOT_FINAL", "provisional fill cannot book inventory")
            protection = (bound['cause_key'].startswith('protection:') or bound['cause_key'].startswith(
                f"exit-flat:{self.grid_id}:native-residual:"))
            if protection:
                metadata = c.execute('SELECT value FROM runtime_state WHERE key=?',
                                     ('order-protection:' + bound['intent_id'],)).fetchone()
                if not metadata or not bound['reduce_only']:
                    raise store.StoreError('PROTECTION_METADATA_INVALID', 'protective execution has no valid type')
            inserted = store.insert_execution(c, ev)
            if inserted == "duplicate":
                return {"outcome": "duplicate"}
            if inserted == "conflict":  # pragma: no cover - raise path
                raise store.StoreError("DATA_CONFLICT", "unreachable")
            c.execute("INSERT INTO execution_owners VALUES(?,?)",
                      (ev.execution_key, self.grid_id))
            # order cumulative (order may be foreign to this grid when
            # backfilled; then there is no bound order row to update)
            if client_id:
                row = c.execute(
                    "SELECT intent_id FROM exchange_orders WHERE "
                    "account = ? AND client_id = ?",
                    (ev.account, client_id)).fetchone()
                if row is not None:
                    total_row = c.execute(
                        "SELECT cumulative_qty FROM exchange_orders "
                        "WHERE account = ? AND client_id = ?",
                        (ev.account, client_id)).fetchone()
                    initial_layout = c.execute('SELECT value FROM runtime_state WHERE key=?',
                        ('initial-position-layout:' + row['intent_id'],)).fetchone()
                    if initial_layout:
                        import json
                        c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)', (
                            'initial-position-fill:' + ev.execution_key,
                            json.dumps({'offset': total_row['cumulative_qty'], 'qty_base': ev.qty_base,
                                        'intent_id': row['intent_id']}, sort_keys=True)))
                    total = models.canonical_decimal_text(
                        models.parse_decimal_text(total_row["cumulative_qty"],
                                                  "cum")
                        + models.parse_decimal_text(ev.qty_base, "qty"))
                    if Decimal(total) > Decimal(bound["qty_base"]):
                        raise store.StoreError("DATA_CONFLICT", "fill cumulative quantity exceeds order")
                    now = models.utc_now_iso()
                    c.execute(
                        "UPDATE exchange_orders SET cumulative_qty = ?, "
                        "updated_ts = ? WHERE account = ? AND client_id = ?",
                        (total, now, ev.account, client_id))
                    complete = Decimal(total) >= Decimal(bound["qty_base"])
                    c.execute("UPDATE order_intents SET state=?, updated_ts=? "
                              "WHERE intent_id=? AND (state NOT IN "
                              "('REJECTED','EXPIRED_EPOCH') OR ?)",
                              ("FILLED" if complete else ("CANCELED" if bound["state"] == "CANCELED" else "PARTIAL"),
                               now, row["intent_id"], int(complete)))
                    if complete:
                        c.execute("UPDATE exchange_orders SET terminal_state='FILLED' "
                                  "WHERE account=? AND client_id=?",
                                  (ev.account, client_id))
            qty = models.parse_decimal_text(ev.qty_base, "qty")
            price = models.parse_decimal_text(ev.price, "price")
            ts = ev.exchange_ts
            # inventory: consume opposite lots FIFO, remainder opens a lot
            opposite = "sell" if ev.side == "buy" else "buy"
            consumptions = self.inv.consume_fifo(c, side=opposite, qty=qty)
            consumed = sum((t for _, _, t in consumptions), Decimal(0))
            residual = qty - consumed
            if protection and residual > 0:
                raise store.StoreError('PROTECTION_INVENTORY_MISMATCH', 'protective execution exceeds owned opposite inventory')
            if residual > 0:
                self.inv.open_lot(c, side=ev.side, qty=residual, price=price,
                                  exec_key=ev.execution_key, ts=ts)
            rows = pairing.PairingEngine.allocation_rows(
                c, grid_id=self.grid_id,
                fill={"execution_key": ev.execution_key, "side": ev.side,
                      "qty_base": ev.qty_base, "price": ev.price,
                      "fee_amount": ev.fee_amount},
                consumptions=consumptions)
            pairing.PairingEngine.insert_rows(c, rows)
            if protection:
                grid = c.execute('SELECT epoch,status,desired_mode FROM grids WHERE grid_id=?', (self.grid_id,)).fetchone()
                if grid['status'] == 'CLOSED':
                    raise store.StoreError('PROTECTION_CLOSED_GRID_FILL', 'closed grid received an unaccounted protective execution')
                epoch = grid['epoch'] + int(grid['status'] != 'STOPPING' or grid['desired_mode'] != 'STOPPED')
                c.execute("UPDATE grids SET epoch=?,status='STOPPING',desired_mode='STOPPED',updated_ts=? WHERE grid_id=?",
                          (epoch, models.utc_now_iso(), self.grid_id))
                c.execute("UPDATE order_intents SET state='EXPIRED_EPOCH' WHERE grid_id=? AND epoch<? AND state IN ('PLANNED','BLOCKED_SLOT')", (self.grid_id, epoch))
                c.execute('DELETE FROM refill_demands WHERE grid_id=?', (self.grid_id,))
                store.register_event(c, event_key='protection-triggered:' + ev.execution_key, scope=self.grid_id,
                    event_type='protection_triggered', payload={'intent_id': bound['intent_id'], 'qty': ev.qty_base, 'epoch': epoch})
                from .protection_exit import enqueue_exit
                enqueue_exit(c, self.grid_id, epoch, 'native_protection_fill')
            # grid refill rule (spec §6.3): a fill on a GRID order at
            # level L side S creates an exit intent at the adjacent level
            # with the REAL filled quantity — for opening fills this is
            # the inventory's exit demand, for closing fills it restarts
            # the loop. Exit-flat IOC fills never refill.
            refill = None
            if client_id:
                intent_row = c.execute(
                    "SELECT level_index, cause_key, revision FROM order_intents "
                    "WHERE intent_id = (SELECT intent_id FROM "
                    "exchange_orders WHERE account = ? AND client_id = ?)",
                    (ev.account, client_id)).fetchone()
                if intent_row is not None and intent_row["cause_key"] \
                        .startswith(("initial:", "refill:")):
                    old_levels = [r[0] for r in c.execute(
                        "SELECT price FROM grid_levels WHERE grid_id=? AND revision=? "
                        "ORDER BY level_index", (self.grid_id, intent_row["revision"]))]
                    exit_j = intent_row["level_index"] + (1 if ev.side == "buy" else -1)
                    exit_j = exit_j if 0 <= exit_j < len(old_levels) else None
                    grid = c.execute("SELECT epoch, desired_mode, status,active_revision FROM grids "
                                     "WHERE grid_id=?", (self.grid_id,)).fetchone()
                    legacy = intent_row["revision"] != (grid["active_revision"] or self.revision)
                    # Old versions may exit surviving stock, but never restart
                    # an old entry cycle after the current version replaces it.
                    refill_qty = residual if legacy else qty
                    if exit_j is not None and refill_qty > 0:
                        exit_px = old_levels[exit_j]
                        minq = models.parse_decimal_text(self.rules.min_qty
                                                         or "0", "minq")
                        minn = models.parse_decimal_text(
                            self.rules.min_notional or "0", "minn")
                        side = "sell" if ev.side == "buy" else "buy"
                        rev = intent_row["revision"]
                        dust = c.execute("SELECT qty FROM refill_demands WHERE grid_id=? "
                                         "AND revision=? AND level_index=? AND side=?",
                                         (self.grid_id, rev, exit_j, side)).fetchone()
                        demand = refill_qty + (Decimal(dust[0]) if dust else Decimal(0))
                        reduce_only = legacy or self.exit_reduce_only(side)
                        c.execute("INSERT INTO refill_demands VALUES(?,?,?,?,?,?,?) "
                                  "ON CONFLICT(grid_id,revision,level_index,side) "
                                  "DO UPDATE SET qty=excluded.qty",
                                  (self.grid_id, rev, exit_j, side, exit_px,
                                   int(reduce_only), str(demand)))
                        active = grid["desired_mode"] == "ACTIVE" and grid["status"] in ("STARTING", "RUNNING")
                        occupied = self._slot_occupied(c, rev, exit_j, side)
                        eligible = (active and not occupied and demand >= minq and demand
                                    * models.parse_decimal_text(exit_px,
                                                                "px")
                                    >= minn)
                        if eligible:
                            cause = f"refill:{ev.execution_key}"
                            existing = c.execute(
                                "SELECT intent_id FROM order_intents WHERE "
                                "cause_key = ?", (cause,)).fetchone()
                            if existing is None:
                                import uuid
                                now = models.utc_now_iso()
                                iid = str(uuid.uuid4())
                                c.execute(
                                    "INSERT INTO order_intents(intent_id, "
                                    "grid_id, cause_key, revision, "
                                    "level_index, side, qty_base, price, "
                                    "policy, reduce_only, epoch, state, "
                                    "created_ts, updated_ts) VALUES("
                                    "?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                    (iid, self.grid_id, cause, rev,
                                     exit_j,
                                     side,
                                     models.canonical_decimal_text(demand), exit_px,
                                     self.execution_policy,
                                     int(reduce_only),
                                     grid["epoch"], "PLANNED", now, now))
                                c.execute("DELETE FROM refill_demands WHERE grid_id=? "
                                          "AND revision=? AND level_index=? AND side=?",
                                          (self.grid_id, rev, exit_j, side))
                                refill = iid
            seq, _ = store.register_event(
                c, event_key=f"fill-applied:{ev.execution_key}",
                scope=self.grid_id, event_type="fill_applied",
                payload={"execution_key": ev.execution_key,
                         "source_seq": ev.source_seq,
                         "refill_intent": refill})
            store.set_checkpoint(c, f"fills:{self.grid_id}", seq)
            return {"outcome": "applied", "refill_intent": refill,
                    'protection_triggered': protection,
                    "paired_qty": models.canonical_decimal_text(consumed),
                    "event_seq": seq}
        return await self.store.write(w)

    def _slot_occupied(self, c, revision, level, side):
        return c.execute("SELECT 1 FROM order_intents WHERE grid_id=? AND revision=? "
            "AND level_index=? AND side=? AND state IN ('PLANNED','DISPATCHING','ACCEPTED','OPEN','PARTIAL',"
            "'UNKNOWN','CANCEL_REQUESTED','CANCEL_PENDING','BLOCKED_SLOT') LIMIT 1",
            (self.grid_id, revision, level, side)).fetchone() is not None

    async def release_demands(self):
        """Keep one physical order per slot; partial fills wait durably
        until the previous slot order is terminal."""
        def w(c):
            grid = c.execute("SELECT epoch,status,desired_mode FROM grids WHERE grid_id=?", (self.grid_id,)).fetchone()
            if grid["desired_mode"] != "ACTIVE" or grid["status"] not in ("STARTING", "RUNNING"):
                return []
            released=[]
            for demand in c.execute("SELECT * FROM refill_demands WHERE grid_id=?", (self.grid_id,)).fetchall():
                qty=Decimal(demand["qty"])
                if self._slot_occupied(c,demand["revision"],demand["level_index"],demand["side"]):
                    continue
                if qty < Decimal(self.rules.min_qty or "0") or qty*Decimal(demand["price"]) < Decimal(self.rules.min_notional or "0"):
                    continue
                import uuid
                iid=uuid.uuid4().hex
                now=models.utc_now_iso()
                c.execute("INSERT INTO order_intents(intent_id,grid_id,cause_key,revision,level_index,side,qty_base,price,policy,reduce_only,epoch,state,created_ts,updated_ts) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,'PLANNED',?,?)",
                    (iid,self.grid_id,f"refill:aggregate:{iid}",demand["revision"],demand["level_index"],demand["side"],models.canonical_decimal_text(qty),
                     demand["price"],self.execution_policy,demand["reduce_only"],grid["epoch"],now,now))
                c.execute("DELETE FROM refill_demands WHERE grid_id=? AND revision=? AND level_index=? AND side=?",
                    (self.grid_id,demand["revision"],demand["level_index"],demand["side"]))
                store.register_event(c,event_key=f"refill-released:{iid}",scope=self.grid_id,event_type="refill_released",payload={"intent_id":iid,"qty":str(qty)})
                released.append(iid)
            return released
        return await self.store.write(w)

    # --------------------------------------------------------- slot intent
    def plan_candidates(self) -> List[Dict[str, Any]]:
        """The plan's SEND_CANDIDATE list (fresh plan on resume; stale
        refill queues are never replayed)."""
        return list(self.candidates)

    async def place_initial(self, candidates: List[Dict[str, Any]], *, epoch=None) \
            -> List[Dict[str, Any]]:
        """Create PLANNED intents for the plan's SEND_CANDIDATE orders."""
        out = []
        epoch = self.epoch if epoch is None else epoch
        for cand in candidates:
            if cand.get("status") != "SEND_CANDIDATE":
                continue
            rev = cand.get("revision", self.revision)
            cause = f"initial:{self.grid_id}:r{rev}:e{epoch}:" \
                    f"{cand['level_index']}:{cand['side']}"
            intent = await self.journal.create_intent(
                grid_id=self.grid_id, cause_key=cause,
                revision=rev,
                level_index=cand["level_index"], side=cand["side"],
                qty_base=cand["qty_base"], price=cand["price"],
                policy=self.execution_policy,
                reduce_only=bool(cand.get("reduce_only", cand.get("purpose") == "initial_exit")),
                epoch=epoch, expected_epoch=epoch,
                wait_for_entry_price=cand.get('purpose') == 'pending_entry' and self.execution_policy == 'gtc')
            out.append(intent)
        return out

    # ------------------------------------------------------------- views
    def snapshot(self, c) -> Dict[str, Any]:
        """Read-only projection for API/UI (spec §13.1)."""
        lots = self.inv.open_lots(c)
        pairs = pairing.PairingEngine.pair_summary(c, self.grid_id)
        intents = c.execute(
            "SELECT state, COUNT(*) AS n FROM order_intents WHERE "
            "grid_id = ? GROUP BY state", (self.grid_id,)).fetchall()
        return {"grid_id": self.grid_id, "direction": self.direction,
                "levels": self.levels, "open_lots": lots,
                "net_position": models.canonical_decimal_text(
                    self.inv.net_position(c)),
                "pairs": pairs,
                "intent_states": {r["state"]: r["n"] for r in intents}}
