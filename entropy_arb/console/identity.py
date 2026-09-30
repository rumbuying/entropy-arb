"""Strategy identity resolution (V2-006, spec §7.1).

A strategy is the long-lived trading identity: type + symbol + the real
deployments of both legs. worker ids, profile names, parameter hashes are
NOT identity — retuning or restarting keeps the strategy; changing the
traded market, the leg deployments or the strategy type starts a NEW one
(with parent_id linking back).

The launch identity is computed from what the worker will ACTUALLY run
(symbol / base venue / base dex / hedge venue / type), so a profile
launched into a different market resolves to a different strategy instead
of silently merging history (spec §7.1: "启动遇到不匹配创建新身份").
"""
from __future__ import annotations

import hashlib
from typing import Dict, Optional, Tuple


def launch_identity(*, symbol: str, base: str, base_dex: str,
                    hedge: str, strategy_type: str) -> str:
    key = "|".join([symbol.upper(), base.lower(), (base_dex or "").lower(),
                    hedge.lower(), strategy_type])
    return key


def strategy_name(symbol: str, base: str, hedge: str,
                  strategy_type: str) -> str:
    kind = "maker" if strategy_type == "maker_hedge" else "basis"
    return f"{symbol.upper()} {base}↔{hedge} {kind}"


def resolve_strategy(storage, *, profile: str, symbol: str, base: str,
                     base_dex: str, hedge: str, strategy_type: str) \
        -> Tuple[Dict, bool]:
    """Return (strategy, created). Reuses an existing strategy with the
    same launch identity; otherwise creates one. Also (re)links the
    profile → identity mapping — one profile MAY map to several strategies
    (launched into different markets), which is exactly what the
    launch_identity key encodes."""
    ident = launch_identity(symbol=symbol, base=base, base_dex=base_dex,
                            hedge=hedge, strategy_type=strategy_type)
    found = storage.find_strategy_by_identity(ident)
    if found is not None:
        storage.link_profile(profile=profile, strategy_id=found["id"],
                             launch_identity=ident)
        return found, False
    strategy = storage.create_strategy(
        name=strategy_name(symbol, base, hedge, strategy_type),
        symbol=symbol, type_=strategy_type, base_venue=base,
        base_market=base_dex or "", hedge_venue=hedge)
    storage.link_profile(profile=profile, strategy_id=strategy["id"],
                         launch_identity=ident)
    return strategy, True
