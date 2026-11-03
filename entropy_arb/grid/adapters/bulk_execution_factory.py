"""Execution-capable Bulk account factory for the ordinary grid engine.

Assembles the already-verified signer, order transport, portfolio budget and
public risk feed from the saved vault record, inside the venue worker process
(the only grid writer). Assembly alone grants nothing: write_enabled and
live_qualified stay False until the account baseline is captured and the
operator trial scope is armed, and every send still passes the executor
authorities, the executor dispatch gate and the policy wire fence.
"""
import copy
import time

from .base import AdapterError
from .bulk_budget import BulkAccountBudget
from .bulk_execution import BulkOrderTransport
from .bulk_identity import BulkSdkStopIdentity
from .bulk_market import BulkMarketTransport
from .bulk_risk_feed import BulkRiskFeed
from .bulk_wire import BulkKeychain


def execution_venue(record, *, store, session, symbols, execution_gate, clock_ms=None):
    """Build the full execution stack for one saved Bulk account revision.

    ``record`` is the complete vault row including credentials. The caller
    owns the session, the store writer role and the returned venue's stream
    and risk-feed lifecycle.
    """
    from .bulk import BulkAccountVenue
    if (not isinstance(record, dict) or record.get('venue') != 'bulk'
            or not isinstance(record.get('credentials'), dict)
            or not isinstance(record['credentials'].get('secret_key'), str)
            or not record['credentials']['secret_key']):
        raise AdapterError('BULK_EXECUTION_RECORD_SCOPE_CONFLICT')
    network = record.get('network')
    if network != 'mainnet':
        raise AdapterError('BULK_NETWORK_UNSUPPORTED')
    owner = record.get('account_address')
    clock = clock_ms or (lambda: time.time_ns() // 1_000_000)
    reads = BulkMarketTransport(session, network=network)
    protocol = BulkKeychain(network=network, owner=owner,
        secret=record['credentials']['secret_key'],
        native_identity=BulkSdkStopIdentity(network=network))
    sender = BulkOrderTransport(session, network=network, owner=owner)
    budget = BulkAccountBudget(account=record['account_id'], network=network,
        credential_revision=record['revision'],
        credential_ref=record['credential_ref'], clock_ms=clock)
    feed = BulkRiskFeed(session, network=network, symbols=tuple(symbols))
    venue = BulkAccountVenue(reads, store=store, owner=owner, network=network,
        credential_revision=record['revision'],
        credential_ref=record['credential_ref'], history_start_slot=0,
        protocol=protocol, order_transport=sender, execution_gate=execution_gate,
        risk=budget, risk_feed=feed, clock_ms=clock)
    if venue._account.account_id != record['account_id']:
        raise AdapterError('BULK_EXECUTION_ACCOUNT_SCOPE_CONFLICT')
    venue.preview_capabilities = dict(directions=['long'], margin_modes=['cross'],
        spacings=['arithmetic', 'geometric'], execution_policies=['gtc', 'post_only'],
        protection_modes=['native'])
    return venue


async def attach_execution_venue(runtime, vault, trial, session, *, market_ref):
    """Replace the saved account's preview bridge with the execution factory.

    Only the account/credential revision named by the loaded operator policy
    is attached; the immutable flat-account baseline is captured first, and
    only then are the qualification flags armed. Returns the armed account
    venue. Refuses to replace an already execution-capable venue.
    """
    if vault is None:
        raise AdapterError('BULK_EXECUTION_VAULT_UNAVAILABLE')
    row = vault.records.get(trial.policy['account_id'])
    if (row is None or row.get('venue') != 'bulk' or row.get('revision') != trial.policy['credential_revision']):
        raise AdapterError('BULK_EXECUTION_ACCOUNT_UNAUTHORIZED')
    aid = row['account_id']
    old = runtime.venues.get(aid)
    if old is not None and not getattr(old, 'preview_only', False):
        raise AdapterError('BULK_EXECUTION_REPLACEMENT_REFUSED')
    symbols = []
    for market in trial.policy['markets']:
        parts = market.split('|')
        if (len(parts) != 5 or parts[0] != 'bulk:' + row['network'] or parts[3:] != ['linear_perp', 'USDC']):
            raise AdapterError('BULK_EXECUTION_MARKET_SCOPE_CONFLICT')
        if parts[2] not in symbols:
            symbols.append(parts[2])
    gate = getattr(trial, 'execution_gate', None)
    if not callable(gate):
        raise AdapterError('BULK_EXECUTION_GATE_UNCONFIGURED')
    venue = execution_venue(row, store=runtime.store, session=session,
        symbols=symbols, execution_gate=gate)
    identity = await venue.resolve_account()
    if (identity.account_id != aid or identity.owner != row['account_address']
            or identity.deployment != 'bulk:' + row['network']
            or identity.credential_revision != row['revision']
            or venue.credential_ref != row['credential_ref']
            or not venue.protocol or venue.order_transport is None
            or venue.risk_feed is None or getattr(venue, 'preview_only', False)
            or venue.write_enabled or venue.live_qualified):
        raise AdapterError('BULK_EXECUTION_SCOPE_INVALID')
    venue.display_name = row.get('name', aid)
    if old is not None:
        runtime.venues.pop(aid)
        runtime.executors.pop(aid, None)
    await runtime.add_venue(venue)
    try:
        baseline = await venue.capture_recovery_baseline(market_ref)
    except BaseException:
        # The preview bridge was replaced; leaving an unsigned bridge would
        # misrepresent the account. The worker process fails startup instead.
        runtime.venues.pop(aid, None)
        runtime.executors.pop(aid, None)
        raise
    venue.history_start_slot = baseline['anchor']['as_of_slot']
    venue.execution_qualified = True
    venue.write_enabled = True
    venue.live_qualified = True
    for view in venue.views.values():
        view.live_qualified = True
    return venue
