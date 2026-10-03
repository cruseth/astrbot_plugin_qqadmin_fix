"""插件更名（astrbot_plugin_qqadmin -> astrbot_plugin_qqadmin_fix）数据迁移测试。

只加载 config.py 中的迁移纯函数，并额外用一个受控桩环境导入真实 main.py，
验证「导入期自动触发迁移」这一调用点。测试不依赖真实 AstrBot 运行时。
"""
import ast
import importlib.util
import logging
import os
import shutil
import stat
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _module(name, **attrs):
    value = types.ModuleType(name)
    value.__dict__.update(attrs)
    value.__path__ = []
    sys.modules[name] = value
    return value


@pytest.fixture(scope="module")
def cfg():
    """加载 config.py，仅提供它导入所需的最小 AstrBot 桩。"""
    saved = dict(sys.modules)
    log = logging.getLogger("qqadmin-migration-test")
    for name in [
        "astrbot", "astrbot.api", "astrbot.core", "astrbot.core.config",
        "astrbot.core.star", "astrbot.core.utils",
    ]:
        _module(name)
    sys.modules["astrbot"].logger = log
    sys.modules["astrbot.api"].logger = log
    _module("astrbot.core.config.astrbot_config", AstrBotConfig=dict)
    _module("astrbot.core.star.context", Context=object)
    _module("astrbot.core.star.star_tools", StarTools=object)
    _module("astrbot.core.utils.astrbot_path", get_astrbot_plugin_path=lambda: ROOT)
    spec = importlib.util.spec_from_file_location("qqadmin_migration_config", ROOT / "config.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["qqadmin_migration_config"] = module
    spec.loader.exec_module(module)
    yield module
    for name in list(sys.modules):
        if name.startswith(("astrbot", "qqadmin_migration_config")):
            if name in saved:
                sys.modules[name] = saved[name]
            else:
                sys.modules.pop(name, None)


def _write_legacy_config(root, text, *, bom=False):
    path = root / "config" / "astrbot_plugin_qqadmin_config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    if bom:
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    return path


def _write_legacy_data(root):
    old = root / "plugin_data" / "astrbot_plugin_qqadmin"
    (old / "welcome_images").mkdir(parents=True, exist_ok=True)
    (old / "qqadmin_data_v3.db").write_bytes(b"SQLite legacy payload")
    (old / "curfew_data.json").write_text('{"10": "23:00"}', encoding="utf-8")
    (old / "welcome_images" / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return old


def _snapshot(root):
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# ---------- 配置迁移 ----------


def test_config_migrates_when_new_absent(cfg, tmp_path):
    old = _write_legacy_config(tmp_path, '{"欢迎语": "你好", "阈值": 5}', bom=True)
    new = tmp_path / "config" / f"{cfg._PLUGIN_NAME}_config.json"
    assert cfg.migrate_legacy_config_file(tmp_path) is True
    assert new.exists()
    assert new.read_bytes() == old.read_bytes()  # 含 BOM 与中文键值原样保留
    assert old.exists()  # 旧配置不删除


def test_config_does_not_overwrite_existing_new_file(cfg, tmp_path):
    old = _write_legacy_config(tmp_path, "旧内容")
    new = tmp_path / "config" / f"{cfg._PLUGIN_NAME}_config.json"
    new.write_text("新内容", encoding="utf-8")
    assert cfg.migrate_legacy_config_file(tmp_path) is False
    assert new.read_text(encoding="utf-8") == "新内容"
    assert old.exists()


def test_config_noop_when_both_absent(cfg, tmp_path):
    assert cfg.migrate_legacy_config_file(tmp_path) is False
    assert not (tmp_path / "config").exists()


def test_config_copy_failure_is_swallowed(cfg, tmp_path, monkeypatch):
    _write_legacy_config(tmp_path, "旧内容")

    new = tmp_path / "config" / f"{cfg._PLUGIN_NAME}_config.json"
    tmp = new.parent / f".{new.name}.tmp"
    real_copy2 = shutil.copy2

    def fail_after_partial_write(src, dst, *args, **kwargs):
        Path(dst).write_bytes(Path(src).read_bytes()[:2])
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copy2", fail_after_partial_write)
    assert cfg.migrate_legacy_config_file(tmp_path) is False
    assert not new.exists()
    assert not tmp.exists()

    monkeypatch.setattr(shutil, "copy2", real_copy2)
    assert cfg.migrate_legacy_config_file(tmp_path) is True
    assert new.read_text(encoding="utf-8") == "旧内容"


def test_config_migration_makes_read_only_copy_writable(cfg, tmp_path):
    old = _write_legacy_config(tmp_path, '{"阈值": 5}')
    old.chmod(stat.S_IREAD)
    new = tmp_path / "config" / f"{cfg._PLUGIN_NAME}_config.json"

    assert cfg.migrate_legacy_config_file(tmp_path) is True
    assert new.stat().st_mode & stat.S_IWRITE
    assert new.read_text(encoding="utf-8") == '{"阈值": 5}'


# ---------- 数据目录迁移 ----------


def test_data_dir_moves_when_new_absent(cfg, tmp_path):
    old = _write_legacy_data(tmp_path)
    new = tmp_path / "plugin_data" / cfg._PLUGIN_NAME
    assert cfg.migrate_legacy_data_dir(tmp_path) is True
    assert not old.exists()
    assert (new / "qqadmin_data_v3.db").read_bytes() == b"SQLite legacy payload"
    assert (new / "welcome_images" / "pic.png").read_bytes() == b"\x89PNG\r\n\x1a\n"


def test_data_dir_takes_over_empty_new_dir(cfg, tmp_path):
    old = _write_legacy_data(tmp_path)
    new = tmp_path / "plugin_data" / cfg._PLUGIN_NAME
    new.mkdir(parents=True)
    assert cfg.migrate_legacy_data_dir(tmp_path) is True
    assert not old.exists()
    assert (new / "qqadmin_data_v3.db").read_bytes() == b"SQLite legacy payload"
    # 不能出现 new/<旧目录名> 的嵌套残留
    assert not (new / cfg._LEGACY_PLUGIN_NAME).exists()


def test_data_dir_skips_nonempty_new_dir(cfg, tmp_path):
    old = _write_legacy_data(tmp_path)
    new = tmp_path / "plugin_data" / cfg._PLUGIN_NAME
    new.mkdir(parents=True)
    (new / "keep.txt").write_text("新版数据", encoding="utf-8")
    assert cfg.migrate_legacy_data_dir(tmp_path) is False
    assert (new / "keep.txt").read_text(encoding="utf-8") == "新版数据"
    assert (old / "qqadmin_data_v3.db").exists()
    assert not (new / "qqadmin_data_v3.db").exists()


def test_data_dir_migrates_over_auto_created_skeleton(cfg, tmp_path):
    old = _write_legacy_data(tmp_path)
    new = tmp_path / "plugin_data" / cfg._PLUGIN_NAME
    for name in ("group_notice", "file", "welcome_images"):
        (new / name).mkdir(parents=True, exist_ok=True)
    (new / "curfew_data.json").write_text("{}", encoding="utf-8")

    assert cfg.migrate_legacy_data_dir(tmp_path) is True
    assert old.exists()
    assert (new / "qqadmin_data_v3.db").read_bytes() == b"SQLite legacy payload"
    assert (new / "welcome_images" / "pic.png").exists()
    assert (new / "curfew_data.json").read_text(encoding="utf-8") == '{"10": "23:00"}'


def test_data_dir_skips_when_curfew_file_is_not_utf8(cfg, tmp_path):
    """curfew_data.json 不可读或非 UTF-8 时，不得因解码异常中止迁移。"""
    _write_legacy_data(tmp_path)
    new = tmp_path / "plugin_data" / cfg._PLUGIN_NAME
    for name in ("group_notice", "file", "welcome_images"):
        (new / name).mkdir(parents=True, exist_ok=True)
    (new / "curfew_data.json").write_bytes(b"\xff\xfe\x00\x01")
    before = _snapshot(tmp_path)

    # 无法确认为空骨架时保守跳过，且双方数据保持不变。
    assert cfg.migrate_legacy_data_dir(tmp_path) is False
    assert _snapshot(tmp_path) == before


def test_data_dir_skips_new_dir_with_real_data(cfg, tmp_path):
    _write_legacy_data(tmp_path)
    new = tmp_path / "plugin_data" / cfg._PLUGIN_NAME
    new.mkdir(parents=True)
    (new / "qqadmin_data_v3.db").write_bytes(b"new database")
    before = _snapshot(tmp_path)

    assert cfg.migrate_legacy_data_dir(tmp_path) is False
    assert _snapshot(tmp_path) == before
    assert (new / "qqadmin_data_v3.db").read_bytes() == b"new database"


def test_data_dir_noop_when_old_absent(cfg, tmp_path):
    assert cfg.migrate_legacy_data_dir(tmp_path) is False
    assert not (tmp_path / "plugin_data").exists()


def test_data_dir_skips_when_new_path_is_file(cfg, tmp_path):
    old = _write_legacy_data(tmp_path)
    new = tmp_path / "plugin_data" / cfg._PLUGIN_NAME
    new.parent.mkdir(parents=True, exist_ok=True)
    new.write_text("被占用", encoding="utf-8")
    assert cfg.migrate_legacy_data_dir(tmp_path) is False
    assert new.read_text(encoding="utf-8") == "被占用"
    assert old.exists()


def test_data_dir_falls_back_to_copy_when_move_fails(cfg, tmp_path, monkeypatch):
    old = _write_legacy_data(tmp_path)
    new = tmp_path / "plugin_data" / cfg._PLUGIN_NAME

    def boom(*args, **kwargs):
        raise OSError("locked by another process")

    monkeypatch.setattr(os, "rename", boom)
    assert cfg.migrate_legacy_data_dir(tmp_path) is True
    assert old.exists()  # 回退复制保留旧目录
    assert (new / "qqadmin_data_v3.db").read_bytes() == b"SQLite legacy payload"
    assert (new / "welcome_images" / "pic.png").exists()


# ---------- 总入口 ----------


def test_migrate_legacy_state_is_idempotent(cfg, tmp_path):
    _write_legacy_config(tmp_path, '{"欢迎语": "你好"}')
    _write_legacy_data(tmp_path)
    cfg.migrate_legacy_state(tmp_path)
    new_cfg = tmp_path / "config" / f"{cfg._PLUGIN_NAME}_config.json"
    new_data = tmp_path / "plugin_data" / cfg._PLUGIN_NAME
    first = (new_cfg.read_bytes(), _snapshot(new_data))
    cfg.migrate_legacy_state(tmp_path)  # 第二次应为 no-op
    assert (new_cfg.read_bytes(), _snapshot(new_data)) == first
    assert not (tmp_path / "plugin_data" / cfg._LEGACY_PLUGIN_NAME).exists()


def test_migrate_legacy_state_never_raises(cfg, tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("unexpected failure")

    # 路径推导抛出非预期异常：子函数自身不捕获，必须由总入口兜底
    monkeypatch.setattr(cfg, "_resolve_data_root", boom)
    cfg.migrate_legacy_state(tmp_path)
    monkeypatch.undo()

    # 子函数直接抛出非预期异常：同样不得外抛
    monkeypatch.setattr(cfg, "migrate_legacy_config_file", boom)
    monkeypatch.setattr(cfg, "migrate_legacy_data_dir", boom)
    cfg.migrate_legacy_state(tmp_path)


# ---------- 导入期调用点集成 ----------


class _Filters:
    PlatformAdapterType = types.SimpleNamespace(AIOCQHTTP=1)
    EventMessageType = types.SimpleNamespace(GROUP_MESSAGE=1)

    def __getattr__(self, name):
        return lambda *args, **kwargs: (lambda fn: fn)


def _perm_required(*args, **kwargs):
    return lambda fn: fn


class _PermLevel:
    MEMBER = 0
    ADMIN = 1
    OWNER = 2
    SUPERUSER = 3
    UNKNOWN = 4


@pytest.fixture
def imported_main(monkeypatch, tmp_path):
    """在受控桩下导入真实 main.py，返回迁移前后的关键路径。"""
    data_root = tmp_path / "data"
    plugins_dir = data_root / "plugins"
    plugins_dir.mkdir(parents=True)

    old_cfg = data_root / "config" / "astrbot_plugin_qqadmin_config.json"
    old_cfg.parent.mkdir(parents=True, exist_ok=True)
    old_cfg.write_text('{"欢迎语": "旧配置"}', encoding="utf-8-sig")
    old_data = _write_legacy_data(data_root)

    log = logging.getLogger("qqadmin-main-migration-test")

    def stub(name, **attrs):
        value = types.ModuleType(name)
        value.__dict__.update(attrs)
        value.__path__ = []
        monkeypatch.setitem(sys.modules, name, value)
        return value

    for name in [
        "astrbot", "astrbot.api", "astrbot.core", "astrbot.core.message",
        "astrbot.core.platform", "astrbot.core.platform.sources",
        "astrbot.core.platform.sources.aiocqhttp", "astrbot.core.star",
        "astrbot.core.star.filter", "astrbot.core.config", "astrbot.core.utils",
    ]:
        stub(name)
    sys.modules["astrbot"].logger = log
    sys.modules["astrbot.api"].logger = log
    sys.modules["astrbot.core"].AstrBotConfig = dict
    sys.modules["astrbot.core.config"].AstrBotConfig = dict
    stub("astrbot.api.event", filter=_Filters())
    stub("astrbot.api.star", Context=object, Star=object)
    stub("astrbot.core.message.components", Plain=object, At=object)
    stub("astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event",
         AiocqhttpMessageEvent=object)
    stub("astrbot.core.star.filter.event_message_type",
         EventMessageType=_Filters.EventMessageType)
    stub("astrbot.core.config.astrbot_config", AstrBotConfig=dict)
    stub("astrbot.core.star.context", Context=object)
    stub("astrbot.core.star.star_tools", StarTools=object)
    stub("astrbot.core.utils.astrbot_path",
         get_astrbot_plugin_path=lambda: plugins_dir)

    stub("qqadmin_main_migration", __path__=[str(ROOT)])
    stub("qqadmin_main_migration.core",
         BanproHandle=object, CurfewHandle=object, FileHandle=object,
         JoinHandle=object, MemberHandle=object, NormalHandle=object,
         NoticeHandle=object, RecallHandle=object)
    stub("qqadmin_main_migration.data", QQAdminDB=object)
    stub("qqadmin_main_migration.group_info_cache", QQGroupInfoCache=object)
    stub("qqadmin_main_migration.permission",
         PermLevel=_PermLevel, perm_manager=object(), perm_required=_perm_required)
    stub("qqadmin_main_migration.utils", parse_bool=lambda *args, **kwargs: True)
    stub("qqadmin_main_migration.web", QQAdminWebController=object)

    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        value = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, value)
        spec.loader.exec_module(value)
        return value

    config_module = load("qqadmin_main_migration.config", ROOT / "config.py")
    main_module = load("qqadmin_main_migration.main", ROOT / "main.py")
    return types.SimpleNamespace(
        cfg=config_module, main=main_module, data_root=data_root,
        old_cfg=old_cfg, old_data=old_data,
    )


def test_main_import_triggers_migration(imported_main):
    root = imported_main.data_root
    new_cfg = root / "config" / f"{imported_main.cfg._PLUGIN_NAME}_config.json"
    new_data = root / "plugin_data" / imported_main.cfg._PLUGIN_NAME

    assert new_cfg.exists()
    assert new_cfg.read_bytes() == imported_main.old_cfg.read_bytes()
    assert imported_main.old_cfg.exists()

    assert not imported_main.old_data.exists()
    assert (new_data / "qqadmin_data_v3.db").read_bytes() == b"SQLite legacy payload"
    assert (new_data / "welcome_images" / "pic.png").exists()


def test_main_calls_migration_before_class_definition():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "from .config import PluginConfig, migrate_legacy_state" in source
    call_index = source.index("\nmigrate_legacy_state()")
    class_index = source.index("class QQAdminPlugin")
    assert 0 < call_index < class_index


# ---------- 插件名一致性 ----------


def _metadata_plugin_name() -> str:
    """从 metadata.yaml 顶层 name 字段读取插件名（不引入 PyYAML）。"""
    for line in (ROOT / "metadata.yaml").read_text(encoding="utf-8").splitlines():
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip().strip("\"'")
    raise AssertionError("metadata.yaml 缺少顶层 name 字段")


def _web_plugin_name() -> str:
    """从 web.py 源码提取 PLUGIN_NAME 常量，避免导入 web.py。"""
    tree = ast.parse((ROOT / "web.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "PLUGIN_NAME"
            for target in node.targets
        ):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node.value.value
    raise AssertionError("web.py 缺少字符串常量 PLUGIN_NAME")


def test_plugin_name_is_consistent_across_sources(cfg):
    """任何一处改名漏改时，该用例必须失败。"""
    names = {
        "config._PLUGIN_NAME": cfg._PLUGIN_NAME,
        "config.PluginConfig._plugin_name": cfg.PluginConfig._plugin_name,
        "metadata.yaml:name": _metadata_plugin_name(),
        "web.py:PLUGIN_NAME": _web_plugin_name(),
    }
    assert len(set(names.values())) == 1, f"插件名不一致: {names}"


# ---------- 编码回归 ----------


@pytest.mark.parametrize("relative", ["config.py", "main.py", "tests/test_legacy_migration.py"])
def test_sources_are_utf8_without_bom(relative):
    raw = (ROOT / relative).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), f"{relative} 不应带 UTF-8 BOM"
    raw.decode("utf-8")
