"""The recovery progress marker must survive the rebuild's own epoch bump.

2026-11-07 15:46 (g-18e968) and 16:48 (g-50a500f4): the rebuild's activate
moves the grid RECOVERING→STARTING, _mark_active only kept markers for
RECOVERING grids, so the next tick deleted the marker and the rebuild's own
submits — still inside their first-lookup lag — exited the grid as
PROTECTION_FIXED_UNKNOWN_ENTRY ~30-90s after an unconfirmed attempt ended.
Grids covered by a running attempt task keep the marker (epoch carried,
onset preserved while fresh); the moment the attempt ends off-RECOVERING
the marker is deleted and enforcement resumes.
"""
import asyncio,json
from types import SimpleNamespace as NS
import pytest

from entropy_arb.grid.protection_watch import (RECOVERY_WATCH_KEY, RECOVERY_WATCH_SCHEMA,
    RECOVERY_PROGRESS_FRESH_MS, PROTECTION_RECOVERY_TOLERANCE_MS, recovery_in_flight)
from entropy_arb.grid.store import Store
from entropy_arb.grid.stream_recovery import StreamRecoveryScheduler

GID = 'g-covered'


@pytest.fixture
def env(tmp_path):
    st = Store.open(str(tmp_path / 'recovery-marker.sqlite'))
    t = [10_000.0]
    asyncio.run(st.write(lambda c: c.execute(
        "INSERT INTO accounts(account_id,venue,owner,created_ts) VALUES('acc','bulk','owner','t')")))
    scheduler = StreamRecoveryScheduler(NS(store=st), clock=lambda: t[0])
    return st, scheduler, t


async def plant(st, gid, *, epoch, onset, updated):
    await st.write(lambda c: c.execute(
        'INSERT INTO runtime_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
        (RECOVERY_WATCH_KEY + gid, json.dumps(dict(schema=RECOVERY_WATCH_SCHEMA, grid_id=gid,
            epoch=epoch, onset_mono_ms=onset, updated_mono_ms=updated), sort_keys=True))))


def marker(st, gid):
    row = st.read(lambda c: c.execute('SELECT value FROM runtime_state WHERE key=?',
                                      (RECOVERY_WATCH_KEY + gid,)).fetchone())
    return json.loads(row[0]) if row else None


async def seed_grid(st, gid, epoch, status):
    await st.write(lambda c: c.execute(
        "INSERT INTO grids(grid_id,account_id,market_key,status,desired_mode,epoch,created_ts,updated_ts) "
        "VALUES(?,'acc','m',?,'ACTIVE',?,'t','t') ON CONFLICT(grid_id) DO UPDATE SET status=excluded.status,epoch=excluded.epoch",
        (gid, status, epoch)))


async def covered(scheduler, gid):
    hold = asyncio.Event()
    scheduler.task_grids['acc'] = [gid]
    scheduler.tasks['acc'] = asyncio.create_task(hold.wait())
    return hold


async def uncover(scheduler, hold):
    hold.set()
    await asyncio.gather(scheduler.tasks['acc'], return_exceptions=True)
    scheduler.tasks.clear()
    scheduler.task_grids.clear()


def test_marker_survives_rebuild_epoch_bump_while_attempt_runs(env):
    async def run():
        st, scheduler, t = env
        await seed_grid(st, GID, 1, 'RECOVERING')
        await scheduler._mark_active([{'grid_id': GID, 'epoch': 1}])  # the scheduler marked it
        onset = marker(st, GID)['onset_mono_ms']
        hold = await covered(scheduler, GID)
        await seed_grid(st, GID, 2, 'STARTING')  # the rebuild's own bump
        await scheduler._mark_active([])         # next tick: no RECOVERING rows
        m = marker(st, GID)
        assert m is not None and m['epoch'] == 2
        assert m['onset_mono_ms'] == onset  # one continuous episode
        assert recovery_in_flight(st, GID, 2, now_mono_ms=int(t[0] * 1000))
        await uncover(scheduler, hold)
    asyncio.run(run())


def test_marker_is_deleted_when_no_attempt_covers_the_grid(env):
    async def run():
        st, scheduler, t = env
        await seed_grid(st, GID, 1, 'RECOVERING')
        await scheduler._mark_active([{'grid_id': GID, 'epoch': 1}])
        await seed_grid(st, GID, 2, 'STARTING')
        await scheduler._mark_active([])  # nobody covers it: enforcement resumes
        assert marker(st, GID) is None
        assert not recovery_in_flight(st, GID, 2, now_mono_ms=int(t[0] * 1000))
    asyncio.run(run())


def test_marker_is_deleted_once_the_attempt_ends_off_recovering(env):
    async def run():
        st, scheduler, t = env
        await seed_grid(st, GID, 1, 'RECOVERING')
        await scheduler._mark_active([{'grid_id': GID, 'epoch': 1}])
        hold = await covered(scheduler, GID)
        await seed_grid(st, GID, 2, 'STARTING')
        await scheduler._mark_active([])
        assert marker(st, GID) is not None  # covered while it runs
        await uncover(scheduler, hold)
        await scheduler._mark_active([])    # attempt ended off-RECOVERING
        assert marker(st, GID) is None
        assert not recovery_in_flight(st, GID, 2, now_mono_ms=int(t[0] * 1000))
    asyncio.run(run())


def test_stale_marker_does_not_carry_the_onset_across_an_epoch(env):
    async def run():
        st, scheduler, t = env
        await seed_grid(st, GID, 1, 'RECOVERING')
        await scheduler._mark_active([{'grid_id': GID, 'epoch': 1}])
        old_updated = int((t[0] - RECOVERY_PROGRESS_FRESH_MS / 1000 - 1) * 1000)
        await plant(st, GID, epoch=1, onset=5_000_000, updated=old_updated)  # stale
        hold = await covered(scheduler, GID)
        await seed_grid(st, GID, 2, 'STARTING')
        await scheduler._mark_active([])
        m = marker(st, GID)
        assert m['epoch'] == 2
        assert m['onset_mono_ms'] == int(t[0] * 1000) != 5_000_000  # fresh onset
        await uncover(scheduler, hold)
    asyncio.run(run())


def test_episode_tolerance_bounds_the_carried_onset(env):
    async def run():
        st, scheduler, t = env
        await seed_grid(st, GID, 1, 'RECOVERING')
        await scheduler._mark_active([{'grid_id': GID, 'epoch': 1}])
        hold = await covered(scheduler, GID)
        await seed_grid(st, GID, 2, 'STARTING')
        # A 25-minute episode under a live attempt is tolerated…
        await plant(st, GID, epoch=2, onset=int((t[0] - 1500) * 1000), updated=int(t[0] * 1000))
        await scheduler._mark_active([])
        assert recovery_in_flight(st, GID, 2, now_mono_ms=int(t[0] * 1000))
        # …and past the bound it enforces even while the marker is fresh.
        await plant(st, GID, epoch=2,
                    onset=int((t[0] - (PROTECTION_RECOVERY_TOLERANCE_MS / 1000 + 100)) * 1000),
                    updated=int(t[0] * 1000))
        await scheduler._mark_active([])
        assert not recovery_in_flight(st, GID, 2, now_mono_ms=int(t[0] * 1000))
        await uncover(scheduler, hold)
    asyncio.run(run())
