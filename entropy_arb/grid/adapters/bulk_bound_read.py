"""Native order reads with exact account-owned durable action bindings.

A parked stop must not break a sibling entry's original-ID query. Native
terminal rows can omit request metadata; a durable deterministic binding may
supply the REQUEST fields, never executions or complete history coverage.
"""
import copy
from decimal import Decimal

from .base import AdapterError,ExchangeOrder
from .bulk_read import BulkEvidenceReader,OPEN,CANCELED,REJECTED,flag
from .bulk_wire import base58,native_decimal,text,native_tif_matches


class BulkBoundEvidenceReader(BulkEvidenceReader):
    def __init__(self,*args,action_bindings=None,**kwargs):
        super().__init__(*args,**kwargs)
        self.actions=copy.deepcopy(action_bindings or {})
        for oid,binding in self.actions.items():
            base58(oid)
            if (not isinstance(binding,dict) or set(binding)!={'account','client_id','action'}
                    or not isinstance(binding.get('client_id'),str) or not binding['client_id']
                    or not isinstance(binding.get('action'),dict)
                    or binding.get('account')!=self.account or binding.get('client_id')!=self.bindings.get(oid)
                    or binding.get('action',{}).get('symbol')!=self.symbol):
                raise AdapterError('BULK_BOUND_ACTION_SCOPE_CONFLICT')

    async def lookup(self,client_id,exchange_order_id=None,*,start_slot):
        from .bulk_positive_lookup import lookup_original
        return await lookup_original(self,client_id,exchange_order_id,start_slot=start_slot)

    async def lookup_many(self,originals,*,start_slot):
        from .bulk_batch_lookup import lookup_original_batch
        return await lookup_original_batch(self,originals,start_slot=start_slot)

    def _bound(self,oid):
        binding=self.actions.get(oid)
        if binding is None:raise AdapterError('BULK_BOUND_ORIGINAL_ACTION_REQUIRED')
        action=binding['action'];kind=action.get('type')
        if kind=='stop':
            if set(action)!={'type','symbol','size','trigger_price','iso'} or action['iso'] is not False:
                raise AdapterError('BULK_BOUND_STOP_UNVERIFIED')
            side,price,reducing='sell',action['trigger_price'],True
        elif kind=='order':
            if (set(action)!={'type','symbol','is_buy','price','size','reduce_only','iso','order_type'}
                    or action['iso'] is not False or type(action['is_buy']) is not bool
                    or type(action['reduce_only']) is not bool or not isinstance(action['order_type'],dict)
                    or set(action['order_type'])!={'type','tif'} or action['order_type']['type']!='limit'
                    or action['order_type']['tif'] not in ('GTC','IOC','ALO')):raise AdapterError('BULK_BOUND_ORDER_UNVERIFIED')
            side,price,reducing=('buy' if action['is_buy'] else 'sell'),action['price'],action['reduce_only']
        else:raise AdapterError('BULK_BOUND_ACTION_UNVERIFIED')
        qty=native_decimal(action['size'],negative=False);price=native_decimal(price,negative=False)
        if qty<=0 or price<=0:raise AdapterError('BULK_BOUND_AMOUNT_UNVERIFIED')
        return binding,kind,side,qty,price,reducing

    @staticmethod
    def _trigger(row,price):
        trigger=row.get('trigger')
        if (not isinstance(trigger,dict) or trigger.get('isAbove') is not False
                or native_decimal(trigger['px'])!=price
                or any(trigger.get(k) is not None for k in ('lim','oco','pxHi','limHi','trb','stb'))):
            raise AdapterError('BULK_BOUND_STOP_CHANGED')

    def order(self,row,*,terminal=False):
        if not isinstance(row,dict):raise AdapterError('BULK_BOUND_ORDER_INVALID')
        if row.get('orderType')!='stop' and row.get('orderId') not in self.actions:
            return super().order(row,terminal=terminal)
        self._lane(row)
        if row['symbol']!=self.symbol:raise AdapterError('BULK_ORDER_MARKET_MISMATCH')
        oid=row['orderId'];base58(oid);binding,kind,side,qty,price,reducing=self._bound(oid)
        status=row['status']
        if status=='rejectedDuplicate':raise AdapterError('BULK_DUPLICATE_REQUIRES_ORIGINAL_PROOF')
        if kind=='stop':
            if row['orderType']=='market':
                # Mainnet retains the original conditional OID and trigger
                # for a terminal stop, filled or cancelled unfilled. No child
                # is guessed from timing/price/flatness: the durable exact
                # root is required.
                self._trigger(row,price)
                size=native_decimal(row['originalSize'],negative=False)
                executed=native_decimal(row['executedSize'],negative=False)
                execution_price=native_decimal(row['price'],negative=False)
                if (terminal and status in CANCELED and executed==0
                        and row['tif']=='ioc' and row['side']=='sell'
                        and flag(row['reduceOnly']) is True
                        and 0<size<=qty and execution_price==0
                        and native_decimal(row['vwap'],negative=False)==0):
                    # The child proves zero executions and the retained root
                    # carries the exact trigger: the stop never executed.
                    return ExchangeOrder(binding['client_id'],oid,side,text(qty),text(price),'0',True,
                        'rest_query',False,text(price),False),status
                if (not terminal or status!='filled' or row['tif']!='ioc'
                        or row['side']!='sell' or flag(row['reduceOnly']) is not True
                        or not 0<size<=qty or executed!=size or execution_price<=0
                        or native_decimal(row['vwap'],negative=False)!=execution_price):
                    raise AdapterError('BULK_BOUND_STOP_EXECUTION_UNVERIFIED')
                return ExchangeOrder(binding['client_id'],oid,'sell',text(size),text(execution_price),text(executed),True,
                    'rest_query',False,text(price)),status
            if row['orderType']!='stop' or row['tif']!='gtc':raise AdapterError('BULK_BOUND_STOP_CHANGED')
            self._trigger(row,price);flag(row['reduceOnly'])
            if native_decimal(row['price'])!=price:raise AdapterError('BULK_BOUND_STOP_CHANGED')
            if not terminal:
                if (status not in OPEN or native_decimal(row['originalSize'])!=-qty
                        or native_decimal(row['size'])!=-qty or native_decimal(row['filledSize'])!=0):
                    raise AdapterError('BULK_BOUND_STOP_CHANGED')
                return ExchangeOrder(binding['client_id'],oid,side,text(qty),text(price),'0',True,
                    'rest_query',False,text(price),False,conditional_parked=True),status
        elif row['orderType']!='limit' or row.get('trigger') is not None:
            raise AdapterError('BULK_BOUND_ORDER_TYPE_CONFLICT')
        else:
            if not native_tif_matches(row['tif'],binding['action']['order_type']['tif']):
                raise AdapterError('BULK_BOUND_REQUEST_CONFLICT')
            if not terminal:
                order,native_status=super().order(row)
                if (order.side!=side or native_decimal(order.qty_base)!=qty
                        or native_decimal(order.price)!=price or order.reduce_only!=reducing):
                    raise AdapterError('BULK_BOUND_REQUEST_CONFLICT')
                return order,native_status
        if not terminal or status not in CANCELED|REJECTED|{'filled','partiallyFilled'}:
            raise AdapterError('BULK_ORDER_TERMINAL_UNVERIFIED')
        # Current native cancel rows default originalSize/executedSize to zero
        # and side to buy. The known root identifies the original request;
        # these placeholders cannot prove zero executions or infer a buy.
        native_qty=native_decimal(row['originalSize'],negative=False)
        native_filled=native_decimal(row['executedSize'],negative=False)
        if native_qty==0:
            if native_filled!=0 or status not in CANCELED|REJECTED:
                raise AdapterError('BULK_BOUND_EXECUTIONS_UNVERIFIED')
            native_price=native_decimal(row['price'],negative=False)
            if native_price not in (Decimal(0),price) or row['side'] not in ('buy','sell'):
                raise AdapterError('BULK_BOUND_REQUEST_CONFLICT')
            if kind=='order':
                tif=binding['action']['order_type'].get('tif')
                if (not native_tif_matches(row['tif'],tif) or binding['action']['order_type'].get('type')!='limit'
                        or flag(row['reduceOnly'])!=reducing):raise AdapterError('BULK_BOUND_REQUEST_CONFLICT')
            if status in REJECTED and native_filled!=0:raise AdapterError('BULK_REJECTION_HAS_EXECUTIONS')
            return ExchangeOrder(binding['client_id'],oid,side,text(qty),text(price),'0',reducing,
                'rest_query',False,text(price) if kind=='stop' else None,False),status
        if native_qty!=qty or row['side']!=side:raise AdapterError('BULK_BOUND_REQUEST_CONFLICT')
        if kind=='stop':
            if native_filled!=0 or status not in CANCELED|REJECTED:raise AdapterError('BULK_BOUND_STOP_EXECUTION_UNVERIFIED')
            return ExchangeOrder(binding['client_id'],oid,side,text(qty),text(price),'0',True,
                'rest_query',False,text(price),False),status
        order,native_status=super().order(row,terminal=True)
        if (native_decimal(order.price)!=price or order.reduce_only!=reducing
                or not native_tif_matches(row['tif'],binding['action']['order_type']['tif'])):
            raise AdapterError('BULK_BOUND_REQUEST_CONFLICT')
        return order,native_status
