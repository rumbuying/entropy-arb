"""Current unified scope from confirmed, continuous account subscriptions.

Snapshots do not prove fill history. A new connection, missing initial data,
wrong scope or missed heartbeat invalidates the entire observation. No stale
read-through cache is used after a disconnect.
"""
import copy
from .adapters.base import AdapterError


def economic_value(value):
    if isinstance(value, dict):
        return {key: economic_value(item) for key, item in value.items() if key != 'time'}
    if isinstance(value, list):
        return [economic_value(item) for item in value]
    return value


def layout_value(channel, value):
    # Price/UPNL/margin valuations change continuously and are sampled fresh;
    # they must not invalidate a five-second permit on every price tick.
    # Cash movements/funding are invalidated by their dedicated ledger feeds.
    if channel == 'allDexsClearinghouseState' and value is not None:
        return {name: [{key: row['position'].get(key) for key in ('coin', 'szi', 'entryPx', 'leverage')}
                       for row in state['assetPositions']] for name, state in value.items()}
    if channel == 'spotState' and value is not None:
        return [{key: balance.get(key) for key in ('coin', 'token') if key in balance}
                if balance.get('token') == 0 else balance for balance in value['balances']]
    return economic_value(value)


class UnifiedObservation:
    protocol = 'hl-continuous-unified-scope-v1'

    def __init__(self, account):
        self.account = account
        self.names = None
        self.generation = None
        self.revision = 0
        self.heartbeat = None
        self.acknowledged = set()
        self.sent = set()
        self.states = None
        self.spot = None
        self.orders = {}
        self.ledger_ready = set()

    def invalidate(self):
        self.generation = None
        self.heartbeat = None
        self.acknowledged.clear()
        self.sent.clear()
        self.states = self.spot = None
        self.orders.clear()
        self.ledger_ready.clear()
        self.revision += 1

    def begin(self, generation):
        self.invalidate()
        self.generation = generation

    def configure(self, names):
        if self.names != names:
            generation = self.generation
            self.invalidate()
            self.generation = generation
            self.names = list(names)

    def subscriptions(self):
        if self.names is None or self.generation is None:
            return []
        subscriptions = [{'type': 'allDexsClearinghouseState', 'user': self.account.owner},
                         {'type': 'spotState', 'user': self.account.owner},
                         {'type': 'userNonFundingLedgerUpdates', 'user': self.account.owner},
                         {'type': 'userFundings', 'user': self.account.owner}]
        subscriptions += [{'type': 'openOrders', 'user': self.account.owner, 'dex': name} for name in self.names]
        fresh = []
        for sub in subscriptions:
            key = (sub['type'], sub.get('dex'))
            if key not in self.sent:
                self.sent.add(key)
                fresh.append(sub)
        return fresh

    def acknowledge(self, message):
        if message.get('channel') != 'subscriptionResponse':
            return
        data = message.get('data', {})
        sub = data.get('subscription', {})
        key = (sub.get('type'), sub.get('dex'))
        if key not in self.sent or data.get('method') != 'subscribe':
            return
        if not isinstance(sub.get('user'), str) or sub['user'].lower() != self.account.owner:
            raise AdapterError('HL_OBSERVATION_ACCOUNT_MISMATCH')
        self.acknowledged.add(key)

    def ingest(self, message):
        try:
            return self._ingest(message)
        except AdapterError:
            self.invalidate()
            raise
        except (KeyError, TypeError, ValueError, AttributeError):
            self.invalidate()
            raise AdapterError('HL_OBSERVATION_MALFORMED') from None

    def _ingest(self, message):
        channel = message.get('channel')
        if channel not in ('allDexsClearinghouseState', 'spotState', 'openOrders', 'userNonFundingLedgerUpdates', 'userFundings'):
            return False
        data = message.get('data', {})
        if not isinstance(data, dict) or not isinstance(data.get('user'), str) or data['user'].lower() != self.account.owner:
            raise AdapterError('HL_OBSERVATION_ACCOUNT_MISMATCH')
        if channel in ('userNonFundingLedgerUpdates', 'userFundings'):
            field = 'nonFundingLedgerUpdates' if channel == 'userNonFundingLedgerUpdates' else 'fundings'
            updates = data.get(field)
            if not isinstance(updates, list) or any(not isinstance(row, dict) for row in updates):
                raise AdapterError('HL_OBSERVATION_LEDGER_MALFORMED')
            if data.get('isSnapshot') is True:
                self.ledger_ready.add(channel)
            elif updates:
                self.revision += 1
                self.account.private_revision += 1
            return True
        if channel == 'allDexsClearinghouseState':
            rows = data.get('clearinghouseStates')
            if (not isinstance(rows, list) or any(not isinstance(row, list) or len(row) != 2 for row in rows)
                    or len({row[0] for row in rows}) != len(rows)
                    or self.names is None or {row[0] for row in rows} != set(self.names)):
                raise AdapterError('HL_OBSERVATION_DEX_SCOPE_MISMATCH')
            new = {row[0]: row[1] for row in rows}
            old = self.states
            self.states = copy.deepcopy(new)
        elif channel == 'spotState':
            new = data.get('spotState')
            if not isinstance(new, dict) or not isinstance(new.get('balances'), list):
                raise AdapterError('HL_OBSERVATION_SPOT_MALFORMED')
            old = self.spot
            self.spot = copy.deepcopy(new)
        else:
            dex, new = data.get('dex'), data.get('orders')
            if self.names is None or dex not in self.names or not isinstance(new, list):
                raise AdapterError('HL_OBSERVATION_DEX_SCOPE_MISMATCH')
            old = self.orders.get(dex)
            self.orders[dex] = copy.deepcopy(new)
        if layout_value(channel, old) != layout_value(channel, new):
            self.revision += 1
            self.account.private_revision += 1
        return True

    def snapshot(self):
        if (self.names is None or self.generation != self.account.stream_generation
                or not self.account.stream_connected or self.heartbeat is None
                or not 0 <= self.account.stream_clock() - self.heartbeat <= 5
                or self.states is None or self.spot is None or set(self.orders) != set(self.names)
                or self.ledger_ready != {'userNonFundingLedgerUpdates', 'userFundings'}
                or self.acknowledged != self.sent or not self.sent):
            return None
        return {'names': list(self.names), 'states': copy.deepcopy(self.states), 'spot': copy.deepcopy(self.spot),
                'orders': copy.deepcopy(self.orders), 'generation': self.generation,
                'revision': self.revision, 'protocol': self.protocol}

    def matches(self, snapshot):
        current = self.snapshot()
        return current is not None and current['revision'] == snapshot['revision'] and current['generation'] == snapshot['generation']
