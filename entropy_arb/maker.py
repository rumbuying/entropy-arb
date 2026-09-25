"""Venue-generic maker contract.

This module is the boundary between the engine's maker logic (the quote loop,
hedge-on-fill, safety ladder — all exchange-agnostic) and the exchange
adapters that implement the maker role. A venue may act as the maker leg by:

    1. declaring ``maker_capable = True`` as a class attribute, and
    2. implementing the three contract methods below, plus the standard venue
       interface it already implements.

    async def place_maker(is_buy, qty, limit_px, reduce_only=False) -> dict
        Post a post-only (or equivalent "never take liquidity") limit order.
        Returns {order_id, status, err, filled_base, avg_px, unresolved}.
        status "open" means resting and live. A post-only rejection (the
        price would have crossed) is NOT an error: it comes back as
        status "canceled" with reason "would_cross" — expected in fast
        markets, not a failure to retry.

    async def cancel_orders(order_ids=None) -> dict
        Cancel by id list, or — with order_ids None — cancel ALL open orders
        for this venue's market atomically. The market-wide form is the P0
        safety path ("hedge leg went blind → make the quotes disappear") and
        must be a single cheap request on venues that support it.

    def on_fill(cb) -> None
        Register the fill callback, invoked with FillEvent for every newly
        executed quantity. Must be idempotent: re-delivered or replayed
        messages (reconnects, duplicate frames) must not emit the same fill
        twice.

Capability checklist for a candidate maker venue (see MAKER-DESIGN.md §5.3):
post-only semantics, atomic batch cancel, authenticated real-time fills with
an idempotency key, maker fee < taker fee, REST order-status fallback, a test
environment, and enough shared symbols to pair. Adapters hide every
exchange-specific detail (auth scheme, numeric formatting, enums, ws message
shapes, cancel semantics, rate-limit shape) behind this contract.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FillEvent:
    """One incremental fill on a maker order.

    ``qty_delta`` is the NEWLY executed base quantity since the previous
    event for this order — never the cumulative amount — so the engine can
    hedge exactly the delta it just learned about.
    """

    order_id: str
    client_order_id: str
    side: str                 # "buy" | "sell" (our side on the maker venue)
    qty_delta: float
    px: float
    fee: float
    ts: float
    status: str = ""          # order status after the update
    update: str = ""          # venue's update-type tag ("fill", ...)
    error_code: str = ""      # forced-cancellation reason, when present


@dataclass
class MakerParams:
    """The ``maker:`` config block (MAKER-DESIGN.md §7). Fees and inventory
    ladder are shared with the taker strategy; everything here is mode-local."""

    enabled: bool = False
    edge_bps: float = 2.0             # target net locked edge per fill
    costs_bps: float = 5.5            # maker fee + hedge taker fee + hedge slip
    requote_bps: float = 1.0          # anchor move that triggers replace
    requote_sec: float = 30.0         # max quote age (heartbeat requote)
    size_base: float = 0.005          # per-side quote size (base units)
    sides: str = "both"               # both | bid | ask
    hedge_batch_ms: int = 250         # fill→hedge aggregation window
    max_hedge_failures: int = 3
    hedge_retry_sec: float = 0.5
    interval_sec: float = 0.5         # quote-loop tick
    trades_csv: str = "logs/maker-trades.csv"
    selection_csv: str = "logs/maker-selection.csv"


@dataclass
class MakerQuote:
    """Engine-side state for one resting quote (one per enabled side)."""

    side: str                 # "bid" | "ask"
    order_id: str
    px: float
    qty: float
    anchor: float             # hedge-side executable price at quote time
    placed_ts: float
    skew_bps: float


def inventory_skew_bps(position: float, mid, cap_usd: float,
                       scale_bps: float, floor_frac: float) -> float:
    """Surcharge (bps) on the quote side that would ADD to inventory.

    Same ramp as the taker strategy's inventory ladder: zero until the
    position passes ``floor_frac`` of the cap, then linear up to
    ``scale_bps`` at the cap. The relevant position is the maker venue's own
    inventory — the taker hedge venue mirrors it, so one side tells the
    whole story."""
    if scale_bps <= 0 or cap_usd <= 0 or not mid:
        return 0.0
    floor = min(max(floor_frac, 0.0), 0.99)
    u = min(abs(position) * mid / cap_usd, 1.0)
    if u <= floor:
        return 0.0
    return scale_bps * (u - floor) / (1.0 - floor)


def quote_prices(hedge_bid, hedge_ask, costs_bps: float, edge_bps: float,
                 skew_bps: float) -> tuple:
    """Maker quotes anchored to the hedge venue's EXECUTABLE prices.

    locked edge of a bid fill  = hedge_bid / bid − 1 − C   (≥ E + K)
    locked edge of an ask fill = ask / hedge_ask − 1 − C   (≥ E + K)

    Anchoring on executable prices (not mids) means the premium's slow drift
    is absorbed automatically — there is no static midline to go stale."""
    bump = (costs_bps + edge_bps + skew_bps) / 1e4
    bid = hedge_bid / (1.0 + bump) if hedge_bid else None
    ask = hedge_ask * (1.0 + bump) if hedge_ask else None
    return bid, ask


def clamp_to_maker_book(bid_px, ask_px, *, maker_bid, maker_ask, tick,
                        hedge_bid, hedge_ask, costs_bps):
    """Keep post-only quotes on the correct side of the MAKER venue's touch.

    The quotes are anchored to the HEDGE venue's executable prices, which is
    right economically but assumes both venues trade near the same price. On a
    pair with a persistent basis (Lighter-RH sits ~6-10bp above Katana) the
    hedge-anchored price can land beyond the maker venue's touch, where GTX
    rejects it outright (LIMIT_PRICE_CROSSES_SPREAD) — and retrying the same
    price every tick also burns the venue's order budget.

    So clamp each side just inside the touch, then drop the side when the
    clamped price no longer covers costs: not quoting beats quoting a loser.
    Returns (bid_px | None, ask_px | None)."""
    cost = costs_bps / 1e4
    if not (maker_bid and maker_ask and tick):
        return bid_px, ask_px
    if bid_px is not None:
        bid_px = min(bid_px, maker_ask - tick)
        if not (bid_px > 0 and hedge_bid / bid_px - 1.0 >= cost):
            bid_px = None
    if ask_px is not None:
        ask_px = max(ask_px, maker_bid + tick)
        if not (ask_px / hedge_ask - 1.0 >= cost):
            ask_px = None
    return bid_px, ask_px


def requote_reason(*, remaining: float, size: float, anchor_now,
                   anchor_quoted, requote_bps: float, age_sec: float,
                   requote_sec: float, skew_now: float, skew_quoted: float,
                   skew_eps: float = 0.5):
    """Why a resting quote must be replaced, or None to keep it.

    Order matters for the CSV: fill-driven reasons first, then market drift,
    then housekeeping."""
    if remaining <= 1e-12:
        return "filled"
    if remaining < 0.5 * size:
        return "consumed"
    if anchor_now and anchor_quoted and \
            abs(anchor_now / anchor_quoted - 1.0) * 1e4 > requote_bps:
        return "anchor_moved"
    if age_sec >= requote_sec:
        return "aged"
    if abs(skew_now - skew_quoted) > skew_eps:
        return "skew"
    return None
