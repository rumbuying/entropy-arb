""".env credential management: masked reads, validated writes, audited.

Security model (mirrors the project's trust model — whoever can open the
console can already read the .env file on disk):

* values are NEVER returned once stored — only "set" plus the last 4 chars;
* writes go through format validation per key kind, so a typo is rejected
  before it can waste a live session;
* the file is patched line-by-line: comments, ordering and unknown keys are
  preserved (python-dotenv round-trips would destroy them);
* every write appends an audit line to the console log — operation and key
  tail only, never values.
"""
from __future__ import annotations

import os
import re
import time
from typing import Dict, List, Optional

RE_PRIVATE_KEY = re.compile(r"^0x[0-9a-fA-F]{64}$")
RE_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
RE_INT = re.compile(r"^\d+$")

# key -> (kind, description)
KEY_KINDS: Dict[str, str] = {
    "HL_PRIVATE_KEY": "private_key",
    "HL_ACCOUNT_ADDRESS": "address",
    "HL_PRIVATE_KEY_XYZ": "private_key",
    "HL_ACCOUNT_ADDRESS_XYZ": "address",
    "LIGHTER_ACCOUNT_INDEX": "int",
    "LIGHTER_API_KEY_INDEX": "int",
    "LIGHTER_API_PRIVATE_KEY": "private_key",
}

# what each hedge choice needs to be tradeable (creds_complete semantics)
VENUE_REQUIREMENTS: Dict[str, List[str]] = {
    "entropy": ["HL_PRIVATE_KEY"],
    "tradexyz": ["HL_PRIVATE_KEY"],   # XYZ keys optional overrides
    "lighter": ["LIGHTER_ACCOUNT_INDEX", "LIGHTER_API_KEY_INDEX",
                "LIGHTER_API_PRIVATE_KEY"],
    "lighter-rh": ["LIGHTER_ACCOUNT_INDEX", "LIGHTER_API_KEY_INDEX",
                   "LIGHTER_API_PRIVATE_KEY"],
}


def validate_value(key: str, value: str) -> Optional[str]:
    """Return an error message, or None when the value is well-formed."""
    kind = KEY_KINDS.get(key)
    if kind is None:
        return "unknown key"
    if kind == "private_key" and not RE_PRIVATE_KEY.match(value):
        return "expect 0x + 64 hex chars (agent wallet private key)"
    if kind == "address" and not RE_ADDRESS.match(value):
        return "expect 0x + 40 hex chars (main account address)"
    if kind == "int" and not RE_INT.match(value):
        return "expect an integer"
    return None


def _line_pattern(key: str):
    return re.compile(rf"^(\s*)(?:export\s+)?{re.escape(key)}\s*=(.*)$")


class SecretsManager:
    def __init__(self, env_path: str, audit_log=None) -> None:
        self.env_path = env_path
        self._audit = audit_log   # callable(str), already value-free

    # ------------------------------------------------------------------ read

    def _raw(self) -> Optional[str]:
        try:
            with open(self.env_path) as fh:
                return fh.read()
        except FileNotFoundError:
            return None

    def status(self) -> dict:
        raw = self._raw()
        values = self._parse(raw) if raw is not None else {}
        keys = {}
        for key, kind in KEY_KINDS.items():
            v = values.get(key)
            keys[key] = {
                "set": v is not None,
                "tail": (v[-4:] if v is not None and len(v) >= 4
                         else ("···" if v is not None else None)),
                "kind": kind,
                "valid": None if v is None else validate_value(key, v) is None,
                "error": None if v is None else validate_value(key, v),
            }
        venues = {}
        for venue, reqs in VENUE_REQUIREMENTS.items():
            venues[venue] = all(values.get(k) for k in reqs)
        return {"exists": raw is not None, "keys": keys, "venues": venues}

    def _parse(self, raw: str) -> Dict[str, str]:
        out = {}
        for ln in raw.splitlines():
            m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$",
                         ln)
            if m:
                v = m.group(2).strip().strip('"').strip("'")
                out[m.group(1)] = v
        return out

    # ----------------------------------------------------------------- write

    def update(self, updates: Dict[str, str]) -> dict:
        """Set keys to new values; empty string deletes the key's line.
        Rejects the whole batch if any value is malformed."""
        errors = {}
        for key, val in (updates or {}).items():
            if key not in KEY_KINDS:
                errors[key] = "unknown key"
                continue
            val = (val or "").strip()
            if val == "":
                continue                    # delete — always allowed
            err = validate_value(key, val)
            if err:
                errors[key] = err
        if errors:
            return {"ok": False, "errors": errors, "status": self.status()}

        raw = self._raw()
        lines = raw.splitlines() if raw is not None else [
            "# entropy-arb credentials / 密钥配置",
            "# created by the web console — never commit this file",
            "",
        ]
        applied = []
        for key, val in (updates or {}).items():
            val = (val or "").strip()
            pat = _line_pattern(key)
            replaced = False
            for i, ln in enumerate(lines):
                if pat.match(ln):
                    if not replaced and val:
                        lines[i] = f"{key}={val}"
                        replaced = True
                    elif replaced or not val:
                        lines.pop(i)
            if val and not replaced:
                lines.append(f"{key}={val}")
            applied.append((key, val))

        content = "\n".join(lines) + "\n"
        d = os.path.dirname(os.path.abspath(self.env_path))
        os.makedirs(d, exist_ok=True)
        with open(self.env_path, "w") as fh:
            fh.write(content)
        try:
            os.chmod(self.env_path, 0o600)
        except OSError:
            pass
        for key, val in applied:
            self._audit(f"secrets: {key} "
                        + ("cleared" if not val else f"set ····{val[-4:]}"))
        return {"ok": True, "errors": {}, "status": self.status()}


def mask_updates_for_audit(updates: Dict[str, str]) -> str:
    """Human-safe summary of an update batch (for request logging)."""
    parts = []
    for k, v in (updates or {}).items():
        v = (v or "").strip()
        parts.append(f"{k}={'<clear>' if not v else '····' + v[-4:]}")
    return " ".join(parts)


def audit_writer(log_path: str):
    """Append-only audit log helper shared by console modules."""
    def write(msg: str) -> None:
        d = os.path.dirname(os.path.abspath(log_path))
        os.makedirs(d, exist_ok=True)
        with open(log_path, "a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    return write
