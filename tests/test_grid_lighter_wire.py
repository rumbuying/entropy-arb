"""Order encoding must not silently round, rewrite IDs or borrow HL rules."""
from dataclasses import replace

import pytest

from entropy_arb.grid.adapters.base import OrderRequest, AdapterError
from entropy_arb.grid.adapters.lighter_wire import LighterWireRules


META = dict(market_id=0, market_type='perp', status='active', multiplier='1',
            supported_size_decimals=4, supported_price_decimals=2,
            min_base_amount='0.001', min_quote_amount='10')
RULES = LighterWireRules.from_native(META)
ORDER = OrderRequest('intent', '000000000042', 'account', RULES.market_ref,
                     'buy', '0.01', '2300.12', 'gtc', False, 1)
PARAMS = dict(account='account', nonce=7, api_key_index=3, now_ms=1000, resting_expiry_ms=601000)


def test_exact_encoding_and_explicit_good_till_time_semantics():
    encoded = RULES.compile(ORDER, **PARAMS)
    assert encoded == dict(market_index=0, client_order_index=42, base_amount=100,
        price=230012, is_ask=False, order_type=0, time_in_force=1, reduce_only=False,
        trigger_price=0, order_expiry=601000, nonce=7, api_key_index=3)
    assert ORDER.client_id == '000000000042'
    rh = LighterWireRules.from_native(META, venue='lighter-rh')
    with pytest.raises(AdapterError, match='SCOPE_MISMATCH'):
        rh.compile(ORDER, **PARAMS)


@pytest.mark.parametrize('patch', [
    {'qty_base': '0.01001'}, {'price': '2300.121'}, {'qty_base': '0'},
    {'qty_base': '0.01000000000000000000000000000000000001'},
    {'client_id': '0'}, {'client_id': str(2**48)}, {'account': 'other'},
    {'whole_position': True}, {'trigger_price': '2200'}, {'request_deadline_sec': 30},
    {'qty_base': '0.001', 'price': '2300'}, {'side': 'unknown'}, {'reduce_only': 'true'},
])
def test_invalid_or_unverified_features_do_not_reach_signer(patch):
    with pytest.raises((AdapterError, ValueError)):
        RULES.compile(replace(ORDER, **patch), **PARAMS)


@pytest.mark.parametrize('expiry', [None, 300999, 1000+29*86400_000])
def test_expiring_order_requires_explicit_bounded_expiry(expiry):
    with pytest.raises(AdapterError):
        RULES.compile(ORDER, **(PARAMS | {'resting_expiry_ms': expiry}))


def test_ioc_is_limit_price_bounded_and_reduce_only_is_retained():
    encoded = RULES.compile(replace(ORDER, policy='reduce_only_ioc', reduce_only=True, side='sell'), **PARAMS)
    assert encoded['order_type'] == 0 and encoded['time_in_force'] == 0
    assert encoded['order_expiry'] == 0 and encoded['reduce_only'] is True and encoded['is_ask'] is True
    assert encoded['price'] == 230012
    with pytest.raises(AdapterError, match='POLICY_INVALID'):
        RULES.compile(replace(ORDER, policy='reduce_only_ioc'), **PARAMS)


def test_nonunit_contract_multiplier_and_bool_integer_are_not_coerced():
    for metadata in (META | {'multiplier': '10'}, META | {'market_id': True}):
        with pytest.raises(AdapterError): LighterWireRules.from_native(metadata)
    with pytest.raises(AdapterError): RULES.compile(ORDER, **(PARAMS | {'nonce': True}))
