# config.py
from __future__ import annotations

import os
import random
import shutil
import stat
import json
import re
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from types import MappingProxyType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context
from astrbot.core.star.star_tools import StarTools
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_path

# 插件更名（astrbot_plugin_qqadmin -> astrbot_plugin_qqadmin_fix）后的
# 数据迁移源/目标名。配置文件名与数据目录名都由这两个常量拼装。
_LEGACY_PLUGIN_NAME = "astrbot_plugin_qqadmin"
_PLUGIN_NAME = "astrbot_plugin_qqadmin_fix"


def _resolve_data_root(data_root: str | Path | None = None) -> Path:
    """返回 AstrBot data 根目录。

    默认由 ``get_astrbot_plugin_path()``（形如 ``<data>/plugins``）取 ``.parent`` 推导；
    测试可显式传入临时目录，避免依赖真实 AstrBot 环境。
    """
    if data_root is not None:
        return Path(data_root)
    return Path(get_astrbot_plugin_path()).parent


def migrate_legacy_config_file(data_root: str | Path | None = None) -> bool:
    """旧配置存在且新配置不存在时，复制旧配置为新配置。

    返回是否发生了迁移；任何可预期异常都被吞掉并记为告警，绝不抛出。
    """
    old: Path | None = None
    new: Path | None = None
    tmp: Path | None = None
    try:
        root = _resolve_data_root(data_root)
        old = root / "config" / f"{_LEGACY_PLUGIN_NAME}_config.json"
        new = root / "config" / f"{_PLUGIN_NAME}_config.json"
        tmp = new.parent / f".{new.name}.tmp"
        if new.exists():
            return False
        if not old.exists():
            return False
        new.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old, tmp)
        tmp.chmod(tmp.stat().st_mode | stat.S_IWRITE)
        os.replace(tmp, new)
    except (OSError, shutil.Error) as exc:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                logger.warning(
                    f"[migration] 清理配置迁移临时文件失败: {tmp}（原因: {cleanup_exc}）"
                )
        logger.warning(
            f"[migration] 迁移插件配置失败: {old} -> {new}（原因: {exc}）"
        )
        return False
    logger.info(f"[migration] 已迁移插件配置: {old} -> {new}")
    return True


_WELCOME_CQ_IMAGE_RE = re.compile(r"\[CQ:image,[^\]]*\]", re.IGNORECASE)


def _remove_cq_image_tokens(text: Any) -> Any:
    if not isinstance(text, str):
        return text
    return _WELCOME_CQ_IMAGE_RE.sub("", text)


def migrate_welcome_config_file(data_root: str | Path | None = None) -> bool:
    """迁移默认模板配置中的欢迎旧键。

    只处理 ``default.items``，写回采用同目录临时文件加原子替换。函数幂等、
    异常兜底，任何失败都只记录告警，不向插件加载流程抛出。
    """
    config_path: Path | None = None
    tmp: Path | None = None
    try:
        root = _resolve_data_root(data_root)
        config_path = root / "config" / f"{_PLUGIN_NAME}_config.json"
        tmp = config_path.parent / f".{config_path.name}.tmp"
        if not config_path.is_file():
            return False

        raw = config_path.read_bytes()
        has_bom = raw.startswith(b"\xef\xbb\xbf")
        data = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(data, dict):
            logger.warning("[migration] 欢迎配置迁移跳过：配置根节点不是对象")
            return False

        default = data.get("default")
        if not isinstance(default, dict):
            return False
        items = default.get("items")
        if not isinstance(items, dict):
            return False

        changed = False
        missing = object()
        old_mention = items.pop("join_welcome_cq_mention", missing)
        old_image = items.pop("join_welcome_cq_image", missing)
        if old_mention is not missing or old_image is not missing:
            changed = True

        if "join_welcome_mention" not in items or items.get("join_welcome_mention") is None:
            items["join_welcome_mention"] = (
                old_mention
                if old_mention is not missing and isinstance(old_mention, bool)
                else True
            )
            changed = True
        if "join_welcome_image" not in items or items.get("join_welcome_image") is None:
            items["join_welcome_image"] = []
            changed = True
        if (
            "join_welcome_image_before" not in items
            or items.get("join_welcome_image_before") is None
        ):
            items["join_welcome_image_before"] = False
            changed = True

        if old_image is False:
            new_welcome = _remove_cq_image_tokens(items.get("join_welcome"))
            if new_welcome != items.get("join_welcome"):
                items["join_welcome"] = new_welcome
                changed = True

        if not changed:
            return False

        config_path.parent.mkdir(parents=True, exist_ok=True)
        encoding = "utf-8-sig" if has_bom else "utf-8"
        with open(tmp, "w", encoding=encoding, newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, config_path)
        logger.info("[migration] 已迁移欢迎配置旧键: %s", config_path)
        return True
    except Exception as exc:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        logger.warning(
            "[migration] 欢迎配置迁移失败: %s（原因: %s）", config_path, exc
        )
        return False


def _is_auto_created_skeleton(path: Path) -> bool:
    """判断目录是否仅包含插件初始化时自动创建的空骨架。"""
    if not path.is_dir():
        return False

    skeleton_dirs = {"group_notice", "file", "welcome_images"}
    for entry in path.iterdir():
        if entry.is_dir():
            if entry.name not in skeleton_dirs or any(entry.iterdir()):
                return False
            continue
        if not entry.is_file() or entry.name != "curfew_data.json":
            return False
        try:
            content = entry.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            # 不可读或非 UTF-8 时无法确认为空骨架，保守视为已有数据。
            return False
        if content not in ("", "{}"):
            return False
    return True


def migrate_legacy_data_dir(data_root: str | Path | None = None) -> bool:
    """按判定顺序迁移旧插件数据目录。

    判定顺序（先命中先返回）：
    1. 旧目录不存在 -> 返回 False；
    2. 新路径存在但不是目录 -> 告警并返回 False，绝不覆盖；
    3. 新目录存在但并非自动骨架 -> 告警并返回 False，保留双方数据；
    4. 新目录不存在、为空或仅含自动骨架 -> 优先整体移动旧目录；
    5. 移动失败 -> 回退非破坏性复制并保留旧目录，告警提示人工核对。

    返回是否发生了迁移；任何可预期异常都被吞掉并记为告警，绝不抛出。
    """
    old: Path | None = None
    new: Path | None = None
    try:
        root = _resolve_data_root(data_root)
        old = root / "plugin_data" / _LEGACY_PLUGIN_NAME
        new = root / "plugin_data" / _PLUGIN_NAME
        if not old.is_dir():
            return False
        if new.exists() and not new.is_dir():
            logger.warning(f"[migration] 新数据路径已存在且不是目录，跳过迁移: {new}")
            return False
        if new.is_dir():
            if not _is_auto_created_skeleton(new):
                logger.warning(
                    f"[migration] 新数据目录已有内容，跳过以免覆盖: {new}"
                )
                return False
            try:
                new.rmdir()
            except OSError:
                # 自动骨架非空时无法整体重命名，改为只增量合并。
                if not _is_auto_created_skeleton(new):
                    logger.warning(
                        f"[migration] 新数据目录已有内容，跳过以免覆盖: {new}"
                    )
                    return False
                new.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(old, new, dirs_exist_ok=True, symlinks=True)
                logger.warning(
                    f"[migration] 新数据目录仅含自动骨架，已复制并保留旧目录: {old} -> {new}"
                )
                return True
        try:
            os.rename(old, new)
        except OSError as exc:
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(old, new, dirs_exist_ok=True, symlinks=True)
            logger.warning(
                f"[migration] 移动失败，已改为复制并保留旧目录: {old} -> {new}（原因: {exc}）"
            )
            return True
        logger.info(f"[migration] 已迁移插件数据目录: {old} -> {new}")
        return True
    except (OSError, shutil.Error) as exc:
        logger.warning(
            f"[migration] 迁移插件数据目录失败: {old} -> {new}（原因: {exc}）"
        )
        return False


def migrate_legacy_state(data_root: str | Path | None = None) -> None:
    """总入口：依次迁移配置与数据目录。

    该函数在插件模块导入期被调用，因此必须兜底捕获所有异常，只记日志不抛出，
    保证迁移失败不会阻断插件加载。
    """
    try:
        try:
            migrate_legacy_config_file(data_root)
        except Exception as exc:
            logger.warning(
                f"[migration] 自动迁移失败，插件将继续加载，请手动检查旧配置: {exc}",
                exc_info=True,
            )
        try:
            migrate_welcome_config_file(data_root)
        except Exception as exc:
            logger.warning(
                f"[migration] 自动迁移欢迎配置失败，插件将继续加载，请手动检查旧配置: {exc}",
                exc_info=True,
            )
        try:
            migrate_legacy_data_dir(data_root)
        except Exception as exc:
            logger.warning(
                f"[migration] 自动迁移失败，插件将继续加载，请手动检查旧数据: {exc}",
                exc_info=True,
            )
    except Exception as exc:
        logger.warning(
            f"[migration] 自动迁移失败，插件将继续加载，请手动检查旧数据: {exc}",
            exc_info=True,
        )


class ConfigNode:
    """
    配置节点, 把 dict 变成强类型对象。

    规则：
    - schema 来自子类类型注解
    - 声明字段：读写，写回底层 dict
    - 未声明字段和下划线字段：仅挂载属性，不写回
    - 支持 ConfigNode 多层嵌套（lazy + cache）
    """

    _SCHEMA_CACHE: dict[type, dict[str, type]] = {}
    _FIELDS_CACHE: dict[type, set[str]] = {}

    @classmethod
    def _schema(cls) -> dict[str, type]:
        return cls._SCHEMA_CACHE.setdefault(cls, get_type_hints(cls))

    @classmethod
    def _fields(cls) -> set[str]:
        return cls._FIELDS_CACHE.setdefault(
            cls,
            {k for k in cls._schema() if not k.startswith("_")},
        )

    @staticmethod
    def _is_optional(tp: type) -> bool:
        if get_origin(tp) in (Union, UnionType):
            return type(None) in get_args(tp)
        return False

    def __init__(self, data: MutableMapping[str, Any]):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_children", {})
        for key, tp in self._schema().items():
            if key.startswith("_"):
                continue
            if key in data:
                continue
            if hasattr(self.__class__, key):
                continue
            if self._is_optional(tp):
                continue
            logger.warning(f"[config:{self.__class__.__name__}] 缺少字段: {key}")

    def __getattr__(self, key: str) -> Any:
        if key in self._fields():
            value = self._data.get(key)
            tp = self._schema().get(key)

            if isinstance(tp, type) and issubclass(tp, ConfigNode):
                children: dict[str, ConfigNode] = self.__dict__["_children"]
                if key not in children:
                    if not isinstance(value, MutableMapping):
                        raise TypeError(
                            f"[config:{self.__class__.__name__}] "
                            f"字段 {key} 期望 dict，实际是 {type(value).__name__}"
                        )
                    children[key] = tp(value)
                return children[key]

            return value

        if key in self.__dict__:
            return self.__dict__[key]

        raise AttributeError(key)

    def __setattr__(self, key: str, value: Any) -> None:
        if key in self._fields():
            self._data[key] = value
            return
        object.__setattr__(self, key, value)

    def raw_data(self) -> Mapping[str, Any]:
        """
        底层配置 dict 的只读视图
        """
        return MappingProxyType(self._data)

    def save_config(self) -> None:
        """
        保存配置到磁盘（仅允许在根节点调用）
        """
        if not isinstance(self._data, AstrBotConfig):
            raise RuntimeError(
                f"{self.__class__.__name__}.save_config() 只能在根配置节点上调用"
            )
        self._data.save_config()


# ============ 插件自定义配置 ==================


class VoteBanConfig(ConfigNode):
    ttl: int
    threshold: int


class PluginConfig(ConfigNode):
    default: dict
    admin_audit: bool
    random_ban_time: str
    vote_ban: VoteBanConfig
    llm_get_msg_count: int
    level_threshold: int
    perms: dict

    _db_version = 3
    _plugin_name: str = _PLUGIN_NAME

    def __init__(self, cfg: AstrBotConfig, context: Context):
        super().__init__(cfg)
        self.context = context
        self.admins_id = self._clean_ids(context.get_config().get("admins_id", []))

        self.data_dir = StarTools.get_data_dir(self._plugin_name)
        self.plugin_dir = Path(get_astrbot_plugin_path()) / self._plugin_name

        self.db_path = self.data_dir / f"qqadmin_data_v{self._db_version}.db"
        self.ban_lexicon_path = self.plugin_dir / "SensitiveLexicon.json"
        self.group_notice_dir = self.data_dir / "group_notice"
        self.group_notice_dir.mkdir(parents=True, exist_ok=True)
        self.curfew_file = self.data_dir / "curfew_data.json"
        if not self.curfew_file.exists():
            self.curfew_file.write_text("{}", encoding="utf-8")
        self.file_dir = self.data_dir / "file"
        self.file_dir.mkdir(parents=True, exist_ok=True)
        self.welcome_image_dir = self.data_dir / "welcome_images"
        self.welcome_image_dir.mkdir(parents=True, exist_ok=True)

        self.spamming_count = 5
        self.spamming_interval = 0.5
        self.refresh_runtime_settings()

    @staticmethod
    def _clean_ids(ids: list) -> list[str]:
        """过滤并规范化数字 ID"""
        return [str(i) for i in ids if str(i).isdigit()]

    def get_ban_time(self, seconds=None) -> int:
        """获取禁言时间"""
        if seconds is None or not isinstance(seconds, int):
            return random.randint(self.min_ban_time, self.max_ban_time)
        else:
            return min(max(seconds, 0), 2592000)

    @staticmethod
    def _resolve_ban_time_range(random_ban_time: str) -> tuple[int, int]:
        try:
            min_ban_time, max_ban_time = map(int, str(random_ban_time).split("~", 1))
        except ValueError:
            min_ban_time, max_ban_time = 30, 300

        min_ban_time = max(min_ban_time, 1)
        max_ban_time = min(max(max_ban_time, min_ban_time), 2592000)
        return min_ban_time, max_ban_time

    def get_ban_time_with_range(
        self, random_ban_time: str | None, seconds: int | None = None
    ) -> int:
        if not random_ban_time:
            return self.get_ban_time(seconds)

        min_ban_time, max_ban_time = self._resolve_ban_time_range(random_ban_time)
        if seconds is None or not isinstance(seconds, int):
            return random.randint(min_ban_time, max_ban_time)
        return min(max(seconds, 0), 2592000)

    def build_group_default_config(self) -> dict[str, Any]:
        return {
            **self.default,
            "admin_audit": self.admin_audit,
            "random_ban_time": self.random_ban_time,
            "vote_ban": {
                "ttl": self.vote_ban.ttl,
                "threshold": self.vote_ban.threshold,
            },
            "llm_get_msg_count": self.llm_get_msg_count,
            "level_threshold": self.level_threshold,
            "perms": dict(self.perms),
        }

    def refresh_runtime_settings(self) -> None:
        """刷新依赖配置的运行时缓存。"""
        try:
            min_ban_time, max_ban_time = self._resolve_ban_time_range(
                str(self.random_ban_time)
            )
        except ValueError:
            logger.warning(
                f"[config:{self.__class__.__name__}] random_ban_time 格式错误: "
                f"{self.random_ban_time}，已回退到 30~300"
            )
            min_ban_time, max_ban_time = 30, 300
            self.random_ban_time = "30~300"

        self.min_ban_time = max(min_ban_time, 1)
        self.max_ban_time = min(max(max_ban_time, self.min_ban_time), 2592000)
