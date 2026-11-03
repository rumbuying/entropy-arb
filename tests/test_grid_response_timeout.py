import asyncio
import tempfile

import pytest

from entropy_arb.console.grid import GridProxy
from entropy_arb.grid.rpc import RpcError, RuntimeClient, RuntimeRpcServer
from entropy_arb.grid.runtime import GridRuntime


def test_response_timeout_does_not_claim_runtime_unreachable():
    class Client:
        async def call(self, *args, **kwargs): raise TimeoutError()
        async def call_idempotent(self, *args, **kwargs): raise TimeoutError()
    async def run():
        proxy = GridProxy('/unit')
        async def get(): return Client()
        proxy._get = get
        with pytest.raises(RpcError) as error:
            await proxy.call('accounts_commission_probe', {})
        assert error.value.code == 'RUNTIME_RESPONSE_TIMEOUT'
        with pytest.raises(RpcError) as error:
            await proxy.call_op('stop_flat', {}, 'same-id')
        assert error.value.code == 'RUNTIME_RESPONSE_TIMEOUT'
    asyncio.run(run())


def test_disconnected_rpc_client_does_not_repeat_or_discard_background_result(tmp_path):
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'sim.db'), execution_mode='simulation')
        directory = tempfile.TemporaryDirectory(prefix='gpr-', dir='/tmp')
        socket_path = directory.name + '/s'
        server = RuntimeRpcServer(rt, socket_path)
        released, completed = asyncio.Event(), asyncio.Event()
        calls, unhandled = [], []
        async def handler(params):
            calls.append(params)
            await released.wait()
            await rt.store.write(lambda c: c.execute("INSERT INTO runtime_state VALUES('unit:durable-result','complete')"))
            completed.set()
            return {'state': 'CONFIRMED'}
        original_methods = server._methods
        server._methods = lambda: {**original_methods(), 'unit_delayed': handler}
        loop = asyncio.get_running_loop();previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda loop, context: unhandled.append(context))
        await server.start()
        client = RuntimeClient(socket_path)
        try:
            await client.connect()
            with pytest.raises(TimeoutError): await client.call('unit_delayed', {}, timeout=.01)
            released.set();await completed.wait()
            await asyncio.sleep(.01)
            assert len(calls) == 1 and not unhandled
            assert rt.store.read(lambda c: c.execute("SELECT value FROM runtime_state WHERE key='unit:durable-result'").fetchone()[0]) == 'complete'
        finally:
            loop.set_exception_handler(previous)
            await client.close();await server.stop();rt.close();directory.cleanup()
    asyncio.run(run())


def test_waiting_preview_uses_an_independent_connection_and_leaves_status_responsive(tmp_path):
    async def run():
        rt=GridRuntime.open(str(tmp_path/'isolated-sim.db'),execution_mode='simulation')
        directory=tempfile.TemporaryDirectory(prefix='gpr-',dir='/tmp');socket=directory.name+'/s'
        server=RuntimeRpcServer(rt,socket);entered,released=asyncio.Event(),asyncio.Event()
        async def delayed(params):
            entered.set();await released.wait();return {'observed':True}
        original=server._methods;server._methods=lambda:{**original(),'preview':delayed}
        await server.start();proxy=GridProxy(socket);task=None
        try:
            task=asyncio.create_task(proxy.call('preview',{}))
            await asyncio.wait_for(entered.wait(),1)
            status=await asyncio.wait_for(proxy.call('status',{}),.2)
            assert status['healthy'] and not task.done()
            released.set();assert await task=={'observed':True}
        finally:
            released.set()
            if task:await task
            if proxy._client:await proxy._client.close()
            await server.stop();rt.close();directory.cleanup()
    asyncio.run(run())
