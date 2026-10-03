from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

from astrbot.api import logger
from astrbot.api.star import Context

try:
    from quart import jsonify as quart_jsonify
    from quart import request as quart_request_obj
except ImportError:
    quart_jsonify = None
    quart_request_obj = None

from .config import PluginConfig
from .data import QQAdminDB
from .group_info_cache import QQGroupInfoCache
from .page_service import QQAdminPageService
from .page_service import PageRequestError

PLUGIN_NAME = "astrbot_plugin_qqadmin_fix"


class QQAdminWebController:
    def __init__(
        self,
        context: Context,
        cfg: PluginConfig,
        db: QQAdminDB,
        group_cache: QQGroupInfoCache,
    ):
        self.context = context
        self.service = QQAdminPageService(cfg, db, group_cache)

    def register_routes(self) -> None:
        routes = [
            ("/ping", self.page_ping, ["GET"], "Page ping"),
            (
                "/settings/bootstrap",
                self.page_bootstrap,
                ["GET"],
                "Load settings page bootstrap data",
            ),
            (
                "/settings/groups/refresh",
                self.page_refresh_groups,
                ["POST"],
                "Refresh QQ group list",
            ),
            (
                "/settings/groups/roles",
                self.page_refresh_group_roles,
                ["POST"],
                "Load bot roles for QQ groups",
            ),
            ("/settings/group", self.page_get_group, ["GET"], "Load one group config"),
            (
                "/settings/group",
                self.page_update_group,
                ["POST"],
                "Update one group config",
            ),
            (
                "/settings/group/reset",
                self.page_reset_group,
                ["POST"],
                "Reset one group config",
            ),
            (
                "/settings/groups/<group_id>/welcome-image/upload",
                self.page_upload_group_welcome_image,
                ["POST"],
                "Upload group welcome image",
            ),
            (
                "/settings/groups/<group_id>/welcome-image/delete",
                self.page_delete_group_welcome_image,
                ["POST"],
                "Delete group welcome image",
            ),
        ]
        for path, handler, methods, desc in routes:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}{path}",
                self._wrap_handler(handler),
                methods,
                desc,
            )

    @staticmethod
    def _check_quart_available() -> None:
        if quart_jsonify is None or quart_request_obj is None:
            raise RuntimeError("Web framework is unavailable")

    @staticmethod
    def _jsonify(payload: dict[str, Any]):
        QQAdminWebController._check_quart_available()
        return cast(Callable[[dict[str, Any]], Any], quart_jsonify)(payload)

    @staticmethod
    def _request():
        QQAdminWebController._check_quart_available()
        return cast(Any, quart_request_obj)

    def _wrap_handler(
        self, handler: Callable[..., Awaitable]
    ) -> Callable[..., Awaitable]:
        async def wrapped(*args, **kwargs):
            self._check_quart_available()
            try:
                return await handler(*args, **kwargs)
            except PageRequestError as exc:
                return self._jsonify({"ok": False, "message": str(exc)}), exc.status
            except ValueError as exc:
                return self._jsonify({"ok": False, "message": str(exc)}), 400
            except Exception as exc:
                logger.exception("QQAdmin page request failed")
                return self._jsonify({"ok": False, "message": str(exc)}), 500

        wrapped.__name__ = handler.__name__
        return wrapped

    async def page_ping(self):
        return self._jsonify({"ok": True, "message": "pong"})

    async def page_bootstrap(self):
        return self._jsonify(
            {"ok": True, "data": await self.service.get_bootstrap_payload()}
        )

    async def page_refresh_groups(self):
        return self._jsonify(
            {"ok": True, "data": await self.service.list_groups(force=True)}
        )

    async def page_refresh_group_roles(self):
        payload = await self._request().get_json(force=True, silent=True) or {}
        force = str(payload.get("force", "")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        return self._jsonify(
            {"ok": True, "data": await self.service.list_groups_with_bot_roles(force)}
        )

    async def page_get_group(self):
        request = self._request()
        group_id = request.args.get("group_id", "")
        force = request.args.get("force", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        return self._jsonify(
            {
                "ok": True,
                "data": await self.service.get_group_config(group_id, force=force),
            }
        )

    async def page_update_group(self):
        payload = await self._request().get_json(force=True, silent=True) or {}
        group_id = payload.get("group_id")
        config = payload.get("config")
        result = await self.service.update_group_config(group_id, config)
        return self._jsonify(
            {"ok": True, "message": "Group config saved", "data": result}
        )

    async def page_reset_group(self):
        payload = await self._request().get_json(force=True, silent=True) or {}
        group_id = payload.get("group_id")
        result = await self.service.reset_group_config(group_id)
        return self._jsonify(
            {"ok": True, "message": "Group config reset", "data": result}
        )

    async def page_upload_group_welcome_image(self, group_id: str):
        files = await self._request().files
        file = files.get("file") if files else None
        if file is None:
            raise PageRequestError("Missing uploaded file", 400)
        result = await self.service.upload_group_welcome_image(group_id, file)
        return self._jsonify(
            {"ok": True, "message": "Uploaded", "data": result}
        )

    async def page_delete_group_welcome_image(self, group_id: str):
        payload = await self._request().get_json(force=True, silent=True) or {}
        path = payload.get("path")
        if not isinstance(path, str) or not path:
            raise PageRequestError("Missing image path", 400)
        result = await self.service.delete_group_welcome_image(group_id, path)
        return self._jsonify(
            {"ok": True, "message": "Deleted", "data": result}
        )
