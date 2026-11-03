"""Finite mainnet leverage calibration; not a general account-settings API.

The caller owns the Bulk runtime/vault locks and checks its grant/deadline.
Only one BTC maximum-leverage setting is signed. An unknown send is retained
and never replayed. The original setting can be restored only while flat.
"""
import json
import os
import time
from decimal import Decimal
from pathlib import Path

from .base import AdapterError
from .bulk_wire import native_decimal, uint


class BulkLeverageCalibration:
    def __init__(self, *, path, owner, original, target, deadline_ms, read, sign, send,
                 clock_ms=lambda: time.time_ns()//1000000, nonce=lambda: time.time_ns()):
        self.path=Path(path);self.owner=owner
        self.original=native_decimal(original,negative=False)
        self.target=native_decimal(target,negative=False)
        if not Decimal(1)<=self.target<self.original<=Decimal(40):
            raise AdapterError('BULK_CALIBRATION_LEVERAGE_SCOPE')
        self.deadline_ms=uint(deadline_ms);self.read,self.sign,self.send=read,sign,send
        self.clock_ms,self.nonce=clock_ms,nonce
        self.state=json.loads(self.path.read_text()) if self.path.exists() else dict(
            owner=owner,original=str(self.original),target=str(self.target),deadline_ms=deadline_ms,steps={})
        if any(self.state[k]!=v for k,v in dict(owner=owner,original=str(self.original),
                target=str(self.target),deadline_ms=deadline_ms).items()):
            raise AdapterError('BULK_CALIBRATION_ORIGINAL_CHANGED')

    def save(self):
        temporary=self.path.with_suffix('.tmp')
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as f:
            json.dump(self.state,f,sort_keys=True);f.flush();os.fsync(f.fileno())
        os.replace(temporary,self.path)
        directory=os.open(self.path.parent,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)

    @staticmethod
    def flat_leverage(account):
        if (account.get('kind')!='MasterEOA' or account.get('parent')
                or account.get('positions')!=[] or account.get('openOrders')!=[]
                or account.get('pendingOnFill') or account.get('lockedAmounts')):
            raise AdapterError('BULK_CALIBRATION_FLAT_ACCOUNT_REQUIRED')
        settings=account.get('leverageSettings')
        if not isinstance(settings,list):raise AdapterError('BULK_CALIBRATION_SETTINGS_UNVERIFIED')
        rows=[x for x in settings if x.get('symbol')=='BTC-USD']
        if len(rows)!=1:raise AdapterError('BULK_CALIBRATION_SETTINGS_UNVERIFIED')
        return native_decimal(rows[0]['leverage'],negative=False)

    @staticmethod
    def accepted(response):
        try:
            statuses=response['response']['data']['statuses']
            return (response['status']=='ok' and response['response']['type']=='order'
                and isinstance(statuses,list) and len(statuses)==1 and isinstance(statuses[0],dict)
                and len(statuses[0])==1 and not any(
                    word in next(iter(statuses[0])).lower() for word in ('error','reject','fail')))
        except (KeyError,TypeError):return False

    async def apply(self, *, restore=False):
        key='restore' if restore else 'set'
        desired=self.original if restore else self.target
        previous=self.target if restore else self.original
        if self.clock_ms()>self.deadline_ms:raise AdapterError('BULK_CALIBRATION_DEADLINE')
        before=await self.read();actual=self.flat_leverage(before)
        row=self.state['steps'].get(key)
        if row:
            # A second invocation can inspect, never sign or send again.
            if row.get('acknowledged') is True and actual==desired:
                row['observed']=True;self.save();return True
            raise AdapterError('BULK_CALIBRATION_ORIGINAL_MUST_BE_QUERIED')
        if restore and not self.state['steps'].get('set',{}).get('observed'):
            raise AdapterError('BULK_CALIBRATION_SET_UNCONFIRMED')
        if actual!=previous:raise AdapterError('BULK_CALIBRATION_SETTINGS_CHANGED')
        nonce=uint(self.nonce());signed=self.sign(float(desired),nonce)
        expected=[{'updateUserSettings':{'m':{'BTC-USD':float(desired)}}}]
        if (set(signed)!={'actions','nonce','account','signer','signature'}
                or signed['actions']!=expected or signed['account']!=self.owner
                or signed['signer']!=self.owner or signed['nonce']!=nonce
                or not isinstance(signed['signature'],str) or not signed['signature']):
            raise AdapterError('BULK_CALIBRATION_SIGNING_SCOPE')
        if self.flat_leverage(await self.read())!=previous or self.clock_ms()>self.deadline_ms:
            raise AdapterError('BULK_CALIBRATION_PRE_SEND_CHANGED')
        # Fsync before I/O; even timeout/cancellation cannot authorize replay.
        row=dict(nonce=nonce,desired=str(desired),state='MAY_HAVE_SENT',acknowledged=False,observed=False)
        self.state['steps'][key]=row;self.save()
        response=await self.send(signed)
        row['acknowledged']=self.accepted(response)
        row['state']='ACKNOWLEDGED' if row['acknowledged'] else 'RESPONSE_UNCONFIRMED';self.save()
        if not row['acknowledged']:raise AdapterError('BULK_CALIBRATION_RESPONSE_UNCONFIRMED')
        # Read-only retries are the coordinator's responsibility; no re-send.
        if self.flat_leverage(await self.read())!=desired:
            raise AdapterError('BULK_CALIBRATION_UPDATE_NOT_OBSERVED')
        row['observed']=True;self.save();return True
