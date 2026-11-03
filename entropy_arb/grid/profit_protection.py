"""Server ledger TP/SL. Fresh evidence freezes the epoch before closeout."""
import json
import time
from decimal import Decimal, localcontext

from . import models


REACHED = ('PROFIT_TAKE_REACHED', 'PROFIT_LOSS_REACHED')


def clock_ms(venue):
    return venue.clock_ms() if hasattr(venue, 'clock_ms') else int(time.time() * 1000)


class ProfitProtectionManager:
    def __init__(self, runtime):
        self.rt = runtime
        self.observations = {}

    def event_seq(self):
        return self.rt.store.read(lambda c: c.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0])

    def evaluate(self, grid_id, observation):
        self.observations.pop(grid_id, None)
        cfg = self.rt.config(grid_id)
        rule = cfg.profit_protection
        if rule is None:
            return None
        venue = self.rt.venue_for(cfg.account_ref, cfg.market_ref)
        stamp = models.parse_iso_ts(observation['market_ts'], 'profit_quote')
        age = clock_ms(venue) - int(stamp.timestamp() * 1000)
        if not -1000 <= age < 5000:
            return {'code': 'RISK_PROFIT_QUOTE_STALE'}
        before = self.event_seq()
        unknown_costs = self.rt.store.read(lambda c: c.execute(
            'SELECT 1 FROM executions e JOIN execution_owners o USING(execution_key) '
            'WHERE o.grid_id=? AND (e.fee_amount IS NULL OR e.fee_asset IS NULL OR e.fee_asset!=?) LIMIT 1',
            (grid_id, cfg.capital.asset)).fetchone())
        if unknown_costs:
            return {'code': 'RISK_PROFIT_DATA_UNAVAILABLE'}
        perf = self.rt.performance(grid_id, mark=observation['mark'])
        if perf['reconciliation_status'] != 'booked':
            return {'code': 'RISK_PROFIT_DATA_UNAVAILABLE'}
        first = self.rt.store.read(lambda c: c.execute(
            'SELECT config_json FROM grid_versions WHERE grid_id=? ORDER BY revision LIMIT 1', (grid_id,)).fetchone()[0])
        denominator = json.loads(first)['capital']['total']
        if self.event_seq() != before:
            return {'code': 'RISK_OBSERVATION_CHANGED_WAIT'}
        with localcontext() as context:
            context.prec = 50
            value = Decimal(perf['net_pnl'])
            if rule.basis == 'roi':
                value = value / Decimal(denominator) * 100
        code = None
        if rule.stop_loss is not None and value <= Decimal(rule.stop_loss):
            code = 'PROFIT_LOSS_REACHED'
        elif rule.take_profit is not None and value >= Decimal(rule.take_profit):
            code = 'PROFIT_TAKE_REACHED'
        result = {'code': code, 'basis': rule.basis,
                  'value': models.canonical_decimal_text(value), 'net_pnl': perf['net_pnl'],
                  'denominator': denominator, 'denominator_rule': 'initial_allocation',
                  'event_seq': before, 'market_ts': observation['market_ts'],
                  'mark': observation['mark'], 'epoch': self.rt.lifecycle._grid(grid_id)['epoch']}
        self.observations[grid_id] = result
        return result

    async def request_exit(self, grid_id, *, epoch, code):
        evidence = self.observations.get(grid_id)
        if not evidence or evidence['epoch'] != epoch or evidence['code'] != code:
            return None
        cfg = self.rt.config(grid_id)
        venue = self.rt.venue_for(cfg.account_ref, cfg.market_ref)
        stamp = models.parse_iso_ts(evidence['market_ts'], 'profit_quote')
        if not -1000 <= clock_ms(venue) - int(stamp.timestamp() * 1000) < 5000:
            return None
        return await self.rt.protection.request_exit(grid_id, expected_epoch=epoch,
            reason=code, include_paused=True, evidence=evidence,
            evidence_fresh=lambda: -1000 <= clock_ms(venue) - int(stamp.timestamp() * 1000) < 5000)
