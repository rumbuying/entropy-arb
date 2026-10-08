"""star_probe: watchlist parsing, feed-set diffing, atomic status file, and
the recorder + feed-wrapper cooperation — all with fake feeds, no network.

Run:  python3 -m pytest tests/test_star_probe.py -q
"""
import asyncio
import csv
import importlib.util
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.discovery import MarketListing  # noqa: E402
from entropy_arb.venue_bars import HEADER, VenueMinuteRecorder, \
    venue_bar_path  # noqa: E402


def _load_star_probe():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "tools", "star_probe.py")
    spec = importlib.util.spec_from_file_location("star_probe_under_test",
                                                  path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod     # dataclasses resolves cls.__module__
    spec.loader.exec_module(mod)
    return mod


sp = _load_star_probe()


def listing(venue="katana", symbol="DOGE"):
    return MarketListing(venue=venue, symbol=symbol,
                         market=f"{symbol}-USD",
                         taker_fee_bps=4.0, maker_fee_bps=1.0,
                         tick=0.001, step=1.0, market_id=7)


def set_book(book, bid="100.0", ask="100.2"):
    book.apply_hl([[{"px": bid, "sz": "5"}], [{"px": ask, "sz": "5"}]])


class FakeFeed:
    """Stands in for a BookFeed: touches the book like a live ws stream,
    optionally going silent (watchdog bait) or crashing (wrapper bait)."""

    def __init__(self, book, fail_after=0, touch=True, tick=0.005):
        self.book = book
        self.fail_after = fail_after
        self.touch = touch
        self.tick = tick
        self.runs = 0
        self.stopped = False

    async def run(self, stop):
        self.runs += 1
        n = 0
        while not stop.is_set():
            n += 1
            if self.touch:
                set_book(self.book)
            if self.fail_after and n >= self.fail_after:
                raise RuntimeError("boom")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.tick)
            except asyncio.TimeoutError:
                pass
        self.stopped = True


# -- parse_watchlist ------------------------------------------------------

def test_parse_watchlist_defaults():
    wl = sp.parse_watchlist("")
    assert wl["symbols"] == []
    assert wl["venues"] is None            # None = every venue
    assert wl["depth_levels"] == 3
    assert wl["max_spread_bps"] == 50.0
    assert wl["rescan_minutes"] == 30.0
    assert wl["max_feeds"] == 24
    assert wl["errors"] == []


def test_parse_watchlist_spec_example():
    text = """
symbols:
  - symbol: DOGE
  - symbol: ANTH
    aliases: {lighter-rh: ANTHROPIC}
    venues: [hl, katana]
venues: [hl, hl:io, lighter, lighter-rh, katana, backpack, bulk]
depth_levels: 3
max_spread_bps: 50
rescan_minutes: 30
max_feeds: 24
"""
    wl = sp.parse_watchlist(text)
    assert [e["symbol"] for e in wl["symbols"]] == ["DOGE", "ANTH"]
    doge, anth = wl["symbols"]
    assert doge["aliases"] == {} and doge["venues"] is None
    assert anth["aliases"] == {"lighter-rh": "ANTHROPIC"}
    assert anth["venues"] == ["hl", "katana"]      # per-symbol subset wins
    assert wl["venues"][:2] == ["hl", "hl:io"]
    assert wl["max_feeds"] == 24


def test_parse_watchlist_string_entries_and_overrides():
    wl = sp.parse_watchlist("""
symbols: [doge, WIF]
depth_levels: 1
max_spread_bps: 0
rescan_minutes: 5
max_feeds: 8
""")
    assert wl["symbols"] == [
        {"symbol": "DOGE", "aliases": {}, "venues": None},   # uppercased
        {"symbol": "WIF", "aliases": {}, "venues": None}]
    assert wl["depth_levels"] == 1
    assert wl["max_spread_bps"] == 0.0
    assert wl["rescan_minutes"] == 5.0
    assert wl["max_feeds"] == 8


def test_parse_watchlist_tolerates_bad_entries():
    wl = sp.parse_watchlist("""
symbols:
  - 42
  - symbol: ""
  - symbol: DOGE
    aliases: {lighter-rh: ""}
  - symbol: DOGE
""")
    assert [e["symbol"] for e in wl["symbols"]] == ["DOGE"]   # dup dropped
    assert wl["symbols"][0]["aliases"] == {}                  # empty alias dropped
    assert len(wl["errors"]) == 3                             # 42, "", dup


def test_parse_watchlist_rejects_non_mapping():
    try:
        sp.parse_watchlist("- just\n- a\n- list\n")
    except ValueError:
        pass
    else:
        raise AssertionError("non-mapping watchlist must raise")


# -- plan_feeds / diff_watchlist_entries ----------------------------------

def test_plan_feeds_add_remove():
    to_start, to_stop = sp.plan_feeds(
        ["DOGE@hl", "DOGE@katana"], ["DOGE@katana", "WIF@bulk"])
    assert to_start == ["WIF@bulk"]
    assert to_stop == ["DOGE@hl"]


def test_plan_feeds_edges_and_order():
    assert sp.plan_feeds([], ["A", "B"]) == (["A", "B"], [])
    assert sp.plan_feeds(["A", "B"], []) == ([], ["A", "B"])
    to_start, to_stop = sp.plan_feeds(["A"], ["B", "A", "B"])  # dup collapses
    assert to_start == ["B"] and to_stop == []
    # desired order is preserved for starts (start priority)
    assert sp.plan_feeds([], ["Z", "A"])[0] == ["Z", "A"]


def test_diff_watchlist_entries():
    old = [{"symbol": "DOGE", "aliases": {}, "venues": None},
           {"symbol": "ANTH", "aliases": {"lighter-rh": "ANTHROPIC"},
            "venues": None}]
    new = [{"symbol": "DOGE", "aliases": {}, "venues": None},
           {"symbol": "ANTH", "aliases": {}, "venues": ["hl"]},
           {"symbol": "WIF", "aliases": {}, "venues": None}]
    added, removed, changed = sp.diff_watchlist_entries(old, new)
    assert added == ["WIF"]
    assert removed == []
    assert changed == ["ANTH"]    # aliases/venues changed -> re-resolve


# -- status heartbeat ------------------------------------------------------

def test_status_payload_shape_and_atomic_write():
    tmp = tempfile.mkdtemp()
    probe = sp.StarProbe(watchlist_path=os.path.join(tmp, "wl.yaml"),
                         logs_dir=tmp)
    book = OrderBook()
    set_book(book)
    slot = sp.Slot(symbol="T", venue="katana", market="T-USD",
                   listing=listing(), book=book, feed=FakeFeed(book),
                   recorder=VenueMinuteRecorder(
                       venue_bar_path(tmp, "T", "katana"), book,
                       staleness_sec=1e9))
    probe.slots[slot.key] = slot
    probe.unresolved = {"X": {"bulk": "not listed"}}
    probe.dropped = ["X@hl"]
    probe.started_total = 1

    payload = probe._status_payload(now=1_700_000_000.0)
    assert set(payload) == {"ts", "pid", "watchlist", "watchlist_mtime",
                            "feeds", "unresolved", "dropped_for_budget",
                            "started_total"}
    assert payload["ts"] == 1_700_000_000.0
    assert payload["pid"] == os.getpid()
    assert payload["watchlist"] == os.path.join(tmp, "wl.yaml")
    assert payload["unresolved"] == {"X": {"bulk": "not listed"}}
    assert payload["dropped_for_budget"] == ["X@hl"]
    assert payload["started_total"] == 1
    (f,) = payload["feeds"]
    assert set(f) == {"symbol", "venue", "market", "running",
                      "last_sample_age_sec", "rows", "rebuilds",
                      "wide_skipped", "stale_skipped"}
    assert (f["symbol"], f["venue"], f["market"]) == ("T", "katana", "T-USD")
    assert f["running"] is False               # task not started yet
    assert f["last_sample_age_sec"] is not None
    assert f["rebuilds"] == 0

    # atomic write: tmp + os.replace, nothing left behind, overwrite works
    path = os.path.join(tmp, "discovery", "scanner-status.json")
    sp.write_status(path, payload)
    assert not os.path.exists(path + ".tmp")
    with open(path) as fh:
        assert json.load(fh)["ts"] == payload["ts"]
    sp.write_status(path, {**payload, "ts": 1.0})
    with open(path) as fh:
        assert json.load(fh)["ts"] == 1.0
    assert os.listdir(os.path.dirname(path)) == ["scanner-status.json"]


# -- feed wrapper + recorder cooperation (fake feeds) ----------------------

def test_feed_wrapper_and_recorder_cooperate():
    tmp = tempfile.mkdtemp()

    async def scenario():
        probe = sp.StarProbe(watchlist_path=os.path.join(tmp, "wl.yaml"),
                             logs_dir=tmp, interval=0.01)
        stop = probe.stop
        book = OrderBook()
        path = venue_bar_path(tmp, "T", "katana")
        rec = VenueMinuteRecorder(path, book, staleness_sec=1e9,
                                  interval_sec=0.01)
        slot = sp.Slot(symbol="T", venue="katana", market="T-USD",
                       listing=listing(), book=book,
                       feed=FakeFeed(book, fail_after=3), recorder=rec)
        probe.slots[slot.key] = slot
        slot.rec_task = asyncio.create_task(rec.run(stop), name="rec-T")
        slot.task = asyncio.create_task(probe._feed_wrapper(slot),
                                        name="feed-T")
        await asyncio.sleep(0.08)     # fake feed touches, then crashes

        assert slot.task.done()       # wrapper swallowed the crash...
        assert slot.task.exception() is None   # ...without failing the task
        assert slot.errors == 1 and "boom" in slot.last_error
        assert slot.rec_task.done() is False   # recorder keeps sampling
        st = probe._status_payload()
        assert st["feeds"][0]["running"] is False
        assert st["feeds"][0]["rebuilds"] == 0

        stop.set()
        await asyncio.gather(slot.task, slot.rec_task,
                             return_exceptions=True)
        assert slot.feed.runs == 1        # one run, ended by the crash
        assert slot.rec_task.exception() is None  # clean recorder shutdown
        return path

    path = asyncio.run(scenario())
    with open(path, newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == HEADER                  # VenueMinuteRecorder schema
    assert len(rows) == 2                     # close() flushed the minute
    assert int(rows[1][-1]) >= 1              # the qualifying samples
    assert float(rows[1][2]) == 100.0         # bid close from the fake feed
    assert float(rows[1][3]) == 100.2         # ask close


def test_watchdog_rebuilds_stale_feed_and_keeps_recorder():
    tmp = tempfile.mkdtemp()

    async def scenario():
        probe = sp.StarProbe(watchlist_path=os.path.join(tmp, "wl.yaml"),
                             logs_dir=tmp, interval=0.01,
                             stale_rebuild_sec=1.0, reload_interval=1e9)
        probe._make_feed = lambda l, b: FakeFeed(b, touch=False)  # silent ws
        slot = probe._start_slot("T", "katana", listing())
        old_task, old_recorder, old_feed = slot.task, slot.recorder, slot.feed
        t0 = 1_700_000_000.0

        await probe._watchdog_once(now=t0)        # arms the grace timer
        assert slot.stale_since == t0 and slot.rebuilds == 0
        await probe._watchdog_once(now=t0 + 0.5)  # still within grace
        assert slot.rebuilds == 0
        await probe._watchdog_once(now=t0 + 1.5)  # stale > 90s-equivalent
        assert slot.rebuilds == 1
        assert probe.started_total == 2
        assert slot.task is not old_task          # feed task rebuilt...
        assert slot.feed is not old_feed
        assert slot.recorder is old_recorder      # ...same recorder kept
        assert slot.stale_since is None           # watchdog re-armed
        await asyncio.sleep(0.02)
        assert slot.task.done() is False          # replacement is running

        # a book receiving frames resets the watchdog instead of rebuilding
        slot.book.alive_ts = t0 + 1.9
        await probe._watchdog_once(now=t0 + 2.0)
        assert slot.stale_since is None and slot.rebuilds == 1

        probe.stop.set()
        await asyncio.gather(slot.task, slot.rec_task,
                             return_exceptions=True)

    asyncio.run(scenario())


def test_watchdog_rebuilds_crashed_feed_without_grace():
    tmp = tempfile.mkdtemp()

    async def scenario():
        probe = sp.StarProbe(watchlist_path=os.path.join(tmp, "wl.yaml"),
                             logs_dir=tmp, interval=0.01,
                             stale_rebuild_sec=3600.0)
        probe._make_feed = lambda l, b: FakeFeed(b, fail_after=1)
        slot = probe._start_slot("T", "katana", listing())
        first = slot.task
        await asyncio.sleep(0.05)
        assert first.done()                       # wrapper caught the crash
        assert slot.errors == 1
        await probe._watchdog_once(now=1.0)       # done task -> immediate
        assert slot.rebuilds == 1 and slot.task is not first
        probe.stop.set()
        await asyncio.gather(slot.task, slot.rec_task,
                             return_exceptions=True)

    asyncio.run(scenario())


# -- _sync: resolution, budget, unresolved, hot add/remove -----------------

class FakeUniverse:
    """Monkeypatch target for entropy_arb.discovery.universe."""

    def __init__(self, per_symbol):
        self.per_symbol = per_symbol      # symbol -> (listed venues, missing)
        self.calls = []

    async def __call__(self, session, symbol, aliases=None, venues=None,
                       catalog=None):
        self.calls.append(symbol)
        listed, missing = self.per_symbol[symbol]
        return {
            "symbol": symbol,
            "aliases": dict(aliases or {}),
            "ts": 0.0,
            "venues": list(venues or []),
            "listings": {v: listing(venue=v, symbol=symbol).to_dict()
                         for v in sorted(listed)},   # real universe sorts
            "missing": dict(missing),
            "pairs": [],
        }


def _with_fakes(universe_fake, feed_tick=0.005):
    """Patch star_probe.universe / feed_factory; returns an undo closure."""
    orig_universe, orig_factory = sp.universe, sp.feed_factory

    def fake_factory(l, book, notify, session=None):
        return FakeFeed(book, tick=feed_tick)

    sp.universe = universe_fake
    sp.feed_factory = fake_factory

    def undo():
        sp.universe = orig_universe
        sp.feed_factory = orig_factory

    return undo


async def _stop_all(probe):
    probe.stop.set()
    tasks = [t for s in probe.slots.values()
             for t in (s.task, s.rec_task) if t is not None]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def test_sync_budget_unresolved_and_hot_reload():
    tmp = tempfile.mkdtemp()
    uni = FakeUniverse({
        "DOGE": (["hl", "katana", "lighter", "backpack"],
                 {"bulk": "not listed"}),
        "ANTH": (["hl"], {"katana": "not listed", "bulk": "not listed"}),
    })
    undo = _with_fakes(uni)

    async def scenario():
        probe = sp.StarProbe(watchlist_path=os.path.join(tmp, "wl.yaml"),
                             logs_dir=tmp, interval=0.01)
        probe._session = object()      # fake universe ignores it
        wl = sp.parse_watchlist("""
symbols: [DOGE, ANTH]
venues: [hl, katana, lighter, backpack, bulk]
max_feeds: 3
""")
        probe.wl = wl
        await probe._sync(wl)

        # budget: DOGE resolves 4 venues (desired order sorts venues), only
        # 3 fit, ANTH@hl is dropped for budget
        assert list(probe.slots) == ["DOGE@backpack", "DOGE@hl",
                                     "DOGE@katana"]
        assert probe.dropped == ["DOGE@lighter", "ANTH@hl"]
        assert probe.started_total == 3
        assert probe.unresolved == {
            "DOGE": {"bulk": "not listed"},
            "ANTH": {"katana": "not listed", "bulk": "not listed"}}
        st = probe._status_payload()
        assert st["dropped_for_budget"] == probe.dropped
        assert st["started_total"] == 3
        assert all(f["running"] for f in st["feeds"])

        # hot add: raising the budget starts the previously dropped feeds
        wl2 = sp.parse_watchlist("symbols: [DOGE, ANTH]\n"
                                 "venues: [hl, katana, lighter, backpack,"
                                 " bulk]\nmax_feeds: 8\n")
        await probe._sync(wl2)
        assert probe.dropped == []
        assert set(probe.slots) == {"DOGE@backpack", "DOGE@hl", "DOGE@katana",
                                    "DOGE@lighter", "ANTH@hl"}
        assert probe.started_total == 5

        # hot remove: dropping ANTH stops its feed; close() flushes its row
        await asyncio.sleep(0.05)
        wl3 = sp.parse_watchlist("symbols: [DOGE]\n"
                                 "venues: [hl, katana, lighter, backpack,"
                                 " bulk]\nmax_feeds: 8\n")
        await probe._sync(wl3)
        assert "ANTH@hl" not in probe.slots
        assert "ANTH" not in probe.unresolved
        anth_path = venue_bar_path(tmp, "ANTH", "hl")
        with open(anth_path, newline="") as fh:
            anth_before = fh.read()
        assert len(anth_before.strip().splitlines()) == 2  # header + 1 row

        # idempotent resync: nothing to start, nothing to stop, and the
        # removed symbol's existing CSV is left untouched
        started_before = probe.started_total
        await probe._sync(wl3)
        assert probe.started_total == started_before
        assert list(probe.slots) == ["DOGE@backpack", "DOGE@hl", "DOGE@katana",
                                     "DOGE@lighter"]
        with open(anth_path, newline="") as fh:
            assert fh.read() == anth_before

        await _stop_all(probe)

    try:
        asyncio.run(scenario())
    finally:
        undo()


def test_sync_keeps_feed_when_catalog_errors():
    """A venue API hiccup (error: ...) must not stop a running feed; only
    'not listed' may."""
    tmp = tempfile.mkdtemp()
    uni = FakeUniverse({
        "DOGE": (["katana"], {}),
        "DOGE_ERR": (["katana"], {"katana": "error: 502 Bad Gateway"}),
    })

    async def scenario():
        probe = sp.StarProbe(watchlist_path=os.path.join(tmp, "wl.yaml"),
                             logs_dir=tmp, interval=0.01)
        probe._session = object()
        probe.wl = sp.parse_watchlist("symbols: [DOGE]\nvenues: [katana]\n")
        await probe._sync(probe.wl)
        assert "DOGE@katana" in probe.slots

        uni.per_symbol["DOGE"] = uni.per_symbol.pop("DOGE_ERR")
        await probe._sync(probe.wl)
        assert "DOGE@katana" in probe.slots          # kept through the hiccup
        assert probe.unresolved == {"DOGE": {"katana":
                                             "error: 502 Bad Gateway"}}

        uni.per_symbol["DOGE"] = ([], {"katana": "not listed"})
        await probe._sync(probe.wl)
        assert "DOGE@katana" not in probe.slots      # delisted -> stopped
        await _stop_all(probe)

    undo = _with_fakes(uni)
    try:
        asyncio.run(scenario())
    finally:
        undo()


def test_resolve_watchlist_unresolved_shape():
    tmp = tempfile.mkdtemp()
    uni = FakeUniverse({
        "DOGE": (["hl", "katana"], {"bulk": "not listed"}),
        "GHOST": ([], {"hl": "not listed", "katana": "not listed"}),
    })

    async def scenario():
        probe = sp.StarProbe(watchlist_path=os.path.join(tmp, "wl.yaml"),
                             logs_dir=tmp)
        probe._session = object()
        wl = sp.parse_watchlist("symbols: [DOGE, GHOST]\n"
                                "venues: [hl, katana, bulk]\n")
        resolved, unresolved = await probe.resolve_watchlist(wl)
        assert list(resolved) == ["DOGE@hl", "DOGE@katana"]
        ent = resolved["DOGE@katana"]
        assert (ent.symbol, ent.venue) == ("DOGE", "katana")
        assert ent.listing.market == "DOGE-USD"
        assert ent.listing.market_id == 7
        assert unresolved == {
            "DOGE": {"bulk": "not listed"},
            "GHOST": {"hl": "not listed", "katana": "not listed"}}

    undo = _with_fakes(uni)
    try:
        asyncio.run(scenario())
    finally:
        undo()


def test_recorder_writes_to_expected_star_path():
    """The slot's CSV lands at logs/minutes-<SYM>-@<venue>.csv (the '@'
    keeps it out of the engine's and the pair probes' globs)."""
    tmp = tempfile.mkdtemp()
    p = venue_bar_path(tmp, "DOGE", "katana")
    assert os.path.basename(p) == "minutes-DOGE-@katana.csv"
    p2 = venue_bar_path(tmp, "DOGE", "hl:io")
    assert os.path.basename(p2) == "minutes-DOGE-@hl-io.csv"
    book = OrderBook()
    set_book(book)
    rec = VenueMinuteRecorder(p, book, staleness_sec=1e9)
    rec.sample()
    rec.close()
    assert os.path.exists(p)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
