"""Native-format account reads keep portfolio validation on the WS fast path."""
import asyncio
import json
from types import SimpleNamespace

import aiohttp
import pytest

from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.hyperliquid import HyperliquidGridVenue, HyperliquidTransport
from entropy_arb.grid.hl_rate import shared_read_budget
from entropy_arb.grid.hl_ws_market import AccountInfoSocket
from test_grid_hl_unified import UnifiedTransport
from test_grid_hyperliquid import OWNER


class NativeSocket:
    def __init__(self, source, corrupt=None):
        self.source, self.corrupt = source, corrupt
        self.closed = False
        self.sent, self.queue = [], asyncio.Queue()

    async def send_json(self, request):
        self.sent.append(request)
        assert request['request']['type'] == 'info'
        payload = request['request']['payload']
        result = await self.source.post('/info', payload)
        response = {'channel': 'post', 'data': {'id': request['id'], 'response': {
            'type': 'info', 'payload': {'type': payload['type'], 'data': result}}}}
        if self.corrupt:
            self.corrupt(response)
        await self.queue.put(SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(response)))

    async def receive(self):
        return await self.queue.get()

    async def close(self):
        self.closed = True


class NativeSession:
    def __init__(self, socket):
        self.socket, self.connections, self.closed = socket, 0, False
        self.additional = []

    async def ws_connect(self, *args, **kwargs):
        self.connections += 1
        if self.connections == 1:
            return self.socket
        socket = NativeSocket(self.socket.source, self.socket.corrupt)
        self.additional.append(socket)
        return socket


def setup(source=None, corrupt=None):
    source = source or UnifiedTransport()
    ws = NativeSocket(source, corrupt)
    session = NativeSession(ws)
    transport = HyperliquidTransport(session)
    http = []

    async def post(path, payload):
        http.append(payload)
        return await source.post(path, payload)

    transport.post = post
    venue = HyperliquidGridVenue(transport, account_address=OWNER, symbol='BTC', clock_ms=lambda: 30000,
                                account_mode_confirmed=True)
    return venue, transport, source, ws, session, http


def test_full_unified_snapshot_is_fresh_and_charges_shared_weight_for_every_native_read():
    async def run():
        venue, transport, source, ws, session, http = setup()
        budget = shared_read_budget(transport.base_url)
        await budget.acquire(100)
        for total in ('199.79', '180.12'):
            source.balance['total'] = total
            async with transport.read_sequence(350):
                snapshot = await venue.account_snapshot()
            assert snapshot.assets == [('USDC', total)]
            assert snapshot.evidence['unified_scope']['dexes'] == ['', 'xyz']
        assert sum(w for _, w in budget.spent) == 352
        assert session.connections == 1 and not http
        kinds = [r['request']['payload']['type'] for r in ws.sent]
        assert kinds.count('spotClearinghouseState') == 2
        assert sum(r['request']['payload'].get('dex') == 'xyz' for r in ws.sent) == 4
        source.foreign_orders = [{'oid': 123}]
        async with transport.read_sequence(350):
            with pytest.raises(AdapterError, match='FOREIGN_DEX_EXPOSURE'):
                await venue.account_snapshot()
        await ws.close()
    asyncio.run(run())


def test_execution_rules_refresh_over_ws_charges_weight_without_reusing_old_fees():
    from entropy_arb.grid.hl_rate import critical_instrument_rules

    async def run():
        venue, transport, source, ws, session, http = setup()
        original = source.post
        fee = '0.00016'

        async def post(path, payload):
            result = await original(path, payload)
            return dict(result, userAddRate=fee) if payload['type'] == 'userFees' else result

        source.post = post
        budget = shared_read_budget(transport.base_url)
        await budget.acquire(800, critical=True)
        first = await critical_instrument_rules(venue, venue.market.market_key)
        fee = '0.00020'
        second = await critical_instrument_rules(venue, venue.market.market_key)
        assert first.maker_fee_rate == '0.00016' and second.maker_fee_rate == '0.0002'
        assert first.revision != second.revision and not http
        assert sum(w for _, w in budget.spent) == 880 and session.connections == 1
        assert [r['request']['payload']['type'] for r in ws.sent] == ['meta', 'userFees'] * 2
        await ws.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['foreign_position', 'other_asset', 'bad_hold', 'duplicate', 'mode_drift', 'wrong_coin'])
def test_ws_account_transport_cannot_bypass_existing_portfolio_rejections(failure):
    async def run():
        venue, transport, source, ws, _, http = setup()
        if failure == 'foreign_position':
            source.foreign_positions = [{'position': {'szi': '1'}}]
        if failure == 'other_asset':
            source.other = [{'coin': 'HYPE', 'token': 150, 'total': '1', 'hold': '0'}]
        if failure == 'bad_hold':
            source.balance['hold'] = '200'
        if failure == 'duplicate':
            source.other = [dict(source.balance)]
        if failure == 'mode_drift':
            source.drift = True
        if failure == 'wrong_coin':
            original = source.post

            async def post(path, payload):
                result = await original(path, payload)
                if payload['type'] == 'activeAssetData':
                    result['coin'] = 'ETH'
                return result
            source.post = post
        async with transport.read_sequence(350):
            with pytest.raises(AdapterError):
                await venue.account_snapshot()
        assert not http and not venue.write_enabled
        await ws.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind,fields', [
    ('order', {}), ('userRole', {'user': OWNER}),
    ('clearinghouseState', {'user': OWNER, 'signature': {}}),
    ('clearinghouseState', {'user': 'agent-name'}),
    ('spotClearinghouseState', {'user': OWNER, 'dex': 'xyz'}),
    ('perpDexs', {'user': OWNER}), ('activeAssetData', {'user': OWNER}),
    ('activeAssetData', {'user': OWNER, 'coin': 'ETH;bad'}),
    ('meta', {'user': OWNER}), ('userFees', {'user': OWNER, 'signature': {}}),
])
def test_unsigned_account_batch_validates_later_requests_before_any_io(kind, fields):
    async def run():
        _, transport, _, ws, session, http = setup()
        with pytest.raises(AdapterError, match='READ_ONLY'):
            await transport.account_infos([('perpDexs', {})] * 4 + [(kind, fields)])
        assert not ws.sent and session.connections == 0 and not http
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['id', 'action', 'rate_limit', 'http_cooldown'])
def test_account_protocol_or_rate_failures_never_fall_back(failure):
    def corrupt(response):
        if failure == 'id':
            response['data']['id'] += 10
        if failure == 'action':
            response['data']['response']['type'] = 'action'
        if failure == 'rate_limit':
            response['data']['response'] = {'type': 'error', 'payload': '429 Too Many Requests'}

    async def run():
        _, transport, _, ws, session, http = setup(corrupt=corrupt)
        budget = shared_read_budget(transport.base_url)
        if failure == 'http_cooldown':
            budget.rate_limited()
        with pytest.raises(AdapterError):
            await transport.account_info('perpDexs')
        assert not http
        if failure in ('rate_limit', 'http_cooldown'):
            assert budget.blocked_until > budget.clock()
        if failure == 'http_cooldown':
            assert session.connections == 0
        else:
            assert ws.closed
    asyncio.run(run())


def test_noncritical_page_reads_keep_original_http_path():
    async def run():
        venue, transport, _, _, session, http = setup()
        await venue.account_snapshot()
        assert http and session.connections == 0
        async with transport.public_read_sequence(350):
            await venue.account_snapshot()
        assert session.connections == 0
    asyncio.run(run())


def test_temporary_socket_failure_falls_back_to_original_scoped_http_reads(monkeypatch):
    from entropy_arb.grid import hl_ws_market

    async def run():
        _, transport, _, _, _, http = setup()
        socket = AccountInfoSocket(transport.session, transport.ws_url, transport.ssl_context)

        async def unavailable(requests):
            raise AdapterError('HL_WS_MARKET_UNAVAILABLE')

        socket.exchange = unavailable
        monkeypatch.setattr(hl_ws_market, 'shared_account_socket', lambda *args: socket)
        requests = [('clearinghouseState', {'user': OWNER, 'dex': 'xyz'}), ('perpDexs', {})]
        result = await transport.account_infos(requests)
        assert len(result) == 2
        assert http == [{'type': kind, **fields} for kind, fields in requests]
    asyncio.run(run())


@pytest.mark.parametrize('failure', [None, 'position', 'foreign_order'])
def test_real_risk_and_executor_pipeline_still_checks_fresh_ws_account_before_signing(tmp_path, monkeypatch, failure):
    from decimal import Decimal
    from entropy_arb.grid.runtime import GridRuntime
    from test_grid_live_risk import running, refill, orders_sent
    from test_grid_hyperliquid import raw_order

    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')

    async def run():
        rt = GridRuntime.open(str(tmp_path / 'offline-native.db'), execution_mode='production')
        try:
            gid, cfg, venue, source, signer, qty = await running(rt)
            # Pin native response production separately from HTTP so this
            # test can prove runtime risk previews use both WS read paths.
            provider = SimpleNamespace(post=source.post)
            http = []

            async def post(path, payload):
                http.append(payload.get('type'))
                return await provider.post(path, payload)

            source.post = post
            ws = NativeSocket(provider)
            session = NativeSession(ws)
            native = HyperliquidTransport(session)
            # Only replace the read path; fixture signing/dispatch and all
            # real scheduler, ownership and final permit checks remain.
            source.account_read_active = lambda: True
            source.account_infos = native.account_infos
            source.account_info = native.account_info
            source.market_info = native.market_info
            preview_http = []
            previews = []
            original_preview = rt._preview

            async def preview(*args, **kwargs):
                start = len(http)
                try:
                    return await original_preview(*args, **kwargs)
                finally:
                    if kwargs.get('purpose') == 'dispatch-risk':
                        previews.append(True)
                        preview_http.extend(http[start:])

            rt._preview = preview
            if failure == 'position':
                source.qty = str(Decimal(qty) + Decimal('.00001'))
            if failure == 'foreign_order':
                source.order_rows.append(raw_order(cloid='0x' + 'ef' * 16, oid=881100))
            before = orders_sent(source)
            intent = await refill(rt, gid, qty)
            result = await rt.executors[cfg.account_ref].submit_intent(intent['intent_id'], epoch=intent['epoch'])
            assert ws.sent and all(r['request']['type'] == 'info' for r in ws.sent)
            if failure is None:
                assert result['outcome'] == 'ACCEPTED' and orders_sent(source) == before + 1
            else:
                assert result['outcome'] == 'BLOCKED' and orders_sent(source) == before
                assert rt.lifecycle._grid(gid)['desired_mode'] == 'PAUSED'
            assert not rt.live_risk.permits
            assert previews and not {'meta', 'userFees', 'metaAndAssetCtxs', 'l2Book'} & set(preview_http)
            assert not {'meta', 'userFees'} & set(http)
            await ws.close()
            for socket in session.additional:
                await socket.close()
        finally:
            rt.close()
    asyncio.run(run())
