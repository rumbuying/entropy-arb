"""Native Bulk account-wide budget ownership, separate from initial sizing.

A budget is never availableMargin interpreted as initial trading capacity.
Only a fully reconciled account can reuse its already-reserved maintenance
margin. Native impact remains a debit; no per-market copy of account equity
or HL leverage formula is used. The separate allocation model budgets future
inventory and stress; neither component replaces native order admission.
"""
from dataclasses import dataclass
from decimal import Decimal,localcontext,ROUND_CEILING
from fractions import Fraction
import re

from .. import models
from ..journal import TRANSITIONS
from .base import AdapterError
from .bulk_market import symbol
from .bulk_wire import native_decimal,text,uint,base58,native_tif_matches

ACTIVE=frozenset(('OPEN','PARTIAL','ACCEPTED'))
UNCERTAIN=frozenset(('UNKNOWN','DISPATCHING','CANCEL_PENDING','CANCEL_REQUESTED'))


def reject(code):raise AdapterError('BULK_BUDGET_'+code)


def leverage_settings(native):
    settings=native['leverageSettings'];leverage={}
    if not isinstance(settings,list):reject('LEVERAGE_UNVERIFIED')
    for item in settings:
        coin=item['symbol']
        if not isinstance(coin,str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]{0,48}-USD',coin):reject('LEVERAGE_UNVERIFIED')
        if coin in leverage:reject('LEVERAGE_UNVERIFIED')
        value=native_decimal(item['leverage'],negative=False)
        if value<1:reject('LEVERAGE_UNVERIFIED')
        leverage[coin]=value
    return leverage


def lane(row):
    if not isinstance(row,dict) or row.get('iso') is not False or row.get('isoPubkey') not in (None,''):
        reject('ISOLATED_UNSUPPORTED')


@dataclass(frozen=True)
class BulkMarginBalance:
    total: Decimal
    available: Decimal
    used: Decimal
    impact: Decimal
    buffered: Decimal
    buffer_rate: Decimal
    locked: Decimal

    @classmethod
    def parse(cls,account):
        if account.get('kind')!='MasterEOA' or account.get('parent'):reject('ACCOUNT_KIND_UNVERIFIED')
        try:
            row=account['margin']
            total,available,used,impact,buffered,rate=[native_decimal(row[k]) for k in
                ('totalMargin','availableMargin','marginUsed','executionImpact','bufferedMargin','marginBufferRate')]
            if used<0 or impact>0 or buffered<used or not 0<=rate<=1 or available>total:
                reject('MARGIN_INCONSISTENT')
            # Native FullAccount explicitly omits lockedAmounts when empty.
            # availableMargin is totalMargin-minus-maintenance, not transferable
            # collateral; pending withdrawal/transfer locks remain a debit.
            locks=account.get('lockedAmounts',[])
            if not isinstance(locks,list):reject('LOCKS_UNVERIFIED')
            seen=set();locked=Decimal(0)
            with localcontext() as ctx:
                ctx.prec=512
                for lock in locks:
                    identity=lock['hash'];base58(identity);uint(lock['lockedAt'])
                    value=native_decimal(lock['amount'],negative=False)
                    if lock['symbol']!='USDC' or value<=0 or identity in seen:reject('LOCKS_UNVERIFIED')
                    seen.add(identity);locked+=value
            # Both reported values cap re-credit. Inexact native f64 formatting
            # can only reduce capacity, never manufacture a positive balance.
            return cls(total,available,used,impact,buffered,rate,locked)
        except (KeyError,TypeError,ValueError,ArithmeticError):reject('MARGIN_UNVERIFIED')

    @property
    def liquid_budget(self):
        with localcontext() as ctx:
            ctx.prec=512
            if self.available<0:return Decimal(0)
            return max(Decimal(0),min(self.total,self.available+self.used)+self.impact-self.locked)


def order_matches(native,owned):
    """Exact original order ownership, including intrinsically reducing stops."""
    owned=dict(owned);owned['reduce_only']=bool(owned['reduce_only'])
    try:
        lane(native)
        if native['status'] not in ('resting','working','placed','modified'):return False
        qty=native_decimal(owned['qty_base'],negative=False)
        done=native_decimal(owned.get('cumulative_qty','0'),negative=False)
        if qty<=0 or done>qty or type(owned['reduce_only']) is not bool:return False
        original=native_decimal(native['originalSize']);remaining=native_decimal(native['size'])
        filled=native_decimal(native['filledSize']).copy_abs()
        if native['orderType']=='stop':
            trigger=native.get('trigger')
            return (owned['side']=='sell' and owned['reduce_only'] is True and not owned.get('whole_position')
                and native['tif']=='gtc' and owned['policy']=='gtc'
                and done==filled==0 and original==remaining==-qty
                and type(native['reduceOnly']) is bool and isinstance(trigger,dict)
                and trigger.get('isAbove') is False
                and native_decimal(trigger['px'])==native_decimal(owned['trigger_price'])
                and native_decimal(native['price'])==native_decimal(owned['trigger_price'])
                and all(trigger.get(k) is None for k in ('lim','oco','pxHi','limHi','trb','stb')))
        if native['orderType']!='limit' or native.get('trigger') is not None or owned.get('trigger_price') is not None:return False
        tif={'gtc':'GTC','post_only':'ALO'}.get(owned['policy'])
        return (not owned.get('whole_position') and owned['side'] in ('buy','sell')
            and original==(qty if owned['side']=='buy' else -qty)
            and remaining==((qty-done) if owned['side']=='buy' else -(qty-done)) and filled==done
            and native['reduceOnly'] is owned['reduce_only'] and native_tif_matches(native['tif'],tif)
            and native_decimal(native['price'])==native_decimal(owned['price']))
    except (KeyError,TypeError,ValueError,ArithmeticError,AdapterError):return False


class BulkAccountBudget:
    """One economic account, all markets, one shared capital ceiling.

    portfolio_capacity returns the FULL account ceiling: Runtime.check_budget
    subtracts existing reservations in its write transaction. Subtracting them
    here as well would charge sibling grids twice. remaining_for_grid is only
    for the sizing model, never the transaction's full-capacity input.
    """
    model_id='bulk-master-cross-account-budget-v1'

    def __init__(self,*,account,network,credential_revision,credential_ref,clock_ms):
        if network not in ('mainnet','testnet'):reject('DEPLOYMENT_UNVERIFIED')
        if not all(isinstance(v,str) and v for v in (account,credential_revision,credential_ref)):reject('SCOPE_REQUIRED')
        self.account,self.network,self.revision,self.credential_ref=account,network,credential_revision,credential_ref
        self.deployment='bulk:'+network;self.clock_ms=clock_ms

    def validate_account(self,parent):
        if (parent._account.account_id!=self.account or parent.network!=self.network
                or parent._account.credential_revision!=self.revision or parent.credential_ref!=self.credential_ref):
            reject('SCOPE_CONFLICT')

    def account_assets(self,native):
        balance=BulkMarginBalance.parse(native)
        # Observed free equity only; this is not an initial-margin risk quote.
        return [('USDC',text(max(Decimal(0),min(balance.available,balance.liquid_budget))))]

    def _market(self,value):
        parts=value.split('|') if isinstance(value,str) else []
        if len(parts)!=5 or parts[0]!=self.deployment or parts[1]!='' or parts[3:]!=['linear_perp','USDC']:
            reject('MARKET_SCOPE_CONFLICT')
        return symbol(parts[2])

    def portfolio_capacity(self,config,snapshot,context):
        try:return self._capacity(config,snapshot,context)
        except (KeyError,TypeError,ValueError,ArithmeticError):reject('EVIDENCE_INCOMPLETE')

    def _capacity(self,config,snapshot,context):
        if (config.account_ref!=self.account or snapshot.account!=self.account or context['account']!=self.account
                or config.credential_ref!=self.credential_ref):reject('SCOPE_CONFLICT')
        if config.capital.asset!='USDC' or config.margin_mode!='cross':reject('SETTLEMENT_UNSUPPORTED')
        selected=self._market(config.market_ref);evidence=snapshot.evidence or {}
        if (snapshot.source!='bulk:full-account' or snapshot.margin_mode!='cross'
                or evidence.get('account')!=self.account or evidence.get('deployment')!=self.deployment
                or evidence.get('credential_revision')!=self.revision):reject('SNAPSHOT_SCOPE_CONFLICT')
        observed=uint(evidence['observed_from_ms']);now=uint(self.clock_ms())
        if not observed<=now<=observed+5000:reject('SNAPSHOT_STALE')
        native=evidence['native_account'];balance=BulkMarginBalance.parse(native)
        if list(snapshot.assets)!=self.account_assets(native):reject('ASSET_EVIDENCE_CONFLICT')
        if not isinstance(context['contexts'],dict):reject('PORTFOLIO_INCOMPLETE')
        native_decimal(context['other_reserved'],negative=False)
        by_symbol={};owned_orders={};expected_active=set();clients=set()
        for market,owned in context['contexts'].items():
            coin=self._market(market)
            if (coin in by_symbol or owned['account']!=self.account or owned['market']!=market
                    or owned['margin_mode']!='cross'):reject('PORTFOLIO_SCOPE_CONFLICT')
            if not isinstance(owned['orders'],list):reject('PORTFOLIO_INCOMPLETE')
            native_decimal(owned['qty_base']);by_symbol[coin]=owned
            for order in owned['orders']:
                if order['state'] not in TRANSITIONS:reject('ORDER_STATE_UNVERIFIED')
                if order['state'] in UNCERTAIN:reject('ORDER_UNRESOLVED')
                if order['state'] not in ACTIVE:continue
                oid=order['exchange_order_id'];cid=order['client_id']
                if not isinstance(oid,str) or not oid or not isinstance(cid,str) or not cid or oid in owned_orders or cid in clients:
                    reject('ORDER_BINDING_CONFLICT')
                base58(oid)
                if order.get('account')!=self.account:reject('ORDER_SCOPE_CONFLICT')
                if order.get('market') is not None and order['market']!=market:reject('ORDER_SCOPE_CONFLICT')
                owned_orders[oid]=(coin,order);clients.add(cid);expected_active.add(oid)
        leverage=leverage_settings(native)
        # Bulk setting is a maximum, not HL's per-position chosen leverage.
        # A lower grid leverage needs no account settings mutation.
        demands={coin:native_decimal(owned['leverage'],negative=False) for coin,owned in by_symbol.items()}
        configured=native_decimal(config.leverage,negative=False)
        demands[selected]=max(demands.get(selected,configured),configured)
        for coin,value in demands.items():
            chosen=value
            if chosen<1 or coin not in leverage or chosen>leverage[coin]:reject('LEVERAGE_LIMIT')
        if not isinstance(native['positions'],list) or not isinstance(native['openOrders'],list):reject('SNAPSHOT_INCOMPLETE')
        if not isinstance(native.get('pendingOnFill',[]),list) or native.get('pendingOnFill'):
            reject('PENDING_CONDITIONAL_UNVERIFIED')
        seen=set()
        for position in native['positions']:
            lane(position);coin=symbol(position['symbol']);qty=native_decimal(position['size'])
            if coin in seen:reject('DUPLICATE_POSITION')
            seen.add(coin);owned=by_symbol.get(coin)
            if qty!=0 and (owned is None or qty!=native_decimal(owned['qty_base'])):reject('POSITION_UNRECONCILED')
            if owned is not None and qty!=native_decimal(owned['qty_base']):reject('POSITION_UNRECONCILED')
        if any(native_decimal(o['qty_base'])!=0 and coin not in seen for coin,o in by_symbol.items()):reject('POSITION_UNRECONCILED')
        if balance.used>0 and not any(native_decimal(p['size'])!=0 for p in native['positions']):
            reject('MARGIN_OWNERSHIP_UNVERIFIED')
        found=set()
        for row in native['openOrders']:
            lane(row);oid=row['orderId'];coin=symbol(row['symbol'])
            if oid in found:reject('DUPLICATE_ORDER')
            found.add(oid);bound=owned_orders.get(oid)
            if bound is None or bound[0]!=coin:reject('FOREIGN_ORDER')
            if not order_matches(row,bound[1]):reject('ORDER_PARAMETERS_CONFLICT')
        if found!=expected_active:reject('ORDER_UNRECONCILED')
        if balance.liquid_budget==0:return '0'
        self._pending_floors(config,context,native)
        return text(balance.liquid_budget)

    def _pending_floors(self,config,context,native):
        """Minimum application reservation; never native order admission.

        Native availableMargin stayed unchanged with two real resting buys.
        Require held native margin plus pending buy notional / the grid's
        requested leverage inside its existing capital reservation. This
        excludes fees, future slots and stress loss; the range model adds them.
        Reducing sells/stops cannot finance buys before they actually execute.
        No subtraction here: Runtime already subtracts full sibling budgets.
        """
        result=[];seen=set()
        for market,owned in context['contexts'].items():
            leverage=native_decimal(owned['leverage'],negative=False)
            if leverage<1:reject('LEVERAGE_LIMIT')
            reserved=native_decimal(owned['reserved_amount'],negative=False)
            if owned['grid_id']==context.get('selected_grid_id'):
                reserved=native_decimal(config.capital.total,negative=False)
            total=Fraction(0)
            for order in owned['orders']:
                if order['state'] not in ACTIVE|{'PLANNED','BLOCKED_SLOT'}:continue
                cid=order['client_id']
                if not isinstance(cid,str) or not cid or cid in seen:reject('ORDER_BINDING_CONFLICT')
                seen.add(cid)
                # Ledger rows encode reduce_only as 0/1 and carry no market
                # column: the market is the context key they are grouped under.
                if (order.get('account')!=self.account or order.get('market',market)!=market
                        or order['side'] not in ('buy','sell')):
                    reject('ORDER_SCOPE_CONFLICT')
                if bool(order['reduce_only']):continue
                if order['side']!='buy':reject('SHORT_ORDER_UNSUPPORTED')
                qty=native_decimal(order['qty_base'],negative=False)
                done=native_decimal(order.get('cumulative_qty','0'),negative=False)
                price=native_decimal(order['price'],negative=False)
                if qty<=0 or done>qty or price<=0:reject('ORDER_PARAMETERS_CONFLICT')
                total+=(Fraction(qty)-Fraction(done))*Fraction(price)
            floor=total/Fraction(leverage)
            held=Fraction(0);worst=Fraction(0)
            for position in native['positions']:
                if position['symbol']==self._market(market) and native_decimal(position['size'])!=0:
                    try:
                        held=Fraction(native_decimal(position['maintenanceMargin'],negative=False))
                        worst=Fraction(native_decimal(position.get('worstCaseMaintenanceMargin',position['maintenanceMargin']),negative=False))
                    except (KeyError,TypeError,ValueError):reject('POSITION_MARGIN_UNVERIFIED')
            floor=max(held+floor,worst)
            if Fraction(reserved)<floor:reject('PENDING_RESERVATION_INSUFFICIENT')
            with localcontext() as ctx:
                ctx.prec=60;ctx.rounding=ROUND_CEILING
                result.append(dict(market=market,reserved_amount=text(reserved),
                    pending_buy_notional=text(Decimal(total.numerator)/Decimal(total.denominator)),
                    native_held_margin=text(Decimal(held.numerator)/Decimal(held.denominator)),
                    minimum_entry_reservation=text(Decimal(floor.numerator)/Decimal(floor.denominator))))
        return result

    def reservation_evidence(self,config,snapshot,context):
        capacity=self.portfolio_capacity(config,snapshot,context)
        return dict(account_capacity=capacity,other_reserved=context['other_reserved'],
            markets=self._pending_floors(config,context,snapshot.evidence['native_account']),native_order_admission=False)

    def remaining_for_grid(self,config,snapshot,context):
        capacity=native_decimal(self.portfolio_capacity(config,snapshot,context),negative=False)
        with localcontext() as ctx:
            ctx.prec=512
            return text(max(Decimal(0),capacity-native_decimal(context['other_reserved'],negative=False)))

    def range_model(self,venue,config,lines,market_snapshot,account_snapshot,*,owned_context=None,portfolio_context=None):
        """Application allocation preview; not a native admission certificate.

        Full requested leverage remains available up to both native ceilings.
        Protection, current native risk data and live qualification still gate
        actual writes independently of this application's stress budget.
        """
        from .bulk_range_budget import BulkLongRangeBudget,inventory_terms,ceiling
        if (config.direction!='long' or config.margin_mode!='cross' or config.capital.asset!='USDC'):
            raise AdapterError('BULK_RANGE_LONG_CROSS_ONLY')
        if (venue.market.market_key!=config.market_ref or venue.wire_rules is None or venue.rules is None
                or venue.wire_rules.market_ref!=config.market_ref or market_snapshot.market!=config.market_ref):
            raise AdapterError('BULK_RANGE_MARKET_SCOPE_CONFLICT')
        if market_snapshot.mark is None:raise AdapterError('BULK_RANGE_MARK_UNAVAILABLE')
        if native_decimal(config.leverage,negative=False)>native_decimal(venue.wire_rules.max_leverage,negative=False):
            raise AdapterError('BULK_RANGE_MARKET_LEVERAGE_LIMIT')
        native=(account_snapshot.evidence or {}).get('native_account',{})
        if portfolio_context is None:
            if native.get('positions') or native.get('openOrders') or owned_context is not None:
                raise AdapterError('BULK_RANGE_PORTFOLIO_CONTEXT_REQUIRED')
            portfolio_context=dict(account=self.account,contexts={},other_reserved='0',selected_grid_id=None)
        remaining=self.remaining_for_grid(config,account_snapshot,portfolio_context)
        selected=portfolio_context['contexts'].get(config.market_ref)
        if owned_context is not None:
            if (selected is None or owned_context.get('account')!=self.account
                    or owned_context.get('market')!=config.market_ref
                    or owned_context.get('grid_id')!=portfolio_context.get('selected_grid_id')
                    or owned_context.get('grid_id')!=selected['grid_id']
                    or native_decimal(owned_context['qty_base'])!=native_decimal(selected['qty_base'])):
                raise AdapterError('BULK_RANGE_OWNED_SCOPE_CONFLICT')
            fields=('intent_id','client_id','revision','level_index','state','side','qty_base','price',
                'reduce_only','whole_position','cumulative_qty','exchange_order_id','trigger_price')
            def orders_fingerprint(rows):return sorted(models.sha256_hex({k:r.get(k) for k in fields}) for r in rows)
            if orders_fingerprint(owned_context['orders'])!=orders_fingerprint(selected['orders']):
                raise AdapterError('BULK_RANGE_OWNED_SCOPE_CONFLICT')
        elif selected is not None:raise AdapterError('BULK_RANGE_OWNED_CONTEXT_REQUIRED')
        terms,effective,working_prices=inventory_terms(owned_context,grid_count=config.grid_count)
        observed_margin=Decimal(0)
        for position in native['positions']:
            if position['symbol']==venue.symbol and native_decimal(position['size'])!=0:
                try:
                    margin=native_decimal(position['maintenanceMargin'],negative=False)
                    worst=native_decimal(position.get('worstCaseMaintenanceMargin',position['maintenanceMargin']),negative=False)
                except (KeyError,TypeError,ValueError):raise AdapterError('BULK_RANGE_POSITION_MARGIN_UNVERIFIED') from None
                observed_margin=max(observed_margin,margin,worst)
        total=native_decimal(effective if effective is not None else config.capital.total)
        total=max(Decimal(0),total)
        stress=list(working_prices)
        with localcontext() as ctx:
            ctx.prec=256
            tick=native_decimal(venue.wire_rules.tick,negative=False)
            if market_snapshot.ask is not None:
                ask=native_decimal(market_snapshot.ask,negative=False)
                stress.append(text((ask*Decimal('1.002')/tick).to_integral_value(rounding=ROUND_CEILING)*tick))
            protection=config.protection
            if protection is not None:
                if protection.lower_stop is not None:
                    value=(native_decimal(protection.lower_stop,negative=False)*Decimal('0.98')/tick).to_integral_value(rounding='ROUND_FLOOR')*tick
                    if value<=0:raise AdapterError('BULK_RANGE_STRESS_PRICE_INVALID')
                    stress.append(text(value))
                if protection.upper_stop is not None:
                    stress.append(text((native_decimal(protection.upper_stop,negative=False)*Decimal('1.02')/tick).to_integral_value(rounding=ROUND_CEILING)*tick))
        model=BulkLongRangeBudget(qty_step=venue.rules.qty_step,prices=tuple(lines['levels']),mark=market_snapshot.mark,
            grid_count=config.grid_count,leverage=config.leverage,taker_fee=venue.rules.taker_fee_rate,
            total_budget=text(total),account_remaining=remaining,stress_prices=tuple(stress),
            observed_margin=text(observed_margin),**terms)
        high=max(Fraction(native_decimal(v,negative=False)) for v in (*model.prices,*model.stress_prices,model.mark))
        step=Fraction(native_decimal(model.qty_step,negative=False))
        bound=Fraction(total)*config.leverage/(config.grid_count*high*step)
        k=bound.numerator//bound.denominator
        # Native quantities use unsigned 64-bit fixed-point units at 1e-8.
        step_units=step*100000000
        if step_units.denominator!=1 or not 1<=step_units<=2**64-1:
            raise AdapterError('BULK_RANGE_QUANTITY_STEP_INVALID')
        k=min(k,(2**64-1)//step_units.numerator)
        # Keep one candidate even for exhausted capital: the demand solver
        # then reports BUDGET_EXCEEDED instead of invalid instrument rules.
        return model,ceiling(step*max(1,k))
