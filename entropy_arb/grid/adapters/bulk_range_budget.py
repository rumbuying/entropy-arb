"""Long-grid capital allocation, not a replica of Bulk order admission.

The application reserves full possible inventory at the requested leverage,
entry/exit fees and a loss envelope over the configured/protective prices.
Native account/market leverage ceilings are checked by the factory. Venue
admission, protection and recovery qualifications remain separate controls.
No correlation credit or early credit from resting exit orders is taken.
"""
from dataclasses import dataclass,replace
from decimal import Decimal,localcontext,ROUND_CEILING
from fractions import Fraction

from .. import models
from .base import AdapterError
from .bulk_wire import native_decimal,text


def nonnegative(value):return Fraction(native_decimal(value,negative=False))


def ceiling(value):
    with localcontext() as ctx:
        ctx.prec=60;ctx.rounding=ROUND_CEILING
        return text(Decimal(value.numerator)/Decimal(value.denominator))


@dataclass(frozen=True)
class BulkLongRangeBudget:
    qty_step:str
    prices:tuple
    mark:str
    grid_count:int
    leverage:int
    taker_fee:str
    total_budget:str
    account_remaining:str
    gross_qty:str='0'
    current_qty:str='0'
    legacy_qty:str='0'
    pending_qty:str='0'
    legacy_pending_qty:str='0'
    observed_margin:str='0'
    current_slots:tuple=()
    pending_slots:tuple=()
    stress_prices:tuple=()
    service_coverage_enabled:bool=False
    model_id:str='bulk-long-cross-capital-budget-v1'
    ordinary_utilization='0.9'
    monotone_demand=True

    def __post_init__(self):
        if (type(self.grid_count) is not int or self.grid_count<2
                or type(self.leverage) is not int or self.leverage<1
                or len(self.prices)!=self.grid_count+1):
            raise AdapterError('BULK_RANGE_INPUT_INVALID')
        step=nonnegative(self.qty_step)
        values=[nonnegative(x) for x in (*self.prices,*self.stress_prices,self.mark)]
        if step<=0 or not values or min(values)<=0:raise AdapterError('BULK_RANGE_INPUT_INVALID')
        gross,current,legacy,pending,old_pending=[nonnegative(x) for x in
            (self.gross_qty,self.current_qty,self.legacy_qty,self.pending_qty,self.legacy_pending_qty)]
        if current+legacy!=gross or old_pending>pending:raise AdapterError('BULK_RANGE_INVENTORY_INVALID')
        slots=self.current_slots or ('0',)*self.grid_count
        working=self.pending_slots or ('0',)*self.grid_count
        if (len(slots)!=self.grid_count or len(working)!=self.grid_count
                or sum(map(nonnegative,slots),Fraction(0))!=current
                or sum(map(nonnegative,working),Fraction(0))!=pending-old_pending):
            raise AdapterError('BULK_RANGE_SLOT_INVENTORY_INVALID')
        fee=Fraction(native_decimal(self.taker_fee))
        if abs(fee)>Fraction(1,100):raise AdapterError('BULK_RANGE_FEE_UNVERIFIED')
        nonnegative(self.total_budget);nonnegative(self.account_remaining)
        nonnegative(self.observed_margin)

    def with_service_coverage(self):
        if self.service_coverage_enabled:return self
        return replace(self,service_coverage_enabled=True,model_id=self.model_id+':service-envelope-v1')

    def demand_row(self,*,k):
        models.parse_strict_int(k,'k',minimum=1)
        step=nonnegative(self.qty_step);unit=step*k
        prices=[nonnegative(v) for v in (*self.prices,*self.stress_prices,self.mark)]
        low,high=min(prices),max(prices)
        gross,current,legacy,pending,old_pending=[nonnegative(x) for x in
            (self.gross_qty,self.current_qty,self.legacy_qty,self.pending_qty,self.legacy_pending_qty)]
        slots=tuple(map(nonnegative,self.current_slots or ('0',)*self.grid_count))
        working=tuple(map(nonnegative,self.pending_slots or ('0',)*self.grid_count))
        # Each slot is nondecreasing in unit. An oversized existing lot occupies
        # only its own slot, so it cannot hide future buys in other empty slots.
        # Legacy revisions are additional obligations with no overlap credit.
        exposure=sum((max(unit,x+p) for x,p in zip(slots,working)),Fraction(0))+legacy+old_pending
        future_entries=sum((max(Fraction(0),unit-x,p) for x,p in zip(slots,working)),Fraction(0))+old_pending
        fee=max(Fraction(0),Fraction(native_decimal(self.taker_fee)))
        fees=(2*future_entries+gross)*high*fee
        ordinary=max(exposure*high/self.leverage,nonnegative(self.observed_margin))+fees
        adverse=ordinary+exposure*(high-low)
        row=dict(k=k,ordinary=ceiling(ordinary),adverse=ceiling(adverse),account=ceiling(adverse))
        if self.service_coverage_enabled:row['service_open_notional']=ceiling(2*exposure*high)
        return row

    def demand_rows(self,*,k_min,k_max):
        if k_max-k_min>100000:raise AdapterError('BULK_RANGE_MONOTONE_SOLVER_REQUIRED')
        return [self.demand_row(k=k) for k in range(k_min,k_max+1)]


def inventory_terms(context,*,grid_count):
    """Resolve immutable ledger inventory for one selected long-only grid."""
    try:return _inventory_terms(context,grid_count=grid_count)
    except (KeyError,TypeError,ValueError,ArithmeticError):
        raise AdapterError('BULK_RANGE_INVENTORY_UNVERIFIED') from None


def _inventory_terms(context,*,grid_count):
    models.parse_strict_int(grid_count,'grid_count',minimum=2)
    slots=[Fraction(0)]*grid_count;working=[Fraction(0)]*grid_count
    if context is None:return dict(gross_qty='0',current_qty='0',legacy_qty='0',pending_qty='0',legacy_pending_qty='0',
        current_slots=tuple('0' for _ in slots),pending_slots=tuple('0' for _ in slots)),None,()
    revision=models.parse_strict_int(context['revision'],'revision',minimum=1)
    net=nonnegative(context['qty_base']);gross=Fraction(0);legacy=Fraction(0);pending=Fraction(0);old_pending=Fraction(0)
    prices=[]
    for lot in context['lots']:
        if lot['side']!='buy':raise AdapterError('BULK_RANGE_SHORT_UNSUPPORTED')
        qty=nonnegative(lot['qty_remaining']);gross+=qty
        if models.parse_strict_int(lot['origin']['revision'],'revision',minimum=1)!=revision:legacy+=qty
        else:
            origin=lot['origin']
            if origin['cause_key'].startswith('initial-position:'):
                allocations=lot['initial_exit_allocations']
                if sum((nonnegative(a['qty_base']) for a in allocations),Fraction(0))!=qty:
                    raise AdapterError('BULK_RANGE_INITIAL_ALLOCATION_UNVERIFIED')
                for a in allocations:
                    index=models.parse_strict_int(a['level_index'],'level_index',minimum=1)-1
                    if index>=grid_count:raise AdapterError('BULK_RANGE_SLOT_SCOPE_CONFLICT')
                    slots[index]+=nonnegative(a['qty_base'])
            else:
                index=models.parse_strict_int(origin['level_index'],'level_index',minimum=0)
                if index>=grid_count:raise AdapterError('BULK_RANGE_SLOT_SCOPE_CONFLICT')
                slots[index]+=qty
    if gross!=net:raise AdapterError('BULK_RANGE_INVENTORY_INVALID')
    for order in context['orders']:
        if order['state'] in ('UNKNOWN','DISPATCHING','CANCEL_PENDING','CANCEL_REQUESTED'):
            raise AdapterError('BULK_RANGE_ORDER_UNRESOLVED')
        if order['state'] not in ('PLANNED','BLOCKED_SLOT','OPEN','PARTIAL','ACCEPTED'):continue
        price=nonnegative(order['price']);prices.append(ceiling(price))
        # Ledger rows encode reduce_only as 0/1, not JSON booleans.
        if not isinstance(order['reduce_only'],(bool,int)) or isinstance(order['reduce_only'],bool) is False and order['reduce_only'] not in (0,1):
            raise AdapterError('BULK_RANGE_ORDER_INVALID')
        if bool(order['reduce_only']):continue
        if order['side']!='buy':raise AdapterError('BULK_RANGE_SHORT_UNSUPPORTED')
        qty=nonnegative(order['qty_base']);done=nonnegative(order.get('cumulative_qty','0'))
        if done>qty:raise AdapterError('BULK_RANGE_ORDER_INVALID')
        remaining=qty-done;pending+=remaining
        if models.parse_strict_int(order['revision'],'revision',minimum=1)!=revision:old_pending+=remaining
        else:
            index=models.parse_strict_int(order['level_index'],'level_index',minimum=0)
            if index>=grid_count:raise AdapterError('BULK_RANGE_SLOT_SCOPE_CONFLICT')
            working[index]+=remaining
    return dict(gross_qty=ceiling(gross),current_qty=ceiling(gross-legacy),legacy_qty=ceiling(legacy),
        pending_qty=ceiling(pending),legacy_pending_qty=ceiling(old_pending),current_slots=tuple(map(ceiling,slots)),
        pending_slots=tuple(map(ceiling,working))),context['effective_total'],tuple(prices)
