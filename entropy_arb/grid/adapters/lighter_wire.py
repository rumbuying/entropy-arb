"""Exact native order encoding for the pinned Lighter SDK (no network I/O).

Not an execution grant. Expiring resting orders, native protection, small
residuals and native terminal/history evidence still require certification.
An adapter must persist the returned native expiry with its original intent.
"""
from dataclasses import dataclass
from decimal import Decimal, localcontext
import re

from .. import models
from .base import AdapterError, OrderRequest

SDK_COMMIT = 'a7a9ba88514d38a863a33642acd41f8adf442e5c'
MAX_CLIENT_INDEX = 2**48 - 1
MAX_PRICE = 2**32 - 1


def integer(value, maximum, *, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise AdapterError('LIGHTER_NATIVE_INTEGER_INVALID')
    return value


def scaled(value, decimals, maximum):
    if not isinstance(value, str) or len(value) > 128:
        raise AdapterError('LIGHTER_NATIVE_PRECISION_INVALID')
    amount = models.parse_decimal_text(value, 'native_amount', allow_negative=False)
    with localcontext() as context:
        context.prec = max(64, len(amount.as_tuple().digits) + decimals + 1)
        units = amount * Decimal(10)**decimals
    if units != units.to_integral_value() or not 1 <= units <= maximum:
        raise AdapterError('LIGHTER_NATIVE_PRECISION_INVALID')
    return int(units)


@dataclass(frozen=True)
class LighterWireRules:
    market_ref: str
    market_index: int
    size_decimals: int
    price_decimals: int
    min_qty: str
    min_notional: str

    @classmethod
    def from_native(cls, row, *, venue='lighter', network='mainnet'):
        if venue not in ('lighter', 'lighter-rh') or network not in ('mainnet', 'testnet') or venue == 'lighter-rh' and network != 'mainnet':
            raise AdapterError('LIGHTER_DEPLOYMENT_UNVERIFIED')
        if row.get('market_type') != 'perp' or row.get('status') != 'active':
            raise AdapterError('LIGHTER_MARKET_NOT_ACTIVE')
        if models.parse_decimal_text(row.get('multiplier'), 'multiplier') != 1:
            raise AdapterError('LIGHTER_CONTRACT_MULTIPLIER_UNVERIFIED')
        mid = integer(row['market_id'], 2**15-1)
        sd, pd = (integer(row[key], 18) for key in ('supported_size_decimals', 'supported_price_decimals'))
        for key in ('min_base_amount', 'min_quote_amount'):
            if models.parse_decimal_text(row[key], key, allow_negative=False) <= 0:
                raise AdapterError('LIGHTER_MINIMUM_UNVERIFIED')
        market = models.MarketIdentity(f'{venue}:{network}', str(mid), '', 'linear_perp', 'USDC')
        return cls(market.market_key, mid, sd, pd, row['min_base_amount'], row['min_quote_amount'])

    def compile(self, order: OrderRequest, *, account, nonce, api_key_index, now_ms, resting_expiry_ms=None):
        if order.account != account or order.market != self.market_ref:
            raise AdapterError('LIGHTER_ORDER_SCOPE_MISMATCH')
        if order.whole_position or order.trigger_price is not None:
            raise AdapterError('LIGHTER_PROTECTION_UNVERIFIED')
        if order.request_deadline_sec is not None:
            raise AdapterError('LIGHTER_REQUEST_DEADLINE_UNVERIFIED')
        if not isinstance(order.client_id, str) or not re.fullmatch(r'[0-9]{1,15}', order.client_id):
            raise AdapterError('LIGHTER_CLIENT_ID_INVALID')
        cid = integer(int(order.client_id), MAX_CLIENT_INDEX, minimum=1)
        quantity = scaled(order.qty_base, self.size_decimals, MAX_CLIENT_INDEX)
        price = scaled(order.price, self.price_decimals, MAX_PRICE)
        if (Decimal(order.qty_base) < Decimal(self.min_qty)
                or Decimal(order.qty_base)*Decimal(order.price) < Decimal(self.min_notional)):
            # Never copy HL's small-notional/whole-position exceptions.
            raise AdapterError('LIGHTER_ORDER_BELOW_MINIMUM')
        if order.side not in ('buy', 'sell') or type(order.reduce_only) is not bool:
            raise AdapterError('LIGHTER_ORDER_FLAGS_INVALID')
        policies = {'gtc': 1, 'post_only': 2, 'ioc': 0, 'reduce_only_ioc': 0}
        if order.policy not in policies or order.policy == 'reduce_only_ioc' and not order.reduce_only:
            raise AdapterError('LIGHTER_ORDER_POLICY_INVALID')
        expiry = 0
        if order.policy in ('gtc', 'post_only'):
            integer(now_ms, 2**63-1)
            integer(resting_expiry_ms, 2**63-1)
            if not now_ms + 300_000 <= resting_expiry_ms <= now_ms + 28*86400_000:
                raise AdapterError('LIGHTER_ORDER_EXPIRY_INVALID')
            expiry = resting_expiry_ms
        return dict(market_index=self.market_index, client_order_index=cid,
            base_amount=quantity, price=price, is_ask=order.side == 'sell',
            order_type=0, time_in_force=policies[order.policy], reduce_only=order.reduce_only,
            trigger_price=0, order_expiry=expiry, nonce=integer(nonce, MAX_CLIENT_INDEX),
            api_key_index=integer(api_key_index, 254))
