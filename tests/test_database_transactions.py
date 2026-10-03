import asyncio
import copy
import types

import pytest

from test_regressions import code, run


def test_concurrent_add_remove_and_copy_isolation(code, tmp_path):
    async def scenario():
        cfg = types.SimpleNamespace(db_path=tmp_path / "parallel.db", default={"block_ids": []})
        db = code.data.QQAdminDB(cfg)
        await db.init()
        try:
            await asyncio.wait_for(asyncio.gather(
                *(db.add("10", "block_ids", str(i)) for i in range(50)),
                *(db.add("11", "block_ids", str(i)) for i in range(50)),
            ), 10)
            assert len(await db.get("10", "block_ids")) == 50
            result = await db.get("10", "block_ids")
            result.clear()
            assert len(await db.get("10", "block_ids")) == 50
            await asyncio.gather(*(db.remove("10", "block_ids", str(i)) for i in range(25)))
            assert len(await db.get("10", "block_ids")) == 25
        finally:
            await db.close()
        reopened = code.data.QQAdminDB(cfg)
        await reopened.init()
        try:
            assert len(await reopened.get("10", "block_ids")) == 25
            assert len(await reopened.get("11", "block_ids")) == 50
        finally:
            await reopened.close()
    run(scenario())


@pytest.mark.parametrize("operation", [
    "ensure", "set", "replace", "add", "remove", "import", "delete", "follow", "follow_all",
    "get", "all",
])
def test_commit_failure_rolls_back_all_paths(code, tmp_path, operation):
    async def scenario():
        cfg = types.SimpleNamespace(db_path=tmp_path / f"{operation}.db",
                                    default={"block_ids": [], "join_switch": False})
        db = code.data.QQAdminDB(cfg)
        await db.init()
        await db.set("10", "block_ids", ["old"])
        if operation == "all":
            db.default_cfg["new"] = 7
        before = copy.deepcopy(db._cache)
        original = db._conn.commit
        async def fail():
            raise RuntimeError("commit failed")
        db._conn.commit = fail
        operations = {
            "ensure": lambda: db.ensure_group("12"),
            "set": lambda: db.set("10", "block_ids", ["bad"]),
            "replace": lambda: db.replace_group("10", {"block_ids": ["bad"]}),
            "add": lambda: db.add("10", "block_ids", "bad"),
            "remove": lambda: db.remove("10", "block_ids", "old"),
            "import": lambda: db.import_cn_lines("10", "进群黑名单: bad"),
            "delete": lambda: db.delete_group("10"),
            "follow": lambda: db.follow_default("10"),
            "follow_all": lambda: db.follow_default(),
            "get": lambda: db.get("10", "missing", "bad"),
            "all": lambda: db.all("10"),
        }
        with pytest.raises(RuntimeError):
            await operations[operation]()
        assert db._cache == before
        assert not db._conn.in_transaction
        db._conn.commit = original
        await db.set("11", "block_ids", ["good"])
        await db.close()
        reopened = code.data.QQAdminDB(cfg)
        await reopened.init()
        try:
            assert await reopened.get("10", "block_ids") == ["old"]
            assert await reopened.get("11", "block_ids") == ["good"]
            assert "12" not in reopened._cache
            assert "missing" not in reopened._cache["10"]
        finally:
            await reopened.close()
    run(scenario())


def test_cancel_after_sql_waits_for_complete_write(code, tmp_path):
    async def scenario():
        cfg = types.SimpleNamespace(db_path=tmp_path / "cancel.db", default={"block_ids": []})
        db = code.data.QQAdminDB(cfg)
        await db.init()
        entered, release = asyncio.Event(), asyncio.Event()
        original = db._conn.execute
        async def pause(sql, params=()):
            result = await original(sql, params)
            if sql.lstrip().startswith("INSERT"):
                entered.set()
                await release.wait()
            return result
        db._conn.execute = pause
        task = asyncio.create_task(db.add("10", "block_ids", "bad"))
        await asyncio.wait_for(entered.wait(), 5)
        assert "10" not in db._cache
        task.cancel()
        await asyncio.sleep(0)
        assert db._write_lock.locked() and not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not db._conn.in_transaction
        db._conn.execute = original
        await db.add("11", "block_ids", "good")
        await db.close()
        reopened = code.data.QQAdminDB(cfg)
        await reopened.init()
        try:
            assert await reopened.get("10", "block_ids") == ["bad"]
            assert await reopened.get("11", "block_ids") == ["good"]
        finally:
            await reopened.close()
    run(scenario())


def test_close_waits_for_writer(code, tmp_path):
    async def scenario():
        cfg = types.SimpleNamespace(db_path=tmp_path / "close.db", default={"block_ids": []})
        db = code.data.QQAdminDB(cfg)
        await db.init()
        async with db._write_lock:
            writer = asyncio.create_task(db.add("10", "block_ids", "20"))
            closer = asyncio.create_task(db.close())
            await asyncio.sleep(0)
            assert not writer.done() and not closer.done()
        await asyncio.wait_for(asyncio.gather(writer, closer), 5)
        await db.init()
        assert await db.get("10", "block_ids") == ["20"]
        await db.close()
    run(scenario())


def test_cancel_after_real_commit_publishes_cache(code, tmp_path):
    async def scenario():
        cfg = types.SimpleNamespace(db_path=tmp_path / "committed-cancel.db", default={"block_ids": []})
        db = code.data.QQAdminDB(cfg)
        await db.init()
        committed, release = asyncio.Event(), asyncio.Event()
        original = db._conn.commit
        async def commit_then_pause():
            await original()
            committed.set()
            await release.wait()
        db._conn.commit = commit_then_pause
        task = asyncio.create_task(db.add("10", "block_ids", "20"))
        await committed.wait()
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        try:
            assert db.get_group_snapshot("10")["block_ids"] == ["20"]
        except BaseException:
            await db.close()
            raise
        db._conn.commit = original
        await db.add("11", "block_ids", "21")
        await db.close()
        reopened = code.data.QQAdminDB(cfg)
        await reopened.init()
        try:
            assert await reopened.get("10", "block_ids") == ["20"]
            assert await reopened.get("11", "block_ids") == ["21"]
        finally:
            await reopened.close()
    run(scenario())


def test_repeated_cancel_waits_for_rollback(code, tmp_path):
    async def scenario():
        cfg = types.SimpleNamespace(db_path=tmp_path / "double-cancel.db", default={"block_ids": []})
        db = code.data.QQAdminDB(cfg)
        await db.init()
        committing, rolling_back, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        commit, rollback = db._conn.commit, db._conn.rollback
        async def pause_commit():
            committing.set()
            await release.wait()
            raise RuntimeError("commit failed")
        async def pause_rollback():
            rolling_back.set()
            await rollback()
        db._conn.commit, db._conn.rollback = pause_commit, pause_rollback
        task = asyncio.create_task(db.add("10", "block_ids", "bad"))
        await committing.wait()
        task.cancel()
        task.cancel()
        await asyncio.sleep(0)
        assert db._write_lock.locked()
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert rolling_back.is_set()
        assert not db._conn.in_transaction
        db._conn.commit, db._conn.rollback = commit, rollback
        await db.add("11", "block_ids", "good")
        await db.close()
    run(scenario())
