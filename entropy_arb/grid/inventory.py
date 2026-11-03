"""Inventory ledger primitives (spec §12): FIFO lots over REAL fills.

All amounts are Decimal; nothing here reads floats. Lots are opened from
executions and consumed strictly FIFO; consumption records are the input
to grid pairing (``pairing.py``). A fill replay hits the executions
unique key first and never reaches these functions twice (the actor's
transaction guarantees that ordering).
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from . import models
from .store import Store


def _dec(v: str) -> Decimal:
    return models.parse_decimal_text(v, "qty")


def _fmt(d: Decimal) -> str:
    return models.canonical_decimal_text(d)


class Inventory:
    """Lot store view for one grid. All writes happen inside the CALLER's
    transaction (the actor passes the writer connection)."""

    def __init__(self, st: Store, grid_id: str) -> None:
        self.store = st
        self.grid_id = grid_id

    # ------------------------------------------------------------- writes
    def open_lot(self, c, *, side: str, qty: Decimal, price: Decimal,
                 exec_key: str, ts: str) -> str:
        lot_id = str(uuid.uuid4())
        c.execute(
            "INSERT INTO inventory_lots(lot_id, grid_id, side, "
            "qty_remaining, price, source_execution_key, open_ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (lot_id, self.grid_id, side, _fmt(qty), _fmt(price), exec_key,
             ts))
        return lot_id

    def consume_fifo(self, c, *, side: str, qty: Decimal) \
            -> List[Tuple[str, str, Decimal]]:
        """Consume up to ``qty`` from the OLDEST open lots of ``side``.
        Returns [(lot_id, source_execution_key, taken)]. Residual demand
        beyond open inventory returns as a shortage the caller must
        handle (reduce-only orders must never exceed real stock)."""
        rows = c.execute(
            "SELECT lot_id, qty_remaining, price, source_execution_key, "
            "open_ts FROM inventory_lots WHERE grid_id = ? AND side = ? "
            "AND qty_remaining > 0 ORDER BY open_ts, lot_id",
            (self.grid_id, side)).fetchall()
        remaining = qty
        out: List[Tuple[str, str, Decimal]] = []
        for r in rows:
            if remaining <= 0:
                break
            have = _dec(r["qty_remaining"])
            taken = min(have, remaining)
            left = have - taken
            closed = "1" if left == 0 else None
            c.execute(
                "UPDATE inventory_lots SET qty_remaining = ?, "
                "closed_ts = COALESCE(?, closed_ts) WHERE lot_id = ?",
                (_fmt(left), models.utc_now_iso() if closed else None,
                 r["lot_id"]))
            out.append((r["lot_id"], r["source_execution_key"], taken))
            remaining -= taken
        return out

    # ------------------------------------------------------------- reads
    def net_position(self, c) -> Decimal:
        rows = c.execute(
            "SELECT side, qty_remaining, price FROM inventory_lots "
            "WHERE grid_id = ? AND qty_remaining > 0",
            (self.grid_id,)).fetchall()
        signed = Decimal(0)
        for r in rows:
            q = _dec(r["qty_remaining"])
            signed += q if r["side"] == "buy" else -q
        return signed

    def unrealized(self, c, mark: Decimal) -> Decimal:
        rows = c.execute(
            "SELECT side, qty_remaining, price FROM inventory_lots "
            "WHERE grid_id = ? AND qty_remaining > 0",
            (self.grid_id,)).fetchall()
        total = Decimal(0)
        for r in rows:
            q = _dec(r["qty_remaining"])
            px = _dec(r["price"])
            if r["side"] == "buy":
                total += (mark - px) * q
            else:
                total += (px - mark) * q
        return total

    def open_lots(self, c) -> List[Dict[str, Any]]:
        rows = c.execute(
            "SELECT * FROM inventory_lots WHERE grid_id = ? AND "
            "qty_remaining > 0 ORDER BY open_ts, lot_id",
            (self.grid_id,)).fetchall()
        return [dict(r) for r in rows]
