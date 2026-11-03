"""Futures grid trading package (GRID-TRADING-DEVELOPMENT-SPEC).

Modules are added as their task lands; nothing here touches the legacy
console database or the old arbitrage engines. The runtime store owned by
GridRuntime lives in ``store.py``; the console keeps read-only projections
only.
"""
