"""Grid store tests (handoff §6): SQL-level uniqueness, mid-transaction
failure rollback, migration interruption, idempotency map persistence,
backup/restore boundary, single-writer thread discipline."""
import json
import os
import sqlite3
import threading

import pytest

from entropy_arb.grid import models, store


@pytest.fixture()
def path(tmp_path):
    return str(tmp_path / "grid-runtime.sqlite")


def _exec(key="test:a:m:t1", qty="0.004", price="1000", side="buy",
          account="A", market="M", fee="0.0004"):
    return models.ExecutionEvent(
        execution_key=key, account=account, market=market,
        order_client_id="cid-1", exchange_order_id="ex-1", trade_id="t1",
        side=side, qty_base=qty, price=price, fee_amount=fee,
        fee_asset="USDC", is_maker=True,
        exchange_ts="2026-10-11T00:00:00Z",
        received_ts="2026-10-11T00:00:01Z", source="stream",
        source_seq="42", finality="final")


class TestMigrations:
    def test_fresh_open_creates_v3(self, path):
        s = store.Store.open(path)
        try:
            assert store.schema_version_of(path) == store.SCHEMA_VERSION
        finally:
            s.close()

    def test_interrupted_migration_rolls_back(self, path):
        store.migrate(path)
        v0 = store.schema_version_of(path)
        bad = [(v0 + 1, "CREATE TABLE good(x INTEGER);\n"
                        "CREATE TABLE oops(;")]
        with pytest.raises(sqlite3.Error):
            store.migrate(path, migrations=bad)
        # version and DDL both rolled back
        assert store.schema_version_of(path) == v0
        conn = store.connect(path, readonly=True)
        try:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert "good" not in names
        finally:
            conn.close()

    def test_unknown_higher_schema_refuses(self, path):
        store.migrate(path)
        conn = store.connect(path)
        conn.execute("PRAGMA user_version = 99")
        conn.close()
        with pytest.raises(store.StoreError) as e:
            store.Store.open(path)
        assert e.value.code == "SCHEMA_TOO_NEW"


def _mk_grid(c, grid_id="g1"):
    """Insert an account + grid so intent rows satisfy FKs."""
    c.execute("INSERT OR IGNORE INTO accounts(account_id, venue, deployment,"
              " owner, fixture, created_ts) VALUES(?,?,?,?,1,'t')",
              (grid_id, "hl", "", "owner-" + grid_id))
    c.execute("INSERT OR IGNORE INTO grids(grid_id, account_id, market_key,"
              " status, desired_mode, created_ts, updated_ts)"
              " VALUES(?,?,?, 'RUNNING', 'ACTIVE', 't', 't')",
              (grid_id, grid_id, "M"))


class TestExecutionDedup:
    def test_first_insert_then_duplicate_then_conflict(self, path):
        s = store.Store.open(path)
        try:
            res = s.write_sync(lambda c: store.insert_execution(c, _exec()))
            assert res == "inserted"
            res = s.write_sync(lambda c: store.insert_execution(c, _exec()))
            assert res == "duplicate"
            with pytest.raises(store.StoreError) as e:
                s.write_sync(lambda c: store.insert_execution(
                    c, _exec(qty="0.005")))
            assert e.value.code == "DATA_CONFLICT"
            with pytest.raises(store.StoreError):
                s.write_sync(lambda c: store.insert_execution(
                    c, _exec(side="sell")))
            with pytest.raises(store.StoreError):
                s.write_sync(lambda c: store.insert_execution(
                    c, _exec(price="1001")))
        finally:
            s.close()

    def test_fee_amend_is_revision_not_new_volume(self, path):
        s = store.Store.open(path)
        try:
            s.write_sync(lambda c: store.insert_execution(c, _exec()))
            s.write_sync(lambda c: store.amend_execution_fee(
                c, "test:a:m:t1", "0.0005", "USDC"))
            row = s.read(lambda c: dict(c.execute(
                "SELECT qty_base, fee_amount FROM executions").fetchone()))
            assert row["qty_base"] == "0.004"
            assert row["fee_amount"] == "0.0005"
        finally:
            s.close()

    def test_concurrent_same_key_single_row(self, path):
        s = store.Store.open(path)
        try:
            barrier = threading.Barrier(8)
            results = []

            def worker(i):
                barrier.wait()
                try:
                    results.append(s.write_sync(
                        lambda c: store.insert_execution(c, _exec()),
                        tx=True))
                except store.StoreError as e:
                    results.append(e)

            threads = [threading.Thread(target=worker, args=(i,))
                       for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            n = s.read(lambda c: c.execute(
                "SELECT COUNT(*) FROM executions").fetchone()[0])
            assert n == 1
            # identical economics -> the losers see the SAME fill as a
            # duplicate, never a second row and never a conflict
            assert results.count("inserted") == 1
            assert results.count("duplicate") == 7
        finally:
            s.close()

    def test_mid_transaction_failure_leaves_nothing(self, path):
        s = store.Store.open(path)
        try:
            def both(c):
                _mk_grid(c, "g1")
                store.insert_execution(c, _exec())
                c.execute(
                    "INSERT INTO order_intents(intent_id, grid_id, "
                    "cause_key, revision, level_index, side, qty_base, "
                    "price, policy, reduce_only, epoch, state, "
                    "created_ts, updated_ts) VALUES(?,?,?,?,?,?,?,?,"
                    "?,?,?,?,?,?)",
                    ("i1", "g1", "cause-1", 1, 0, "buy", "0.01", "100",
                     "post_only", 0, 0, "PLANNED", "t", "t"))
                raise RuntimeError("injected between the two inserts")

            with pytest.raises(RuntimeError):
                s.write_sync(both, tx=True)
        finally:
            s.close()
        # reopen: neither row exists
        s2 = store.Store.open(path)
        try:
            counts = s2.read(lambda c: dict(c.execute(
                "SELECT (SELECT COUNT(*) FROM executions) AS e, "
                "(SELECT COUNT(*) FROM order_intents) AS i").fetchone()))
            assert counts == {"e": 0, "i": 0}
        finally:
            s2.close()


class TestIdempotency:
    def test_same_request_same_payload_replays_result(self, path):
        s = store.Store.open(path)
        try:
            def reg(c):
                return store.register_request(
                    c, owner_scope="console", request_id="req-1",
                    request_hash="h1", result={"grid_id": "g1"})

            r1, dup1 = s.write_sync(reg)
            r2, dup2 = s.write_sync(reg)
            assert dup1 is False and dup2 is True
            assert r1 == r2 == {"grid_id": "g1"}
        finally:
            s.close()

    def test_same_request_different_payload_conflict(self, path):
        s = store.Store.open(path)
        try:
            s.write_sync(lambda c: store.register_request(
                c, owner_scope="console", request_id="req-1",
                request_hash="h1", result={}))
            with pytest.raises(store.StoreError) as e:
                s.write_sync(lambda c: store.register_request(
                    c, owner_scope="console", request_id="req-1",
                    request_hash="h2", result={}))
            assert e.value.code == "IDEMPOTENCY_CONFLICT"
        finally:
            s.close()

    def test_idempotency_map_survives_reopen(self, path):
        s = store.Store.open(path)
        s.write_sync(lambda c: store.register_request(
            c, owner_scope="s", request_id="r", request_hash="h",
            result={"ok": 1}))
        s.close()
        s2 = store.Store.open(path)
        try:
            res, dup = s2.write_sync(lambda c: store.register_request(
                c, owner_scope="s", request_id="r", request_hash="h",
                result={"ok": 1}))
            assert dup is True and res == {"ok": 1}
        finally:
            s2.close()

    def test_cause_key_unique_in_sql(self, path):
        s = store.Store.open(path)

        def intent(c, iid):
            c.execute(
                "INSERT INTO order_intents(intent_id, grid_id, cause_key, "
                "revision, level_index, side, qty_base, price, policy, "
                "reduce_only, epoch, state, created_ts, updated_ts) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (iid, "g1", "fill:123", 1, 0, "buy", "0.01", "100",
                 "post_only", 0, 0, "PLANNED", "t", "t"))

        try:
            s.write_sync(lambda c: _mk_grid(c, "g1"))
            s.write_sync(lambda c: intent(c, "i1"))
            with pytest.raises(sqlite3.IntegrityError):
                s.write_sync(lambda c: intent(c, "i2"))
        finally:
            s.close()


class TestEvents:
    def test_event_key_dedup_append_only(self, path):
        s = store.Store.open(path)
        try:
            seq1, dup1 = s.write_sync(lambda c: store.register_event(
                c, event_key="fill:1", scope="g1", event_type="fill",
                payload={"qty": "0.01"}))
            seq2, dup2 = s.write_sync(lambda c: store.register_event(
                c, event_key="fill:1", scope="g1", event_type="fill",
                payload={"qty": "0.01"}))
            assert seq1 == seq2 and dup1 is False and dup2 is True
            # append-only is enforced in SQL: a guard trigger aborts any
            # UPDATE on the original event rows
            s.write_sync(lambda c: c.execute(
                "CREATE TRIGGER guard_events BEFORE UPDATE ON events "
                "BEGIN SELECT RAISE(ABORT, 'events are append-only'); END"))
            with pytest.raises(sqlite3.IntegrityError):
                s.write_sync(lambda c: c.execute(
                    "UPDATE events SET payload_json = '{}' "
                    "WHERE event_key = 'fill:1'"))
        finally:
            s.close()

    def test_checkpoint_independent_of_events(self, path):
        s = store.Store.open(path)
        try:
            seq, _ = s.write_sync(lambda c: store.register_event(
                c, event_key="e1", scope="g1", event_type="x", payload={}))
            s.write_sync(lambda c: store.set_checkpoint(c, "g1", seq))
            assert s.read(lambda c: store.get_checkpoint(c, "g1")) == seq
            assert s.read(lambda c: c.execute(
                "SELECT payload_json FROM events WHERE event_key='e1'"
            ).fetchone()[0]) == "{}"
        finally:
            s.close()

    def test_counters_persist(self, path):
        s = store.Store.open(path)
        s.write_sync(lambda c: store.bump_counter(c, "epoch"))
        s.write_sync(lambda c: store.bump_counter(c, "epoch", by=3))
        s.close()
        s2 = store.Store.open(path)
        try:
            assert s2.read(lambda c: store.get_counter(c, "epoch")) == 4
        finally:
            s2.close()


class TestGridClaim:
    def test_one_active_grid_per_account_market(self, path):
        s = store.Store.open(path)

        def mk(c, gid, status, account="acc-1"):
            c.execute("INSERT OR IGNORE INTO accounts(account_id, venue,"
                      " deployment, owner, fixture, created_ts)"
                      " VALUES(?,?,?,?,1,'t')",
                      (account, "hl", "", "owner-" + account))
            c.execute(
                "INSERT INTO grids(grid_id, account_id, market_key, status,"
                " desired_mode, created_ts, updated_ts) "
                "VALUES(?,?,?,?,?,'t','t')",
                (gid, account, "M", status, "ACTIVE"))

        try:
            s.write_sync(lambda c: mk(c, "a-running", "RUNNING"))
            # SAME account+market: a second non-closed grid is refused
            with pytest.raises(sqlite3.IntegrityError):
                s.write_sync(lambda c: mk(c, "b-second", "RUNNING"))
            # residual position still claims the market
            s.write_sync(lambda c: c.execute(
                "UPDATE grids SET status='STOPPED_WITH_POSITION' "
                "WHERE grid_id='a-running'"))
            with pytest.raises(sqlite3.IntegrityError):
                s.write_sync(lambda c: mk(c, "c-third", "RUNNING"))
            # CLOSED releases the claim
            s.write_sync(lambda c: c.execute(
                "UPDATE grids SET status='CLOSED' WHERE grid_id='a-running'"))
            s.write_sync(lambda c: mk(c, "d-fourth", "RUNNING"))
            # a different account on the same market is a separate claim
            s.write_sync(lambda c: mk(c, "e-other-acct", "RUNNING",
                                      account="acc-2"))
        finally:
            s.close()


class TestBackupRestore:
    def test_backup_manifest_from_completed_copy(self, path, tmp_path):
        s = store.Store.open(path)
        try:
            s.write_sync(lambda c: store.register_event(
                c, event_key="e1", scope="g", event_type="x", payload={}))
            s.write_sync(lambda c: store.register_event(
                c, event_key="e2", scope="g", event_type="x", payload={}))
        finally:
            s.close()
        dest = str(tmp_path / "backup.sqlite")
        manifest = store.backup(path, dest)
        assert manifest["schema_version"] == store.SCHEMA_VERSION
        assert manifest["last_seq"] == 2
        assert manifest["integrity"] == "ok"
        assert os.path.exists(dest + ".manifest.json")

    def test_restore_creates_recovering_boundary(self, path, tmp_path):
        s = store.Store.open(path)
        try:
            for i in range(3):
                s.write_sync(lambda c, i=i: store.register_event(
                    c, event_key=f"e{i}", scope="g", event_type="x",
                    payload={}))
        finally:
            s.close()
        dest = str(tmp_path / "backup.sqlite")
        store.backup(path, dest)
        live2 = str(tmp_path / "restored.sqlite")
        manifest = store.restore_from_backup(dest, live2)
        assert manifest["last_seq"] == 3
        s2 = store.Store.open(live2)
        try:
            got = s2.read(lambda c: c.execute(
                "SELECT COUNT(*) FROM events").fetchone()[0])
            assert got == 3
        finally:
            s2.close()


class TestWriterThread:
    def test_close_drains_accepted_writes(self, path):
        s = store.Store.open(path)
        s.write_sync(lambda c: c.execute(
            "INSERT INTO counters(name, value) VALUES('x', 1)"))
        s.close()
        # connection closed: further writes are refused
        with pytest.raises(Exception):
            s.write_sync(lambda c: c.execute("SELECT 1"))

    def test_write_error_propagates(self, path):
        s = store.Store.open(path)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                s.write_sync(lambda c: c.execute(
                    "INSERT INTO counters(name) VALUES('x')"))  # value NOT NULL
        finally:
            s.close()
