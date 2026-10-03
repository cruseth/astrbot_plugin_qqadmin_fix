"""欢迎配置去 CQ 化与按群图片上传的后端契约测试。"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import sqlite3
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from PIL import Image as PILImage

from test_legacy_migration import cfg as migration_cfg
from test_regressions import code, run

ROOT = Path(__file__).resolve().parents[1]


def png_bytes(color: str = "red", size: tuple[int, int] = (4, 4)) -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class AwaitableDict(dict):
    def __await__(self):
        async def value():
            return dict(self)

        return value().__await__()


class Upload:
    def __init__(self, filename: str, data: bytes, content_length: int | None = None):
        self.filename = filename
        self._data = data
        self.content_length = len(data) if content_length is None else content_length

    async def read(self) -> bytes:
        return self._data


class Cache:
    def __init__(self, member_count: int = 1):
        self.member_count = member_count
        self.invalidated: list[str | None] = []

    async def get_group(self, group_id: str, force: bool = False) -> dict:
        return {
            "group_id": str(group_id),
            "group_name": f"群 {group_id}",
            "member_count": self.member_count,
        }

    def invalidate(self, group_id: str | None = None) -> None:
        self.invalidated.append(group_id)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def page_web(code):
    saved = dict(sys.modules)
    quart = types.ModuleType("quart")
    request = types.SimpleNamespace(
        files={},
        get_json=AsyncMock(return_value={}),
    )
    quart.jsonify = lambda payload: payload
    quart.request = request
    sys.modules["quart"] = quart
    page_service = load_module(
        "qqadmin_regression.page_service", ROOT / "page_service.py"
    )
    web = load_module("qqadmin_regression.web_upload", ROOT / "web.py")
    try:
        yield types.SimpleNamespace(
            page=page_service,
            web=web,
            request=request,
        )
    finally:
        for name in list(sys.modules):
            if name.startswith("qqadmin_regression.web_upload"):
                sys.modules.pop(name, None)
        sys.modules.pop("qqadmin_regression.page_service", None)
        for name in list(sys.modules):
            if name.startswith("quart"):
                if name in saved:
                    sys.modules[name] = saved[name]
                else:
                    sys.modules.pop(name, None)


@pytest.fixture
def welcome(code):
    return sys.modules["qqadmin_regression.core.welcome"]


def make_service(page_module, code, tmp_path: Path, member_count: int = 1):


    default = {
        "join_welcome": "",
        "join_welcome_mention": True,
        "join_welcome_image": [],
        "join_welcome_image_before": False,
    }
    cfg = types.SimpleNamespace(
        plugin_dir=ROOT,
        data_dir=tmp_path,
        default=default,
        build_group_default_config=lambda: dict(default),
        refresh_runtime_settings=lambda: None,
        save_config=lambda: None,
    )
    db = code.data.QQAdminDB(
        types.SimpleNamespace(db_path=tmp_path / "groups.db", default=default)
    )
    return cfg, db, Cache(member_count)


def test_welcome_config_file_migration(migration_cfg, tmp_path):
    config_path = (
        tmp_path / "config" / f"{migration_cfg._PLUGIN_NAME}_config.json"
    )
    config_path.parent.mkdir(parents=True)
    original = {
        "default": {
            "items": {
                "join_welcome": "欢迎[CQ:image,file=old.png]",
                "join_welcome_cq_mention": False,
                "join_welcome_cq_image": False,
                "join_switch": True,
            }
        },
        "admin_audit": True,
    }
    config_path.write_text(
        json.dumps(original, ensure_ascii=False), encoding="utf-8"
    )

    assert migration_cfg.migrate_welcome_config_file(tmp_path) is True
    migrated = json.loads(config_path.read_text(encoding="utf-8"))
    items = migrated["default"]["items"]
    assert "join_welcome_cq_mention" not in items
    assert "join_welcome_cq_image" not in items
    assert items["join_welcome_mention"] is False
    assert items["join_welcome_image"] == []
    assert items["join_welcome_image_before"] is False
    assert items["join_welcome"] == "欢迎"
    assert items["join_switch"] is True
    assert migrated["admin_audit"] is True

    first_bytes = config_path.read_bytes()
    assert migration_cfg.migrate_welcome_config_file(tmp_path) is False
    assert config_path.read_bytes() == first_bytes


def test_welcome_config_migration_swallows_bad_json(migration_cfg, tmp_path):
    config_path = (
        tmp_path / "config" / f"{migration_cfg._PLUGIN_NAME}_config.json"
    )
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{bad json", encoding="utf-8")
    assert migration_cfg.migrate_welcome_config_file(tmp_path) is False
    assert config_path.read_text(encoding="utf-8") == "{bad json"


def test_group_welcome_migration_is_idempotent(code, tmp_path):
    db_path = tmp_path / "migration.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE groups (group_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    records = {
        "10": {
            "__follow_default__": False,
            "join_welcome": "欢迎[CQ:image,file=old.png]",
            "join_welcome_cq_mention": False,
            "join_welcome_cq_image": False,
        },
        "11": {
            "join_welcome_cq_mention": True,
            "join_welcome_cq_image": True,
        },
    }
    connection.executemany(
        "INSERT INTO groups(group_id, data) VALUES (?, ?)",
        [(gid, json.dumps(data, ensure_ascii=False)) for gid, data in records.items()],
    )
    connection.execute(
        "INSERT INTO groups(group_id, data) VALUES (?, ?)",
        ("12", "not-json"),
    )
    connection.commit()
    connection.close()

    default = {
        "join_welcome": "",
        "join_welcome_mention": True,
        "join_welcome_image": [],
        "join_welcome_image_before": False,
    }

    async def scenario():
        db = code.data.QQAdminDB(
            types.SimpleNamespace(db_path=db_path, default=default)
        )
        await db.init()
        try:
            explicit = db._cache["10"]
            assert explicit["join_welcome_mention"] is False
            assert explicit["join_welcome_image"] == []
            assert explicit["join_welcome_image_before"] is False
            assert explicit["join_welcome"] == "欢迎"
            assert "join_welcome_cq_mention" not in explicit
            assert "join_welcome_cq_image" not in explicit
            assert db._cache["11"] == {}
            assert "12" not in db._cache
        finally:
            await db.close()

        reopened = code.data.QQAdminDB(
            types.SimpleNamespace(db_path=db_path, default=default)
        )
        await reopened.init()
        try:
            assert reopened._cache["10"]["join_welcome_mention"] is False
            assert reopened._cache["11"] == {}
        finally:
            await reopened.close()

    run(scenario())


def test_sanitize_file_list(page_web, code, tmp_path):
    cfg, db, cache = make_service(page_web.page, code, tmp_path)
    service = page_web.page.QQAdminPageService(cfg, db, cache)
    schema = {"type": "file", "default": []}
    assert service._sanitize_value(
        [
            "files/groups/10/join_welcome_image/a.png",
            "files/groups/10/join_welcome_image/a.png",
            "",
            "../escape.png",
            "/abs.png",
            "https://example.com/a.png",
            "files/groups/10/join_welcome_image/b.png",
            "files/groups/10/join_welcome_image/c.png",
            "files/groups/10/join_welcome_image/d.png",
        ],
        schema,
    ) == [
        "files/groups/10/join_welcome_image/a.png",
        "files/groups/10/join_welcome_image/b.png",
        "files/groups/10/join_welcome_image/c.png",
    ]
    with pytest.raises(ValueError):
        service._sanitize_value("not-a-list", schema)


def test_upload_and_delete_group_welcome_image(page_web, code, tmp_path):
    async def scenario():
        cfg, db, cache = make_service(page_web.page, code, tmp_path)
        service = page_web.page.QQAdminPageService(cfg, db, cache)
        await db.init()
        try:
            await db.set("123", "join_welcome_image", [])
            result = await service.upload_group_welcome_image(
                "123", Upload("photo.png", png_bytes())
            )
            path = result["path"]
            assert result["images"] == [path]
            assert result["source"] == "group"
            physical = tmp_path / path
            assert physical.is_file()
            assert db.get_group_snapshot("123")["join_welcome_image"] == [path]

            deleted = await service.delete_group_welcome_image("123", path)
            assert deleted["images"] == []
            assert not physical.exists()
            with pytest.raises(page_web.page.PageRequestError) as exc:
                await service.delete_group_welcome_image("123", path)
            assert exc.value.status == 400
        finally:
            await db.close()

    run(scenario())


def test_concurrent_uploads_are_serialized_per_group(
    page_web, code, tmp_path
):
    async def scenario():
        cfg, db, cache = make_service(page_web.page, code, tmp_path)
        service = page_web.page.QQAdminPageService(cfg, db, cache)
        await db.init()
        try:
            await db.set("123", "join_welcome_image", [])
            original_set = db.set
            first_set_entered = asyncio.Event()
            release_first_set = asyncio.Event()
            second_set_finished = asyncio.Event()
            calls = 0

            async def controlled_set(gid, field, value):
                nonlocal calls
                calls += 1
                call_number = calls
                if call_number == 1:
                    first_set_entered.set()
                    await release_first_set.wait()
                await original_set(gid, field, value)
                if call_number >= 2:
                    second_set_finished.set()

            db.set = controlled_set
            first = asyncio.create_task(
                service.upload_group_welcome_image(
                    "123", Upload("a.png", png_bytes("red"))
                )
            )
            await asyncio.wait_for(first_set_entered.wait(), timeout=1)
            second = asyncio.create_task(
                service.upload_group_welcome_image(
                    "123", Upload("b.png", png_bytes("blue"))
                )
            )
            try:
                await asyncio.wait_for(second_set_finished.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                pass
            release_first_set.set()
            results = await asyncio.gather(first, second)
            images = db.get_group_snapshot("123")["join_welcome_image"]
            assert len(images) == 2
            assert set(images) == {result["path"] for result in results}
            assert all((tmp_path / path).is_file() for path in images)
        finally:
            await db.close()

    run(scenario())


def test_concurrent_deletes_do_not_leave_stale_references(
    page_web, code, tmp_path
):
    async def scenario():
        cfg, db, cache = make_service(page_web.page, code, tmp_path)
        service = page_web.page.QQAdminPageService(cfg, db, cache)
        await db.init()
        paths = [
            "files/groups/123/join_welcome_image/a.png",
            "files/groups/123/join_welcome_image/b.png",
        ]
        directory = tmp_path / "files/groups/123/join_welcome_image"
        directory.mkdir(parents=True)
        for index, path in enumerate(paths):
            (tmp_path / path).write_bytes(
                png_bytes("red" if index == 0 else "blue")
            )
        try:
            await db.set("123", "join_welcome_image", paths)
            original_set = db.set
            first_set_entered = asyncio.Event()
            release_first_set = asyncio.Event()
            second_set_finished = asyncio.Event()
            calls = 0

            async def controlled_set(gid, field, value):
                nonlocal calls
                calls += 1
                call_number = calls
                if call_number == 1:
                    first_set_entered.set()
                    await release_first_set.wait()
                await original_set(gid, field, value)
                if call_number >= 2:
                    second_set_finished.set()

            db.set = controlled_set
            first = asyncio.create_task(
                service.delete_group_welcome_image("123", paths[0])
            )
            await asyncio.wait_for(first_set_entered.wait(), timeout=1)
            second = asyncio.create_task(
                service.delete_group_welcome_image("123", paths[1])
            )
            try:
                await asyncio.wait_for(second_set_finished.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                pass
            release_first_set.set()
            await asyncio.gather(first, second)
            assert db.get_group_snapshot("123")["join_welcome_image"] == []
            assert all(not (tmp_path / path).exists() for path in paths)
        finally:
            await db.close()

    run(scenario())


@pytest.mark.parametrize(
    "group_id,filename,data_kind,member_count,status",
    [
        ("__default__", "a.png", "png", 1, 409),
        ("123", "a.png", "png", 1, 409),
        ("abc", "a.png", "png", 1, 400),
        ("123", "a.png", "png", 0, 404),
        ("123", "a.svg", "png", 1, 415),
        ("123", "a.png", "bad", 1, 422),
        ("123", "a.png", "large", 1, 413),
    ],
)
def test_upload_error_codes(
    page_web, code, tmp_path, group_id, filename, data_kind, member_count, status
):
    async def scenario():
        cfg, db, cache = make_service(page_web.page, code, tmp_path, member_count)
        service = page_web.page.QQAdminPageService(cfg, db, cache)
        await db.init()
        try:
            if not (group_id == "123" and member_count == 1 and status == 409):
                if group_id == "123" and member_count == 1:
                    await db.set("123", "join_welcome_image", [])
            if data_kind == "png":
                payload = png_bytes()
            elif data_kind == "large":
                payload = b"x" * (5 * 1024 * 1024 + 1)
            else:
                payload = b"not-image"
            with pytest.raises(page_web.page.PageRequestError) as exc:
                await service.upload_group_welcome_image(
                    group_id, Upload(filename, payload)
                )
            assert exc.value.status == status
        finally:
            await db.close()

    run(scenario())


def test_upload_rejects_when_three_images(page_web, code, tmp_path):
    async def scenario():
        cfg, db, cache = make_service(page_web.page, code, tmp_path)
        service = page_web.page.QQAdminPageService(cfg, db, cache)
        await db.init()
        try:
            await db.set(
                "123",
                "join_welcome_image",
                [
                    "files/groups/123/join_welcome_image/a.png",
                    "files/groups/123/join_welcome_image/b.png",
                    "files/groups/123/join_welcome_image/c.png",
                ],
            )
            with pytest.raises(page_web.page.PageRequestError) as exc:
                await service.upload_group_welcome_image(
                    "123", Upload("d.png", png_bytes())
                )
            assert exc.value.status == 409
        finally:
            await db.close()

    run(scenario())


def test_delete_default_reference_keeps_physical_file(page_web, code, tmp_path):
    async def scenario():
        cfg, db, cache = make_service(page_web.page, code, tmp_path)
        service = page_web.page.QQAdminPageService(cfg, db, cache)
        await db.init()
        default_rel = "files/default/items/join_welcome_image/a.png"
        default_path = tmp_path / default_rel
        default_path.parent.mkdir(parents=True)
        default_path.write_bytes(png_bytes())
        try:
            await db.set("123", "join_welcome_image", [default_rel])
            deleted = await service.delete_group_welcome_image(
                "123", default_rel
            )
            assert deleted["images"] == []
            assert default_path.is_file()
            assert db.get_group_snapshot("123")["join_welcome_image"] == []
        finally:
            await db.close()

    run(scenario())


def test_delete_rejects_other_group_and_missing_paths(page_web, code, tmp_path):
    async def scenario():
        cfg, db, cache = make_service(page_web.page, code, tmp_path)
        service = page_web.page.QQAdminPageService(cfg, db, cache)
        await db.init()
        try:
            await db.set(
                "123",
                "join_welcome_image",
                ["files/groups/123/join_welcome_image/a.png"],
            )
            with pytest.raises(page_web.page.PageRequestError) as exc:
                await service.delete_group_welcome_image(
                    "123", "files/groups/456/join_welcome_image/a.png"
                )
            assert exc.value.status == 400
            with pytest.raises(page_web.page.PageRequestError) as exc:
                await service.delete_group_welcome_image(
                    "123", "files/default/items/join_welcome_image/a.png"
                )
            assert exc.value.status == 400
            with pytest.raises(page_web.page.PageRequestError) as exc:
                await service.delete_group_welcome_image(
                    "__default__", "files/groups/123/join_welcome_image/a.png"
                )
            assert exc.value.status == 409
        finally:
            await db.close()

    run(scenario())


def test_welcome_message_order_and_at_dedup(welcome, tmp_path):
    data_dir = tmp_path
    default_dir = data_dir / "files/default/items/join_welcome_image"
    group_dir = data_dir / "files/groups/10/join_welcome_image"
    other_dir = data_dir / "files/groups/11/join_welcome_image"
    default_dir.mkdir(parents=True)
    group_dir.mkdir(parents=True)
    other_dir.mkdir(parents=True)
    default_rel = "files/default/items/join_welcome_image/a.png"
    group_rel = "files/groups/10/join_welcome_image/b.png"
    other_rel = "files/groups/11/join_welcome_image/c.png"
    (data_dir / default_rel).write_bytes(png_bytes("red"))
    (data_dir / group_rel).write_bytes(png_bytes("blue"))
    (data_dir / other_rel).write_bytes(png_bytes("green"))

    chain = run(
        welcome.build_welcome(
            "{at}[CQ:at,qq={qq}][CQ:at,qq=20]hi",
            "20",
            "nick",
            data_dir,
            mention=True,
            images=[default_rel, group_rel],
            image_before=False,
            group_id="10",
        )
    )
    assert [p.qq for p in chain if isinstance(p, welcome.At)] == ["20"]
    assert isinstance(chain[1], welcome.Plain)
    assert chain[1].text == "hi"
    assert all(isinstance(item, welcome.Image) for item in chain[2:])

    before = run(
        welcome.build_welcome(
            "hi",
            "20",
            "nick",
            data_dir,
            mention=True,
            images=[default_rel, group_rel],
            image_before=True,
            group_id="10",
        )
    )
    assert isinstance(before[0], welcome.At)
    assert all(isinstance(item, welcome.Image) for item in before[1:3])
    assert isinstance(before[3], welcome.Plain) and before[3].text == "hi"

    images_only = run(
        welcome.build_welcome(
            "",
            "20",
            "nick",
            data_dir,
            mention=True,
            images=[group_rel],
            group_id="10",
        )
    )
    assert isinstance(images_only[0], welcome.At)
    assert isinstance(images_only[1], welcome.Image)

    other = run(
        welcome.build_welcome(
            "hi",
            "20",
            "nick",
            data_dir,
            mention=True,
            images=[other_rel],
            group_id="10",
        )
    )
    assert any(
        isinstance(item, welcome.Plain)
        and item.text == "[欢迎图片不可用]"
        for item in other
    )

    limited = run(
        welcome.build_welcome(
            "hi",
            "20",
            "nick",
            data_dir,
            mention=True,
            images=[default_rel, default_rel, default_rel, default_rel],
            group_id="10",
        )
    )
    assert sum(isinstance(item, welcome.Image) for item in limited) == 3
    assert sum(
        isinstance(item, welcome.Plain)
        and item.text == "[欢迎图片数量超限]"
        for item in limited
    ) == 1


def test_uploaded_image_path_rejections(welcome, tmp_path):
    (tmp_path / "files/default/items/join_welcome_image").mkdir(parents=True)
    (tmp_path / "files/default/items/join_welcome_image/a.png").write_bytes(
        png_bytes()
    )
    for source in (
        "../a.png",
        "/absolute/a.png",
        "C:/a.png",
        "https://example.com/a.png",
        "files/groups/11/join_welcome_image/a.png",
    ):
        with pytest.raises(ValueError):
            welcome.resolve_uploaded_image(source, tmp_path, "10")


def test_build_welcome_does_not_infer_group_id(welcome, tmp_path):
    group_dir = tmp_path / "files/groups/11/join_welcome_image"
    default_dir = tmp_path / "files/default/items/join_welcome_image"
    group_dir.mkdir(parents=True)
    default_dir.mkdir(parents=True)
    group_rel = "files/groups/11/join_welcome_image/a.png"
    default_rel = "files/default/items/join_welcome_image/b.png"
    (tmp_path / group_rel).write_bytes(png_bytes("red"))
    (tmp_path / default_rel).write_bytes(png_bytes("blue"))

    chain = run(
        welcome.build_welcome(
            "", "20", "", tmp_path, images=[group_rel], group_id=None
        )
    )
    assert not any(isinstance(item, welcome.Image) for item in chain)
    assert any(
        isinstance(item, welcome.Plain)
        and item.text == "[欢迎图片不可用]"
        for item in chain
    )

    default_chain = run(
        welcome.build_welcome(
            "", "20", "", tmp_path, images=[default_rel], group_id=None
        )
    )
    assert any(isinstance(item, welcome.Image) for item in default_chain)


def test_uploaded_image_rejects_resolved_path_outside_root(welcome, tmp_path):
    data_dir = tmp_path / "data"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.png").write_bytes(png_bytes())
    parent = data_dir / "files/default/items"
    parent.mkdir(parents=True)
    link = parent / "join_welcome_image"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前系统无权限创建符号链接")

    with pytest.raises(ValueError):
        welcome.resolve_uploaded_image(
            "files/default/items/join_welcome_image/a.png", data_dir, "10"
        )


def test_delete_rejects_resolved_path_outside_root(page_web, code, tmp_path):
    async def scenario():
        data_dir = tmp_path / "data"
        outside = tmp_path / "outside"
        data_dir.mkdir()
        outside.mkdir()
        (outside / "a.png").write_bytes(png_bytes())
        parent = data_dir / "files/groups/123"
        parent.mkdir(parents=True)
        link = parent / "join_welcome_image"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("当前系统无权限创建符号链接")

        cfg, db, cache = make_service(page_web.page, code, data_dir)
        service = page_web.page.QQAdminPageService(cfg, db, cache)
        await db.init()
        rel = "files/groups/123/join_welcome_image/a.png"
        try:
            await db.set("123", "join_welcome_image", [rel])
            with pytest.raises(page_web.page.PageRequestError) as exc:
                await service.delete_group_welcome_image("123", rel)
            assert exc.value.status == 400
            assert (outside / "a.png").is_file()
            assert db.get_group_snapshot("123")["join_welcome_image"] == [rel]
        finally:
            await db.close()

    run(scenario())


def test_web_wrapper_supports_path_params_and_status(page_web):
    controller = page_web.web.QQAdminWebController.__new__(
        page_web.web.QQAdminWebController
    )

    async def handler(group_id: str):
        return {"group_id": group_id}

    assert run(controller._wrap_handler(handler)(group_id="123")) == {
        "group_id": "123"
    }

    async def failure(group_id: str):
        raise page_web.page.PageRequestError("blocked", 409)

    payload, status = run(controller._wrap_handler(failure)(group_id="123"))
    assert status == 409
    assert payload == {"ok": False, "message": "blocked"}


def test_web_upload_delete_handlers(page_web):
    controller = page_web.web.QQAdminWebController.__new__(
        page_web.web.QQAdminWebController
    )
    controller.service = types.SimpleNamespace(
        upload_group_welcome_image=AsyncMock(
            return_value={"path": "files/groups/1/join_welcome_image/a.png"}
        ),
        delete_group_welcome_image=AsyncMock(return_value={"images": []}),
    )
    page_web.web.quart_request_obj = types.SimpleNamespace(
        files=AwaitableDict(file="upload"),
        get_json=AsyncMock(return_value={"path": "files/groups/1/join_welcome_image/a.png"}),
    )

    uploaded = run(controller.page_upload_group_welcome_image("1"))
    assert uploaded["ok"] is True
    controller.service.upload_group_welcome_image.assert_awaited_once_with(
        "1", "upload"
    )

    deleted = run(controller.page_delete_group_welcome_image("1"))
    assert deleted["ok"] is True
    controller.service.delete_group_welcome_image.assert_awaited_once_with(
        "1", "files/groups/1/join_welcome_image/a.png"
    )
