from __future__ import annotations

import asyncio
import copy
import json
import re
from pathlib import Path
from typing import Any
import inspect
import io
import os
import uuid
from pathlib import PurePosixPath
from PIL import Image as PILImage

from astrbot.api import logger

from .config import PluginConfig
from .data import QQAdminDB
from .group_info_cache import QQGroupInfoCache
from .permission import perm_manager
from .utils import parse_bool

DEFAULT_GROUP_ID = "__default__"
FOLLOW_DEFAULT_KEY = "follow_default"
MAX_WELCOME_IMAGES = 3
MAX_WELCOME_IMAGE_BYTES = 5 * 1024 * 1024
MAX_WELCOME_IMAGE_PIXELS = 20_000_000
WELCOME_IMAGE_FORMATS = {
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".gif": "GIF",
    ".webp": "WEBP",
    ".bmp": "BMP",
}


class PageRequestError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = int(status)


class QQAdminPageService:
    def __init__(self, cfg: PluginConfig, db: QQAdminDB, group_cache: QQGroupInfoCache):
        self.cfg = cfg
        self.db = db
        self.group_cache = group_cache
        self.schema = self._load_schema(cfg.plugin_dir / "_conf_schema.json")
        self._group_image_locks: dict[str, asyncio.Lock] = {}

    @property
    def group_schema(self) -> dict[str, Any]:
        return {
            FOLLOW_DEFAULT_KEY: {
                "description": "跟随默认配置",
                "hint": "开启后，该群直接沿用默认群配置，下面的群专属配置项将不可编辑。",
                "type": "bool",
                "default": True,
            },
            **self.schema.get("default", {}).get("items", {}),
            **self._get_group_overlay_schema(),
        }

    async def get_bootstrap_payload(self) -> dict[str, Any]:
        return {
            "schema": {
                "group": self.group_schema,
            },
            "groups": await self.list_groups(),
        }

    def get_default_group_entry(self) -> dict[str, Any]:
        return {
            "group_id": DEFAULT_GROUP_ID,
            "group_name": "默认群",
            "avatar": "",
            "member_count": 0,
            "max_member_count": 0,
            "bot_role": "unknown",
            "is_default_group": True,
            "config": {
                FOLLOW_DEFAULT_KEY: False,
                **copy.deepcopy(self.cfg.build_group_default_config()),
            },
        }

    async def list_groups(self, force: bool = False) -> list[dict[str, Any]]:
        groups = await self.group_cache.list_groups(force=force)
        return await self._build_group_entries(groups)

    async def list_groups_with_bot_roles(
        self, force: bool = False
    ) -> list[dict[str, Any]]:
        groups = await self.group_cache.list_groups_with_bot_roles(
            force_bot_roles=force
        )
        return await self._build_group_entries(groups)

    async def _build_group_entries(
        self,
        groups: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = [self.get_default_group_entry()]
        stale_group_ids: list[str] = []

        for group in groups:
            group_id = str(group.get("group_id", "")).strip()
            if self._should_delete_group(group):
                stale_group_ids.append(group_id)
                continue
            result.append(
                {
                    **group,
                    "is_default_group": False,
                }
            )

        for group_id in stale_group_ids:
            await self._delete_group_data(group_id)

        return result

    async def get_group_config(
        self,
        group_id: str,
        force: bool = False,
    ) -> dict[str, Any]:
        if str(group_id).strip() == DEFAULT_GROUP_ID:
            return self.get_default_group_config()

        group_id = self._normalize_group_id(group_id)
        follow_default = self.db.is_group_follow_default(group_id)
        group_info = await self.group_cache.get_group(group_id, force=force)
        if self._should_delete_group(group_info):
            await self._delete_group_data(group_id)
            raise ValueError(f"group {group_id} no longer exists and has been deleted")
        return {
            "group_id": group_id,
            "group_info": group_info,
            "config": {
                FOLLOW_DEFAULT_KEY: follow_default,
                **self.db.get_group_snapshot(group_id),
            },
            "is_default_group": False,
        }

    async def update_group_config(
        self, group_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if str(group_id).strip() == DEFAULT_GROUP_ID:
            return await self.update_default_group_config(payload)

        group_id = self._normalize_group_id(group_id)
        current = self.db.get_group_snapshot(group_id)
        follow_default_current = self.db.is_group_follow_default(group_id)
        sanitized = self._sanitize_value(
            payload,
            {"type": "object", "items": self.group_schema},
            {FOLLOW_DEFAULT_KEY: follow_default_current, **current},
        )
        follow_default = bool(sanitized.pop(FOLLOW_DEFAULT_KEY, True))
        if follow_default:
            await self.db.follow_default(group_id)
        else:
            await self.db.replace_group(group_id, sanitized)
        self.group_cache.invalidate(group_id)
        return await self.get_group_config(group_id)

    async def reset_group_config(self, group_id: str) -> dict[str, Any]:
        if str(group_id).strip() == DEFAULT_GROUP_ID:
            raise ValueError("default group does not support reset")

        group_id = self._normalize_group_id(group_id)
        await self.db.follow_default(group_id)
        self.group_cache.invalidate(group_id)
        return await self.get_group_config(group_id)

    async def upload_group_welcome_image(self, group_id: str, file) -> dict:
        group_id = self._normalize_group_image_write_id(group_id)
        async with self._get_group_image_lock(group_id):
            await self._prepare_group_image_write(group_id)
            if file is None:
                raise PageRequestError("Missing uploaded file", 400)

            filename = str(getattr(file, "filename", "") or "")
            extension = self._extract_extension(filename)
            if extension not in WELCOME_IMAGE_FORMATS:
                raise PageRequestError("unsupported image type", 415)

            data = await self._read_uploaded_file(file)
            self._validate_welcome_image(data, WELCOME_IMAGE_FORMATS[extension])

            current = list(
                self.db.get_group_snapshot(group_id).get("join_welcome_image") or []
            )
            if len(current) >= MAX_WELCOME_IMAGES:
                raise PageRequestError("too many welcome images", 409)

            directory = self._ensure_group_image_dir(group_id)
            disk_name = f"{uuid.uuid4().hex}{extension}"
            target = directory / disk_name
            tmp = directory / f".{disk_name}.tmp"
            relative_path = f"files/groups/{group_id}/join_welcome_image/{disk_name}"
            try:
                tmp.write_bytes(data)
                os.replace(tmp, target)
            except OSError as exc:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                logger.warning("保存群欢迎图片失败: %s", exc)
                raise PageRequestError("failed to save image", 500) from exc

            images = [*current, relative_path]
            try:
                await self.db.set(group_id, "join_welcome_image", images)
            except Exception as exc:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
                logger.warning("写入群欢迎图片配置失败: %s", exc)
                raise PageRequestError("failed to save image", 500) from exc

            self.group_cache.invalidate(group_id)
            return {
                "path": relative_path,
                "images": images,
                "source": "group",
            }

    async def delete_group_welcome_image(self, group_id: str, path: str) -> dict:
        group_id = self._normalize_group_image_write_id(group_id)
        if not isinstance(path, str) or not path:
            raise PageRequestError("Missing image path", 400)

        async with self._get_group_image_lock(group_id):
            await self._prepare_group_image_write(group_id)
            candidate = self._validate_delete_image_path(group_id, path)
            images = list(
                self.db.get_group_snapshot(group_id).get("join_welcome_image") or []
            )
            if path not in images:
                raise PageRequestError("image path is not in group config", 400)

            remaining = [item for item in images if item != path]
            await self.db.set(group_id, "join_welcome_image", remaining)
            self.group_cache.invalidate(group_id)

            if candidate is not None:
                try:
                    candidate.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("删除群欢迎图片文件失败: %s", exc)
            return {"images": remaining}

    async def _prepare_group_image_write(self, group_id: str) -> None:
        group_info = await self.group_cache.get_group(group_id, force=False)
        if self._should_delete_group(group_info):
            raise PageRequestError("group not found", 404)
        if self.db.is_group_follow_default(group_id):
            raise PageRequestError(
                "disable follow default before uploading group images",
                409,
            )

    def _normalize_group_image_write_id(self, group_id: str | int | None) -> str:
        raw_group_id = str(group_id or "").strip()
        if raw_group_id == DEFAULT_GROUP_ID:
            raise PageRequestError(
                "default group images are managed in the plugin config page",
                409,
            )
        try:
            return self._normalize_group_id(raw_group_id)
        except ValueError as exc:
            raise PageRequestError(str(exc), 400) from exc

    def _get_group_image_lock(self, group_id: str) -> asyncio.Lock:
        lock = self._group_image_locks.get(group_id)
        if lock is None:
            lock = asyncio.Lock()
            self._group_image_locks[group_id] = lock
        return lock

    @staticmethod
    def _extract_extension(filename: str) -> str:
        normalized = str(filename or "").replace("\\", "/")
        basename = normalized.rsplit("/", 1)[-1]
        if basename in {"", ".", ".."} or "." not in basename:
            return ""
        return f".{basename.rsplit('.', 1)[1].lower()}"

    @staticmethod
    async def _read_uploaded_file(file) -> bytes:
        content_length = getattr(file, "content_length", None)
        if isinstance(content_length, int) and content_length > MAX_WELCOME_IMAGE_BYTES:
            raise PageRequestError("image too large", 413)
        read = getattr(file, "read", None)
        if not callable(read):
            raise PageRequestError("Missing uploaded file", 400)
        data = read()
        if inspect.isawaitable(data):
            data = await data
        if not isinstance(data, (bytes, bytearray)):
            raise PageRequestError("Missing uploaded file", 400)
        data = bytes(data)
        if len(data) > MAX_WELCOME_IMAGE_BYTES:
            raise PageRequestError("image too large", 413)
        return data

    @staticmethod
    def _validate_welcome_image(data: bytes, expected_format: str) -> None:
        try:
            with PILImage.open(io.BytesIO(data)) as image:
                if image.format != expected_format or image.format not in WELCOME_IMAGE_FORMATS.values():
                    raise ValueError("image format")
                if image.width * image.height > MAX_WELCOME_IMAGE_PIXELS:
                    raise ValueError("image pixels")
                image.verify()
            with PILImage.open(io.BytesIO(data)) as image:
                pixels = 0
                for frame in range(getattr(image, "n_frames", 1)):
                    image.seek(frame)
                    pixels += image.width * image.height
                    if pixels > MAX_WELCOME_IMAGE_PIXELS:
                        raise ValueError("image pixels")
                    image.load()
        except Exception as exc:
            raise PageRequestError("invalid image", 422) from exc

    def _group_image_dir(self, group_id: str) -> Path:
        return self.cfg.data_dir / "files" / "groups" / group_id / "join_welcome_image"

    def _ensure_group_image_dir(self, group_id: str) -> Path:
        root = Path(self.cfg.data_dir)
        current = root
        for part in ("files", "groups", group_id, "join_welcome_image"):
            current = current / part
            if current.exists() and current.is_symlink():
                raise PageRequestError("invalid image path", 400)
        current.mkdir(parents=True, exist_ok=True)
        root_resolved = root.resolve(strict=True)
        if not current.resolve(strict=True).is_relative_to(root_resolved):
            raise PageRequestError("invalid image path", 400)
        return current

    def _validate_delete_image_path(
        self, group_id: str, path: str
    ) -> Path | None:
        if any(ord(char) < 32 or ord(char) == 127 for char in path):
            raise PageRequestError("invalid image path", 400)
        parts = tuple(path.split("/"))
        if (
            len(parts) != 5
            or parts[0] != "files"
            or parts[4] in {"", ".", ".."}
            or "\\" in path
            or ":" in path
            or PurePosixPath(parts[4]).name != parts[4]
        ):
            raise PageRequestError("invalid image path", 400)

        if parts[1:4] == ("default", "items", "join_welcome_image"):
            kind = "default"
        elif parts[1:4] == ("groups", group_id, "join_welcome_image"):
            kind = "group"
        else:
            raise PageRequestError("invalid image path", 400)

        root = Path(self.cfg.data_dir)
        try:
            root_resolved = root.resolve(strict=True)
        except OSError as exc:
            raise PageRequestError("invalid image path", 400) from exc

        directory = root.joinpath(*parts[:4])
        candidate = directory / parts[4]
        if candidate.exists():
            try:
                directory_resolved = directory.resolve(strict=True)
                candidate_resolved = candidate.resolve(strict=True)
            except OSError as exc:
                raise PageRequestError("invalid image path", 400) from exc
            if (
                not directory_resolved.is_relative_to(root_resolved)
                or not candidate_resolved.is_relative_to(root_resolved)
                or not candidate_resolved.is_relative_to(directory_resolved)
            ):
                raise PageRequestError("invalid image path", 400)
            if not candidate.is_file():
                raise PageRequestError("invalid image path", 400)

        current = root
        for part in parts[:4]:
            current = current / part
            if current.is_symlink():
                raise PageRequestError("invalid image path", 400)
        if candidate.is_symlink():
            raise PageRequestError("invalid image path", 400)

        return candidate if kind == "group" else None

    def get_default_group_config(self) -> dict[str, Any]:
        return {
            "group_id": DEFAULT_GROUP_ID,
            "group_info": {
                "group_id": DEFAULT_GROUP_ID,
                "group_name": "默认群",
                "avatar": "",
                "member_count": 0,
                "max_member_count": 0,
            },
            "config": {
                FOLLOW_DEFAULT_KEY: False,
                **copy.deepcopy(self.cfg.build_group_default_config()),
            },
            "is_default_group": True,
        }

    async def update_default_group_config(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        current = copy.deepcopy(self.cfg.build_group_default_config())
        sanitized = self._sanitize_value(
            payload,
            {"type": "object", "items": self.group_schema},
            {FOLLOW_DEFAULT_KEY: False, **current},
        )
        sanitized.pop(FOLLOW_DEFAULT_KEY, None)
        # 默认模板图片只允许由 AstrBot 内置插件配置页写入。
        sanitized["join_welcome_image"] = copy.deepcopy(
            current.get("join_welcome_image") or []
        )
        self._apply_group_level_updates(sanitized)
        self.db.default_cfg = self.cfg.build_group_default_config()
        self.cfg.refresh_runtime_settings()
        self.cfg.save_config()
        self.group_cache.invalidate()
        return self.get_default_group_config()

    @staticmethod
    def _load_schema(schema_path: Path) -> dict[str, Any]:
        try:
            return json.loads(schema_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("加载页面 schema 失败: %s", exc)
            return {}

    @staticmethod
    def _normalize_group_id(group_id: str | int | None) -> str:
        gid = str(group_id or "").strip()
        if not gid or not gid.isdigit():
            raise ValueError("group_id must be a numeric string")
        return gid

    async def _delete_group_data(self, group_id: str) -> None:
        normalized_group_id = self._normalize_group_id(group_id)
        await self.db.delete_group(normalized_group_id)
        self.group_cache.remove_group(normalized_group_id)

    @staticmethod
    def _should_delete_group(group_info: dict[str, Any]) -> bool:
        group_id = str(group_info.get("group_id", "")).strip()
        if not group_id or group_id == DEFAULT_GROUP_ID:
            return False
        try:
            member_count = int(group_info.get("member_count", 0))
        except (TypeError, ValueError):
            member_count = 0
        return member_count <= 0

    def _apply_group_level_updates(self, updated: dict[str, Any]) -> None:
        default_fields = self.schema.get("default", {}).get("items", {})
        default_updates = {
            key: value for key, value in updated.items() if key in default_fields
        }
        self._merge_dict(self.cfg.default, default_updates)

        if "admin_audit" in updated:
            self.cfg.admin_audit = updated["admin_audit"]
        if "random_ban_time" in updated:
            self.cfg.random_ban_time = updated["random_ban_time"]
        if "vote_ban" in updated:
            self.cfg.vote_ban.ttl = updated["vote_ban"]["ttl"]
            self.cfg.vote_ban.threshold = updated["vote_ban"]["threshold"]
        if "llm_get_msg_count" in updated:
            self.cfg.llm_get_msg_count = updated["llm_get_msg_count"]
        if "level_threshold" in updated:
            self.cfg.level_threshold = updated["level_threshold"]
        if "perms" in updated:
            self._merge_dict(self.cfg.perms, updated["perms"])

        perm_manager.refresh(self.cfg, self.db)

    def _get_group_overlay_schema(self) -> dict[str, Any]:
        keys = [
            "admin_audit",
            "random_ban_time",
            "vote_ban",
            "llm_get_msg_count",
            "level_threshold",
            "perms",
        ]
        return {
            key: copy.deepcopy(self.schema[key]) for key in keys if key in self.schema
        }

    @staticmethod
    def _merge_dict(target: dict[str, Any], source: dict[str, Any] | None) -> None:
        if source is None:
            return
        target.clear()
        target.update(copy.deepcopy(source))

    @staticmethod
    def _is_safe_file_path(value: str) -> bool:
        if not value or value != value.strip():
            return False
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            return False
        if "\\" in value or ":" in value or value.startswith(("/", "//")):
            return False
        parts = value.split("/")
        if len(parts) != 5 or parts[0] != "files":
            return False
        if any(part in {"", ".", ".."} for part in parts):
            return False
        return PurePosixPath(parts[4]).name == parts[4]

    def _sanitize_value(
        self,
        value: Any,
        schema: dict[str, Any],
        current: Any = None,
    ) -> Any:
        field_type = schema.get("type", "string")

        if field_type == "file":
            if value is None:
                return []
            if not isinstance(value, list):
                raise ValueError(f"invalid file list value: {value}")
            result: list[str] = []
            seen: set[str] = set()
            for item in value:
                if not isinstance(item, str):
                    continue
                candidate = item.strip()
                if not candidate or candidate in seen:
                    continue
                if not self._is_safe_file_path(candidate):
                    continue
                seen.add(candidate)
                result.append(candidate)
                if len(result) >= MAX_WELCOME_IMAGES:
                    break
            return result

        if field_type == "object":
            items = schema.get("items", {})
            payload = value if isinstance(value, dict) else {}
            current_map = current if isinstance(current, dict) else {}
            result: dict[str, Any] = {}
            for key, child_schema in items.items():
                child_current = current_map.get(key, child_schema.get("default"))
                child_value = payload[key] if key in payload else child_current
                result[key] = self._sanitize_value(
                    child_value, child_schema, child_current
                )
            return result

        if field_type == "bool":
            parsed = parse_bool(value)
            if parsed is None:
                raise ValueError(f"invalid bool value: {value}")
            return parsed

        if field_type == "int":
            parsed = int(value)
            slider = schema.get("slider", {})
            minimum = slider.get("min")
            maximum = slider.get("max")
            if minimum is not None:
                parsed = max(int(minimum), parsed)
            if maximum is not None:
                parsed = min(int(maximum), parsed)
            return parsed

        if field_type == "list":
            if value is None:
                return []
            if isinstance(value, str):
                items = re.split(r"[\n,，]+", value)
            elif isinstance(value, list):
                items = value
            else:
                raise ValueError(f"invalid list value: {value}")
            return [str(item).strip() for item in items if str(item).strip()]

        options = schema.get("options")
        parsed = str(value or "")
        if options and parsed not in options:
            return str(current if current is not None else schema.get("default", ""))
        return parsed
