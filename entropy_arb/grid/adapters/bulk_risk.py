"""Exact native Bulk risk surfaces and portfolio math, not a live permit.

Sources: official Margin, margin-calculator and getRiskSurfaces documentation;
the native [notional][leverage] grids and regime decay are preserved. Reserve,
correlation-discount and closeout inputs must come from independently checked
native evidence. This module never substitutes availableMargin for capacity.
"""
from dataclasses import dataclass
from decimal import Decimal, localcontext, ROUND_CEILING
from fractions import Fraction
import re

from .base import AdapterError
from .bulk_market import symbol
from .bulk_wire import native_decimal, uint, text


def amount(value, *, positive=False):
    value=native_decimal(value,negative=False)
    if positive and value<=0:raise AdapterError('BULK_RISK_AMOUNT_INVALID')
    return value


def regime(value):
    if type(value) is not int or value not in (-12,-11,-10,0,1,2,10,11,12):
        raise AdapterError('BULK_RISK_REGIME_UNVERIFIED')
    return value


@dataclass(frozen=True)
class RiskPoint:
    origin: Decimal
    equilibrium: Decimal
    persistence: Decimal

    @classmethod
    def parse(cls,row):
        values=[amount(row[k]) for k in ('mmrO','mmrE','p')]
        if any(v>1 for v in values):raise AdapterError('BULK_RISK_POINT_INVALID')
        return cls(*values)

    def at(self,elapsed):
        elapsed=uint(elapsed,2**32-1)
        with localcontext() as ctx:
            ctx.prec=60
            weight=Decimal(1) if elapsed==0 else self.persistence**elapsed
            return self.equilibrium+(self.origin-self.equilibrium)*weight


def knots(rows):
    if not isinstance(rows,list) or not 2<=len(rows)<=1000:
        raise AdapterError('BULK_RISK_KNOTS_INVALID')
    out=tuple(amount(v) for v in rows)
    if any(a>=b for a,b in zip(out,out[1:])):
        raise AdapterError('BULK_RISK_KNOTS_INVALID')
    return out


def bracket(knots,value):
    # No invented extrapolation beyond the native grid, including its lower
    # edge. A separate verified conservative-bound policy is needed there.
    value=amount(value)
    if value<knots[0] or value>knots[-1]:raise AdapterError('BULK_RISK_OUTSIDE_SURFACE')
    for i in range(len(knots)-1):
        if value<=knots[i+1]:return i,(value-knots[i])/(knots[i+1]-knots[i])
    raise AdapterError('BULK_RISK_OUTSIDE_SURFACE')


@dataclass(frozen=True)
class RiskGrid:
    regime: int
    leverage: tuple
    notionals: tuple
    buy: tuple
    sell: tuple

    @classmethod
    def parse(cls,row):
        leverage,notionals=knots(row['leverage']),knots(row['notionals'])
        grids=[]
        for side in ('buy','sell'):
            rows=row[side]
            if (not isinstance(rows,list) or len(rows)!=len(notionals)
                    or any(not isinstance(r,list) or len(r)!=len(leverage) for r in rows)):
                raise AdapterError('BULK_RISK_GRID_DIMENSIONS_INVALID')
            grids.append(tuple(tuple(RiskPoint.parse(p) for p in r) for r in rows))
        return cls(regime(row['regime']),leverage,notionals,*grids)

    def rate(self,*,side,notional,leverage,elapsed):
        if side not in ('buy','sell'):raise AdapterError('BULK_RISK_SIDE_INVALID')
        with localcontext() as ctx:
            ctx.prec=60
            ni,nw=bracket(self.notionals,notional);li,lw=bracket(self.leverage,leverage)
            rows=self.buy if side=='buy' else self.sell
            lower=rows[ni][li].at(elapsed)*(1-lw)+rows[ni][li+1].at(elapsed)*lw
            upper=rows[ni+1][li].at(elapsed)*(1-lw)+rows[ni+1][li+1].at(elapsed)*lw
            return lower*(1-nw)+upper*nw

    def rate_upper_bound(self,*,side,notional_min,notional_max,leverage_min,leverage_max):
        """Bound every bilinear cell and every decay time in a native box.

        Checking only the box's four outer corners misses interior peaks in a
        nonmonotone risk surface. Each clipped native cell has its own corners.
        At a knot, decay is a convex combination of origin and equilibrium, so
        their maximum bounds every elapsed time, including persistence 0/1.
        This is a maintenance-rate bound, never an order-admission decision.
        """
        if side not in ('buy','sell'):raise AdapterError('BULK_RISK_SIDE_INVALID')
        with localcontext() as ctx:
            ctx.prec=60
            nlo,nhi=amount(notional_min),amount(notional_max)
            llo,lhi=amount(leverage_min),amount(leverage_max)
            if nlo>nhi or llo>lhi:raise AdapterError('BULK_RISK_BOUND_WINDOW_INVALID')
            # Validate both edges; do not extrapolate the small-account region.
            for x in (nlo,nhi):bracket(self.notionals,text(x))
            for x in (llo,lhi):bracket(self.leverage,text(x))
            ns=sorted({nlo,nhi,*(x for x in self.notionals if nlo<x<nhi)})
            ls=sorted({llo,lhi,*(x for x in self.leverage if llo<x<lhi)})
            grid=self.buy if side=='buy' else self.sell
            bound=Fraction(0)
            for n in ns:
                ni,_=bracket(self.notionals,text(n))
                nw=(Fraction(n)-Fraction(self.notionals[ni]))/(Fraction(self.notionals[ni+1])-Fraction(self.notionals[ni]))
                for l in ls:
                    li,_=bracket(self.leverage,text(l))
                    lw=(Fraction(l)-Fraction(self.leverage[li]))/(Fraction(self.leverage[li+1])-Fraction(self.leverage[li]))
                    def high(i,j):return Fraction(max(grid[i][j].origin,grid[i][j].equilibrium))
                    lower=high(ni,li)*(1-lw)+high(ni,li+1)*lw
                    upper=high(ni+1,li)*(1-lw)+high(ni+1,li+1)*lw
                    bound=max(bound,lower*(1-nw)+upper*nw)
            # Exact rational weights avoid rounding a conservative bound down.
            ctx.rounding=ROUND_CEILING
            return Decimal(bound.numerator)/Decimal(bound.denominator)


@dataclass(frozen=True)
class BulkRiskSurfaces:
    symbol: str
    live_regime: int
    grids: tuple
    correlations: tuple
    leverage_min: Decimal
    leverage_max: Decimal
    leverage_basis: str
    leverage_start: Decimal
    leverage_half: Decimal

    @classmethod
    def parse(cls,row,*,expected_symbol):
        try:
            if row['symbol']!=symbol(expected_symbol):raise AdapterError('BULK_RISK_SCOPE_CONFLICT')
            grids=tuple(RiskGrid.parse(v) for v in row['surfaces'])
            codes=tuple(v.regime for v in grids)
            if len(grids)!=9 or codes!=tuple(sorted(set(codes))):
                raise AdapterError('BULK_RISK_REGIMES_INCOMPLETE')
            active=regime(row['liveRegime'])
            if active not in codes:raise AdapterError('BULK_RISK_REGIME_UNVERIFIED')
            correlations=[];seen=set()
            for pair,rho in row['corrs']:
                if not isinstance(pair,str) or not re.fullmatch('[A-Za-z0-9._-]+:[A-Za-z0-9._-]+',pair):
                    raise AdapterError('BULK_RISK_CORRELATION_INVALID')
                a,b=pair.split(':');key=tuple(sorted((a,b)));value=native_decimal(rho)
                if a==b or key in seen or abs(value)>1:raise AdapterError('BULK_RISK_CORRELATION_INVALID')
                correlations.append((key,value));seen.add(key)
            curve=row['leverage'];low,high=amount(curve['min'],positive=True),amount(curve['max'],positive=True)
            if low>high:raise AdapterError('BULK_RISK_LEVERAGE_INVALID')
            # Current mainnet uses notional_*, while OpenAPI describes margin_*.
            # Preserve the basis. Never apply one curve's formula to the other.
            bases=[b for b in ('notional','margin') if b+'_0' in curve or b+'_half' in curve]
            if len(bases)!=1:raise AdapterError('BULK_RISK_LEVERAGE_BASIS_UNVERIFIED')
            basis=bases[0];start=amount(curve[basis+'_0']);half=amount(curve[basis+'_half'],positive=True)
            return cls(expected_symbol,active,grids,tuple(sorted(correlations)),low,high,basis,start,half)
        except (KeyError,TypeError,ValueError,ArithmeticError):
            raise AdapterError('BULK_RISK_SURFACE_UNVERIFIED') from None

    def grid(self,code):
        code=regime(code)
        return next(g for g in self.grids if g.regime==code)

    def rate_upper_bound(self,**window):
        """Cover every published regime and decay, within every native grid."""
        return max(g.rate_upper_bound(**window) for g in self.grids)


def correlation_norm(components,correlations):
    """Exact signed portfolio norm; require a complete positive-semidefinite matrix."""
    keys=sorted(components)
    if not keys:return Decimal(0)
    values=[native_decimal(components[k]) for k in keys]
    with localcontext() as ctx:
        ctx.prec=60
        matrix=[]
        for a in keys:
            row=[]
            for b in keys:
                if a==b:value=Decimal(1)
                else:
                    try:value=native_decimal(correlations[tuple(sorted((a,b)))])
                    except KeyError:raise AdapterError('BULK_RISK_CORRELATION_MISSING') from None
                    if abs(value)>1:raise AdapterError('BULK_RISK_CORRELATION_INVALID')
                row.append(value)
            matrix.append(row)
        # LDL decomposition detects inconsistent pairwise correlations rather
        # than hiding a negative quadratic form with max(0,...).
        count=len(keys);lower=[[Decimal(0)]*count for _ in keys];diag=[]
        for i in range(count):
            pivot=matrix[i][i]-sum(lower[i][k]**2*diag[k] for k in range(i))
            if pivot<0:raise AdapterError('BULK_RISK_CORRELATION_NOT_PSD')
            diag.append(pivot);lower[i][i]=Decimal(1)
            for j in range(i+1,count):
                residual=matrix[j][i]-sum(lower[j][k]*lower[i][k]*diag[k] for k in range(i))
                if pivot==0:
                    if residual!=0:raise AdapterError('BULK_RISK_CORRELATION_NOT_PSD')
                    lower[j][i]=Decimal(0)
                else:lower[j][i]=residual/pivot
        squared=sum(values[i]*values[j]*matrix[i][j] for i in range(count) for j in range(count))
        if squared<0:raise AdapterError('BULK_RISK_CORRELATION_NOT_PSD')
        return squared.sqrt()


def portfolio_maintenance(signed_notionals,rates,correlations,*,cascade_bps,reserve_floor_bps,reserve_factor):
    """Documented maintenance component, not initial margin or full capacity.

    Every position needs explicit native reserve/impact inputs. Missing curves
    or unpublished reserve configuration must block the caller, never become 0.
    """
    with localcontext() as ctx:
        ctx.prec=60
        components={};reserve=Decimal(0)
        for key,value in signed_notionals.items():
            notional=native_decimal(value)
            try:
                rate=amount(rates[key]);impact=amount(cascade_bps[key])
                floor=amount(reserve_floor_bps[key]);factor=amount(reserve_factor[key])
            except KeyError:raise AdapterError('BULK_RISK_RESERVE_UNVERIFIED') from None
            if rate>1:raise AdapterError('BULK_RISK_RATE_INVALID')
            components[key]=text(notional*rate)
            all_in=max(impact*(1+factor),floor)
            reserve+=abs(notional)*(all_in-impact)/10000
        return correlation_norm(components,correlations)+reserve


@dataclass(frozen=True)
class ImpactCurve:
    log_min: Decimal
    log_range: Decimal
    bps: tuple

    @classmethod
    def parse(cls,row):
        log_min=native_decimal(row['logMin'])
        log_range=amount(row['logRange'],positive=True)
        values=row['bps']
        if not isinstance(values,list) or len(values)!=250:
            raise AdapterError('BULK_IMPACT_DIMENSIONS_INVALID')
        bps=tuple(amount(v) for v in values)
        if (abs(log_min)>100 or log_range>100 or any(v>10000 for v in bps)
                or any(a>b for a,b in zip(bps,bps[1:]))):
            raise AdapterError('BULK_IMPACT_CURVE_INVALID')
        return cls(log_min,log_range,bps)

    def upper_bound_bps(self,size):
        """Next published size knot, including the first knot for smaller sizes.

        A monotone published curve bounds smaller sizes by its first value;
        quantities beyond the final knot are refused, never capped to it.
        """
        from decimal import ROUND_CEILING
        size=amount(size,positive=True)
        with localcontext() as ctx:
            ctx.prec=60
            coordinate=(size.ln()-self.log_min)/self.log_range*249
            if coordinate>249:raise AdapterError('BULK_IMPACT_SIZE_UNCOVERED')
            index=max(0,int(coordinate.to_integral_value(rounding=ROUND_CEILING)))
            return self.bps[index]


@dataclass(frozen=True)
class BulkImpactObservation:
    symbol: str
    timestamp_ns: int
    full_buy: ImpactCurve
    full_sell: ImpactCurve
    book_buy: ImpactCurve
    book_sell: ImpactCurve
    protocol: str = 'bulk-ws-impact-full-book-ns-v1'

    @classmethod
    def from_stream(cls,message,*,expected_symbol):
        try:
            if (message['type']!='impact' or message['topic']!='impact.'+symbol(expected_symbol)
                    or message['data']['symbol']!=expected_symbol):
                raise AdapterError('BULK_IMPACT_SCOPE_CONFLICT')
            row=message['data']
            # This is the observed current WS contract, not the older HTTP
            # buyBps/sellBps/minSize schema. Nanoseconds are never rescaled.
            if set(row)!={'symbol','timestamp','full','book'}:
                raise AdapterError('BULK_IMPACT_SCHEMA_UNVERIFIED')
            for kind in ('full','book'):
                if set(row[kind])!={'buy','sell'}:raise AdapterError('BULK_IMPACT_SCHEMA_UNVERIFIED')
            curves=[ImpactCurve.parse(row[kind][side]) for kind in ('full','book') for side in ('buy','sell')]
            return cls(expected_symbol,uint(row['timestamp']),*curves)
        except (KeyError,TypeError,ValueError,ArithmeticError):
            raise AdapterError('BULK_IMPACT_RESPONSE_UNVERIFIED') from None

    def closing_bounds(self,*,position_qty,clock_ns,max_age_sec=15):
        max_age_sec=uint(max_age_sec,60)
        if (not max_age_sec or not -1_000_000_000<=uint(clock_ns)-self.timestamp_ns<=max_age_sec*1_000_000_000):
            raise AdapterError('BULK_IMPACT_STALE')
        position=native_decimal(position_qty)
        if position==0:raise AdapterError('BULK_IMPACT_POSITION_REQUIRED')
        full,book=(self.full_sell,self.book_sell) if position>0 else (self.full_buy,self.book_buy)
        size=text(abs(position))
        return dict(full_bps=text(full.upper_bound_bps(size)),book_bps=text(book.upper_bound_bps(size)),
            closing_side='sell' if position>0 else 'buy',timestamp_ns=self.timestamp_ns)
