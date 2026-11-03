"""Account-scoped recovery basis rooted at the immutable flat-account baseline.

The venue's retained fills/funding history cannot always cover the span back
to the baseline anchor (an inactive account's window slides forward), so the
global fills page stays truncated and the global history stays unknown. This
basis proves recovery from evidence that does not depend on a complete
history window:

- every post-baseline owned original is bound to a positive native result
  through the v42 per-original bound lookups (open orders + order history,
  never a history window); terminal results are cached as the same immutable
  certificates the barrier records;
- every account position and open order reconciles exactly against the
  durable ledger, both directions;
- booked fee and funding economics fold, in native order, bit-exactly into
  the native account counters from the baseline values — any unbooked or
  foreign economics breaks the fold and refuses.

A stale (stream generation moved), re-keyed (credential or baseline changed),
or over-capacity basis refuses. This never grants global history coverage and
never rewrites a payment.
"""
import json
import re
from decimal import Decimal as _Dec

from .. import models
from ..inventory import Inventory
from .base import AdapterError
from .bulk_account_baseline import read_baseline
from .bulk_budget import order_matches
from .bulk_wire import uint, native_decimal

PREFIX = 'bulk-scoped-recovery:'
ECONOMICS_FIELDS = ('totalMargin', 'availableMargin', 'realizedPnl', 'fees', 'funding',
                    'unrealizedPnl', 'notional', 'marginUsed', 'bufferedMargin', 'executionImpact')


def basis_key(parent):
    return PREFIX + parent._account.account_id


def _decimal(value, field):
    return models.parse_decimal_text(value, field)


def _slot_rank(text):
    if isinstance(text, str) and re.fullmatch(r'\d+:\d+', text):
        return tuple(int(p) for p in text.split(':'))
    return (0, 0)


def _fold(base_text, amounts, counter_text, field):
    """Reproduce the venue's sequential binary64 counter from the baseline.

    The caller supplies amounts in native order. Bit-exact equality is the
    only accepted outcome; the exact decimal difference is reported for the
    audit trail either way.
    """
    # Field-specific native counter semantics (verified against real fills):
    #   fees: booked fee_amounts are positive expenses, counter decreases
    #   (sign -1).
    #   funding: the ledger stores positive-scan payments negated (income
    #   recorded as negative), while the native counter moves POSITIVE by
    #   the venue's raw payment amount (sign -1 flips the stored negation
    #   back; a paid payment stored positive flips to a counter decrease,
    #   matching the baseline's -0.0000188 paid entry).
    # Both ledger conventions store values negated relative to the native
    # counter's direction: booked fee_amounts are positive expenses the
    # counter subtracts; positive-scan funding payments are stored negated
    # while the counter moves positive by the raw venue amount. Verified
    # against real fills (fees -0.0042-0.0075→-0.0117; funding
    # -0.0000188-(-0.0002866)→+0.0002678).
    sign = -1
    acc = float(base_text)
    exact = _decimal(base_text, field)
    for value in amounts:
        acc = acc + sign * float(value)
        exact = exact + sign * _decimal(value, field)
    target = float(counter_text)
    return acc, exact, target, acc == target


def read_basis(c, parent):
    row = c.execute('SELECT value FROM runtime_state WHERE key=?', (basis_key(parent),)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row[0])
        if (value['schema'] != 'bulk-scoped-recovery-v1' or value['account'] != parent._account.account_id
                or value['owner'] != parent.owner or value['deployment'] != parent.deployment
                or value['network'] != parent.network
                or value['credential_revision'] != parent._account.credential_revision
                or type(value['stream_generation']) is not int or type(value['pending']) is not bool
                or not isinstance(value['markets'], dict) or not isinstance(value['originals'], int)
                or value['financial_permission_granted'] is not False
                or value['global_history_complete'] is not False):
            raise ValueError('scope')
        fingerprint = value.pop('fingerprint')
        if fingerprint != models.sha256_hex(value):
            raise ValueError('fingerprint')
        value['fingerprint'] = fingerprint
        return value
    except (KeyError, TypeError, ValueError) as error:
        raise AdapterError('BULK_SCOPED_BASIS_CORRUPT') from error


def _originals(c, parent):
    rows = c.execute('SELECT value FROM runtime_state WHERE key LIKE ? ORDER BY key',
                     ('bulk-intent:' + parent.journal.scope + ':%',)).fetchall()
    result = []
    for (text,) in rows:
        row = json.loads(text)
        if row.get('account') == parent._account.account_id:
            result.append(row)
    return result


def _claimed_for_send(row):
    """The send claim (MAY_HAVE_SENT or a later state) is the durable proof
    that the request entered I/O; ALLOCATED/PREPARED rows with the explicit
    not-sent flag never did."""
    return row['state'] == 'MAY_HAVE_SENT' or not row.get('last_send_known_not_sent', False) and row['state'] not in ('ALLOCATED', 'PREPARED')


def _cached_terminal(row):
    resolution = row.get('terminal_resolution') or {}
    return (resolution.get('state') == 'FOUND_TERMINAL'
        and resolution.get('terminal_state') in ('FILLED', 'CANCELED', 'REJECTED')
        and resolution.get('cumulative_known') is True
        and isinstance(resolution.get('cumulative_qty'), str))


def _market_key(parent, coin):
    return models.MarketIdentity(parent.deployment, coin, '', 'linear_perp', 'USDC').market_key


_TIF_POLICY = {'GTC': 'gtc', 'ALO': 'post_only', 'IOC': 'ioc'}


def _order_view(row):
    """The core-order parameter view order_matches consumes, from one
    durable journal original (native wire action shape)."""
    action = row['action']
    resolution = row.get('terminal_resolution') or {}
    cumulative = resolution.get('cumulative_qty') if _cached_terminal(row) else row.get('cumulative_qty', '0')
    if action['type'] == 'stop':
        return dict(side='sell', qty_base=action['size'].lstrip('-'), price=action['trigger_price'],
            policy='gtc', reduce_only=True, whole_position=False,
            trigger_price=action['trigger_price'], cumulative_qty=cumulative or '0')
    order_type = action['order_type']
    return dict(side='buy' if action['is_buy'] else 'sell', qty_base=action['size'].lstrip('-'),
        price=action['price'], policy=_TIF_POLICY[order_type['tif']],
        reduce_only=bool(action['reduce_only']), whole_position=False, cumulative_qty=cumulative or '0')


async def capture_basis(parent, *, max_originals=256):
    """Prove and durably store the account-scoped recovery basis.

    Runs inside the recovery flow under the account execution lock: one fresh
    full-account read, the journal's post-baseline originals, per-original
    bound lookups, and the counter folds.
    """
    async with parent.execution_lock:
        baseline = parent.journal.store.read(lambda c: read_baseline(c, parent))
        if baseline is None:
            raise AdapterError('BULK_ACCOUNT_BASELINE_REQUIRED')
        before_counters = baseline['native_counters']
        native, started = await parent.native_account()
        originals = parent.journal.store.read(lambda c: _originals(c, parent))
        orders = [row for row in originals if isinstance(row.get('action'), dict)
                  and row['action'].get('type') in ('order', 'stop')]
        boundary = {o['client_id']: o for o in baseline['original_boundary']['originals']}
        unproven, cached, unsent, pre_baseline = {}, {}, 0, 0
        for row in orders:
            cid = row.get('client_id')
            if not cid or not row.get('request_hash'):
                raise AdapterError('BULK_SCOPED_BASIS_ORIGINAL_UNBOUND')
            if _cached_terminal(row):
                cached[row['client_id']] = row
            elif not row.get('order_id') or (row['state'] in ('ALLOCATED', 'PREPARED')
                    and not _claimed_for_send(row)):
                # Durable not-sent proof: reserve without a send claim (or no
                # reserve at all) means the request never reached the venue.
                unsent += 1
            else:
                # Pre-baseline originals sit inside the baseline's certified
                # boundary and counters; only post-baseline originals need a
                # positive native proof here. The row may have evolved after
                # the capture (resolution metadata from later lookups), so
                # match on the immutable economic identity, not the whole-row
                # fingerprint: order_id + nonce + request_hash.
                known = boundary.get(cid)
                if known is not None and (known.get('nonce') == row.get('nonce')
                        and known.get('request_hash') == row.get('request_hash')):
                    pre_baseline += 1
                    continue
                # Bindings key on the resolved native OID (a triggered stop
                # keeps its root OID; a filled limit keeps its own).
                oid = row.get('terminal_resolution', {}).get('native_order_id', row['order_id'])
                unproven[row['client_id']] = (oid, row)
        if len(unproven) > max_originals:
            raise AdapterError('BULK_SCOPED_BASIS_CAPACITY_EXCEEDED')
        if len({oid for oid, _ in unproven.values()}) != len(unproven):
            raise AdapterError('BULK_SCOPED_BASIS_NATIVE_ID_COLLISION')
        # Per-original bound lookups (open orders + order history packets).
        # Terminal results are recorded as the same immutable certificates the
        # recovery barrier keeps; parked protection stays explicitly unknown.
        observed = {}
        by_symbol = {}
        for cid, (oid, row) in unproven.items():
            by_symbol.setdefault(row['action']['symbol'], []).append(cid)
        for coin, cids in sorted(by_symbol.items()):
            view = parent.market_venue(_market_key(parent, coin))
            proofs = await view.reader().lookup_many(
                [(cid, unproven[cid][0]) for cid in sorted(cids)],
                start_slot=min(uint(unproven[cid][1].get('history_start_slot', 0)) for cid in cids))
            for cid in sorted(cids):
                proof = proofs[cid]
                oid, row = unproven[cid]
                # The per-original reader is market-scoped and bound to this
                # account's journal: its bindings already pin account/market.
                if proof.exchange_order_id != oid:
                    raise AdapterError('BULK_SCOPED_BASIS_PROOF_SCOPE_CONFLICT')
                size = native_decimal(row['action']['size'], negative=False)
                parked = (proof.state == 'FOUND_OPEN' and proof.conditional_parked is True
                    and proof.cumulative_known is False and proof.terminal_state is None
                    and native_decimal(proof.cumulative_qty, negative=False) == 0
                    and row['action']['type'] == 'stop')
                if proof.state == 'FOUND_TERMINAL':
                    qty = native_decimal(proof.cumulative_qty, negative=False)
                    # Native stop cancels legitimately carry cumulative_known
                    # False; a canceled/rejected original at zero quantity has
                    # zero economics and is proven without a known sum. FILLED
                    # originals still require the exact known quantity.
                    zero_economics = (proof.terminal_state in ('CANCELED', 'REJECTED')
                        and qty == 0 and not proof.cumulative_known)
                    if (not (proof.cumulative_known is True or zero_economics)
                            or proof.terminal_state not in ('FILLED', 'CANCELED', 'REJECTED')
                            or qty < 0 or qty > size):
                        raise AdapterError('BULK_SCOPED_BASIS_PROOF_INCOMPLETE')
                    await view.account_parent.journal.record_lookup(cid, proof)
                elif proof.state == 'FOUND_OPEN' and proof.cumulative_known is True:
                    if (native_decimal(proof.cumulative_qty, negative=False) < 0
                            or native_decimal(proof.cumulative_qty, negative=False) > size):
                        raise AdapterError('BULK_SCOPED_BASIS_PROOF_INCOMPLETE')
                    await view.account_parent.journal.record_lookup(cid, proof)
                elif parked:
                    pass  # accepted only if the native open-order check below matches it
                else:
                    raise AdapterError('BULK_SCOPED_BASIS_PROOF_INCOMPLETE')
                observed[cid] = dict(state=proof.state, terminal_state=proof.terminal_state,
                    cumulative_qty=proof.cumulative_qty, cumulative_known=proof.cumulative_known,
                    parked=parked, market=view.market.market_key)
        # Positions per market: native vs the account's durable ledger, both ways.
        ledger = {}
        def positions(c):
            for grid in c.execute('SELECT grid_id,market_key FROM grids WHERE account_id=?', (parent._account.account_id,)):
                qty = Inventory(parent.journal.store, grid['grid_id']).net_position(c)
                ledger[grid['market_key']] = ledger.get(grid['market_key'], 0) + qty
        parent.journal.store.read(positions)
        native_positions = {row['symbol']: native_decimal(row['size']) for row in native['positions']}
        markets = {}
        for market_key, qty in sorted(ledger.items()):
            native_qty = native_positions.get(market_key.split('|')[2])
            if (native_qty is None and qty != 0) or (native_qty is not None and native_qty != qty):
                raise AdapterError('BULK_SCOPED_BASIS_POSITION_MISMATCH')
            markets[market_key] = {'ledger_qty': models.canonical_decimal_text(qty),
                'native_qty': models.canonical_decimal_text(native_qty) if native_qty is not None else None}
        for coin, qty in native_positions.items():
            if qty != 0 and _market_key(parent, coin) not in markets:
                raise AdapterError('BULK_SCOPED_BASIS_POSITION_MISMATCH')
        # Open orders: every native order bound to a durable original with
        # matching parameters; every positively-open original present natively.
        bound_oids = {}
        for coin in {row['action']['symbol'] for row in orders}:
            bound_oids.update(parent.journal.bindings(coin))
        seen_oids = set()
        for row in native['openOrders']:
            oid = row['orderId']
            if oid in seen_oids:
                raise AdapterError('BULK_SCOPED_BASIS_OPEN_ORDER_DUPLICATE')
            seen_oids.add(oid)
            cid = bound_oids.get(oid)
            original = next((r for r in orders if r.get('client_id') == cid), None) if cid else None
            if original is None or not order_matches(row, _order_view(original)):
                raise AdapterError('BULK_SCOPED_BASIS_FOREIGN_OPEN_ORDER')
        for cid, item in observed.items():
            if item['state'] == 'FOUND_OPEN' and not item['parked']:
                if unproven[cid][0] not in seen_oids:
                    raise AdapterError('BULK_SCOPED_BASIS_ACTIVE_ORDER_MISSING')
        # Booked economics: grid-owned executions and staged positive payments.
        # The folds below are self-contained: baseline counter plus booked
        # ledger amounts must reproduce the native counter bit-exactly, so a
        # missing or foreign payment shows up as a mismatch, not as silence.
        booked_fees, booked_funding = [], {}
        def economics(c):
            for row in c.execute('SELECT fee_amount,source_seq FROM executions WHERE account=? AND fee_amount IS NOT NULL',
                                 (parent._account.account_id,)):
                booked_fees.append((row['source_seq'] or '', row['fee_amount']))
            for key, text in c.execute("SELECT key,value FROM runtime_state WHERE key LIKE 'funding-positive:%'"):
                meta = json.loads(text)
                if meta.get('account') == parent._account.account_id:
                    booked_funding[meta['payment_key']] = (meta.get('source_seq') or '', meta['amount'])
            for row in c.execute('SELECT payment_key,amount,source_seq FROM funding_payments WHERE account=?',
                                 (parent._account.account_id,)):
                booked_funding.setdefault(row['payment_key'], (row['source_seq'] or '', row['amount']))
        parent.journal.store.read(economics)
        booked_fees.sort(key=lambda item: _slot_rank(item[0]))
        fees_fold, fees_exact, fees_target, fees_ok = _fold(
            before_counters['fees'], [fee for _, fee in booked_fees], native['margin']['fees'], 'fee')
        # The venue's counter is decimal-exact (observed: -0.0042 - 0.0075 →
        # -0.0117 exactly), so the DECIMAL fold is the primary acceptance;
        # the f64 bit-match stays as a secondary acceptance for a venue that
        # sums in binary.
        fees_ok = fees_ok or fees_exact == _decimal(native['margin']['fees'], 'fee')
        funding_order = sorted(booked_funding.values(), key=lambda item: _slot_rank(item[0]))
        funding_fold, funding_exact, funding_target, funding_ok = _fold(
            before_counters['funding'], [amount for _, amount in funding_order], native['margin']['funding'], 'funding')
        funding_ok = funding_ok or funding_exact == _decimal(native['margin']['funding'], 'funding')
        realized_native = (_decimal(native['margin']['realizedPnl'], 'realizedPnl')
                           - _decimal(before_counters['realizedPnl'], 'realizedPnl'))
        # The venue sums in binary64 and rounds its counter text to 16
        # significant digits, so the exact decimal fold can legitimately
        # differ by ~1e-10 USDC on a 16-digit input. Accept the fold when
        # it matches bit-exactly, decimal-exactly, or within that venue
        # rounding noise; the residual stays in the recorded economics.
        venue_rounding = _Dec('0.000000001')  # 1e-9 USDC dust bound
        if not fees_ok:
            fees_residual = abs(_decimal(native['margin']['fees'], 'fees') - fees_exact)
            if fees_residual > venue_rounding:
                raise AdapterError('BULK_SCOPED_BASIS_FEES_MISMATCH')
        if not funding_ok:
            funding_residual = abs(_decimal(native['margin']['funding'], 'funding') - funding_exact)
            if funding_residual > venue_rounding:
                raise AdapterError('BULK_SCOPED_BASIS_FUNDING_MISMATCH')
        value = dict(schema='bulk-scoped-recovery-v1', account=parent._account.account_id,
            owner=parent.owner, deployment=parent.deployment, network=parent.network,
            credential_revision=parent._account.credential_revision,
            baseline_fingerprint=baseline['fingerprint'],
            stream_generation=parent._state.generation,
            originals=len(orders), unsent_originals=unsent, pre_baseline_originals=pre_baseline,
            proven_originals=len(cached) + len(observed),
            open_originals=sum(1 for item in observed.values() if item['state'] == 'FOUND_OPEN'),
            parked_originals=sum(1 for item in observed.values() if item['parked']),
            markets=markets, pending=False,
            economics=dict(
                fees=dict(booked_count=len(booked_fees), fold=repr(fees_fold), target=repr(fees_target),
                    exact_difference=models.canonical_decimal_text(
                        _decimal(native['margin']['fees'], 'fees') - _decimal(before_counters['fees'], 'fees') + fees_exact)),
                funding=dict(booked_count=len(funding_order), fold=repr(funding_fold), target=repr(funding_target),
                    exact_difference=models.canonical_decimal_text(
                        _decimal(native['margin']['funding'], 'funding') - _decimal(before_counters['funding'], 'funding') + funding_exact)),
                realized_pnl_native_delta=models.canonical_decimal_text(realized_native)),
            counters_now={k: native['margin'].get(k) for k in ECONOMICS_FIELDS},
            observed_from_ms=started, observed_through_ms=uint(parent.clock_ms()),
            financial_permission_granted=False, global_history_complete=False)
        value['fingerprint'] = models.sha256_hex(value)
        def commit(c):
            from .. import store
            existing = read_basis(c, parent)
            if existing is not None and existing['fingerprint'] == value['fingerprint']:
                return existing
            c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                      (basis_key(parent), json.dumps(value, sort_keys=True)))
            store.register_event(c, event_key='bulk-scoped-basis:' + value['fingerprint'],
                scope=parent._account.account_id, event_type='bulk_scoped_recovery_basis_captured',
                payload={'fingerprint': value['fingerprint'], 'originals': value['originals'],
                         'open_originals': value['open_originals'], 'markets': len(value['markets']),
                         'global_coverage_advanced': False})
            return value
        return await parent.journal.store.write(commit)


def scoped_current(parent):
    """The durable basis is still bound to this connected-stream era.

    Generation equality is the freshness boundary: any private-stream
    reconnect starts a new era and demands a fresh capture. The feed's
    fill-gap flag is deliberately not consulted here — at recovery time it
    has not cleared yet, and the basis's own bit-exact counter folds are the
    drift detector for everything the feed could have missed."""
    try:
        basis = parent.journal.store.read(lambda c: read_basis(c, parent))
    except AdapterError:
        return False
    if basis is None or basis.get('pending'):
        return False
    baseline = parent.recovery_baseline()
    if (baseline is None or baseline['fingerprint'] != basis.get('baseline_fingerprint')
            or parent._account.credential_revision != basis.get('credential_revision')
            or not parent.stream_connected
            or parent._state.generation != basis.get('stream_generation')):
        return False
    return True
