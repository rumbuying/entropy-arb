"""Grid pairing (spec §12): quantity-linked explanation of completed
economic loops.

Rules that cannot drift:

- A fill's quantity and fee may be allocated ONCE. pair_allocations rows
  are unique per (grid, buy_execution_key, sell_execution_key) — but a
  single fill pair can split across multiple rows when quantities differ
  (partial pairing), so the uniqueness key is enforced at the allocation
  granularity the writer chooses: one row per (buy lot consumption, sell
  fill) event with a distinct pair key suffix.

- Pair profit = (sell_px - buy_px) * qty - buy_fee_alloc - sell_fee_alloc.
  Fee allocation is proportional to the consumed quantity; the sum over
  all allocations of one fill equals that fill's total fee (conservation
  is asserted here, not hoped for).

- Pairing is an EXPLANATION layer: it never feeds back into the FIFO
  realized figure, and FIFO realized never includes pairing profit twice
  (spec §12: "配对利润不是主账本余额").
"""
from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Any, Dict, List, Optional

from . import models


def _dec(v: Optional[str]) -> Decimal:
    if v is None:
        return Decimal(0)
    return models.parse_decimal_text(v, "amount")


def _fmt(d: Decimal) -> str:
    return models.canonical_decimal_text(d)


def fee_share(total_fee, consumed: Decimal, whole: Decimal) -> Decimal:
    """Proportional fee share; ``total_fee`` may be a Decimal, a Decimal
    text, or None. The caller passes exact Decimals."""
    fee = total_fee if isinstance(total_fee, Decimal) else _dec(total_fee)
    if whole == 0:
        return Decimal(0)
    with localcontext() as context:
        context.prec = 50
        return fee * consumed / whole


class PairingEngine:
    """Incremental pairing inside the actor's fill transaction.

    State is the pair_allocations + inventory_lots tables themselves; this
    class only computes rows. ``consumptions`` must be what the caller
    actually took from lots (single source of truth).
    """

    @staticmethod
    def allocation_rows(c, *, grid_id: str, fill: Dict[str, Any],
                        consumptions: List[Any]) -> List[Dict[str, str]]:
        """Build pair rows for a fill consuming opposite-side lots.

        ``fill``: executions row dict (side, qty_base, price, fee_amount).
        ``consumptions``: [(lot_id, source_execution_key, taken)] from
        Inventory.consume_fifo against the OPPOSITE side. The lot's source
        execution is the OPENER; the current fill is the CLOSER. Works in
        both directions (sell closing buy lots; buy closing sell lots).
        """
        rows: List[Dict[str, str]] = []
        closer_side = fill["side"]
        closer_px = _dec(fill["price"])
        closer_fee = _dec(fill["fee_amount"])
        closer_whole = _dec(fill["qty_base"])
        allocated = {}

        def allocate(key, side, total_fee, whole, taken):
            # Include earlier fills and rows staged in this same transaction.
            # The final allocation receives the exact remainder, including
            # rebates and rounding left by older versions of this allocator.
            with localcontext() as context:
                context.prec = 100
                if key not in allocated:
                    fee_column = 'buy_fee_alloc' if side == 'buy' else 'sell_fee_alloc'
                    key_column = 'buy_execution_key' if side == 'buy' else 'sell_execution_key'
                    prior = c.execute(f'SELECT qty,{fee_column} FROM pair_allocations '
                                      f'WHERE grid_id=? AND {key_column}=?', (grid_id,key)).fetchall()
                    allocated[key] = [sum((_dec(r[0]) for r in prior),Decimal(0)),
                                      sum((_dec(r[1]) for r in prior),Decimal(0))]
                qty_before, fee_before = allocated[key]
                after = qty_before + taken
                if taken <= 0 or after > whole:
                    raise models.GridModelError('PAIR_QUANTITY_OVERALLOCATED', 'pair quantity exceeds execution')
                share = total_fee - fee_before if after == whole else fee_share(total_fee,taken,whole)
                allocated[key] = [after, fee_before+share]
                return share
        for lot_id, opener_key, taken in consumptions:
            lot = c.execute(
                "SELECT price, source_execution_key FROM inventory_lots "
                "WHERE lot_id = ?", (lot_id,)).fetchone()
            if lot is None:
                continue
            opener_exec = c.execute(
                "SELECT fee_amount, qty_base, side, price FROM executions "
                "WHERE execution_key = ?", (opener_key,)).fetchone()
            if opener_exec is None:
                continue
            if closer_side == "sell":
                buy_key, sell_key = opener_key, fill["execution_key"]
                buy_px = _dec(opener_exec["price"])
                sell_px = closer_px
                buy_fee_alloc = allocate(opener_key, 'buy', _dec(opener_exec["fee_amount"]),
                                         _dec(opener_exec["qty_base"]), taken)
                sell_fee_alloc = allocate(fill['execution_key'], 'sell', closer_fee, closer_whole, taken)
            else:
                buy_key, sell_key = fill["execution_key"], opener_key
                buy_px = closer_px
                sell_px = _dec(opener_exec["price"])
                buy_fee_alloc = allocate(fill['execution_key'], 'buy', closer_fee, closer_whole, taken)
                sell_fee_alloc = allocate(opener_key, 'sell', _dec(opener_exec["fee_amount"]),
                                          _dec(opener_exec["qty_base"]), taken)
            rows.append({
                "grid_id": grid_id,
                "buy_execution_key": buy_key,
                "sell_execution_key": sell_key,
                "qty": _fmt(taken),
                "buy_fee_alloc": _fmt(buy_fee_alloc),
                "sell_fee_alloc": _fmt(sell_fee_alloc),
            })
        return rows

    @staticmethod
    def insert_rows(c, rows: List[Dict[str, str]]) -> None:
        for r in rows:
            c.execute(
                "INSERT INTO pair_allocations(grid_id, buy_execution_key, "
                "sell_execution_key, qty, buy_fee_alloc, sell_fee_alloc) "
                "VALUES(?,?,?,?,?,?)",
                (r["grid_id"], r["buy_execution_key"],
                 r["sell_execution_key"], r["qty"], r["buy_fee_alloc"],
                 r["sell_fee_alloc"]))

    # ------------------------------------------------------------ reads
    @staticmethod
    def pair_summary(c, grid_id: str) -> Dict[str, Any]:
        rows = c.execute(
            "SELECT p.qty, p.buy_fee_alloc, p.sell_fee_alloc, "
            "b.price AS buy_price, s.price AS sell_price "
            "FROM pair_allocations p "
            "JOIN executions b ON b.execution_key = p.buy_execution_key "
            "JOIN executions s ON s.execution_key = p.sell_execution_key "
            "WHERE p.grid_id = ?", (grid_id,)).fetchall()
        with localcontext() as context:
            context.prec = 100
            qty_total = Decimal(0)
            net = Decimal(0)
            for r in rows:
                q = _dec(r["qty"])
                qty_total += q
                net += (_dec(r["sell_price"]) - _dec(r["buy_price"])) * q
                net -= _dec(r["buy_fee_alloc"])
                net -= _dec(r["sell_fee_alloc"])
            return {"pair_qty": _fmt(qty_total), "pair_net": _fmt(net),
                    "pairs": len(rows)}
