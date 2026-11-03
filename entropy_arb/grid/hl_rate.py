"""Conservative shared REST pacing; signed writes are never retried.

Hyperliquid limits REST by IP, not account. All transports on this event loop
and API origin share a rolling read budget, leaving room for exchange actions
and other applications. Independent processes still need deployment coordination.
"""
import asyncio
import math
import time
import weakref
from collections import deque
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class ReadReservation:
    budget: object
    entry: tuple
    remaining: int
    active: bool = True
    critical: bool = True


_reservation = ContextVar('hl_read_reservation', default=None)


LIGHT_INFO = frozenset(("l2Book", "allMids", "clearinghouseState", "orderStatus",
                        "spotClearinghouseState", "exchangeStatus"))
PAGED_INFO = frozenset(("recentTrades", "historicalOrders", "userFills", "userFillsByTime",
                       "fundingHistory", "userFunding", "nonUserFundingUpdates", "twapHistory",
                       "userTwapSliceFills", "userTwapSliceFillsByTime", "delegatorHistory",
                       "delegatorRewards", "validatorStats"))


def info_weight(kind):
    return 2 if kind in LIGHT_INFO else 60 if kind == "userRole" else 20


async def critical_instrument_rules(venue, market):
    """Control-path rules stay fresh without waiting behind public UI reads."""
    transport = getattr(venue, 'transport', None)
    if getattr(venue, 'venue', None) == 'hl' and hasattr(transport, 'read_sequence'):
        async with transport.read_sequence(40):
            return await venue.load_instrument(market)
    return await venue.load_instrument(market)


async def critical_market_snapshot(venue):
    """Execution observations may use the reserved allowance; UI reads may not.

    The native quote consists of metaAndAssetCtxs and l2Book. Nested public
    quote collection inherits this critical reservation without double charge.
    """
    if hasattr(venue,'execution_market_snapshot'):
        return await venue.execution_market_snapshot()
    transport=getattr(venue,'transport',None)
    if hasattr(transport,'read_sequence'):
        async with transport.read_sequence(22):
            return await venue.market_snapshot()
    return await venue.market_snapshot()


async def critical_account_snapshot(venue):
    """Protection/recovery needs fresh account evidence even at the UI ceiling."""
    transport=getattr(venue,'transport',None)
    if hasattr(transport,'read_sequence'):
        async with transport.read_sequence(350):
            return await venue.account_snapshot()
    return await venue.account_snapshot()


async def critical_position_snapshot(venue):
    """Protection-only quantity checks need no portfolio margin reservation."""
    if not hasattr(venue, 'position_snapshot'):
        return await critical_account_snapshot(venue)
    transport = getattr(venue, 'transport', None)
    if hasattr(transport, 'read_sequence'):
        async with transport.read_sequence(2):
            return await venue.position_snapshot()
    return await venue.position_snapshot()


async def critical_open_orders(venue):
    """Final closeout must not wait behind the UI's allowance ceiling."""
    transport = getattr(venue, 'transport', None)
    if getattr(venue, 'venue', None) == 'hl' and hasattr(transport, 'read_sequence'):
        async with transport.read_sequence(20):
            return await venue.open_orders()
    return await venue.open_orders()


class ReadBudget:
    def __init__(self, *, limit=900, window=60, clock=time.monotonic, sleep=asyncio.sleep, reserved=None):
        self.limit, self.window, self.clock, self.sleep = limit, window, clock, sleep
        self.reserved = (350 if limit >= 900 else 0) if reserved is None else reserved
        self.spent = deque()
        self.blocked_until = 0
        self.last_rate_limit = None
        self.lock = asyncio.Lock()
        self.capacity_changed = asyncio.Event()
        self.execution_waiters = 0
        self.wait_count = 0
        self.wait_seconds = 0.0
        self.last_wait = None

    def notify_capacity(self):
        changed=self.capacity_changed
        self.capacity_changed=asyncio.Event()
        changed.set()

    async def wait_change_or_time(self, changed, delay):
        """Refunded reservations can become available before window expiry."""
        sleeper=asyncio.create_task(self.sleep(min(delay,self.window)))
        wake=asyncio.create_task(changed.wait())
        try:
            done,_=await asyncio.wait((sleeper,wake),return_when=asyncio.FIRST_COMPLETED)
            if sleeper in done:await sleeper
            if wake in done:await wake
        finally:
            for task in (sleeper,wake):
                if not task.done():task.cancel()
            await asyncio.gather(sleeper,wake,return_exceptions=True)

    def prune(self, now):
        while self.spent and self.spent[0][0] <= now - self.window:
            self.spent.popleft()

    async def acquire(self, weight, *, critical=False, foreground=False):
        if not 0 < weight <= self.limit:
            raise ValueError("invalid REST weight")
        while True:
            async with self.lock:
                now = self.clock()
                self.prune(now)
                scope = _reservation.get()
                critical_scope = scope and scope.budget is self and scope.active and scope.critical
                active_scope = scope and scope.budget is self and scope.active
                ceiling = self.limit if critical or critical_scope else self.limit - self.reserved
                if weight > ceiling:
                    raise ValueError("invalid REST weight")
                reservation = self.current_reservation()
                if (reservation and now >= self.blocked_until and weight <= reservation.remaining):
                    reservation.remaining -= weight
                    return None
                used = sum(item[1] for item in self.spent)
                delay = max(0, self.blocked_until - now)
                # Stop admitting new display/background sequences while an
                # ordinary execution sequence is waiting. Let in-flight
                # scopes finish; blocking them can deadlock their transport
                # lock. Critical protection/cancellation remains independent.
                if self.execution_waiters and not (critical or critical_scope or active_scope or foreground):
                    delay = max(delay, self.window)
                if used + weight > ceiling:
                    delay = max(delay, self.spent[0][0] + self.window - now)
                if delay <= 0:
                    entry = (now, weight)
                    self.spent.append(entry)
                    return entry
                changed=self.capacity_changed
            # A cancelled waiter reserves nothing and sends nothing.
            started = self.clock()
            self.wait_count += 1
            try:
                await self.wait_change_or_time(changed,delay)
            finally:
                elapsed = max(0, self.clock() - started)
                self.wait_seconds += elapsed
                self.last_wait = {'weight': weight, 'critical': bool(critical or critical_scope),
                                  'elapsed_ms': int(elapsed * 1000)}

    def capacity_available(self, weight, *, critical=True):
        now = self.clock();self.prune(now)
        if self.current_reservation() and now >= self.blocked_until:
            return True
        scope = _reservation.get()
        if (not critical and self.execution_waiters
                and not (scope and scope.budget is self and scope.active)):
            return False
        ceiling = self.limit if critical else self.limit - self.reserved
        return now >= self.blocked_until and sum(item[1] for item in self.spent) + weight <= ceiling

    def current_reservation(self):
        current = _reservation.get()
        if (current and current.budget is self and current.active and current.remaining > 0
                and self.clock() < current.entry[0] + self.window):
            return current
        return None

    @asynccontextmanager
    async def read_sequence(self, weight, *, critical=True, foreground=False):
        """Reserve a read-only sequence before collecting fresh evidence.

        Parallel child reads consume the same allowance under the budget
        lock. Unused weight is refunded; each HTTP read is charged once.
        No signed action is queued or retried by this mechanism.
        """
        if self.current_reservation():
            yield
            return
        entry = await self.acquire(weight, critical=critical, foreground=foreground)
        reservation = ReadReservation(self, entry, weight, critical=critical)
        token = _reservation.set(reservation)
        try:
            yield
        finally:
            reservation.active = False
            _reservation.reset(token)
            async with self.lock:
                for index, recorded in enumerate(self.spent):
                    if recorded is entry:
                        used = entry[1] - reservation.remaining
                        if used:
                            self.spent[index] = (entry[0], used)
                        else:
                            del self.spent[index]
                        if reservation.remaining:self.notify_capacity()
                        break

    @asynccontextmanager
    async def execution_read_sequence(self, weight=350):
        """Ordinary execution precedes new display reads at the PUBLIC ceiling.

        This is scheduling priority, never permission to use the protected
        allowance, exceed the IP limit or send an expired signed action.
        """
        self.execution_waiters += 1
        self.notify_capacity()
        try:
            async with self.read_sequence(weight, critical=False, foreground=True):
                yield
        finally:
            self.execution_waiters -= 1
            self.notify_capacity()

    async def wait_capacity(self, weight):
        """Preparation hint, not a reservation or authority to send."""
        if not 0 < weight <= self.limit:
            raise ValueError('invalid REST preparation weight')
        while True:
            async with self.lock:
                now = self.clock();self.prune(now)
                if self.capacity_available(weight):
                    return
                delay = max(0, self.blocked_until - now)
                if self.spent:
                    delay = max(delay, self.spent[0][0] + self.window - now)
                changed=self.capacity_changed
            await self.wait_change_or_time(changed,delay)

    def charge_response(self, kind, body):
        if isinstance(body, list) and (kind in PAGED_INFO or kind == "candleSnapshot"):
            divisor = 60 if kind == "candleSnapshot" else 20
            weight = math.ceil(len(body) / divisor)
            if weight:
                self.spent.append((self.clock(), weight))

    def rate_limited(self, *, source='unspecified'):
        self.blocked_until = max(self.blocked_until, self.clock() + self.window)
        self.last_rate_limit = {'source': source, 'monotonic': self.clock()}


_by_loop = weakref.WeakKeyDictionary()


def shared_read_budget(origin):
    loop = asyncio.get_running_loop()
    origins = _by_loop.setdefault(loop, {})
    if origin not in origins:
        origins[origin] = ReadBudget()
    return origins[origin]
