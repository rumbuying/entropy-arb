"""Persistent order journal: intent state machine (spec §7.1).

Transitions are monotone and enforced in one place. Terminal states never
leave; FILLED is never demoted by a late cancel; a cancel that races a
fill loses to FILLED. EXPIRED_EPOCH applies only to UNSENT intents
(PLANNED/BLOCKED_SLOT) — already-sent requests can not be recalled by an
epoch bump and must go through cancel + late-fill reconciliation.

UNKNOWN is a state, not an error: budget stays reserved, the executor
queries, and only certified evidence resolves it (spec §7.2).
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from . import models, store
from .store import Store, StoreError

TRANSITIONS: Dict[str, set] = {
    "PLANNED": {"DISPATCHING", "EXPIRED_EPOCH", "BLOCKED_SLOT"},
    "BLOCKED_SLOT": {"PLANNED", "EXPIRED_EPOCH"},
    "DISPATCHING": {"ACCEPTED", "REJECTED", "UNKNOWN", "EXPIRED_EPOCH"},
    "ACCEPTED": {"OPEN", "PARTIAL", "FILLED", "CANCEL_REQUESTED", "CANCELED", "UNKNOWN"},
    "OPEN": {"PARTIAL", "FILLED", "CANCEL_REQUESTED", "CANCELED",
             "UNKNOWN"},
    "PARTIAL": {"FILLED", "CANCEL_REQUESTED", "CANCELED", "UNKNOWN"},
    "CANCEL_REQUESTED": {"OPEN", "PARTIAL", "CANCEL_PENDING", "FILLED", "CANCELED", "UNKNOWN"},
    "CANCEL_PENDING": {"OPEN", "PARTIAL", "FILLED", "CANCELED", "UNKNOWN"},
    "UNKNOWN": {"OPEN", "PARTIAL", "FILLED", "CANCELED", "REJECTED", "PLANNED"},
    "FILLED": set(),
    "REJECTED": set(),
    "CANCELED": set(),
    "EXPIRED_EPOCH": set(),
}

UNSENT_STATES = ("PLANNED", "BLOCKED_SLOT")
OPEN_STATES = ("ACCEPTED", "OPEN", "PARTIAL", "CANCEL_REQUESTED",
               "CANCEL_PENDING")


class JournalError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def can_transition(current: str, nxt: str) -> bool:
    return nxt in TRANSITIONS.get(current, set())


def is_native_protection(intent):
    """Economic classification; fixed stops never become whole-position orders."""
    return bool(intent.get('whole_position') or intent.get('protection_kind') == 'fixed_quantity')


class OrderJournal:
    """All order_intents / exchange_orders / dispatch_attempts writes."""

    def __init__(self, st: Store) -> None:
        self.store = st

    @staticmethod
    def pending_account_fixed_terminals(c, account):
        """Account collateral remains fenced even after a grid was closed.

        Include all grids, including historical closed grids and all markets;
        terminal markers belong to the account's shared risk domain.
        """
        grids = c.execute('SELECT grid_id FROM grids WHERE account_id=?', (account,)).fetchall()
        pending = []
        for grid in grids:
            pending.extend(OrderJournal.pending_fixed_terminals(c, grid['grid_id'], account))
            # A not-yet-terminal stop can have raced an I/O claim. A second
            # market must not increase this account's exposure while its
            # original stop is DISPATCHING or UNKNOWN.
            rows=c.execute("SELECT i.* FROM order_intents i JOIN runtime_state p "
                "ON p.key='order-protection:'||i.intent_id WHERE i.grid_id=? "
                "AND i.state IN ('DISPATCHING','UNKNOWN')",(grid['grid_id'],)).fetchall()
            pending.extend(dict(row) for row in rows
                           if OrderJournal.decorate(c,row).get('protection_kind')=='fixed_quantity')
        return pending

    @staticmethod
    def pending_fixed_terminals(c, grid_id, account):
        """Find unresolved obligations, including terminals predating markers."""
        result=[]
        rows=c.execute("SELECT i.*,e.exchange_order_id,e.terminal_state,e.cumulative_qty,e.raw_receipt_json, "
            "s.value AS marker,v.value AS verified FROM order_intents i "
            "JOIN runtime_state p ON p.key='order-protection:'||i.intent_id "
            "LEFT JOIN exchange_orders e ON e.intent_id=i.intent_id AND e.account=? "
            "LEFT JOIN runtime_state s ON s.key='fixed-stop-terminal-unverified:'||i.intent_id "
            "LEFT JOIN runtime_state v ON v.key='fixed-stop-terminal-verified:'||i.intent_id "
            "WHERE i.grid_id=? AND (i.state IN ('FILLED','CANCELED','REJECTED') OR e.terminal_state IS NOT NULL)",
            (account,grid_id)).fetchall()
        for row in rows:
            if OrderJournal.decorate(c,row).get('protection_kind')!='fixed_quantity':continue
            if OrderJournal._fixed_not_sent_proved(c,row,account):continue
            fingerprint={'exchange_order_id':row['exchange_order_id'],'terminal_state':row['terminal_state'],
                         'cumulative_qty':row['cumulative_qty']}
            try: verified=json.loads(row['verified']) if row['verified'] else None
            except (ValueError,TypeError):verified=None
            if row['marker'] is not None or verified!=fingerprint:result.append(dict(row))
        return result

    @staticmethod
    def _fixed_not_sent_proved(c, row, account):
        """Recheck the durable native tombstone and absence of contrary facts."""
        from .adapters.base import OrderRequest
        marker=c.execute('SELECT value FROM runtime_state WHERE key=?',
                         ('fixed-stop-not-sent:'+row['intent_id'],)).fetchone()
        if not marker or row['state']!='REJECTED' or row['marker'] is not None:
            return False
        if (row['exchange_order_id'] is not None or row['terminal_state'] is not None
                or row['cumulative_qty']!='0' or row['raw_receipt_json'] is not None):
            return False
        try:
            proof=json.loads(marker[0]);intent=OrderJournal.decorate(c,row)
            grid=c.execute('SELECT account_id,market_key FROM grids WHERE grid_id=?',(row['grid_id'],)).fetchone()
            if grid is None or grid['account_id']!=account:return False
            req=OrderRequest(row['intent_id'],row['client_id'],account,grid['market_key'],row['side'],
                row['qty_base'],row['price'],row['policy'],bool(row['reduce_only']),row['epoch'],
                trigger_price=intent.get('trigger_price'),whole_position=bool(intent.get('whole_position')))
            native=c.execute('SELECT value FROM runtime_state WHERE key=?',(proof['native_key'],)).fetchone()
            if native is None:return False
            native=json.loads(native[0]);scope=models.sha256_hex([native['network'],native['owner']])
            digest=models.sha256_hex(req.to_dict())
            if (proof['schema']!='bulk-fixed-not-sent-v1'
                    or proof['native_key']!='bulk-intent:'+scope+':'+row['client_id']
                    or proof['account']!=account or proof['intent_id']!=row['intent_id']
                    or proof['grid_id']!=row['grid_id'] or proof['client_id']!=row['client_id']
                    or proof['request']!=req.to_dict() or proof['request_hash']!=digest
                    or proof['stage'] not in ('preparation_not_sent','final_gate_not_sent')
                    or native['account']!=account or native['client_id']!=row['client_id']
                    or native['intent_id']!=row['intent_id'] or native['request_hash']!=digest
                    or native['state']!='ABORTED_NOT_SENT' or native.get('order_id')!=proof['native_order_id']
                    or native['action'].get('type')!='stop'
                    or proof['action_hash']!=models.sha256_hex(native['action'])
                    or any(k in native for k in ('receipt','native_evidence','terminal_resolution'))):return False
            if proof['stage']=='final_gate_not_sent' and native.get('last_send_known_not_sent') is not True:return False
            if c.execute('SELECT 1 FROM executions WHERE account=? AND (order_client_id=? OR (? IS NOT NULL AND exchange_order_id=?)) LIMIT 1',
                         (account,row['client_id'],proof['native_order_id'],proof['native_order_id'])).fetchone():return False
            return True
        except (KeyError,TypeError,ValueError):
            return False

    @staticmethod
    def decorate(c, row):
        value = dict(row)
        metadata = c.execute("SELECT value FROM runtime_state WHERE key=?",
                             ("order-protection:" + value['intent_id'],)).fetchone()
        if metadata:
            protection = json.loads(metadata[0])
            residual_exit = value['cause_key'].startswith(f"exit-flat:{value['grid_id']}:native-residual:")
            fixed = protection.get('whole_position') is False and protection.get('kind') == 'fixed_quantity'
            fixed_valid = (value['cause_key'].startswith(f"protection:{value['grid_id']}:")
                and value['policy'] == 'gtc' and value['side'] == 'sell' and value['level_index'] == -1
                and value['price'] == protection.get('trigger_price'))
            whole_valid = (protection.get('whole_position') is True
                and value['policy'] == 'reduce_only_ioc'
                and (value['cause_key'].startswith('protection:') or residual_exit))
            if (not value['reduce_only'] or not (fixed and fixed_valid or whole_valid)
                    or models.parse_decimal_text(protection['trigger_price'], 'trigger_price') <= 0):
                raise JournalError('PROTECTION_METADATA_INVALID', 'protection metadata conflicts with order intent')
            value.update(trigger_price=protection['trigger_price'], whole_position=not fixed)
            if fixed:
                value['protection_kind'] = 'fixed_quantity'
        layout = c.execute("SELECT value FROM runtime_state WHERE key=?",
                           ('initial-position-layout:' + value['intent_id'],)).fetchone()
        if layout:
            if not value['cause_key'].startswith('initial-position:') or value['reduce_only']:
                raise JournalError('INITIAL_POSITION_LAYOUT_INVALID', 'initial layout conflicts with intent')
            value['initial_position_layout'] = json.loads(layout[0])
        entry = c.execute('SELECT value FROM runtime_state WHERE key=?',
                          ('entry-price-wait:' + value['intent_id'],)).fetchone()
        if entry:
            if not value['cause_key'].startswith('initial:') or value['reduce_only'] or value['policy'] != 'gtc':
                raise JournalError('ENTRY_CONDITION_INVALID', 'entry condition conflicts with intent')
            value['entry_price_condition'] = json.loads(entry[0])
        return value

    # ------------------------------------------------------------- queries
    def get_intent(self, intent_id: str) -> Optional[Dict[str, Any]]:
        def q(c):
            row = c.execute("SELECT * FROM order_intents WHERE intent_id = ?",
                            (intent_id,)).fetchone()
            return self.decorate(c, row) if row else None
        return self.store.read(q)

    def intents_for_grid(self, grid_id: str, *,
                         states: Optional[List[str]] = None) \
            -> List[Dict[str, Any]]:
        def q(c):
            if states:
                marks = ",".join("?" * len(states))
                rows = c.execute(
                    f"SELECT * FROM order_intents WHERE grid_id = ? AND "
                    f"state IN ({marks}) ORDER BY created_ts, intent_id",
                    (grid_id, *states)).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM order_intents WHERE grid_id = ? "
                    "ORDER BY created_ts, intent_id",
                    (grid_id,)).fetchall()
            return [self.decorate(c, r) for r in rows]
        return self.store.read(q)

    # ------------------------------------------------------------- writes
    async def create_intent(self, *, grid_id: str, cause_key: str,
                            revision: int, level_index: int, side: str,
                            qty_base: str, price: str, policy: str,
                            reduce_only: bool, epoch: int, expected_epoch=None,
                            trigger_price=None, whole_position=False,
                            initial_position_layout=None, wait_for_entry_price=False) -> Dict[str, Any]:
        """Idempotent by cause_key (SQL UNIQUE): a replayed causal event
        returns the ORIGINAL intent, never a second one."""
        import uuid
        now = models.utc_now_iso()
        if wait_for_entry_price and (wait_for_entry_price is not True or not cause_key.startswith('initial:')
                                    or reduce_only or policy != 'gtc'):
            raise JournalError('ENTRY_CONDITION_INVALID', 'only a new GTC entry may wait for its price')
        if whole_position or trigger_price is not None:
            residual_exit = cause_key.startswith(f'exit-flat:{grid_id}:native-residual:')
            fixed_valid = (whole_position is False and cause_key.startswith(f'protection:{grid_id}:')
                and policy == 'gtc' and side == 'sell' and price == trigger_price)
            whole_valid = (whole_position is True and (cause_key.startswith('protection:') or residual_exit)
                and policy == 'reduce_only_ioc')
            if (not (fixed_valid or whole_valid)
                    or reduce_only is not True or level_index != -1
                    or models.parse_decimal_text(trigger_price, 'trigger_price') <= 0
                    or models.parse_decimal_text(qty_base, 'protection_bound') <= 0):
                raise JournalError('PROTECTION_INTENT_INVALID', 'invalid protective order scope or bound')

        def w(c):
            if expected_epoch is not None:
                current = c.execute("SELECT epoch FROM grids WHERE grid_id=?", (grid_id,)).fetchone()
                if current is None or current[0] != expected_epoch:
                    raise StoreError("OPERATION_INTERRUPTED", "grid changed before planning orders")
            if initial_position_layout is not None:
                from decimal import Decimal
                levels = c.execute('SELECT level_index FROM grid_levels WHERE grid_id=? AND revision=?',
                                   (grid_id, revision)).fetchall()
                valid = {row[0] for row in levels}
                slots = initial_position_layout
                if (not cause_key.startswith('initial-position:') or reduce_only or policy != 'ioc'
                        or side not in ('buy', 'sell') or not isinstance(slots, list) or not slots
                        or any(not isinstance(slot, dict) or set(slot) != {'level_index', 'qty_base'}
                               or type(slot['level_index']) is not int or slot['level_index'] not in valid
                               or slot['level_index'] - (1 if side == 'buy' else -1) not in valid
                               or models.parse_decimal_text(slot['qty_base'], 'initial_slot_qty') <= 0 for slot in slots)
                        or len({slot['level_index'] for slot in slots}) != len(slots)
                        or sum((Decimal(slot['qty_base']) for slot in slots), Decimal(0)) != Decimal(qty_base)):
                    raise JournalError('INITIAL_POSITION_LAYOUT_INVALID', 'initial slots must cover the exact order quantity')
            try:
                c.execute(
                    "INSERT INTO order_intents(intent_id, grid_id, "
                    "cause_key, revision, level_index, side, qty_base, "
                    "price, policy, reduce_only, epoch, state, "
                    "created_ts, updated_ts) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), grid_id, cause_key, revision,
                     level_index, side, qty_base, price, policy,
                     int(reduce_only), epoch, "PLANNED", now, now))
            except Exception:
                row = c.execute(
                    "SELECT * FROM order_intents WHERE cause_key = ?",
                    (cause_key,)).fetchone()
                if row is not None:
                    existing = self.decorate(c, row)
                    if existing.get('initial_position_layout') != initial_position_layout:
                        raise JournalError('INITIAL_POSITION_LAYOUT_CONFLICT', 'initial slots changed for existing cause')
                    if bool(existing.get('entry_price_condition')) != bool(wait_for_entry_price):
                        raise JournalError('ENTRY_CONDITION_CONFLICT', 'entry condition changed for existing cause')
                    if (bool(existing.get('whole_position')) != bool(whole_position)
                            or existing.get('trigger_price') != trigger_price
                            or trigger_price is not None and any(existing[key] != value for key, value in (
                                ('side', side), ('qty_base', qty_base), ('price', price), ('revision', revision)))):
                        raise JournalError('PROTECTION_IDEMPOTENCY_CONFLICT', 'protection changed for existing cause')
                    return existing
                raise
            row = c.execute("SELECT * FROM order_intents WHERE cause_key=?",
                            (cause_key,)).fetchone()
            if whole_position or trigger_price is not None:
                metadata = {'whole_position': whole_position, 'trigger_price': trigger_price}
                if not whole_position:
                    metadata['kind'] = 'fixed_quantity'
                c.execute("INSERT INTO runtime_state(key,value) VALUES(?,?)", (
                    'order-protection:' + row['intent_id'], json.dumps(metadata, sort_keys=True)))
            if initial_position_layout is not None:
                c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)', (
                    'initial-position-layout:' + row['intent_id'], json.dumps(initial_position_layout, sort_keys=True)))
            if wait_for_entry_price:
                c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',
                          ('entry-price-wait:' + row['intent_id'], json.dumps({'armed': False})))
            return self.decorate(c, row)
        return await self.store.write(w)

    async def transition(self, intent_id: str, nxt: str, *,
                         client_id: Optional[str] = None,
                         exchange_order_id: Optional[str] = None) -> str:
        """Guarded monotone transition. Returns the (possibly unchanged)
        persisted state. Racing outcomes (late fill vs cancel) resolve by
        the table: terminal FILLED/CANCELED never move."""
        now = models.utc_now_iso()

        def w(c):
            row = c.execute(
                "SELECT state FROM order_intents WHERE intent_id = ?",
                (intent_id,)).fetchone()
            if row is None:
                raise JournalError("INTENT_NOT_FOUND",
                                   f"intent {intent_id} missing")
            current = row["state"]
            if current == nxt:
                return current
            if not can_transition(current, nxt):
                return current      # racing loser keeps its (stronger) state
            sets = ["state = ?", "updated_ts = ?"]
            args: List[Any] = [nxt, now]
            if client_id is not None:
                sets.append("client_id = ?")
                args.append(client_id)
            args.append(intent_id)
            c.execute(f"UPDATE order_intents SET {', '.join(sets)} "
                      f"WHERE intent_id = ?", args)
            if exchange_order_id is not None:
                c.execute(
                    "UPDATE exchange_orders SET exchange_order_id = ?, "
                    "updated_ts = ? WHERE intent_id = ?",
                    (exchange_order_id, now, intent_id))
            return nxt
        return await self.store.write(w)

    async def expire_epoch(self, grid_id: str, epoch: int) -> int:
        """Pause/stop: invalidate every UNSENT intent of older epochs.
        Returns how many were expired. In-flight (sent) requests are NOT
        touched here — they go through cancel + late-fill checks."""
        now = models.utc_now_iso()

        def w(c):
            cur = c.execute(
                "UPDATE order_intents SET state = 'EXPIRED_EPOCH', "
                "updated_ts = ? WHERE grid_id = ? AND epoch < ? AND "
                "state IN ('PLANNED','BLOCKED_SLOT')",
                (now, grid_id, epoch))
            return cur.rowcount
        return await self.store.write(w)

    async def record_attempt(self, intent_id: str, *, request_hash: str,
                             result: str, error_code: Optional[str] = None,
                             detail: Optional[dict] = None) -> None:
        now = models.utc_now_iso()

        def w(c):
            c.execute(
                "INSERT INTO dispatch_attempts(intent_id, request_hash, "
                "result, error_code, detail_json, created_ts) "
                "VALUES(?,?,?,?,?,?)",
                (intent_id, request_hash, result, error_code,
                 json.dumps(detail or {}, sort_keys=True), now))
        await self.store.write(w)

    async def bind_exchange_order(self, *, account: str, client_id: str,
                                  intent_id: str,
                                  exchange_order_id: Optional[str]) -> None:
        now = models.utc_now_iso()

        def w(c):
            c.execute(
                "INSERT INTO exchange_orders(account, client_id, "
                "exchange_order_id, intent_id, cumulative_qty, "
                "terminal_state, updated_ts) VALUES(?,?,?,?, '0', NULL, ?) "
                "ON CONFLICT(account, client_id) DO UPDATE SET "
                "exchange_order_id = COALESCE(excluded.exchange_order_id, "
                "exchange_orders.exchange_order_id), updated_ts = "
                "excluded.updated_ts",
                (account, client_id, exchange_order_id, intent_id, now))
        await self.store.write(w)

    async def note_fill(self, *, account: str, client_id: str,
                        qty_base: str) -> None:
        """Add executed qty to the exchange order's cumulative. The fill
        itself is recorded once in executions; this only maintains the
        per-order aggregate (spec §6.2). Decimal sums are computed in
        Python inside the tx — never via SQL arithmetic."""
        def w2(c):
            row = c.execute(
                "SELECT intent_id, cumulative_qty FROM exchange_orders "
                "WHERE account = ? AND client_id = ?",
                (account, client_id)).fetchone()
            if row is None:
                raise JournalError("ORDER_NOT_BOUND",
                                   f"fill for unbound client id "
                                   f"{client_id!r} (account {account})")
            total = models.canonical_decimal_text(
                models.parse_decimal_text(row["cumulative_qty"], "cum")
                + models.parse_decimal_text(qty_base, "qty"))
            now2 = models.utc_now_iso()
            c.execute(
                "UPDATE exchange_orders SET cumulative_qty = ?, "
                "updated_ts = ? WHERE account = ? AND client_id = ?",
                (total, now2, account, client_id))
            intent_id = row["intent_id"]
            st = c.execute("SELECT state FROM order_intents WHERE "
                           "intent_id = ?", (intent_id,)).fetchone()
            if st is None:
                return
            state = st["state"]
            if state in ("OPEN", "PARTIAL", "ACCEPTED",
                         "CANCEL_REQUESTED", "CANCEL_PENDING", "UNKNOWN"):
                c.execute("UPDATE order_intents SET state='PARTIAL', "
                          "updated_ts=? WHERE intent_id=? AND state != "
                          "'FILLED'", (now2, intent_id))
        return await self.store.write(w2)

    async def mark_terminal(self, *, account: str, client_id: str,
                            terminal: str) -> None:
        """Order-level terminal state (FILLED/CANCELED). A late cancel
        result cannot demote FILLED (transition guard)."""
        now = models.utc_now_iso()

        def w(c):
            row = c.execute(
                "SELECT i.* FROM exchange_orders e JOIN order_intents i ON i.intent_id=e.intent_id "
                "WHERE e.account = ? AND e.client_id = ?", (account, client_id)).fetchone()
            if row is None:
                raise JournalError("ORDER_NOT_BOUND",
                                   f"client id {client_id!r} unbound")
            intent = self.decorate(c, row)
            if intent.get('protection_kind') == 'fixed_quantity':
                # Terminality and execution coverage are independent facts.
                # Persist the unresolved obligation atomically with terminality,
                # including when cancellation or UNKNOWN resolution owns it.
                key='fixed-stop-terminal-unverified:' + row['intent_id']
                c.execute("DELETE FROM runtime_state WHERE key=?",('fixed-stop-terminal-verified:'+row['intent_id'],))
                c.execute("INSERT INTO runtime_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps({'grid_id':row['grid_id'], 'intent_id':row['intent_id'],
                                      'account':account, 'client_id':client_id,'generation':uuid.uuid4().hex}, sort_keys=True)))
            c.execute(
                "UPDATE exchange_orders SET terminal_state = ?, "
                "updated_ts = ? WHERE account = ? AND client_id = ?",
                (terminal, now, account, client_id))
            await_none = row["intent_id"]
            if terminal in ("FILLED", "CANCELED"):
                c.execute(
                    "UPDATE order_intents SET state = ?, updated_ts = ? "
                    "WHERE intent_id = ? AND state NOT IN "
                    "('FILLED','REJECTED','CANCELED','EXPIRED_EPOCH')",
                    (terminal, now, await_none))
        return await self.store.write(w)
