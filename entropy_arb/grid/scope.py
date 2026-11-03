"""Account occupancy (G04, spec §11.1): TWO different things.

- Economic claim (durable, in the runtime store): one non-closed owner
  per account auth scope. Survives process death; residual positions and
  UNKNOWN orders keep it. Released only after order/position
  reconciliation proves handover.
- Process control (advisory file locks): short-term, per signer/account
  scope, dies with the process. Prevents grid writers from using the same
  account or signer concurrently.

These controls are internal to the grid module. Arbitrage uses its own
accounts and execution paths; users keep its economic accounts separate.
Claim checks never release on lease timeout; only reconciliation may release.
"""
from __future__ import annotations

import hashlib
import os
from typing import Dict, List, Optional, Tuple

from . import models
from .store import Store


def account_scope_key(venue: str, deployment: str, owner: str) -> str:
    return f"acct:{venue}|{deployment}|{owner}"


def signer_scope_key(venue: str, deployment: str, scope: str) -> str:
    return f"signer:{venue}|{deployment}|{scope}"


def legacy_scope_keys(venue: str, deployment: str, symbol: str) -> str:
    """The market key shape old console ops uses (venue[:dex]:SYMBOL),
    expressed in the same namespace so both worlds collide correctly."""
    return f"market:{venue}|{deployment}|{symbol.upper()}"


class ScopeInUse(Exception):
    def __init__(self, scope_key: str, holder: str) -> None:
        super().__init__(f"{scope_key} held by {holder}")
        self.scope_key = scope_key
        self.holder = holder


class ScopeManager:
    def __init__(self, st: Store, lock_dir: str,
                 owner_id: str) -> None:
        self.store = st
        self.lock_dir = lock_dir
        self.owner_id = owner_id
        os.makedirs(lock_dir, exist_ok=True, mode=0o700)
        self._held_locks: Dict[str, int] = {}   # scope_key -> fd

    # ------------------------------------------------------------- claims
    def claim(self, scope_keys: List[str], *, owner_type: str = "grid") \
            -> None:
        """Take economic claims (all-or-nothing) in the store. Raises
        ScopeInUse listing the blocker. Claims are durable: process death
        does NOT release them."""
        def w(c):
            for key in sorted(scope_keys):
                row = c.execute(
                    "SELECT owner_id, state FROM scope_claims WHERE "
                    "scope_key = ?", (key,)).fetchone()
                if row is not None and row["state"] == "held":
                    raise ScopeInUse(key, row["owner_id"])
            for key in scope_keys:
                c.execute(
                    "INSERT INTO scope_claims(scope_key, owner_type, "
                    "owner_id, state, acquired_ts) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(scope_key) DO UPDATE SET owner_type = "
                    "excluded.owner_type, owner_id = excluded.owner_id, "
                    "state = 'held', acquired_ts = excluded.acquired_ts, "
                    "released_ts = NULL",
                    (key, owner_type, self.owner_id, "held",
                     models.utc_now_iso()))
        self.store.write_sync(w)

    def release(self, scope_keys: List[str]) -> None:
        """Explicit release — only legitimate after the caller proved
        (reconciliation) that no residual risk remains."""
        def w(c):
            for key in scope_keys:
                c.execute(
                    "UPDATE scope_claims SET state = 'released', "
                    "released_ts = ? WHERE scope_key = ? AND owner_id = ?",
                    (models.utc_now_iso(), key, self.owner_id))
        self.store.write_sync(w)

    def holder_of(self, scope_key: str) -> Optional[str]:
        def q(c):
            row = c.execute(
                "SELECT owner_id FROM scope_claims WHERE scope_key = ? "
                "AND state = 'held'", (scope_key,)).fetchone()
            return row["owner_id"] if row else None
        return self.store.read(q)

    # ------------------------------------------------------- process locks
    def acquire_local(self, scope_keys: List[str]) -> None:
        """fcntl locks in sorted order; on any miss release what we got.
        Raises ScopeInUse on conflict."""
        import fcntl
        held: List[str] = []
        for key in sorted(scope_keys):
            path = os.path.join(
                self.lock_dir,
                hashlib.sha256(key.encode()).hexdigest() + ".lock")
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                for h in held:
                    fd2 = self._held_locks.pop(h)
                    fcntl.flock(fd2, fcntl.LOCK_UN)
                    os.close(fd2)
                raise ScopeInUse(key, "another-process")
            self._held_locks[key] = fd
            held.append(key)

    def release_local(self, scope_keys: Optional[List[str]] = None) -> None:
        import fcntl
        keys = scope_keys if scope_keys is not None \
            else list(self._held_locks)
        for key in keys:
            fd = self._held_locks.pop(key, None)
            if fd is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def close(self) -> None:
        self.release_local()
