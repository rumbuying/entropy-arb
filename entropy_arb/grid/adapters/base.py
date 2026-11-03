"""GridVenue adapter contract (spec §5) + result types.

Types and exceptions ARE part of the contract:

- ``SubmitResult``: ACCEPTED / REJECTED / UNKNOWN — HTTP 200 or a tx hash
  never implies the order is resting; UNKNOWN keeps budget and forces a
  lookup (spec §7.2).
- ``LookupResult``: only a certified terminal proof may be ABSENT_FINAL;
  one 404 is NOT_FOUND_TRANSIENT.
- ``CancelResult``: submitted/terminal/UNKNOWN — success never proves the
  order never filled.
- ``HistoryPage``: empty records does NOT imply full coverage;
  ``complete``/``truncated`` carry that truth.
- Adapters MUST NOT replace or re-generate ``client_id`` (spec §5
  OrderRequest) and MUST NOT round quantities/prices themselves — the
  engine sends already-validated native values.

The legacy maker contract (``maker.py``) stays untouched; grid adapters
implement THIS interface only, and the fixture adapter is the only one
allowed to run without credentials (simulation isolation, spec §2).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Dict, List, Optional, Sequence

from .. import models


@dataclass(frozen=True)
class OrderRequest:
    """What the executor sends. ``client_id`` is engine-assigned and
    persistent; ``request_deadline`` bounds venue-side validity when the
    venue supports it (``None`` = unsupported)."""
    intent_id: str
    client_id: str
    account: str
    market: str
    side: str                  # buy | sell
    qty_base: str              # DecimalText; accounting upper bound for whole_position
    price: str                 # DecimalText
    policy: str                # post_only | gtc | ioc | reduce_only_ioc
    reduce_only: bool
    epoch: int
    request_deadline_sec: Optional[int] = None
    trigger_price: Optional[str] = None
    whole_position: bool = False

    def to_dict(self) -> Dict[str, object]:
        return {"intent_id": self.intent_id, "client_id": self.client_id,
                "account": self.account, "market": self.market,
                "side": self.side, "qty_base": self.qty_base,
                "price": self.price, "policy": self.policy,
                "reduce_only": self.reduce_only, "epoch": self.epoch,
                "request_deadline_sec": self.request_deadline_sec,
                "trigger_price": self.trigger_price, "whole_position": self.whole_position}


@dataclass(frozen=True)
class SubmitResult:
    state: str                 # ACCEPTED | REJECTED | UNKNOWN
    exchange_order_id: Optional[str] = None
    reject_code: Optional[str] = None
    stage: str = ""            # where it stopped (e.g. "http", "ws_ack")
    executed_qty: str = "0"    # cumulative clue, NOT a second fill record
    detail: str = ""

    def __post_init__(self) -> None:
        if self.state not in models.SUBMIT_STATES:
            raise ValueError(f"bad submit state {self.state!r}")


@dataclass(frozen=True)
class CancelResult:
    state: str                 # SUBMITTED | TERMINAL | UNKNOWN
    final_state: Optional[str] = None   # when TERMINAL: FILLED/CANCELED
    detail: str = ""

    def __post_init__(self) -> None:
        if self.state not in ("SUBMITTED", "TERMINAL", "UNKNOWN"):
            raise ValueError(f"bad cancel state {self.state!r}")


@dataclass(frozen=True)
class LookupResult:
    state: str                 # LOOKUP_STATES
    exchange_order_id: Optional[str] = None
    cumulative_qty: str = "0"
    terminal_state: Optional[str] = None  # FILLED | CANCELED | REJECTED
    detail: str = ""
    cumulative_known: bool = True  # zero-size full-position orders do not encode executions
    native_status: Optional[str] = None
    native_status_ms: Optional[int] = None
    conditional_parked: bool = False  # positive untouched conditional-book proof, not fill/history coverage

    def __post_init__(self) -> None:
        if self.state not in models.LOOKUP_STATES:
            raise ValueError(f"bad lookup state {self.state!r}")


@dataclass(frozen=True)
class ExchangeOrder:
    """One open-order snapshot row (reconciler input)."""
    client_id: Optional[str]
    exchange_order_id: str
    side: str
    qty_base: str
    price: str
    cumulative_qty: str
    reduce_only: bool
    source: str                # rest_query | stream
    whole_position: bool = False
    trigger_price: Optional[str] = None
    cumulative_known: bool = True
    conditional_parked: bool = False


@dataclass(frozen=True)
class HistoryPage:
    """Backfill page. ``complete=False`` means a coverage GAP exists —
    recovery must not advance past it (spec §9.3)."""
    records: Sequence[models.ExecutionEvent]
    next_cursor: Optional[str]
    coverage_start: Optional[str]
    coverage_through: Optional[str]
    complete: bool
    truncated: bool = False
    source_revision: str = ""
    scan_pending: bool = False

    def __post_init__(self) -> None:
        if self.records and not self.complete and not self.truncated:
            # an incomplete page that is not marked truncated is a
            # contract violation: callers would treat a gap as coverage
            raise ValueError("incomplete HistoryPage must be truncated or "
                             "carry a next_cursor")


@dataclass(frozen=True)
class VenueEvent:
    """Private-stream event (normalized by the adapter)."""
    kind: str                  # fill | order_update
    execution: Optional[models.ExecutionEvent] = None
    order: Optional[ExchangeOrder] = None
    raw_seq: Optional[str] = None


@dataclass(frozen=True)
class FundingRecord:
    payment_key: str
    amount: str                # signed; positive = paid by us
    asset: str
    ts: str
    source_seq: Optional[str] = None


@dataclass(frozen=True)
class FundingPositiveEvidence:
    """Known native payments inside observed bounds; no coverage assertion.

    observed_through is a query snapshot bound, never a covered cursor. Scope
    and each immutable payment are checked again at the ledger transaction.
    """
    account: str
    market: str
    observed_start: str
    observed_through: str
    source_revision: str
    records: Sequence[FundingRecord]


@dataclass(frozen=True)
class FundingPage:
    """A complete window is distinct from a list of known payments."""
    records: Sequence[FundingRecord]
    coverage_start: Optional[str]
    coverage_through: Optional[str]
    complete: bool
    truncated: bool = False
    source_revision: str = ""
    scan_pending: bool = False
    positive_evidence: Optional[FundingPositiveEvidence] = None

    def __post_init__(self):
        if self.complete and (self.truncated or self.coverage_start is None or self.coverage_through is None or not self.source_revision):
            raise ValueError("complete funding page needs a covered boundary and source revision")
        if not self.complete and self.coverage_through is not None:
            raise ValueError("incomplete funding page must not advance coverage")


@dataclass(frozen=True)
class AccountSnapshot:
    account: str
    ts: str
    assets: Sequence[Sequence[str]]      # [asset, free_amount] rows
    margin_mode: Optional[str]
    position_qty_base: Optional[str]     # signed
    position_reason: Optional[str] = None
    source: str = "adapter"
    evidence: Optional[Dict] = None

    def to_risk_snapshot(self, snapshot_id: str) -> models.RiskSnapshot:
        return models.RiskSnapshot(
            account=self.account, snapshot_id=snapshot_id, ts=self.ts,
            assets=tuple((a[0], a[1]) for a in self.assets),
            margin_mode=self.margin_mode,
            position_qty_base=self.position_qty_base,
            position_reason=self.position_reason, source=self.source)


class AdapterError(Exception):
    """Adapter contract violation (used by conformance tests)."""


class GridVenue:
    """The ONE trading interface the engine talks to. Implementations:
    adapters/fixture.py (simulation) and per-venue real adapters once
    audited (see VENUE-CAPABILITY-AUDIT.md)."""

    venue: str = ""
    deployment: str = ""

    def portfolio_capacity(self, config, snapshot, context):
        """Venue-specific shared collateral calculation; never assume HL.

        An unimplemented model must block sizing/reservation rather than
        silently treat a balance or another venue's margin formula as cash.
        """
        raise AdapterError('VENUE_PORTFOLIO_RISK_UNVERIFIED')

    async def resolve_account(self) -> models.AccountIdentity:
        raise NotImplementedError

    async def load_instrument(self, market_ref: str) -> models.InstrumentRules:
        raise NotImplementedError

    async def account_snapshot(self) -> AccountSnapshot:
        raise NotImplementedError

    async def market_snapshot(self) -> models.MarketSnapshot:
        raise NotImplementedError

    async def submit(self, order: OrderRequest) -> SubmitResult:
        raise NotImplementedError

    async def cancel(self, client_id: str,
                     exchange_order_id: Optional[str] = None) -> CancelResult:
        raise NotImplementedError

    async def lookup(self, client_id: str,
                     exchange_order_id: Optional[str] = None) -> LookupResult:
        raise NotImplementedError

    async def open_orders(self) -> List[ExchangeOrder]:
        raise NotImplementedError

    async def fills(self, cursor: Optional[str] = None,
                    through: Optional[str] = None) -> HistoryPage:
        raise NotImplementedError

    async def funding_payments(self, cursor: Optional[str] = None,
                               through: Optional[str] = None) \
            -> List[FundingRecord]:
        raise NotImplementedError

    async def funding_history(self, cursor: Optional[str] = None,
                              through: Optional[str] = None) -> FundingPage:
        # An old list-only implementation cannot manufacture a coverage proof.
        return FundingPage(await self.funding_payments(cursor, through), cursor, None, complete=False)

    def funding_cursor_rank(self, cursor: str) -> int:
        raise AdapterError("FUNDING_CURSOR_UNVERIFIED")

    def poll(self) -> List[VenueEvent]:
        """Drain pending private events (simulation + adapter shim path).
        Real venue adapters replace this with an async stream feeding the
        same normalized types."""
        raise NotImplementedError
