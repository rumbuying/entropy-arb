"""Bounded public OHLCV observations; never execution price evidence."""
from decimal import Decimal, InvalidOperation

from .models import canonical_decimal_text

INTERVALS = {"1m": 60_000, "5m": 300_000, "15m": 900_000,
             "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
LIMIT = 120


def request_window(interval, end_ms):
    if interval not in INTERVALS:
        raise ValueError("CANDLE_INTERVAL_INVALID")
    step = INTERVALS[interval]
    return (end_ms // step - LIMIT + 1) * step, end_ms


def normalize(rows, *, market, symbol, interval, start_ms, end_ms):
    step = INTERVALS[interval]
    if not isinstance(rows, list) or len(rows) > LIMIT + 1:
        raise ValueError("CANDLE_RESPONSE_INVALID")
    bars = {}
    for row in rows:
        try:
            if not isinstance(row, dict) or row['s'] != symbol or row['i'] != interval:
                raise ValueError()
            t, end = row['t'], row['T']
            if (type(t) is not int or type(end) is not int or t % step
                    or end != t + step - 1 or not start_ms <= t <= end_ms):
                raise ValueError()
            values = [Decimal(str(row[k])) for k in ('o', 'h', 'l', 'c', 'v')]
            o, h, l, c, v = values
            if (any(not x.is_finite() or len(str(x)) > 64 or abs(x.as_tuple().exponent) > 30 or abs(x.adjusted()) > 30 for x in values)
                    or min(o, h, l, c) <= 0 or v < 0 or not l <= min(o, c) <= max(o, c) <= h):
                raise ValueError()
            count = row['n']
            if type(count) is not int or not 0 <= count <= 10**12:
                raise ValueError()
            bar = dict(zip(('open', 'high', 'low', 'close', 'volume'),
                           map(canonical_decimal_text, values)))
            bar.update(open_ms=t, close_ms=end, trades=count, closed=end < end_ms)
            if t in bars and bars[t] != bar:
                raise ValueError()
            bars[t] = bar
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise ValueError("CANDLE_RESPONSE_INVALID") from error
    expected = range(start_ms, end_ms // step * step + 1, step)
    missing = [t for t in expected if t not in bars]
    return {'market': market, 'symbol': symbol, 'interval': interval,
            'source': 'hyperliquid:candleSnapshot', 'observed_ms': end_ms,
            'start_ms': start_ms, 'end_ms': end_ms, 'limit': LIMIT,
            'missing_open_ms': missing, 'complete_window': not missing,
            'candles': [bars[t] for t in sorted(bars)]}
