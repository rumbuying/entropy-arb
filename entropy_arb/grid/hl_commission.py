"""Bounded native passive-order commissioning, separate from grid eligibility.

The startup allowance authorizes an aggregate MARK notional, not margin or
wallet withdrawals. It never changes a venue's write_enabled/live_qualified.
Every entry is journaled before a single send. Replays only reconcile/cancel.
"""
import asyncio
import json
import re
from contextlib import AsyncExitStack
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from . import models
from .adapters.base import AdapterError, OrderRequest
from .credentials import CredentialError

PREARMED_KINDS = ('position_protected_ioc', 'fixed_prearmed_ioc')


class PassiveCommissioning:
    def __init__(self, runtime, *, allowance='0'):
        self.runtime=runtime
        self.allowance=models.parse_decimal_text(allowance,'commissioning_allowance')
        if self.allowance<0:raise ValueError('commissioning allowance must be nonnegative')

    async def write(self,key,record):
        await self.runtime.store.write(lambda c:c.execute(
            'INSERT INTO runtime_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (key,json.dumps(record,sort_keys=True))))

    def records(self):
        return self.runtime.store.read(lambda c:[(r[0],json.loads(r[1])) for r in c.execute(
            "SELECT key,value FROM runtime_state WHERE key LIKE 'passive-commission:%'")])

    def fresh_quote(self,venue,quote):
        stamp=int(models.parse_iso_ts(quote.ts,'quote_time').timestamp()*1000)
        if not -1000<=venue.clock_ms()-stamp<=5000:
            raise AdapterError('HL_COMMISSION_QUOTE_STALE')

    def diagnostic(self,error):
        result={'type':type(error).__name__}
        if isinstance(error,(AdapterError,CredentialError)) and re.fullmatch(r'[A-Z0-9_:.-]{1,100}',str(error)):
            result['code']=str(error)
        return result

    def same_clean(self, left, right):
        def economic(record):
            return {**record, 'scope': {key: value for key, value in record['scope'].items() if key != 'observation'}}
        return economic(left) == economic(right)

    def only_owned_stop(self,record,orders):
        protection=record.get('protection')
        if not orders:return True
        if not protection or len(orders)!=1:return False
        order=orders[0]
        full_position=protection.get('mode')=='full_position'
        expected_qty=Decimal(0) if full_position else Decimal(record['qty'])
        valid=(order.get('coin')=='BTC' and order.get('cloid')==protection['client_id']
               and order.get('reduceOnly') is True and order.get('isTrigger') is True
               and order.get('side')=='A' and order.get('orderType')=='Stop Market' and Decimal(order.get('triggerPx','-1'))==Decimal(protection['trigger'])
               and Decimal(order.get('origSz','-1'))==expected_qty
               and Decimal(order.get('sz','-1'))==expected_qty
               and (not full_position or order.get('isPositionTpsl') is True)
               and (record.get('kind') != 'fixed_prearmed_ioc' or order.get('isPositionTpsl') is False)
               and Decimal(order.get('limitPx','-1'))==Decimal(protection['price']))
        if valid:
            protection['oid']=str(order['oid'])
            record['protection_verified']=True
        return valid

    async def cancel_stop(self,view,key,record):
        protection=record.get('protection')
        if not protection:return True
        if protection.get('mode')=='full_position':
            from .hl_position_probe import PositionStopProbe
            stop_record={'client_id':protection['client_id'],'side':'A','trigger':protection['trigger'],
                         'price':protection['price']}
            if protection.get('oid'):stop_record['oid']=protection['oid']
            try:
                probe=PositionStopProbe(self)
                proof=await probe.status(view,stop_record)
                if proof and proof['status']=='open':
                    await self.control(view,{'type':'cancelByCloid','cancels':[{'asset':view.asset_id,
                        'cloid':protection['client_id']}]},record,key,'protection_cancel')
                    for _ in range(6):
                        proof=await probe.status(view,stop_record)
                        if proof and proof['status']!='open':break
                        await asyncio.sleep(.25)
                from .adapters.hyperliquid import CANCELED_STATUSES
                if proof and (proof['status'] in CANCELED_STATUSES or proof['status']=='filled'):
                    protection.update(oid=proof['oid'],terminal_state='FILLED' if proof['status']=='filled' else 'CANCELED')
                    await self.write(key,record)
                    return True
            except Exception:pass
            return False
        try:
            proof=await view.lookup(protection['client_id'],protection.get('oid'))
            if proof.state=='FOUND_OPEN':
                await self.control(view,{'type':'cancelByCloid','cancels':[{'asset':view.asset_id,
                    'cloid':protection['client_id']}]},record,key,'protection_cancel')
                proof=await view.lookup(protection['client_id'],protection.get('oid'))
            if proof.state=='FOUND_TERMINAL':
                protection.update(oid=proof.exchange_order_id,terminal_state=proof.terminal_state)
                await self.write(key,record)
                return True
        except Exception:pass
        return False

    async def authorized(self,row,venue):
        if (venue.owner!=row['account_address'] or not venue.signer
                or venue.signer.address!=row['signer_address'] or venue.signer.vault_address):
            raise CredentialError('COMMISSION_SCOPE_UNVERIFIED','需要已核对的主账户代理签名',422)
        auth=await venue.authorization_check()
        if auth.owner!=venue.owner or auth.master!=venue.owner or auth.vault_address:
            raise CredentialError('COMMISSION_SCOPE_UNVERIFIED','验收只支持主账户自有授权',422)

    async def clean(self,root,*,owned_stop=None):
        snap=await root.account_snapshot()
        evidence=snap.evidence or {}
        if any(Decimal(p['szi'])!=0 for p in evidence['positions']):
            raise CredentialError('COMMISSION_ACCOUNT_NOT_CLEAN','验收要求账户无仓位',422)
        orders=evidence.get('order_rows')
        if not isinstance(orders,list):
            raise AdapterError('HL_COMMISSION_ORDERS_UNVERIFIED')
        if orders and (owned_stop is None or not self.only_owned_stop(owned_stop,orders)):
            raise CredentialError('COMMISSION_ACCOUNT_NOT_CLEAN','验收要求账户无挂单或只有已核对的验收保护',422)
        free=next((Decimal(v) for a,v in snap.assets if a=='USDC'),Decimal(0))
        if free<50:raise CredentialError('COMMISSION_CAPITAL_INSUFFICIENT','验收要求至少 50 USDC 可用保证金',422)
        if evidence.get('abstraction')!='unifiedAccount' or not evidence.get('unified_scope'):
            raise CredentialError('COMMISSION_MODE_UNVERIFIED','此验收仅支持已核对的 USDC 统一主永续账户',422)
        return {'free':models.canonical_decimal_text(free),'mode':evidence['abstraction'],'scope':evidence['unified_scope']}

    async def control(self,venue,action,record,key,stage,*,fence=None):
        """Persist dispatch intent before HTTP. Never retry an uncertain send."""
        if fence is not None and hasattr(venue.transport, 'prepare_read_capacity'):
            await venue.transport.prepare_read_capacity()
        expiry=venue.clock_ms()+10000
        signed=await venue.signer.sign(action,expiry)
        if fence is not None and not await fence():
            record[stage]={'state':'NOT_SENT','code':'COMMISSION_EXIT_OBSERVATION_CHANGED'}
            await self.write(key,record)
            return None
        record[stage]={'state':'DISPATCHING','nonce':signed['nonce'],'expires_after':expiry}
        await self.write(key,record)
        if venue.clock_ms()>=expiry:
            record[stage]['state']='NOT_SENT'
            await self.write(key,record)
            return None
        try:
            response=await venue.transport.post('/exchange',signed)
            record[stage]['state']='ACK' if response.get('status')=='ok' else 'REJECTED' if response.get('status')=='err' else 'UNKNOWN'
            if response.get('status')=='err' and isinstance(response.get('response'),str):
                record[stage]['reason']=response['response'][:160]
            await self.write(key,record)
            return response
        except Exception:
            record[stage]['state']='UNKNOWN'
            await self.write(key,record)
            return None

    async def reconcile(self,row,root,key,record):
        try:
            return await self._reconcile(row,root,key,record)
        except Exception as error:
            record.update(state='UNKNOWN',code='COMMISSION_RECONCILIATION_UNAVAILABLE',diagnostic=self.diagnostic(error))
            await self.write(key,record)
            return record

    async def _reconcile(self,row,root,key,record):
        """Cancel only the journaled CLOID; independently prove terminal/flat."""
        if record.get('kind') in ('position_stop_probe', 'position_pair_probe', 'fixed_stop_probe'):
            from .hl_position_probe import PositionStopProbe
            return await PositionStopProbe(self).reconcile(row,root,key,record)
        await self.authorized(row,root)
        view=root.market_venue(record['market'])
        await view.load_instrument(record["market"])
        if record['state']=='PREPARED' or (record.get('kind') in PREARMED_KINDS
                and (not record.get('entry_dispatched') or record.get('entry_rejected'))):
            if record.get('kind') in PREARMED_KINDS:
                # A stop may already be live while the entry remains known
                # unsent. Cancel only that stop after independently proving
                # no position; never turn a restart into an entry resend.
                await self.clean(root,owned_stop=record)
                if (record.get('position_stop_dispatch',{}).get('state') not in (None,'NOT_SENT')
                        and not record.get('position_stop_rejected')):
                    if not await self.cancel_stop(view,key,record):
                        record.update(state='UNKNOWN',code='COMMISSION_PREARMED_STOP_UNRESOLVED')
                        await self.write(key,record)
                        return record
                await self.clean(root)
            # Entry cannot be transmitted until the outer state is durably
            # DISPATCHING. A PREPARED crash may only have armed the deadman.
            if record.get('deadman'):
                await self.control(view,{'type':'scheduleCancel'},record,key,'deadman_clear')
            record.update(state='REJECTED' if record.get('entry_rejected') else 'NOT_SENT',
                          code='COMMISSION_ENTRY_REJECTED' if record.get('entry_rejected') else 'COMMISSION_PREPARED_RECOVERY')
            await self.write(key,record)
            return record
        try:proof=await view.lookup(record['client_id'],record.get('oid'))
        except Exception:proof=None
        if proof is None or proof.state not in ('FOUND_OPEN','FOUND_TERMINAL'):
            record.update(state='UNKNOWN',code='COMMISSION_ORDER_UNRESOLVED')
            await self.write(key,record)
            return record
        record['oid']=proof.exchange_order_id
        if proof.state=='FOUND_OPEN':
            await self.control(view,{'type':'cancelByCloid','cancels':[{'asset':view.asset_id,'cloid':record['client_id']}]},record,key,'cancel')
            # One cancellation ACK is not terminal proof. Native orderStatus
            # plus the full account snapshot must both agree before closure.
            for _ in range(6):
                proof=await view.lookup(record['client_id'],record['oid'])
                if proof.state=='FOUND_TERMINAL':break
                await asyncio.sleep(.25)
        record['order_proof']={'state':proof.state,'terminal_state':proof.terminal_state,
                               'cumulative_qty':proof.cumulative_qty}
        snap=await root.account_snapshot()
        orders=await root._info('frontendOpenOrders',user=root.owner)
        positions=[p for p in snap.evidence['positions'] if Decimal(p['szi'])!=0]
        if (record.get('kind') in PREARMED_KINDS and positions and len(positions)==1
                and positions[0]['coin']=='BTC' and Decimal(positions[0]['szi'])>0
                and orders and self.only_owned_stop(record,orders)):
            record['fixed_stop_survived_entry' if record['kind']=='fixed_prearmed_ioc'
                   else 'position_stop_survived_entry']=True
            record['protected_position_observation']={
                'qty':positions[0]['szi'],'stop_oid':record['protection']['oid'],
                'origSz':orders[0]['origSz'],'sz':orders[0]['sz'],
                'isPositionTpsl':orders[0].get('isPositionTpsl'), 'observed_ms':view.clock_ms()}
            await self.write(key,record)
        if (proof.state=='FOUND_TERMINAL' and not orders and not positions and Decimal(proof.cumulative_qty)==0
                and not record.get('protection')):
            record.update(state='CONFIRMED',completed_ts=models.utc_now_iso(),flat=True)
            record.pop('code',None);record.pop('diagnostic',None)
            # Clear the dead man's switch only after terminal/flat proof.
            if record.get('deadman',{}).get('state')=='ACK' and not record.get('deadman_clear'):
                await self.control(view,{'type':'scheduleCancel'},record,key,'deadman_clear')
            await self.write(key,record)
            return record
        if (positions or record.get('exits') or record.get('protection')) and proof.state=='FOUND_TERMINAL' and self.only_owned_stop(record,orders):
            return await self.exit_owned(row,root,view,key,record)
        if positions:
            record.update(state='FILLED_REQUIRES_EXIT',flat=False,code='COMMISSION_FILL_REQUIRES_OWNERSHIP')
        else:record.update(state='UNKNOWN',code='COMMISSION_TERMINAL_UNVERIFIED')
        await self.write(key,record)
        return record

    async def exit_owned(self,row,root,view,key,record):
        """Close only inventory independently proved from this probe's fills.

        A reduce-only request may exceed a dust position to meet the native
        notional minimum. Native r=true is mandatory; resulting execution
        must still be at most the proved position. This is a commissioning
        experiment, not permission for the production dust path.
        """
        record.update(state='FILLED_REQUIRES_EXIT',flat=False)
        await self.write(key,record)
        for attempt in range(3):
            snap=await root.account_snapshot()
            positions=[p for p in snap.evidence['positions'] if Decimal(p['szi'])!=0]
            orders=await root._info('frontendOpenOrders',user=root.owner)
            if not self.only_owned_stop(record,orders):
                record['code']='COMMISSION_FOREIGN_ORDER';break
            exits=record.setdefault('exits',[])
            if exits and exits[-1]['state']=='PREPARED' and 'exit_dispatch_'+str(len(exits)) not in record:
                # No HTTP is possible before its durable stage DISPATCHING.
                # A pre-sign or fence exception leaves this exit known unsent.
                exits[-1]['state']='NOT_SENT'
                await self.write(key,record)
            if exits and exits[-1]['state'] not in ('TERMINAL','REJECTED','NOT_SENT'):
                close=exits[-1]
                proof=await view.lookup(close['client_id'],close.get('oid'))
                if proof.state!='FOUND_TERMINAL':
                    record['code']='COMMISSION_EXIT_UNRESOLVED';break
                close.update(state='TERMINAL',oid=proof.exchange_order_id,terminal_state=proof.terminal_state)
                await self.write(key,record)
            fills=await root._info('userFillsByTime',user=root.owner,startTime=record['born_ms']-2000,
                                   endTime=view.clock_ms(),aggregateByTime=False)
            if not isinstance(fills,list) or len(fills)>=2000:
                record['code']='COMMISSION_HISTORY_UNVERIFIED';break
            owned_oids={str(record['oid']):'B'}
            if record.get('protection',{}).get('oid'):
                owned_oids[record['protection']['oid']]='A'
            for close in exits:
                if close.get('oid'):owned_oids[str(close['oid'])]='A'
            net=Decimal(0);entry=Decimal(0);valid=True
            for f in fills:
                oid=str(f.get('oid'))
                if f.get('coin')!=view.symbol or oid not in owned_oids or f.get('side')!=owned_oids[oid] or f.get('feeToken')!='USDC':
                    valid=False;break
                qty=models.parse_decimal_text(f['sz'],'execution_qty')
                if qty<=0:valid=False;break
                net+=qty if f['side']=='B' else -qty
                if f['side']=='B':entry+=qty
            actual=Decimal(positions[0]['szi']) if len(positions)==1 and positions[0]['coin']==view.symbol else Decimal(0)
            if (not valid or entry<=0 or entry>Decimal(record['qty']) or net<0
                    or positions and (len(positions)!=1 or positions[0]['coin']!=view.symbol or actual!=net)
                    or not positions and net!=0):
                record['code']='COMMISSION_INVENTORY_UNPROVED';break
            record['fills']=fills
            if not positions:
                if record.get('kind') == 'fixed_prearmed_ioc' and 'flat_stop_observation' not in record:
                    from .hl_position_probe import PositionStopProbe
                    protection = record['protection']
                    stop_record = {'kind':'fixed_stop_probe','client_id':protection['client_id'],
                        'side':'A','trigger':protection['trigger'],'price':protection['price'],
                        'wire':protection['wire'],'oid':protection.get('oid')}
                    native = await PositionStopProbe(self).status(root, stop_record)
                    record['flat_stop_observation'] = {'order_proof':native, 'observed_ms':view.clock_ms(),
                        'ledger_net':'0','positions':[], 'orders':orders}
                    record['fixed_stop_survived_flat'] = bool(native and native['status']=='open'
                        and len(orders)==1 and self.only_owned_stop(record,orders))
                    await self.write(key,record)
                if not await self.cancel_stop(view,key,record):
                    record['code']='COMMISSION_PROTECTION_TERMINAL_UNVERIFIED';break
                final=await root.account_snapshot()
                remaining=await root._info('frontendOpenOrders',user=root.owner)
                if remaining or any(Decimal(p['szi'])!=0 for p in final.evidence['positions']):
                    record['code']='COMMISSION_FINAL_FLAT_UNVERIFIED';break
                record.update(state='CONFIRMED',flat=True,completed_ts=models.utc_now_iso(),filled_roundtrip=True)
                record.pop('code',None);record.pop('diagnostic',None)
                if record.get('deadman',{}).get('state')=='ACK':
                    await self.control(view,{'type':'scheduleCancel'},record,key,'deadman_clear')
                await self.write(key,record);return record
            if exits and exits[-1]['state'] not in ('TERMINAL','REJECTED','NOT_SENT'):
                close=exits[-1]
                proof=await view.lookup(close['client_id'],close.get('oid'))
                if proof.state!='FOUND_TERMINAL':
                    record['code']='COMMISSION_EXIT_UNRESOLVED';break
                close.update(state='TERMINAL',oid=proof.exchange_order_id,terminal_state=proof.terminal_state)
                await self.write(key,record)
                # Re-read fills after the terminal OID becomes known. An ACK
                # never supplies a synthetic execution or clears inventory.
                continue
            quote=await view.market_snapshot()
            self.fresh_quote(view,quote)
            if not quote.bid or not quote.ask:
                record['code']='COMMISSION_EXIT_QUOTE_UNVERIFIED';break
            price=(Decimal(quote.bid)*Decimal('.99')).to_integral_value(rounding=ROUND_FLOOR)
            step=Decimal(view.rules.qty_step)
            qty=max(actual,(Decimal(view.rules.min_notional)/price/step).to_integral_value(rounding=ROUND_CEILING)*step)
            if price<=0 or qty>Decimal(record['qty'])*2 or qty*price>30:
                record['code']='COMMISSION_EXIT_BOUND_EXCEEDED';break
            cid='0x'+models.sha256_hex([record['client_id'],'reduce-only-exit',len(exits)])[:32]
            request=OrderRequest('commission-exit',cid,row['account_id'],record['market'],'sell',
                models.canonical_decimal_text(qty),models.canonical_decimal_text(price),'reduce_only_ioc',True,0)
            wire=view._wire(request)
            await self.authorized(row,root)
            close={'client_id':cid,'qty':request.qty_base,'price':request.price,'state':'PREPARED'}
            exits.append(close);await self.write(key,record)
            async def unchanged_inventory():
                await self.authorized(row,root)
                check=await root.account_snapshot()
                native=[p for p in check.evidence['positions'] if Decimal(p['szi'])!=0]
                working=await root._info('frontendOpenOrders',user=root.owner)
                return (self.only_owned_stop(record,working) and len(native)==1 and native[0]['coin']==view.symbol
                        and Decimal(native[0]['szi'])==actual)
            response=await self.control(view,{'type':'order','orders':[wire],'grouping':'na'},record,key,
                                        'exit_dispatch_'+str(len(exits)),fence=unchanged_inventory)
            close['state']='UNKNOWN'
            if response and response.get('status')=='ok' and response.get('response',{}).get('type')=='order':
                statuses=response['response']['data']['statuses']
                if len(statuses)==1 and isinstance(statuses[0],dict):
                    status=statuses[0]
                    if 'error' in status:close.update(state='REJECTED',reason=status['error'][:160])
                    elif 'filled' in status:close['oid']=str(status['filled']['oid'])
            await self.write(key,record)
            if close['state']=='REJECTED':
                record['code']='COMMISSION_EXIT_REJECTED';break
            proof=await view.lookup(cid,close.get('oid'))
            if proof.state!='FOUND_TERMINAL':
                record['code']='COMMISSION_EXIT_UNRESOLVED';break
            close.update(state='TERMINAL',oid=proof.exchange_order_id,terminal_state=proof.terminal_state)
            await self.write(key,record)
            await asyncio.sleep(.25)
        await self.write(key,record)
        return record

    async def probe(self,row,root,params):
        if params.get('kind')=='position_pair_probe':
            from .hl_position_probe import PositionStopProbe
            return await PositionStopProbe(self).pair_probe(row,root,params)
        if params.get('kind') in ('position_stop_probe', 'fixed_stop_probe'):
            from .hl_position_probe import PositionStopProbe
            return await PositionStopProbe(self).probe(row,root,params)
        if params.get('kind') in ('protected_ioc', *PREARMED_KINDS):
            return await self.protected_roundtrip(row,root,params)
        if params.get('kind') not in (None,'passive'):
            raise CredentialError('COMMISSION_KIND_INVALID','不支持此验收类型',400)
        if self.runtime.execution_mode!='production':
            raise CredentialError('COMMISSION_DISABLED','服务未启用限额实盘验收',403)
        rid=params.get('request_id')
        if not isinstance(rid,str) or not 1<=len(rid)<=128:
            raise CredentialError('REQUEST_ID_REQUIRED','缺少有效请求编号',400)
        context={'account':row['account_id'],'credential_revision':row['revision'],'request_id':rid,
                 'market':root.deployment+'||BTC|linear_perp|USDC'}
        digest=models.sha256_hex(context);key='passive-commission:'+models.sha256_hex([row['account_id'],rid])
        prior=dict(self.records())
        if key in prior:
            record=prior[key]
            if record['request_hash']!=digest:raise CredentialError('IDEMPOTENCY_CONFLICT','验收编号已用于其他凭证')
            if record['state'] in ('PREPARED','DISPATCHING','UNKNOWN','FILLED_REQUIRES_EXIT'):
                return {**await self.reconcile(row,root,key,record),'replayed':True}
            return {**record,'replayed':True}
        if self.allowance==0:
            raise CredentialError('COMMISSION_DISABLED','服务未启用限额实盘验收',403)
        if any(r['state'] in ('PREPARED','DISPATCHING','UNKNOWN','FILLED_REQUIRES_EXIT') for r in prior.values()):
            raise CredentialError('COMMISSION_UNRESOLVED','上一笔验收尚未终结，不能再开单',422)
        controls=self.runtime.store.read(lambda c:[json.loads(r[0]) for r in c.execute(
            "SELECT value FROM runtime_state WHERE key LIKE 'self-funding:%'")])
        if any(r['state'] in ('PREPARED','DISPATCHING','UNKNOWN') for r in controls):
            raise CredentialError('COMMISSION_UNRESOLVED','先核对上一笔资金操作',422)
        await self.authorized(row,root)
        baseline=await self.clean(root)
        view=root.market_venue(context['market']);rules=await view.load_instrument(context["market"]);quote=await view.market_snapshot()
        self.fresh_quote(view,quote)
        if not quote.bid or not quote.ask or Decimal(quote.bid)<=0 or Decimal(quote.ask)<Decimal(quote.bid):
            raise AdapterError('HL_COMMISSION_QUOTE_UNVERIFIED')
        # Integer prices obey native significant-figure rules. A distant ALO
        # is bounded by both its limit notional and its current mark notional.
        price=(Decimal(quote.bid)*Decimal('.9')).to_integral_value(rounding=ROUND_FLOOR)
        step=Decimal(rules.qty_step)
        qty=(Decimal(rules.min_notional)/price/step).to_integral_value(rounding=ROUND_CEILING)*step
        notional=qty*max(price,Decimal(quote.mark),Decimal(quote.ask))
        consumed=sum((Decimal(r['notional']) for r in prior.values() if r['state']!='NOT_SENT'),Decimal(0))
        if notional>15 or consumed+notional>self.allowance:
            raise CredentialError('COMMISSION_ALLOWANCE_EXCEEDED','验收名义金额超过单笔或累计上限',422)
        cloid='0x'+models.sha256_hex(['passive-commission-v1',context])[:32]
        request=OrderRequest('commission:'+rid,cloid,row['account_id'],context['market'],'buy',
            models.canonical_decimal_text(qty),models.canonical_decimal_text(price),'post_only',False,0)
        wire=view._wire(request)
        record={**context,'request_hash':digest,'client_id':cloid,'qty':request.qty_base,'price':request.price,
                'notional':models.canonical_decimal_text(notional),'born_ms':view.clock_ms(),
                'state':'PREPARED','before':baseline,'wire':wire}
        await self.write(key,record)
        # Dead man's switch is account-wide, so it is used ONLY for this clean,
        # isolated commissioning account with no other orders or grid claims.
        dms=await self.control(view,{'type':'scheduleCancel','time':view.clock_ms()+30000},record,key,'deadman')
        if not dms or dms.get('status')!='ok' or dms.get('response',{}).get('type')!='default':
            record.update(state='NOT_SENT',code='COMMISSION_DEADMAN_UNVERIFIED');await self.write(key,record);return record
        expiry=view.clock_ms()+10000
        try:
            signed=await view.signer.sign({'type':'order','orders':[wire],'grouping':'na'},expiry)
            await self.authorized(row,root)
            current=await self.clean(root)
            fresh=await view.market_snapshot()
            self.fresh_quote(view,fresh)
            if (not self.same_clean(current,baseline) or view.clock_ms()>=expiry or Decimal(fresh.bid)<price*Decimal('1.05')
                    or qty*max(Decimal(fresh.mark),Decimal(fresh.ask))>15
                    or consumed+qty*max(Decimal(fresh.mark),Decimal(fresh.ask))>self.allowance):
                raise AdapterError('HL_COMMISSION_OBSERVATION_CHANGED')
        except Exception:
            record.update(state='NOT_SENT',code='COMMISSION_OBSERVATION_CHANGED')
            await self.control(view,{'type':'scheduleCancel'},record,key,'deadman_clear')
            await self.write(key,record);return record
        record.update(state='DISPATCHING',nonce=signed['nonce'],expires_after=expiry)
        await self.write(key,record)
        try:
            response=await view.transport.post('/exchange',signed)
            if response.get('status')=='err':
                record.update(state='REJECTED',code='COMMISSION_VENUE_REJECTED',reason=str(response.get('response'))[:160])
            elif response.get('status')=='ok' and response.get('response',{}).get('type')=='order':
                statuses=response['response']['data']['statuses']
                if len(statuses)==1 and isinstance(statuses[0],dict):
                    status=statuses[0]
                    if 'error' in status:record.update(state='REJECTED',code='COMMISSION_VENUE_REJECTED',reason=status['error'][:160])
                    elif 'resting' in status:record.update(ack='resting',oid=str(status['resting']['oid']))
                    elif 'filled' in status:record.update(ack='filled',oid=str(status['filled']['oid']))
        except Exception:pass
        await self.write(key,record)
        if record['state']=='REJECTED':
            # Explicit native rejection means there is no entry to retry.
            await self.control(view,{'type':'scheduleCancel'},record,key,'deadman_clear')
            return record
        try:
            return await self.reconcile(row,root,key,record)
        except Exception as error:
            record.update(state='UNKNOWN',code='COMMISSION_RECONCILIATION_UNAVAILABLE',diagnostic=self.diagnostic(error))
            await self.write(key,record)
            return record

    async def prepare_roundtrip_entry(self, row, root, view, record, *, wire, stop,
                                     prearmed, baseline, price, trigger, qty, consumed):
        # Reserve the full read fence BEFORE the signed request lifetime starts.
        # A capacity hint alone permits competing observations to use that room.
        async with AsyncExitStack() as stack:
            if hasattr(view.transport, 'read_sequence'):
                await stack.enter_async_context(view.transport.read_sequence(500))
            expiry=view.clock_ms()+10000
            signed=await view.signer.sign({'type':'order','orders':[wire] if prearmed else [wire,stop],
                'grouping':'na' if prearmed else 'normalTpsl'},expiry)
            await self.authorized(row,root)
            current=await self.clean(root,owned_stop=record if prearmed else None)
            fresh=await view.market_snapshot();self.fresh_quote(view,fresh)
            if (not self.same_clean(current,baseline) or view.clock_ms()>=expiry or not fresh.ask or Decimal(fresh.ask)>price
                    or trigger>=Decimal(fresh.mark) or qty*max(price,Decimal(fresh.mark),Decimal(fresh.ask))>15
                    or consumed+qty*max(price,Decimal(fresh.mark),Decimal(fresh.ask))>self.allowance):
                raise AdapterError('HL_COMMISSION_OBSERVATION_CHANGED')
            return signed, expiry

    async def protected_roundtrip(self,row,root,params):
        """Bounded IOC with fixed parent SL or independently prearmed full SL."""
        if self.runtime.execution_mode!='production':
            raise CredentialError('COMMISSION_DISABLED','服务未启用限额实盘验收',403)
        rid=params.get('request_id')
        if not isinstance(rid,str) or not 1<=len(rid)<=128:
            raise CredentialError('REQUEST_ID_REQUIRED','缺少有效请求编号',400)
        full_position=params.get('kind')=='position_protected_ioc'
        prearmed=params.get('kind') in PREARMED_KINDS
        context={'account':row['account_id'],'credential_revision':row['revision'],'request_id':rid,
                 'market':root.deployment+'||BTC|linear_perp|USDC',
                 'kind':params['kind'] if prearmed else 'protected_ioc'}
        key='passive-commission:'+models.sha256_hex([row['account_id'],rid]);digest=models.sha256_hex(context)
        prior=dict(self.records())
        if key in prior:
            record=prior[key]
            if record['request_hash']!=digest:raise CredentialError('IDEMPOTENCY_CONFLICT','验收编号已用于其他请求')
            if record['state'] in ('PREPARED','DISPATCHING','UNKNOWN','FILLED_REQUIRES_EXIT'):
                return {**await self.reconcile(row,root,key,record),'replayed':True}
            return {**record,'replayed':True}
        if self.allowance==0:
            raise CredentialError('COMMISSION_DISABLED','服务未启用限额实盘验收',403)
        if any(r['state'] in ('PREPARED','DISPATCHING','UNKNOWN','FILLED_REQUIRES_EXIT') for r in prior.values()):
            raise CredentialError('COMMISSION_UNRESOLVED','先核对上一笔实盘验收',422)
        controls=self.runtime.store.read(lambda c:[json.loads(r[0]) for r in c.execute(
            "SELECT value FROM runtime_state WHERE key LIKE 'self-funding:%'")])
        if any(r['state'] in ('PREPARED','DISPATCHING','UNKNOWN') for r in controls):
            raise CredentialError('COMMISSION_UNRESOLVED','先核对上一笔资金操作',422)
        await self.authorized(row,root);baseline=await self.clean(root)
        view=root.market_venue(context['market']);rules=await view.load_instrument(context['market'])
        quote=await view.market_snapshot();self.fresh_quote(view,quote)
        if not quote.bid or not quote.ask or Decimal(quote.bid)<=0 or Decimal(quote.ask)<Decimal(quote.bid):
            raise AdapterError('HL_COMMISSION_QUOTE_UNVERIFIED')
        price=(Decimal(quote.ask)*Decimal('1.001')).to_integral_value(rounding=ROUND_CEILING)
        trigger=(Decimal(quote.bid)*Decimal('.97')).to_integral_value(rounding=ROUND_FLOOR)
        stop_price=(trigger*Decimal('.98')).to_integral_value(rounding=ROUND_FLOOR)
        step=Decimal(rules.qty_step)
        qty=(Decimal(rules.min_notional)/stop_price/step).to_integral_value(rounding=ROUND_CEILING)*step
        notional=qty*max(price,Decimal(quote.mark),Decimal(quote.ask))
        consumed=sum((Decimal(r['notional']) for r in prior.values() if r['state']!='NOT_SENT'),Decimal(0))
        if notional>15 or consumed+notional>self.allowance:
            raise CredentialError('COMMISSION_ALLOWANCE_EXCEEDED','验收名义金额超过上限',422)
        cloid='0x'+models.sha256_hex(['protected-ioc-v1',context])[:32]
        stop_cloid='0x'+models.sha256_hex(['protected-ioc-stop-v1',context])[:32]
        entry=OrderRequest('commission:'+rid,cloid,row['account_id'],context['market'],'buy',
            models.canonical_decimal_text(qty),models.canonical_decimal_text(price),'ioc',False,0)
        protection=OrderRequest('commission-stop:'+rid,stop_cloid,row['account_id'],context['market'],'sell',
            entry.qty_base,models.canonical_decimal_text(stop_price),'reduce_only_ioc',True,0)
        wire=view._wire(entry)
        stop=(view.position_stop_wire if full_position else view.stop_loss_wire)(
            protection,models.canonical_decimal_text(trigger))
        record={**context,'request_hash':digest,'client_id':cloid,'qty':entry.qty_base,'price':entry.price,
            'notional':models.canonical_decimal_text(notional),'born_ms':view.clock_ms(),'state':'PREPARED',
            'before':baseline,'wire':wire,'protection':{'client_id':stop_cloid,'trigger':stop['t']['trigger']['triggerPx'],
                'price':protection.price,'wire':stop}}
        if full_position:
            record['protection']['mode']='full_position'
        if prearmed:
            record['entry_dispatched']=False
        await self.write(key,record)
        if prearmed:
            async def prearm_fence():
                await self.authorized(row,root)
                current=await self.clean(root)
                fresh=await view.market_snapshot();self.fresh_quote(view,fresh)
                return self.same_clean(current,baseline) and trigger<Decimal(fresh.mark)
            try:
                stop_response=await self.control(view,{'type':'order','orders':[stop],'grouping':'positionTpsl' if full_position else 'na'},
                    record,key,'position_stop_dispatch',fence=prearm_fence)
                if stop_response:
                    stop_statuses=stop_response.get('response',{}).get('data',{}).get('statuses',[]) if isinstance(stop_response.get('response'),dict) else []
                    if (stop_response.get('status')=='err' or len(stop_statuses)==1
                            and isinstance(stop_statuses[0],dict) and 'error' in stop_statuses[0]):
                        record['position_stop_rejected']=True
                orders=await root._info('frontendOpenOrders',user=root.owner)
                if not orders or not self.only_owned_stop(record,orders):
                    raise AdapterError('HL_COMMISSION_FULL_STOP_UNOBSERVED')
                await self.write(key,record)
            except Exception as error:
                record['diagnostic']=self.diagnostic(error)
                await self.write(key,record)
                return await self.reconcile(row,root,key,record)
        try:
            signed,expiry=await self.prepare_roundtrip_entry(row,root,view,record,wire=wire,stop=stop,
                prearmed=prearmed,baseline=baseline,price=price,trigger=trigger,qty=qty,consumed=consumed)
        except Exception as error:
            record['entry_prepare_diagnostic']=self.diagnostic(error)
            await self.write(key,record)
            if prearmed:
                return await self.reconcile(row,root,key,record)
            record.update(state='NOT_SENT',code='COMMISSION_OBSERVATION_CHANGED')
            await self.write(key,record);return record
        record.update(state='DISPATCHING',nonce=signed['nonce'],expires_after=expiry)
        if prearmed:record['entry_dispatched']=True
        await self.write(key,record)
        try:
            response=await view.transport.post('/exchange',signed)
            if response.get('status')=='err':
                record.update(state='PREPARED' if prearmed else 'REJECTED',reason=str(response.get('response'))[:160])
                if prearmed:record['entry_rejected']=True
            elif response.get('status')=='ok' and response.get('response',{}).get('type')=='order':
                statuses=response['response']['data']['statuses']
                if len(statuses)==(1 if prearmed else 2):
                    entry_ack=statuses[0];stop_ack=None if prearmed else statuses[1]
                    record['batch_ack']=statuses
                    for ack,target in [(entry_ack,record),(stop_ack,record['protection'])]:
                        if isinstance(ack,dict):
                            for typ in ('filled','resting'):
                                if typ in ack:target.update(ack=typ,oid=str(ack[typ]['oid']))
                    if all(isinstance(s,dict) and 'error' in s for s in statuses):
                        record.update(state='PREPARED' if prearmed else 'REJECTED')
                        if prearmed:record['entry_rejected']=True
        except Exception:pass
        await self.write(key,record)
        if record['state']=='REJECTED':return record
        try:return await self.reconcile(row,root,key,record)
        except Exception as error:
            record.update(state='UNKNOWN',code='COMMISSION_RECONCILIATION_UNAVAILABLE',diagnostic=self.diagnostic(error))
            await self.write(key,record);return record
