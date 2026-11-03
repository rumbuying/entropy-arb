"""One protected, finite funding-boundary observation; never an ordinary grid grant."""
from dataclasses import asdict,dataclass

from .base import AdapterError
from .bulk_protection import verify_fixed_stop
from .bulk_roundtrip import BulkProtectedRoundtrip,RoundtripAuthority
from .bulk_wire import native_decimal,text,uint

HOUR_MS=3600000

@dataclass(frozen=True)
class FundingObservationAuthority(RoundtripAuthority):
    mode:str='protected_funding_observation'
    funding_at_ms:int=0

    def validate(self,now_ms,*,opening=False):
        data=asdict(self);data.pop('funding_at_ms')
        if data['mode']!='protected_funding_observation':
            raise AdapterError('BULK_FUNDING_OBSERVATION_AUTHORITY_REQUIRED')
        data['mode']='protected_roundtrip'
        RoundtripAuthority(**data).validate(now_ms,opening=opening)
        boundary=uint(self.funding_at_ms)
        if (boundary%HOUR_MS or not self.approved_ms+60000<=boundary<=self.approved_ms+360000
                or boundary+30000>self.expires_ms):
            raise AdapterError('BULK_FUNDING_OBSERVATION_WINDOW_INVALID')


class BulkFundingObservation(BulkProtectedRoundtrip):
    async def save(self,state):
        if state.get('phase')=='ARM' and 'funding_baseline' not in state:
            # Fixed before either the stop or entry can be submitted; persisted
            # once so a restart cannot reset the economic baseline.
            account,qty,started=await self.observe()
            if qty!=0 or account['openOrders']:
                raise AdapterError('BULK_FUNDING_OBSERVATION_BASELINE_NOT_FLAT')
            state['funding_baseline']=dict(observed_from_ms=started,
                funding=text(native_decimal(account['margin']['funding'])),
                fees=text(native_decimal(account['margin']['fees'])))
            state['funding_observations']=[]
        await super().save(state)

    async def hold_position(self,state,account,qty,observed_from_ms):
        if state.get('phase') in ('EXIT','PROTECTED_RESIDUAL') or state.get('exits') or state.get('funding_observation_finished'):
            return False
        now=self.clock_ms()
        end=min(self.authority.funding_at_ms+30000,self.authority.expires_ms-30000)
        if now>=end:
            state['funding_observation_finished']=True
            state['funding_observation_end_ms']=now
            await self.save(state);return False
        try:
            # Original-order and fill queries can outlive the earlier account
            # snapshot. Re-read rather than weakening the five-second proof.
            account,current_qty,observed_from_ms=await self.observe()
            now=self.clock_ms()
            if current_qty!=qty or now>=end:
                state['funding_observation_finished']=True
                await self.save(state);return False
            self.authority.validate(now)
            verify_fixed_stop(binding=self.binding(),account=self.authority.account,market=self.wire.market_ref,
                native_positions=account['positions'],native_open_orders=account['openOrders'],owned_qty=text(qty),
                pending=[],bindings=self.journal.bindings('BTC-USD'),observed_from_ms=observed_from_ms,now_ms=now)
            positions=[p for p in account['positions'] if p['symbol']=='BTC-USD']
            if len(positions)!=1:raise AdapterError('BULK_FUNDING_OBSERVATION_POSITION_UNVERIFIED')
            sample=dict(at_ms=now,qty=text(qty),stop_present=True,
                account_funding=text(native_decimal(account['margin']['funding'])),
                position_funding=text(native_decimal(positions[0]['funding'])),
                account_fees=text(native_decimal(account['margin']['fees'])))
        except (AdapterError,KeyError,TypeError,ValueError):
            # Lost observation/protection evidence stops the hold. It cannot
            # authorize another entry or defer the existing reduce-only exit.
            state['funding_observation_finished']=True
            state['funding_observation_reason']='BULK_FUNDING_OBSERVATION_EVIDENCE_UNAVAILABLE'
            await self.save(state);return False
        samples=state['funding_observations']
        if len(samples)>=256:
            state['funding_observation_finished']=True
            state['funding_observation_reason']='BULK_FUNDING_OBSERVATION_SAMPLE_LIMIT'
            await self.save(state);return False
        samples.append(sample)
        state['funding_boundary_observed']=any(s['at_ms']<self.authority.funding_at_ms for s in samples) and any(
            s['at_ms']>=self.authority.funding_at_ms for s in samples)
        await self.save(state)
        return True
