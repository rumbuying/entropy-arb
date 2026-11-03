"""Native Bulk risk grid orientation, decay and shared portfolio math."""
import copy
from decimal import Decimal as D, localcontext
import gzip,json
from pathlib import Path

import pytest
from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.adapters.bulk_risk import BulkRiskSurfaces,RiskPoint,RiskGrid,correlation_norm,portfolio_maintenance


def grid():
    return dict(regime=-10,notionals=['100','300'],leverage=['1','3'],
        buy=[[dict(mmrO=x,mmrE='0.02',p='0.5') for x in row] for row in [['0.1','0.2'],['0.3','0.4']]],
        sell=[[dict(mmrO=x,mmrE='0.02',p='0.5') for x in row] for row in [['0.2','0.3'],['0.4','0.5']]])


def test_bilinear_uses_notional_rows_leverage_columns_then_decays_native_regime():
    g=RiskGrid.parse(grid())
    assert g.rate(side='buy',notional='200',leverage='2',elapsed=0)==D('.25')
    assert g.rate(side='buy',notional='200',leverage='2',elapsed=1)==D('.135')
    assert g.rate(side='sell',notional='200',leverage='2',elapsed=0)==D('.35')
    assert RiskPoint.parse(dict(mmrO='0.1',mmrE='0.02',p='0')).at(0)==D('.1')
    assert RiskPoint.parse(dict(mmrO='0.1',mmrE='0.02',p='0')).at(1)==D('.02')


@pytest.mark.parametrize('notional,leverage',[('99','2'),('301','2'),('200','0.9'),('200','3.1')])
def test_no_extrapolation_or_silent_clamp_into_native_grid(notional,leverage):
    with pytest.raises(AdapterError,match='OUTSIDE_SURFACE'):
        RiskGrid.parse(grid()).rate(side='buy',notional=notional,leverage=leverage,elapsed=0)


def test_range_bound_covers_interior_peak_and_equilibrium_not_just_outer_corners():
    row=grid();row['notionals']=['100','200','300'];row['leverage']=['1','2','3']
    cells=[[dict(mmrO='0.01',mmrE='0.02',p='0.5') for _ in range(3)] for _ in range(3)]
    cells[1][1]=dict(mmrO='0.1',mmrE='0.8',p='0.5')
    row['buy']=cells;row['sell']=copy.deepcopy(cells)
    g=RiskGrid.parse(row)
    bound=g.rate_upper_bound(side='buy',notional_min='150',notional_max='250',leverage_min='1.5',leverage_max='2.5')
    assert bound==D('.8')
    for n in ['150','175','200','225','250']:
        for l in ['1.5','1.75','2','2.25','2.5']:
            for elapsed in (0,1,10,100):
                assert g.rate(side='buy',notional=n,leverage=l,elapsed=elapsed)<=bound
    # A single-point window uses interpolation, not the whole surface maximum.
    assert g.rate_upper_bound(side='buy',notional_min='150',notional_max='150',
        leverage_min='1.5',leverage_max='1.5')==D('.215')


@pytest.mark.parametrize('patch',[
    dict(notional_min='99'),dict(notional_max='301'),dict(leverage_min='0.9'),
    dict(leverage_max='3.1'),dict(notional_min='250',notional_max='150'),dict(side='other')])
def test_range_bound_refuses_uncovered_or_reversed_domain(patch):
    with pytest.raises(AdapterError):
        RiskGrid.parse(grid()).rate_upper_bound(**(dict(side='buy',notional_min='100',notional_max='300',
            leverage_min='1',leverage_max='3')|patch))


def test_all_regime_bound_uses_published_surfaces_without_promoting_small_accounts():
    native=json.loads(gzip.decompress((Path(__file__).parent/'fixtures/grid_bulk/risk-mainnet-20261025.json.gz').read_bytes()))
    for coin,row in native.items():
        surface=BulkRiskSurfaces.parse(row,expected_symbol=coin)
        window=dict(side='buy',notional_min='50000',notional_max='100000',leverage_min='1',leverage_max='2')
        bound=surface.rate_upper_bound(**window)
        for g in surface.grids:
            for elapsed in (0,1,100):
                assert g.rate(side='buy',notional='75000',leverage='1.5',elapsed=elapsed)<=bound
        with pytest.raises(AdapterError,match='OUTSIDE_SURFACE'):
            surface.rate_upper_bound(**(window|dict(notional_min='1')))


def test_range_bound_rounds_up_exact_weights_even_with_low_caller_precision():
    from fractions import Fraction
    row=grid();row['notionals']=['100','400'];row['leverage']=['1','4']
    row['buy']=[[dict(mmrO='0',mmrE='0',p='1'),dict(mmrO='1',mmrE='1',p='1')]]*2
    with localcontext() as ctx:
        ctx.prec=6
        bound=RiskGrid.parse(row).rate_upper_bound(side='buy',notional_min='100',notional_max='400',
            leverage_min='2',leverage_max='2')
    assert Fraction(bound)>=Fraction(1,3)
    assert Fraction(bound)-Fraction(1,3)<Fraction(1,10**59)


@pytest.mark.parametrize('field,value',[('mmrO','1.01'),('mmrE','-0.1'),('p','NaN'),('p',0.5)])
def test_non_exact_or_invalid_probability_refused(field,value):
    row=dict(mmrO='0.1',mmrE='0.02',p='0.5');row[field]=value
    with pytest.raises(AdapterError):RiskPoint.parse(row)


def test_captured_current_mainnet_surfaces_keep_notional_curve_not_documented_margin_curve():
    p=Path(__file__).parent/'fixtures/grid_bulk/risk-mainnet-20261025.json.gz'
    native=json.loads(gzip.decompress(p.read_bytes()))
    for market,row in native.items():
        parsed=BulkRiskSurfaces.parse(row,expected_symbol=market)
        assert parsed.leverage_basis=='notional' and parsed.leverage_start==D('250000')
        assert len(parsed.grids)==9 and parsed.live_regime in (-10,0)
        assert parsed.grid(parsed.live_regime).rate(side='buy',notional='50000',leverage='1',elapsed=0)==D('.02')
        wrong=copy.deepcopy(row);wrong['leverage']['margin_0']='0'
        with pytest.raises(AdapterError,match='BASIS_UNVERIFIED'):BulkRiskSurfaces.parse(wrong,expected_symbol=market)
        with pytest.raises(AdapterError,match='SCOPE_CONFLICT'):BulkRiskSurfaces.parse(row,expected_symbol='OTHER-USD')


@pytest.mark.parametrize('patch',[{'regime':True},{'notionals':['100','100']},{'buy':[[dict(mmrO='.1',mmrE='.1',p='.5')]]}])
def test_malformed_grid_cannot_supply_margin(patch):
    with pytest.raises(AdapterError):RiskGrid.parse(grid()|patch)


def test_shared_portfolio_uses_signed_correlation_and_explicit_liquidation_reserve():
    corr={('BTC','ETH'):'0.7'}
    notionals={'BTC':'100','ETH':'-100'}
    rates={'BTC':'0.02','ETH':'0.02'}
    with localcontext() as c:
        c.prec=60;expected=D('2.4').sqrt()+D('.6')
        assert correlation_norm(notionals,corr)==D(6000).sqrt()
        actual=portfolio_maintenance(notionals,rates,corr,
            cascade_bps={'BTC':'10','ETH':'20'},reserve_floor_bps={'BTC':'50','ETH':'0'},
            reserve_factor={'BTC':'0','ETH':'1'})
        assert actual==expected
    with pytest.raises(AdapterError,match='RESERVE_UNVERIFIED'):
        portfolio_maintenance(notionals,rates,corr,cascade_bps={},reserve_floor_bps={},reserve_factor={})


def test_missing_or_impossible_correlations_cannot_grant_a_hedge_discount():
    with pytest.raises(AdapterError,match='MISSING'):correlation_norm({'BTC':'100','ETH':'100'},{})
    with pytest.raises(AdapterError,match='NOT_PSD'):
        correlation_norm({'A':'1','B':'1','C':'1'},{('A','B'):'0.9',('A','C'):'0.9',('B','C'):'-0.9'})
    assert correlation_norm({'A':'1','B':'-1'},{('A','B'):'1'})==0


def native_impact():
    return json.loads(gzip.decompress((Path(__file__).parent/'fixtures/grid_bulk/impact-ws-mainnet-20261025.json.gz').read_bytes()))


def test_current_impact_stream_scope_nanosecond_freshness_and_closing_side():
    from entropy_arb.grid.adapters.bulk_risk import BulkImpactObservation
    row=native_impact();observed=BulkImpactObservation.from_stream(row,expected_symbol='ETH-USD')
    assert observed.timestamp_ns==1792933290003587076
    bounds=observed.closing_bounds(position_qty='0.01',clock_ns=observed.timestamp_ns)
    assert bounds['closing_side']=='sell' and bounds['full_bps']=='0.6242' and bounds['book_bps']=='0.1284'
    short=observed.closing_bounds(position_qty='-0.01',clock_ns=observed.timestamp_ns)
    assert short['closing_side']=='buy' and short['full_bps']=='0.6758'
    for clock in (observed.timestamp_ns+16_000_000_000,observed.timestamp_ns-2_000_000_000):
        with pytest.raises(AdapterError,match='STALE'):
            observed.closing_bounds(position_qty='0.01',clock_ns=clock)
    with pytest.raises(AdapterError,match='SCOPE_CONFLICT'):
        BulkImpactObservation.from_stream(row,expected_symbol='BTC-USD')
    with pytest.raises(AdapterError,match='SIZE_UNCOVERED'):
        observed.closing_bounds(position_qty='1000000000',clock_ns=observed.timestamp_ns)


def test_impact_monotonic_upper_knot_is_not_linear_extrapolation_or_a_zero_default():
    from entropy_arb.grid.adapters.bulk_risk import ImpactCurve
    curve=ImpactCurve.parse(dict(logMin='0',logRange='24.9',bps=[str(i) for i in range(250)]))
    assert curve.upper_bound_bps('1')==0
    assert curve.upper_bound_bps('1.5')==5
    assert curve.upper_bound_bps('2.718281828459045')==10
    assert curve.upper_bound_bps('2.8')==11
    changed=dict(logMin='0',logRange='24.9',bps=[str(i) for i in range(250)])
    changed['bps'][5]='0'
    with pytest.raises(AdapterError,match='CURVE_INVALID'):ImpactCurve.parse(changed)
