"""Lighter nonce-collision recovery.

Two engines sharing one Lighter API key consume each other's nonce; the SDK
refreshes its counter on "invalid nonce" but does not retry, so the failure
reached the engine as a hard send error (2026-09-25: three consecutive hedge
failures → EXPOSED). These tests pin the retry wrapper that fixes it.

Run:  python3 -m pytest tests/test_lighter_nonce.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.venue_lighter import (_is_invalid_nonce,  # noqa: E402
                                       _submit_with_nonce_retry)


class _Resp:
    def __init__(self, code):
        self.code = code


def test_invalid_nonce_detection_covers_all_three_shapes():
    # exactly what the venue returned on 2026-09-25
    assert _is_invalid_nonce(None, "HTTP response body: code=21104 "
                                   "message='invalid nonce'", None)
    assert _is_invalid_nonce(None, None,
                             Exception("BadRequestException: invalid nonce"))
    assert _is_invalid_nonce(_Resp(21104), None, None)
    # unrelated failures must not trigger a retry
    assert not _is_invalid_nonce(_Resp(200), None, None)
    assert not _is_invalid_nonce(None, "RATE_LIMITED: too many requests", None)
    assert not _is_invalid_nonce(None, None, None)


def test_submit_retries_once_after_refreshing():
    calls = {"submit": 0, "refresh": 0}
    bad = (None, "HTTP response body: code=21104 message='invalid nonce'", None)
    good = (_Resp(200), None, None)

    async def submit():
        calls["submit"] += 1
        return bad if calls["submit"] == 1 else good

    async def refresh():
        calls["refresh"] += 1

    resp, err, exc = asyncio.run(
        _submit_with_nonce_retry(submit, refresh))
    assert (resp, err, exc) == good
    assert calls == {"submit": 2, "refresh": 1}


def test_submit_does_not_retry_a_real_failure():
    calls = {"submit": 0, "refresh": 0}

    async def submit():
        calls["submit"] += 1
        return None, "insufficient margin", None

    async def refresh():
        calls["refresh"] += 1

    _, err, _ = asyncio.run(_submit_with_nonce_retry(submit, refresh))
    assert err == "insufficient margin"
    assert calls == {"submit": 1, "refresh": 0}


def test_submit_gives_up_after_one_retry():
    """A second collision must surface, not loop: the engine's own error
    ladder (max_hedge_failures → EXPOSED) is the backstop."""
    calls = {"submit": 0, "refresh": 0}
    bad = (None, "invalid nonce", None)

    async def submit():
        calls["submit"] += 1
        return bad

    async def refresh():
        calls["refresh"] += 1

    _, err, _ = asyncio.run(_submit_with_nonce_retry(submit, refresh))
    assert err == "invalid nonce"
    assert calls == {"submit": 2, "refresh": 1}


def test_refresh_failure_does_not_mask_the_order_error():
    """A refresh that blows up must not replace the order error: the wrapper
    still retries once and returns the original failure."""
    calls = {"submit": 0}

    async def submit():
        calls["submit"] += 1
        return None, "invalid nonce", None

    async def refresh():
        raise RuntimeError("nextNonce endpoint down")

    _, err, _ = asyncio.run(_submit_with_nonce_retry(submit, refresh))
    assert err == "invalid nonce"
    assert calls["submit"] == 2        # retried despite the refresh failure


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:52s} OK")
