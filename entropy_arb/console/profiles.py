"""Named strategy profiles: CRUD over profiles/*.yaml + sidecar metadata.

A profile is a plain config.yaml (same schema, same strict validation); the
console stores the market choice (--symbol / --hedge equivalents) in a JSON
sidecar next to it, so the engine's "markets are an explicit launch decision"
rule stays intact — the console just remembers what to pass.

Every save is validated through the REAL load_config() against the current
.env: whatever the editor accepts, `main.py --config <profile>` will start.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Dict, Optional

import yaml

from ..config import BASE_VENUES, HEDGE_VENUES, ConfigError, load_config

NEW_PROFILE_TEMPLATE = """\
# entropy-arb strategy profile (edited via the web console)
# Thresholds are the whole signal — measure first (Analyzer tab), never guess.

thresholds:
  midline_bps: 0.0     # long-run premium level of this pair (bps)
  upper_bps: 3.0       # sell-entropy entry above midline
  lower_bps: 3.0       # buy-entropy entry below midline

entropy:
  dex: io
  taker_fee_bps: 0.0
  max_position_usd: 500
  max_orders_per_min: 120

hedge:
  taker_fee_bps: 0.0   # tradexyz ~1.0; lighter deployments 0.0
  max_position_usd: 500
  max_orders_per_min: 30

sizing:
  take_fraction: 0.5
  max_order_notional_usd: 250
  min_order_notional_usd: 10

inventory:
  scale_bps: 10
  floor_frac: 0.5

execution:
  premium_persist_sec: 0.3
  cooldown_sec: 0.0
  leg_slippage_bps: 50
  hedge_slippage_bps: 20
  net_tolerance_base: 0.001
  max_consecutive_errors: 3
  staleness_sec: 10
  reconcile_sec: 15

recorder:
  enabled: true
  csv: logs/minutes-{symbol}-{hedge}.csv

logging:
  level: INFO
  dashboard: false
  file: logs/engine-{symbol}-{hedge}.log
  trades_csv: logs/trades-{symbol}-{hedge}.csv
  status_interval_sec: 30

# Maker mode (optional): post-only quotes on the hedge venue, taker hedging
# on the base leg. Mutually exclusive with the taker band strategy above and
# requires a maker_capable hedge venue (currently: katana) + maker params.
# See MAKER-DESIGN.md. Uncomment and set enabled: true to switch modes.
# maker:
#   enabled: true
#   edge_bps: 2.0          # target net locked edge per fill (bps)
#   costs_bps: 5.5         # maker fee + hedge taker fee + hedge slippage
#   requote_bps: 1.0       # anchor move that re-quotes
#   requote_sec: 30        # max quote age
#   size_base: 0.005       # per-side quote size (base units)
#   sides: both            # both | bid | ask
#   hedge_batch_ms: 250
"""


def _safe_name(name: str) -> bool:
    import re
    return bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", name))


class ProfilesManager:
    def __init__(self, profiles_dir: str, env_file: str = ".env",
                 audit_log=None) -> None:
        self.dir = profiles_dir
        self.env_file = env_file
        self._audit = audit_log

    # ------------------------------------------------------------------ paths

    def _yaml_path(self, name: str) -> str:
        return os.path.join(self.dir, f"{name}.yaml")

    def _meta_path(self, name: str) -> str:
        return os.path.join(self.dir, f"{name}.json")

    # ------------------------------------------------------------------- read

    def exists(self, name: str) -> bool:
        return _safe_name(name) and os.path.exists(self._yaml_path(name))

    def list(self) -> list:
        out = []
        if not os.path.isdir(self.dir):
            return out
        for fn in sorted(os.listdir(self.dir)):
            if not fn.endswith(".yaml"):
                continue
            name = fn[:-5]
            try:
                with open(self._yaml_path(name)) as fh:
                    raw = yaml.safe_load(fh) or {}
                thr = raw.get("thresholds") or {}
                rec = (raw.get("recorder") or {}).get("csv")
                mk = raw.get("maker") or {}
                out.append({
                    "name": name,
                    "symbol": self._meta(name).get("symbol"),
                    "hedge": self._meta(name).get("hedge"),
                    "base": self._meta(name).get("base", "hl"),
                    "midline_bps": thr.get("midline_bps"),
                    "upper_bps": thr.get("upper_bps"),
                    "lower_bps": thr.get("lower_bps"),
                    "recorder_csv": rec,
                    "maker": bool(mk.get("enabled")),
                    "updated_ts": os.path.getmtime(self._yaml_path(name)),
                })
            except Exception:
                out.append({"name": name, "error": "unreadable"})
        return out

    def _meta(self, name: str) -> dict:
        try:
            with open(self._meta_path(name)) as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def read(self, name: str) -> dict:
        if not self.exists(name):
            raise FileNotFoundError(f"profile '{name}' not found")
        with open(self._yaml_path(name)) as fh:
            text = fh.read()
        meta = self._meta(name)
        return {"name": name, "yaml": text,
                "symbol": meta.get("symbol"), "hedge": meta.get("hedge"),
                "base": meta.get("base", "hl"),
                "updated_ts": os.path.getmtime(self._yaml_path(name))}

    # -------------------------------------------------------------- validate

    def validate(self, yaml_text: str, symbol: Optional[str],
                 hedge: Optional[str], base: Optional[str] = "hl") -> dict:
        """Run the candidate through the real load_config. Returns
        {ok, error} where error is the exact ConfigError message."""
        if hedge is not None and hedge not in HEDGE_VENUES:
            return {"ok": False,
                    "error": f"hedge must be one of {list(HEDGE_VENUES)}"}
        if base is not None and base not in BASE_VENUES:
            return {"ok": False,
                    "error": f"base must be one of {list(BASE_VENUES)}"}
        if self.dir:
            os.makedirs(self.dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".yaml", dir=self.dir or None)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(yaml_text or "")
            try:
                load_config(tmp, self.env_file,
                            symbol=symbol or "", hedge_venue=hedge or "",
                            base_venue=base or "hl")
                return {"ok": True, "error": None}
            except ConfigError as e:
                return {"ok": False, "error": str(e)}
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # ------------------------------------------------------------------ write

    def save(self, name: str, yaml_text: str, symbol: Optional[str],
             hedge: Optional[str], create: bool = False,
             base: Optional[str] = None) -> dict:
        if not _safe_name(name):
            return {"ok": False, "error": f"invalid profile name {name!r}"}
        # an omitted base preserves the stored one (editors that predate
        # --base must not silently flip a lighter-base profile back to hl)
        if base is None:
            base = self._meta(name).get("base", "hl")
        v = self.validate(yaml_text, symbol, hedge, base)
        if not v["ok"]:
            return v
        os.makedirs(self.dir, exist_ok=True)
        existed = os.path.exists(self._yaml_path(name))
        if create and existed:
            return {"ok": False, "error": f"profile '{name}' already exists"}
        # compose per-profile file paths so parallel workers never share CSVs
        yaml_text = (yaml_text or "")
        if symbol and hedge:
            yaml_text = yaml_text.replace("{symbol}", symbol.upper()) \
                                 .replace("{hedge}", hedge) \
                                 .replace("{base}", base or "hl")
        with open(self._yaml_path(name), "w") as fh:
            fh.write(yaml_text)
        meta = self._meta(name)
        meta.update({"symbol": symbol, "hedge": hedge,
                     "base": base or "hl",
                     "updated_ts": time.time(),
                     "created_ts": meta.get("created_ts", time.time())})
        with open(self._meta_path(name), "w") as fh:
            json.dump(meta, fh, indent=2)
        if self._audit:
            self._audit(f"profile {'created' if not existed else 'saved'}: "
                        f"{name} ({symbol or '?'}/{hedge or '?'})")
        return {"ok": True, "error": None}

    def new_text(self, symbol: Optional[str] = None,
                 hedge: Optional[str] = None) -> str:
        s = (symbol or "SYMBOL").upper()
        hv = hedge or "lighter-rh"
        return NEW_PROFILE_TEMPLATE.replace("{symbol}", s).replace("{hedge}", hv)

    def delete(self, name: str) -> None:
        if not self.exists(name):
            raise FileNotFoundError(f"profile '{name}' not found")
        os.unlink(self._yaml_path(name))
        try:
            os.unlink(self._meta_path(name))
        except FileNotFoundError:
            pass
        if self._audit:
            self._audit(f"profile deleted: {name}")

    # ---------------------------------------------------------- recorder csv

    def maker_enabled(self, name: str) -> bool:
        """True when the profile switches the engine to maker mode."""
        try:
            with open(self._yaml_path(name)) as fh:
                raw = yaml.safe_load(fh) or {}
            return bool((raw.get("maker") or {}).get("enabled"))
        except (FileNotFoundError, yaml.YAMLError):
            return False

    def recorder_csv(self, name: str) -> Optional[str]:
        """Absolute-ish path of the profile's minute CSV (for Analyzer)."""
        try:
            with open(self._yaml_path(name)) as fh:
                raw = yaml.safe_load(fh) or {}
            return (raw.get("recorder") or {}).get("csv")
        except (FileNotFoundError, yaml.YAMLError):
            return None

    def fees_bps(self, name: str) -> Optional[float]:
        """Sum of both legs' taker fees — the Analyzer's default fee input."""
        try:
            with open(self._yaml_path(name)) as fh:
                raw = yaml.safe_load(fh) or {}
            ent = float((raw.get("entropy") or {}).get("taker_fee_bps", 0.0))
            hed = float((raw.get("hedge") or {}).get("taker_fee_bps", 0.0))
            return ent + hed
        except (FileNotFoundError, yaml.YAMLError, ValueError):
            return None

    def threshold_fields(self, yaml_text: str) -> Dict[str, float]:
        try:
            raw = yaml.safe_load(yaml_text) or {}
            thr = raw.get("thresholds") or {}
            return {k: float(thr[k]) for k in
                    ("midline_bps", "upper_bps", "lower_bps") if k in thr}
        except (yaml.YAMLError, ValueError, TypeError):
            return {}
