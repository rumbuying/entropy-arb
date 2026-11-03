"""Conservative USDC-only unified account observation for main perps.

Unified cash comes from spot state, never the per-DEX accountValue or
withdrawable. Foreign DEX exposure and non-USDC assets fail closed. This
module grants no trading qualification or permission to send orders.
Source: official Hyperliquid account-abstraction-modes API documentation.
"""
import asyncio
from decimal import Decimal

from . import models
from .adapters.base import AdapterError

VERSION = 'hl-unified-main-usdc-v1'


async def unified_main_balance(venue, state, orders):
    if not isinstance(orders, list):
        raise AdapterError('HL_UNIFIED_ORDERS_UNVERIFIED')
    root = getattr(venue, 'account_parent', None) or venue
    observer = getattr(root, 'observations', None)
    observed = observer.snapshot() if observer is not None else None
    if observed:
        spot = observed['spot']
        catalog = [None] + [{'name': name} for name in observed['names'][1:]]
    else:
        spot, catalog = await venue._account_infos([
            ('spotClearinghouseState', {'user': venue.owner}), ('perpDexs', {})]) if (
                hasattr(venue, '_account_read_active') and venue._account_read_active()) else await asyncio.gather(
                    venue._info('spotClearinghouseState', user=venue.owner), venue._info('perpDexs'))
    if not isinstance(catalog, list) or not catalog or catalog[0] is not None:
        raise AdapterError('HL_UNIFIED_DEX_CATALOG_UNVERIFIED')
    names = [''] + [item['name'] for item in catalog[1:]]
    if len(names) > 64 or len(set(names)) != len(names) or any(not isinstance(n,str) or not n for n in names[1:]):
        raise AdapterError('HL_UNIFIED_DEX_CATALOG_UNVERIFIED')
    if observer is not None and not observed:
        observer.configure(names)
    foreign_reads = None
    if not observed and hasattr(venue, '_account_read_active') and venue._account_read_active() and names[1:]:
        rows = await venue._account_infos([(kind, {'user': venue.owner, 'dex': dex})
            for dex in names[1:] for kind in ('clearinghouseState', 'frontendOpenOrders')])
        foreign_reads = {dex: rows[index * 2:index * 2 + 2] for index, dex in enumerate(names[1:])}
    async def inspect(dex):
        if observed:
            foreign, working = observed['states'][dex], observed['orders'][dex]
        elif foreign_reads is not None:
            foreign, working = foreign_reads[dex]
        else:
            foreign, working = await asyncio.gather(
                venue._info('clearinghouseState', user=venue.owner, dex=dex),
                venue._info('frontendOpenOrders', user=venue.owner, dex=dex))
        if not isinstance(working, list) or working or any(
                models.parse_decimal_text(p['position']['szi'], 'foreign_qty') != 0 for p in foreign['assetPositions']):
            raise AdapterError('HL_UNIFIED_FOREIGN_DEX_EXPOSURE')
        # Per-DEX equity is deliberately ignored in unified mode; no positions
        # and no margin usage are nevertheless required in unsupported DEXs.
        if models.parse_decimal_text(foreign['crossMarginSummary']['totalMarginUsed'], 'foreign_margin') != 0:
            raise AdapterError('HL_UNIFIED_FOREIGN_DEX_EXPOSURE')
    await asyncio.gather(*(inspect(n) for n in names[1:]))
    if await venue._info('userAbstraction', user=venue.owner) != 'unifiedAccount':
        raise AdapterError('HL_ACCOUNT_MODE_CHANGED')
    if observed and not observer.matches(observed):
        raise AdapterError('HL_UNIFIED_OBSERVATION_CHANGED')
    if observed:
        # Refresh price-dependent cash/hold valuations after awaited scope
        # checks. Only same-generation, unchanged inventory/order/ledger
        # layout is allowed; never use a frozen prior valuation as a cache.
        observed = observer.snapshot()
        spot = observed['spot']
        await asyncio.gather(*(inspect(n) for n in names[1:]))
    total, held, seen = Decimal(0), Decimal(0), set()
    for b in spot['balances']:
        token = b['token']
        if isinstance(token, bool) or not isinstance(token, int) or token < 0 or token in seen:
            raise AdapterError('HL_UNIFIED_BALANCE_UNVERIFIED')
        seen.add(token)
        amount = models.parse_decimal_text(b['total'], 'balance')
        hold = models.parse_decimal_text(b['hold'], 'hold')
        if amount < 0 or hold < 0 or hold > amount:
            raise AdapterError('HL_UNIFIED_BALANCE_UNVERIFIED')
        if token == 0:
            if b['coin'] != 'USDC':raise AdapterError('HL_UNIFIED_BALANCE_UNVERIFIED')
            total, held = amount, hold
        elif amount != 0 or hold != 0:
            raise AdapterError('HL_UNIFIED_NON_USDC_ASSET')
    margin = Decimal(0)
    for row in state['assetPositions']:
        p = row['position']
        qty = models.parse_decimal_text(p['szi'], 'quantity')
        used = models.parse_decimal_text(p['marginUsed'], 'margin')
        if used < 0 or p['leverage']['type'] != 'cross':
            raise AdapterError('HL_UNIFIED_MAIN_CROSS_ONLY')
        if qty == 0 and used != 0:raise AdapterError('HL_UNIFIED_MARGIN_UNVERIFIED')
        margin += used
    # Do not credit a position's released margin twice. Some native holds
    # include reservations; cap free by both cash less holds and cash less
    # current position margin, without assuming a particular hold breakdown.
    free = max(Decimal(0), min(total-held, total-margin))
    scope = {'version':VERSION, 'dexes':names,
             'spot_total':models.canonical_decimal_text(total), 'spot_hold':models.canonical_decimal_text(held)}
    if observed:
        scope['observation'] = {key: observed[key] for key in ('generation', 'revision', 'protocol')}
    return free, {'accountValue':models.canonical_decimal_text(total),
                  'totalMarginUsed':models.canonical_decimal_text(margin)}, scope


def supported_margin_evidence(evidence):
    if evidence.get('abstraction') in ('default','disabled'):return True
    scope = evidence.get('unified_scope')
    if evidence.get('abstraction') != 'unifiedAccount' or not isinstance(scope,dict) or scope.get('version') != VERSION:
        return False
    summary = evidence.get('cross_summary')
    return (isinstance(summary,dict) and summary.get('accountValue') == scope.get('spot_total')
            and isinstance(scope.get('dexes'),list) and scope['dexes'] and scope['dexes'][0] == '')
