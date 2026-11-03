"""Economic account, signer delegation and agent expiry are separate facts.

extraAgents fields follow the pinned official SDK 0.24.0 Info.extra_agents.
Named agents with a verifiable validity boundary are required for execution;
an unmatched/unnamed agent can still be observed but is not armed.
"""
from dataclasses import dataclass

from .adapters.base import AdapterError
from .adapters.hyperliquid import _address, _ms


@dataclass(frozen=True)
class Authorization:
    owner: str
    master: str
    signer: str
    vault_address: str | None
    checked_ms: int
    valid_until_ms: int | None

    def to_dict(self):
        return {"owner": self.owner, "master": self.master, "signer": self.signer,
                "vault_address": self.vault_address, "checked_ms": self.checked_ms,
                "valid_until_ms": self.valid_until_ms}


async def check_authorization(venue, signer_address, *, validity_ms=20000):
    import asyncio
    signer = _address(signer_address)
    # Keep ownership/delegation/expiry evidence fresh for each signed action.
    # Unsigned info may use WS for latency, with HTTP and WS reads charged
    # against the same conservative local budget. Do not cache authorization.
    transport=getattr(venue,'transport',None)
    info=getattr(transport,'authorization_info',None) or venue._info
    if hasattr(transport,'authorization_roles'):
        owner_role,signer_role=await transport.authorization_roles(venue.owner,signer)
    else:
        owner_role, signer_role = await asyncio.gather(
            info("userRole", user=venue.owner), info("userRole", user=signer))
    if not isinstance(owner_role, dict) or not isinstance(signer_role, dict):
        raise AdapterError("HL_AUTHORIZATION_UNVERIFIED")
    if owner_role.get("role") == "user":
        master, vault = venue.owner, None
    elif owner_role.get("role") == "subAccount":
        master, vault = _address(owner_role.get("data", {}).get("master")), venue.owner
    else:
        raise AdapterError("HL_ACCOUNT_TYPE_UNSUPPORTED")
    now = _ms(venue.clock_ms())
    if signer_role.get("role") == "user" and signer == master:
        return Authorization(venue.owner, master, signer, vault, now, None)
    if signer_role.get("role") != "agent" or _address(signer_role.get("data", {}).get("user")) != master:
        raise AdapterError("HL_AGENT_NOT_AUTHORIZED")
    agents = await info("extraAgents", user=master)
    if not isinstance(agents, list):
        raise AdapterError("HL_AGENT_VALIDITY_UNVERIFIED")
    matches = [a for a in agents if isinstance(a, dict) and str(a.get("address", "")).lower() == signer]
    if len(matches) != 1 or not isinstance(matches[0].get("name"), str) or not matches[0]["name"]:
        raise AdapterError("HL_NAMED_AGENT_REQUIRED")
    valid_until = _ms(matches[0].get("validUntil"))
    if valid_until <= now + validity_ms:
        raise AdapterError("HL_AGENT_EXPIRED_OR_EXPIRING")
    return Authorization(venue.owner, master, signer, vault, now, valid_until)
