"""使用局部 SDK 替身执行实际插件代码，不启动 AstrBot。"""
import asyncio
import importlib.util
import inspect
import logging
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiocqhttp import Message as CQMessage


ROOT = Path(__file__).resolve().parents[1]

class Image:
    @classmethod
    def fromBytes(cls, data):
        image = cls()
        image.data = data
        return image


def module(name, **attrs):
    value = types.ModuleType(name)
    value.__dict__.update(attrs)
    value.__path__ = []
    sys.modules[name] = value
    return value


class Plain:
    def __init__(self, text):
        self.text = text


class At:
    def __init__(self, qq, name=""):
        self.qq, self.name = qq, name


class Reply:
    pass


class Filters:
    PlatformAdapterType = types.SimpleNamespace(AIOCQHTTP=1)
    EventMessageType = types.SimpleNamespace(GROUP_MESSAGE=1)

    def __getattr__(self, name):
        return lambda *args, **kwargs: lambda fn: fn


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


@pytest.fixture(scope="module")
def code():
    saved = dict(sys.modules)
    log = logging.getLogger("qqadmin-test")
    for name in [
        "astrbot", "astrbot.api", "astrbot.core", "astrbot.core.message",
        "astrbot.core.platform", "astrbot.core.platform.sources",
        "astrbot.core.platform.sources.aiocqhttp", "astrbot.core.star",
        "astrbot.core.star.filter", "astrbot.core.config", "astrbot.core.utils",
    ]:
        module(name)
    sys.modules["astrbot"].logger = log
    sys.modules["astrbot.api"].logger = log
    sys.modules["astrbot.core"].AstrBotConfig = dict
    sys.modules["astrbot.core.config"].AstrBotConfig = dict
    module("astrbot.api.event", filter=Filters())
    module("astrbot.api.star", Context=object, Star=object)
    module("astrbot.core.message.components", Plain=Plain, At=At, Reply=Reply,
           Image=Image, BaseMessageComponent=object)
    module("astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event",
           AiocqhttpMessageEvent=object)
    module("astrbot.core.star.filter.event_message_type",
           EventMessageType=Filters.EventMessageType)
    module("astrbot.core.config.astrbot_config", AstrBotConfig=dict)
    module("astrbot.core.star.context", Context=object)
    module("astrbot.core.star.star_tools", StarTools=object)
    module("astrbot.core.utils.astrbot_path", get_astrbot_plugin_path=lambda: ROOT)
    module("aiocqhttp", CQHttp=object, Message=CQMessage)
    package = module("qqadmin_regression")
    package.__path__ = [str(ROOT)]
    core = module("qqadmin_regression.core")
    core.__path__ = [str(ROOT / "core")]
    cfg = load("qqadmin_regression.config", ROOT / "config.py")
    load("qqadmin_regression.utils", ROOT / "utils.py")
    data = load("qqadmin_regression.data", ROOT / "data.py")
    perm = load("qqadmin_regression.permission", ROOT / "permission.py")
    normal = load("qqadmin_regression.core.normal_handle", ROOT / "core/normal_handle.py")
    notice = load("qqadmin_regression.core.notice_handle", ROOT / "core/notice_handle.py")
    join = load("qqadmin_regression.core.join_handle", ROOT / "core/join_handle.py")
    for name in ["BanproHandle", "CurfewHandle", "FileHandle", "MemberHandle", "RecallHandle"]:
        setattr(core, name, object)
    core.NormalHandle, core.NoticeHandle, core.JoinHandle = (
        normal.NormalHandle, notice.NoticeHandle, join.JoinHandle
    )
    module("qqadmin_regression.group_info_cache", QQGroupInfoCache=object)
    module("qqadmin_regression.web", QQAdminWebController=object)
    main = load("qqadmin_regression.main", ROOT / "main.py")
    # 执行仓库官方命令解析器源码，只替换其运行时导入。
    module("astrbot.core.platform.astr_message_event", AstrMessageEvent=object)
    module("astrbot.core.star.star_handler", StarHandlerMetadata=object)
    sys.modules["astrbot.core.star.filter"].HandlerFilter = object
    module("astrbot.core.star.filter.custom_filter", CustomFilter=object)
    command = load("astrbot.core.star.filter.command",
                   ROOT / "tests/fixtures/astrbot_v4242_command.py")
    yield types.SimpleNamespace(**locals())
    for name in list(sys.modules):
        if name.startswith(("qqadmin_regression", "astrbot", "aiocqhttp")):
            if name in saved:
                sys.modules[name] = saved[name]
            else:
                sys.modules.pop(name, None)


class Event:
    def __init__(self, parts=None):
        self.parts = parts or [Plain("禁言 "), At("20")]
        self.bot = types.SimpleNamespace(
            set_group_ban=AsyncMock(), set_group_kick=AsyncMock(),
            _send_group_notice=AsyncMock(),
            get_group_member_info=AsyncMock(return_value={"role": "member", "nickname": "{at}"}),
        )
        self.message_str = "禁言 @带 空格(20)"
        self.message_obj = types.SimpleNamespace(raw_message={})
        self.platform_meta = types.SimpleNamespace(name="aiocqhttp")
        self.stopped = False
        self.is_at_or_wake_command = True
        self.extra = {}
        self.send = AsyncMock()

    def get_messages(self):
        return self.parts

    def get_message_str(self):
        return self.message_str

    def set_extra(self, key, value):
        self.extra[key] = value

    def get_group_id(self):
        return "10"

    def get_self_id(self):
        return "99"

    def get_sender_id(self):
        return "30"

    def is_private_chat(self):
        return False

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text

    def chain_result(self, chain):
        return chain


def run(coro):
    return asyncio.run(coro)


async def collect(gen):
    return [item async for item in gen]


@pytest.fixture
def setup(code, monkeypatch):
    db = types.SimpleNamespace(get_group_snapshot=lambda gid: {"random_ban_time": "77~77"})
    cfg = object.__new__(code.cfg.PluginConfig)
    normal = code.normal.NormalHandle(cfg, db)
    manager = code.perm.perm_manager
    async def level(event, uid):
        return code.perm.PermLevel.ADMIN if str(uid) == "99" else code.perm.PermLevel.MEMBER
    monkeypatch.setattr(manager, "get_perm_level", level)
    monkeypatch.setattr(manager, "llm_perm_block", AsyncMock(return_value=None))
    monkeypatch.setattr(manager, "perm_block", AsyncMock(return_value=None))
    monkeypatch.setattr(manager, "_initialized", True)
    plugin = object.__new__(code.main.QQAdminPlugin)
    plugin.normal = normal
    plugin.notice = code.notice.NoticeHandle(plugin, types.SimpleNamespace(group_notice_dir=ROOT))
    return plugin, manager


@pytest.mark.parametrize("duration", [-1, 2592001, True, False, 1.2, 60.0, "x", "1.2", "-1", ""])
def test_invalid_duration(code, setup, duration):
    event = Event()
    result = run(setup[0].normal.set_group_ban(event, duration))
    assert "整数" in result
    event.bot.set_group_ban.assert_not_awaited()


@pytest.mark.parametrize("plain, parsed, expected", [
    ("禁言 ", "@成员(20)", 77),
    ("禁言 60 ", 60, 60),
    ("/禁言 ", "@带", 77),
    ("禁言 0 ", 0, 0),
])
def test_command_structured_args(code, setup, plain, parsed, expected):
    plugin, manager = setup
    event = Event([At("99"), Plain(plain), At("20", "带 空格"), At("20")])
    parser = code.command.CommandFilter("禁言",
        handler_md=types.SimpleNamespace(handler=plugin.__class__.set_group_ban))
    assert inspect.signature(plugin.set_group_ban).parameters["ban_time"].annotation == int | str | None
    event.message_str = f"禁言 {parsed} @带 空格(20)"
    assert parser.filter(event, {})
    converted = event.extra["parsed_params"]
    replies = run(collect(plugin.set_group_ban(event, **converted)))
    assert event.stopped and replies
    event.bot.set_group_ban.assert_awaited_once_with(group_id=10, user_id=20, duration=expected)
    assert manager.llm_perm_block.await_args.kwargs["perm_key"] == (
        "cancel_group_ban" if expected == 0 else "set_group_ban")
    assert ("解除禁言" if expected == 0 else f"{expected}秒") in replies[0]


@pytest.mark.parametrize("parts", [
    [Plain("禁言 wrong "), At("20")],
    [Plain("禁言 -1 "), At("20")],
    [Plain("禁言 60.0 "), At("20")],
])
def test_invalid_explicit_command(setup, parts):
    plugin, _ = setup
    event = Event(parts)
    assert "整数" in run(collect(plugin.set_group_ban(event, "wrong")))[0]
    event.bot.set_group_ban.assert_not_awaited()


def test_cancel_permission_and_feedback(setup):
    plugin, manager = setup
    event = Event()
    assert "解除禁言" in run(collect(plugin.cancel_group_ban(event)))[0]
    assert manager.perm_block.await_args.kwargs["perm_key"] == "cancel_group_ban"
    assert event.stopped


@pytest.mark.parametrize("duration,key", [(60, "set_group_ban"), (0, "cancel_group_ban")])
def test_tool_cannot_skip_auth(setup, duration, key):
    plugin, manager = setup
    manager.llm_perm_block.return_value = "你没权限"
    event = Event()
    assert run(collect(plugin.llm_set_group_ban(event, 20, duration, False))) == ["你没权限"]
    assert manager.llm_perm_block.await_args.kwargs["perm_key"] == key
    assert not event.stopped
    event.bot.set_group_ban.assert_not_awaited()


def test_explicit_target_permission(code, setup, monkeypatch):
    plugin, manager = setup
    monkeypatch.setattr(manager, "get_perm_level", AsyncMock(return_value=code.perm.PermLevel.ADMIN))
    event = Event()
    assert "动不了" in run(collect(plugin.llm_set_group_ban(event, 21, 60)))[0]
    event.bot.set_group_ban.assert_not_awaited()
    assert not event.stopped


def test_unknown_explicit_target_denied(code, setup, monkeypatch):
    plugin, manager = setup
    async def level(event, uid):
        return code.perm.PermLevel.ADMIN if str(uid) == "99" else code.perm.PermLevel.UNKNOWN
    monkeypatch.setattr(manager, "get_perm_level", level)
    event = Event()
    assert "操作失败" in run(collect(plugin.llm_set_group_ban(event, 21, 60)))[0]
    event.bot.set_group_ban.assert_not_awaited()


def test_ban_tool_success_failure_and_no_target(setup):
    plugin, _ = setup
    event = Event()
    assert "60秒" in run(collect(plugin.llm_set_group_ban(event, 20, "60")))[0]
    assert not event.stopped
    event.bot.set_group_ban.side_effect = RuntimeError("offline")
    assert "失败" in run(plugin.normal.set_group_ban(event, 60, 20))
    event.parts = [Plain("禁言")]
    assert "未指定" in run(plugin.normal.set_group_ban(event, 60))


def test_notice_command_tool_and_api_failure(setup):
    plugin, _ = setup
    event = Event()
    assert run(collect(plugin.llm_send_group_notice(event, "正文"))) == ["群公告已发布"]
    event.bot._send_group_notice.assert_awaited_once_with(group_id=10, content="正文")
    assert not event.stopped
    event.message_str = "发布群公告 正文"
    assert run(collect(plugin.send_group_notice(event))) == ["群公告已发布"]
    assert event.stopped
    event.bot._send_group_notice.side_effect = RuntimeError("offline")
    assert run(plugin.notice.send_group_notice(event, "正文")) == "群公告发布失败"


def test_notice_image(code, setup, monkeypatch, tmp_path):
    plugin, _ = setup
    monkeypatch.setattr(code.notice, "download_file", AsyncMock(return_value=tmp_path / "image.png"))
    event = Event()
    assert run(plugin.notice.send_group_notice(event, "正文", "https://image")) == "群公告已发布"
    assert event.bot._send_group_notice.await_args.kwargs["image"] == str(tmp_path / "image.png")
    code.notice.download_file.return_value = None
    event.bot._send_group_notice.reset_mock()
    assert run(plugin.notice.send_group_notice(event, "正文", "https://image")) == "图片获取失败"
    event.bot._send_group_notice.assert_not_awaited()


def test_block_persists_and_partial_failures(code, setup, tmp_path):
    plugin, _ = setup
    async def scenario():
        config = types.SimpleNamespace(db_path=tmp_path / "groups.db",
                                       default={"block_ids": [], "join_switch": False})
        db = code.data.QQAdminDB(config)
        await db.init()
        plugin.normal.db = db
        event = Event([Plain("群拉黑 "), At("20"), At("20"), At("21")])
        async def kick(**kwargs):
            if kwargs["user_id"] == 21:
                raise RuntimeError("offline")
        event.bot.set_group_kick.side_effect = kick
        replies = await collect(plugin.set_group_block(event))
        assert "拉黑" in replies[0] and "踢出失败" in replies[0]
        assert event.bot.set_group_kick.await_count == 2
        assert await db.get("10", "block_ids") == ["20"]
        assert await db.get("10", "join_switch") is False
        await db.close()
        db = code.data.QQAdminDB(config)
        await db.init()
        try:
            assert await db.get("10", "block_ids") == ["20"]
            assert await code.join.JoinHandle(config, db).should_approve("10", "20") == (False, "黑名单用户")
            plugin.normal.db = db
            event.bot.set_group_kick.side_effect = None
            plugin.normal.db.add = AsyncMock(side_effect=RuntimeError("disk full"))
            assert "保存失败" in (await collect(plugin.llm_set_group_block(event, 22)))[0]
        finally:
            await db.close()
    run(scenario())


def test_welcome_components_and_failure_isolation(code, tmp_path):
    async def scenario():
        db = types.SimpleNamespace(get=AsyncMock(side_effect=lambda gid, key, default=None:
            {"join_welcome": "欢迎{at} {qq} {nickname} {unknown} [CQ:at,qq=1]",
             "join_ban_time": 60}.get(key, default)),
            get_group_snapshot=lambda gid: {})
        handle = code.join.JoinHandle(types.SimpleNamespace(welcome_image_dir=tmp_path), db)
        event = Event()
        event.message_obj.raw_message = {"notice_type": "group_increase", "group_id": 10, "user_id": 20}
        await handle.event_monitoring(event)
        chain = event.send.await_args.args[0]
        assert len([item for item in chain if isinstance(item, At)]) == 2
        text = "".join(item.text for item in chain if isinstance(item, Plain))
        assert "20 {at} {unknown}" in text
        event.bot.set_group_ban.assert_awaited_once()
        event.bot.set_group_ban.reset_mock()
        event.send.side_effect = RuntimeError("offline")
        await handle.event_monitoring(event)
        event.bot.set_group_ban.assert_awaited_once()
    run(scenario())


def test_welcome_upload_chinese_field_map(code, tmp_path):
    field_map = code.data.QQAdminDB.FIELD_MAP
    assert field_map["join_welcome_mention"] == "欢迎时 @ 新成员"
    assert field_map["join_welcome_image"] == "欢迎图片"
    assert field_map["join_welcome_image_before"] == "欢迎图片放在欢迎语之前"
    reverse = code.data.QQAdminDB.REVERSE_FIELD_MAP
    assert reverse["欢迎时 @ 新成员"] == "join_welcome_mention"
    assert reverse["欢迎图片"] == "join_welcome_image"
    assert reverse["欢迎图片放在欢迎语之前"] == "join_welcome_image_before"

    async def scenario():
        cfg = types.SimpleNamespace(
            db_path=tmp_path / "field-map.db",
            default={"join_welcome_mention": True, "join_welcome_image": [], "join_welcome_image_before": False},
        )
        db = code.data.QQAdminDB(cfg)
        await db.init()
        try:
            exported = await db.export_cn_lines("10")
            assert "欢迎时 @ 新成员: 开" in exported
            assert "欢迎图片: " in exported
            assert "join_welcome_mention" not in exported
            updated = await db.import_cn_lines(
                "10", "欢迎时 @ 新成员: 关\n欢迎图片放在欢迎语之前: 开")
            assert updated["join_welcome_mention"] is False
            assert updated["join_welcome_image_before"] is True
        finally:
            await db.close()
    run(scenario())


def test_real_permission_mapping(code):
    async def scenario():
        manager = code.perm.PermissionManager()
        cfg = types.SimpleNamespace(admins_id=[])
        db = types.SimpleNamespace(get_group_snapshot=lambda gid: {
            "perms": {"set_group_ban": "成员", "cancel_group_ban": "群主",
                      "set_group_card": "群主"}, "level_threshold": 50})
        manager.lazy_init(cfg, db)
        event = Event()
        async def info(**kwargs):
            return {"role": "admin" if kwargs["user_id"] == 99 else "member"}
        event.bot.get_group_member_info.side_effect = info
        assert await manager.llm_perm_block(event, "set_group_ban") is None
        assert "群主" in await manager.llm_perm_block(event, "cancel_group_ban")
        event.bot.get_group_member_info.side_effect = None
        event.bot.get_group_member_info.return_value = {"role": "member"}
        assert "我没" in await manager.llm_perm_block(event, "set_group_ban")
    run(scenario())


def test_failed_database_write_does_not_poison_retry(code, tmp_path):
    async def scenario():
        cfg = types.SimpleNamespace(db_path=tmp_path / "retry.db", default={"block_ids": []})
        db = code.data.QQAdminDB(cfg)
        await db.init()
        original_save = db._save_to_db
        db._save_to_db = AsyncMock(side_effect=RuntimeError("disk full"))
        with pytest.raises(RuntimeError):
            await db.add("10", "block_ids", "20")
        assert await db.get("10", "block_ids") == []
        db._save_to_db = original_save
        await db.add("10", "block_ids", "20")
        await db.add("10", "block_ids", "20")
        await db.close()
        reloaded = code.data.QQAdminDB(cfg)
        await reloaded.init()
        try:
            assert await reloaded.get("10", "block_ids") == ["20"]
        finally:
            await reloaded.close()
    run(scenario())


@pytest.mark.parametrize("name,args", [
    ("llm_set_group_card", (20, "name")),
    ("llm_set_group_special_title", (20, "title")),
    ("llm_set_group_whole_ban", (True,)),
    ("llm_set_essence_msg", (123,)),
    ("llm_get_essence_msg_list", ()),
    ("llm_set_group_name", ("name",)),
    ("llm_set_group_portrait", ("image",)),
    ("llm_send_group_notice", ("text",)),
    ("llm_get_group_notice", ()),
    ("llm_upload_group_file", ("file",)),
    ("llm_delete_group_file", ("file",)),
    ("llm_view_group_file", ()),
])
def test_all_tools_force_auth(setup, name, args):
    plugin, manager = setup
    manager.llm_perm_block.return_value = "拒绝"
    event = Event()
    assert run(collect(getattr(plugin, name)(event, *args, need_auth=False))) == ["拒绝"]
    assert not event.stopped


@pytest.mark.parametrize("private,platform", [(True, "aiocqhttp"), (False, "telegram")])
def test_unsupported_ban_does_not_stop(setup, private, platform):
    plugin, manager = setup
    event = Event()
    event.is_private_chat = lambda: private
    event.platform_meta.name = platform
    assert run(collect(plugin.set_group_ban(event))) == []
    assert not event.stopped
    manager.llm_perm_block.assert_not_awaited()


@pytest.mark.parametrize("method", ["llm_set_group_kick", "llm_set_group_block"])
def test_kick_block_explicit_target_auth(code, setup, monkeypatch, method):
    plugin, manager = setup
    monkeypatch.setattr(manager, "get_perm_level", AsyncMock(return_value=code.perm.PermLevel.ADMIN))
    event = Event()
    assert "动不了" in run(collect(getattr(plugin, method)(event, 20, reason="违规")))[0]
    event.bot.set_group_kick.assert_not_awaited()


def test_manual_reason_commands_tools_and_failure(setup):
    plugin, _ = setup
    event = Event([Plain("禁言 60 重复 刷屏 "), At("20")])
    replies = run(collect(plugin.set_group_ban(event)))
    assert "理由：重复 刷屏" in replies[0]
    assert "理由" not in run(collect(plugin.llm_set_group_ban(event, 20, 0, reason="忽略")))[0]
    assert "理由：违规" in run(collect(plugin.llm_set_group_kick(event, 20, reason="违规")))[0]
    event.bot.set_group_kick.side_effect = RuntimeError("offline")
    result = run(collect(plugin.llm_set_group_kick(event, 20, reason="违规")))[0]
    assert "失败" in result and "理由" not in result


@pytest.mark.parametrize("command,method", [
    ("踢了", "set_group_kick"), ("群拉黑", "set_group_block"),
])
def test_reason_kick_block_real_filter_dispatch(code, setup, command, method):
    plugin, _ = setup
    plugin.normal.db = types.SimpleNamespace(add=AsyncMock())
    event = Event([At("99"), Plain(f"{command} 重复 刷屏 "), At("20", "昵称 空格")])
    event.message_str = f"{command} 重复 刷屏 @昵称 空格(20)"
    handler = getattr(plugin.__class__, method)
    parser = code.command.CommandFilter(command, handler_md=types.SimpleNamespace(handler=handler))
    assert parser.filter(event, {})
    replies = run(collect(getattr(plugin, method)(event, **event.extra["parsed_params"])))
    assert "理由：重复 刷屏" in replies[0]
    event.bot.set_group_kick.assert_awaited_once()


def test_welcome_write_requires_admin_even_low_config(setup):
    plugin, _ = setup
    plugin.join = types.SimpleNamespace(handle_join_welcome=AsyncMock())
    event = Event()
    event.message_str = "进群欢迎 [CQ:image,file=local]"
    assert "管理员" in run(collect(plugin.handle_join_welcome(event)))[0]
    plugin.join.handle_join_welcome.assert_not_awaited()


@pytest.mark.parametrize("method,raw,target,level,allowed", [
    ("set_config", "群管配置 进群欢迎词: [CQ:at,qq=all]", None, "MEMBER", False),
    ("set_config", "群管配置 进群欢迎词: text", None, "ADMIN", True),
    ("set_config", "群管配置 11 进群欢迎词: text", None, "ADMIN", False),
    ("set_config", "群管配置 11 进群欢迎词: text", None, "SUPERUSER", True),
    ("reset_config", "", None, "MEMBER", False),
    ("reset_config", "", None, "ADMIN", True),
    ("reset_config", "", "11", "ADMIN", False),
    ("reset_config", "", "11", "SUPERUSER", True),
    ("reset_config", "", "all", "ADMIN", False),
    ("reset_config", "", "all", "SUPERUSER", True),
])
def test_config_hard_floor_and_cross_group(code, setup, monkeypatch, method, raw, target, level, allowed):
    plugin, manager = setup
    plugin.db = types.SimpleNamespace(
        import_cn_lines=AsyncMock(), export_cn_lines=AsyncMock(return_value="配置"),
        reset_to_default=AsyncMock(),
    )
    monkeypatch.setattr(manager, "get_perm_level", AsyncMock(return_value=getattr(code.perm.PermLevel, level)))
    event = Event()
    event.message_str = raw
    args = (target,) if method == "reset_config" else ()
    replies = run(collect(getattr(plugin, method)(event, *args)))
    operation = plugin.db.reset_to_default if method == "reset_config" else plugin.db.import_cn_lines
    assert operation.await_count == int(allowed)
    assert replies


@pytest.mark.parametrize("seconds", ["oops", "-1", "2592001", "1.2", "true"])
def test_invalid_ban_number_with_reason_real_dispatch(code, setup, seconds):
    plugin, _ = setup
    event = Event([Plain(f"禁言 {seconds} 原因 "), At("20")])
    event.message_str = f"禁言 {seconds} 原因 @成员(20)"
    parser = code.command.CommandFilter("禁言",
        handler_md=types.SimpleNamespace(handler=plugin.__class__.set_group_ban))
    assert parser.filter(event, {})
    result = run(collect(plugin.set_group_ban(event, **event.extra["parsed_params"])))
    assert "整数" in result[0]
    event.bot.set_group_ban.assert_not_awaited()
