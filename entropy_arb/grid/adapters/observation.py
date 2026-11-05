"""Native read-only connection probes, with an enforced HTTP GET allowlist.

There is no trade signer, execution adapter, environment-key fallback or live
qualification in this module. An authenticated read is not a trading grant.
"""
import asyncio
import base64
import json
import time
from urllib.parse import urlencode

import aiohttp

from .. import models
from .base import AdapterError


ORIGINS = {
    ('lighter', 'mainnet'): 'https://mainnet.zklighter.elliot.ai',
    ('lighter', 'testnet'): 'https://testnet.zklighter.elliot.ai',
    ('lighter-rh', 'mainnet'): 'https://api.rh.lighter.xyz',
    ('backpack', 'mainnet'): 'https://api.backpack.exchange',
    ('katana', 'mainnet'): 'https://api-perps.katana.network',
    ('katana', 'testnet'): 'https://api-perps-sandbox.katana.network',
    ('bulk', 'mainnet'): 'https://mainnet-api1.bulk.trade',
    ('bulk', 'testnet'): 'https://exchange-api.bulk.trade',
    ('ondo', 'mainnet'): 'https://api.ondoperps.xyz',
    ('ondo', 'testnet'): 'https://api.ondoperps-sandbox.xyz',
    ('arcus', 'mainnet'): 'https://api.arcus.xyz',
    ('arcus', 'testnet'): 'https://api.testnet.arcus.xyz',
    ('perpl', 'mainnet'): 'https://app.perpl.xyz/api',
}
PATHS = {
    'lighter': {'/api/v1/orderBookDetails', '/api/v1/account'},
    'backpack': {'/api/v1/markets', '/api/v1/account', '/api/v1/position', '/api/v1/orders'},
    'katana': {'/v1/markets'}, 'bulk': {'/api/v1/exchangeInfo'},
    'ondo': {'/v1/markets'}, 'arcus': {'/v1/markets'}, 'perpl': {'/v1/pub/context'},
}
PATHS['lighter-rh'] = PATHS['lighter']


class ReadTransport:
    def __init__(self, session, venue, network):
        try:
            self.origin = ORIGINS[venue, network]
        except KeyError:
            raise AdapterError('GRID_OBSERVATION_DEPLOYMENT_UNSUPPORTED') from None
        self.session, self.venue = session, venue
        self.paths = frozenset(PATHS[venue])
        self.lock = asyncio.Lock()
        self.next_read = 0

    async def get(self, path, params=None, *, headers=None):
        if path not in self.paths:
            raise AdapterError('GRID_OBSERVATION_PATH_FORBIDDEN')
        async with self.lock:
            await asyncio.sleep(max(0, self.next_read - time.monotonic()))
            self.next_read = time.monotonic() + 1
            # Redirects must not receive credentials. Never include upstream
            # response bodies, headers, URLs or exception text in diagnostics.
            async with self.session.get(self.origin + path, params=params, headers=headers,
                    allow_redirects=False, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status != 200:
                    if response.status == 429:
                        self.next_read = time.monotonic() + 60
                    raise AdapterError('GRID_OBSERVATION_HTTP_FAILED')
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > 2_000_000:
                        raise AdapterError('GRID_OBSERVATION_TOO_LARGE')
                try:
                    return json.loads(raw, parse_float=str,
                        parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
                except (ValueError, UnicodeDecodeError):
                    raise AdapterError('GRID_OBSERVATION_RESPONSE_INVALID') from None


def market_row(venue, network, native, symbol):
    if not isinstance(symbol, str) or not symbol or '|' in symbol or '|' in str(native):
        raise AdapterError('GRID_OBSERVATION_MARKET_INVALID')
    from ..venue_registry import spec_for
    asset = spec_for(venue).settlement_asset
    market = models.MarketIdentity(f'{venue}:{network}', str(native), '', 'linear_perp', asset)
    return dict(market_ref=market.market_key, symbol=symbol, base_asset=symbol,
                asset=asset, venue=venue, network=network, live_eligible=False)


class LighterObservation:
    def __init__(self, transport_factory, venue='lighter'):
        self.transport_factory = transport_factory
        self.venue = venue

    async def markets(self, network):
        data = await self.transport_factory(network).get('/api/v1/orderBookDetails')
        if data.get('code') != 200 or not isinstance(data.get('order_book_details'), list):
            raise AdapterError('LIGHTER_MARKETS_INVALID')
        markets = []
        for row in data['order_book_details']:
            if row.get('status') != 'active' or row.get('market_type') != 'perp':
                continue
            mid = row['market_id']
            if type(mid) is not int or mid < 0:
                raise AdapterError('LIGHTER_MARKETS_INVALID')
            markets.append(market_row(self.venue, network, mid, row['symbol']))
        return markets

    async def check(self, record):
        network, credentials = record['network'], record['credentials']
        transport = self.transport_factory(network)
        data = await transport.get('/api/v1/account', {'by': 'index', 'value': credentials['account_index']})
        accounts = data.get('accounts')
        if data.get('code') != 200 or not isinstance(accounts, list) or len(accounts) != 1:
            raise AdapterError('LIGHTER_ACCOUNT_INVALID')
        account = accounts[0]
        if str(account['account_index']) != credentials['account_index']:
            raise AdapterError('LIGHTER_ACCOUNT_SCOPE_MISMATCH')
        positions = account['positions']
        if not isinstance(positions, list):
            raise AdapterError('LIGHTER_ACCOUNT_INVALID')
        available = models.parse_decimal_text(account['available_balance'], 'available_balance')
        markets = await self.markets(network)
        return {'checks': [dict(key='public_account', state='pass', message='已读取指定索引的公开资金账户'),
            dict(key='authorization', state='pending', message='公开账户读取不证明 API 私钥授权；签名与权限验证待完成'),
            dict(key='markets', state='pass', message=f'已读取 {len(markets)} 个永续市场')],
            'evidence': dict(asset='USDC', available_margin=models.canonical_decimal_text(available),
                market_count=len(markets), position_markets=[str(p['market_id']) for p in positions
                    if models.parse_decimal_text(p['position'], 'position') != 0],
                authorization_verified=False, identity_verified=False, source=f'{self.venue}:public-account')}


class BackpackObservation:
    def __init__(self, transport_factory, *, clock_ms=None):
        self.transport_factory = transport_factory
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def headers(self, credentials, instruction, params=None):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        seed = base64.b64decode(credentials['api_secret'], validate=True)
        signer = Ed25519PrivateKey.from_private_bytes(seed[:32])
        public = signer.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        if public != base64.b64decode(credentials['api_key'], validate=True) or (len(seed) == 64 and seed[32:] != public):
            raise AdapterError('BACKPACK_CREDENTIAL_IDENTITY_MISMATCH')
        timestamp, window = str(self.clock_ms()), '5000'
        values = [('instruction', instruction)] + sorted((params or {}).items())
        values += [('timestamp', timestamp), ('window', window)]
        signature = signer.sign(urlencode(values).encode())
        return {'X-API-Key': credentials['api_key'], 'X-Signature': base64.b64encode(signature).decode(),
                'X-Timestamp': timestamp, 'X-Window': window}

    async def markets(self, network):
        rows = await self.transport_factory(network).get('/api/v1/markets')
        if not isinstance(rows, list):
            raise AdapterError('BACKPACK_MARKETS_INVALID')
        return [market_row('backpack', network, r['symbol'], r['baseSymbol']) for r in rows
                if r.get('marketType') == 'PERP' and r.get('quoteSymbol') == 'USDC' and r.get('orderBookState') == 'Open']

    async def check(self, record):
        t, credentials = self.transport_factory(record['network']), record['credentials']
        state = await t.get('/api/v1/account', headers=self.headers(credentials, 'accountQuery'))
        if not isinstance(state, dict) or type(state.get('liquidating')) is not bool:
            raise AdapterError('BACKPACK_ACCOUNT_INVALID')
        # A successful signed read verifies the key pair and read permission,
        # but the response does not establish an economic account identifier.
        markets = await self.markets(record['network'])
        return {'checks': [dict(key='read_authorization', state='pass', message='API 密钥匹配，已通过交易所签名读取验证'),
            dict(key='liquidating', state='fail' if state['liquidating'] else 'pass', message='账户处于清算中' if state['liquidating'] else '账户未处于清算中'),
            dict(key='account_identity', state='pending', message='资金账户身份与共享保证金模型仍待认证'),
            dict(key='markets', state='pass', message=f'已读取 {len(markets)} 个 USDC 永续市场')],
            'evidence': dict(asset='USDC', available_margin=None, market_count=len(markets),
                identity_verified=False, read_authorization_verified=True, source='backpack:signed-account-read')}


class CatalogObservation:
    """Public protocol audit. A catalogue does not verify submitted keys."""
    def __init__(self, venue, transport_factory):
        self.venue, self.transport_factory = venue, transport_factory

    async def markets(self, network):
        path = next(iter(PATHS[self.venue]))
        data = await self.transport_factory(network).get(path)
        if self.venue == 'katana':
            rows = data['data'] if isinstance(data, dict) else data
            select = lambda r: r.get('status') == 'active'
            native = lambda r: r['market']
        elif self.venue == 'bulk':
            rows = data
            select = lambda r: r.get('status') == 'TRADING'
            native = lambda r: r['symbol']
        elif self.venue == 'ondo':
            rows = data['result']['perps']['tradingPairs']
            select = lambda r: r.get('tradingState') == 'TRADING' and not r.get('disabled', False)
            native = lambda r: r['market']
        elif self.venue == 'arcus':
            rows = data['markets']
            select = lambda r: r.get('status') == 'ONLINE' and r.get('type') == 'PERPETUAL'
            native = lambda r: r['marketDisplayName']
        elif self.venue == 'perpl':
            rows = data['markets']
            select = lambda r: r['config'].get('is_open') is True
            native = lambda r: r['id']
        else:
            raise AdapterError('GRID_CATALOG_UNSUPPORTED')
        if not isinstance(rows, list):
            raise AdapterError('GRID_CATALOG_RESPONSE_INVALID')
        result = []
        for r in rows:
            if not select(r):
                continue
            key = native(r)
            symbol = r['name'] if self.venue == 'perpl' else str(key).removesuffix('-USD.P').removesuffix('-USD')
            result.append(market_row(self.venue, network, key, symbol))
        return result

    async def check(self, record):
        rows = await self.markets(record['network'])
        return {'checks': [dict(key='public_markets', state='pass' if rows else 'fail',
                    message=f'公开接口已读取 {len(rows)} 个市场；不代表凭证有效'),
                dict(key='authorization', state='pending', message='此交易所的资金账户与签名权限检查尚未完成')],
            'evidence': dict(market_count=len(rows), available_margin=None, identity_verified=False,
                             authorization_verified=False, source=f'{self.venue}:public-catalog')}


class TradeXYZObservation:
    def __init__(self, session): self.session = session

    async def markets(self, network):
        if network != 'mainnet':
            raise AdapterError('GRID_OBSERVATION_DEPLOYMENT_UNSUPPORTED')
        from .hyperliquid import HyperliquidTransport
        data = await HyperliquidTransport(self.session, network).post('/info', {'type': 'meta', 'dex': 'xyz'})
        if data.get('collateralToken') != 0 or not isinstance(data.get('universe'), list):
            raise AdapterError('XYZ_COLLATERAL_SCOPE_UNVERIFIED')
        rows = []
        for row in data['universe']:
            symbol = row['name']
            if row.get('isDelisted') or not isinstance(symbol, str) or not symbol.startswith('xyz:'):
                continue
            m = models.MarketIdentity(network, symbol, 'xyz', 'linear_perp', 'USDC')
            rows.append(dict(market_ref=m.market_key, symbol=symbol, base_asset=symbol.split(':', 1)[1],
                asset='USDC', venue='tradexyz', network=network, live_eligible=False))
        return rows

    async def check(self, record):
        rows = await self.markets(record['network'])
        return {'checks': [dict(key='public_markets', state='pass' if rows else 'fail',
                    message=f'已读取 {len(rows)} 个 TradeXYZ USDC 市场；账户与 HL 共享资金风险域'),
                dict(key='authorization', state='pending', message='命名永续部署的账户授权、保证金与保护需独立验收')],
            'evidence': dict(market_count=len(rows), available_margin=None, authorization_verified=False,
                identity_verified=False, source='hl:xyz-public-meta')}


def observation_providers(session):
    transports = {}
    def factory(venue):
        def get(network):
            if (venue, network) not in transports:
                if venue == 'bulk':
                    from .bulk_market import BulkMarketTransport
                    transports[venue, network] = BulkMarketTransport(session, network)
                else:
                    transports[venue, network] = ReadTransport(session, venue, network)
            return transports[venue, network]
        return get
    from .bulk_read import BulkObservation
    from .ondo import OndoObservation
    from .arcus import ArcusObservation
    return {'lighter': LighterObservation(factory('lighter')),
            'lighter-rh': LighterObservation(factory('lighter-rh'), 'lighter-rh'),
            'backpack': BackpackObservation(factory('backpack')),
            'bulk': BulkObservation(factory('bulk')),
            'tradexyz': TradeXYZObservation(session),
            'ondo': OndoObservation(session), 'arcus': ArcusObservation(session),
            **{v: CatalogObservation(v, factory(v)) for v in ('katana', 'perpl')}}
