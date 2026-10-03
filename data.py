import asyncio
import copy
import json
from functools import wraps

import aiosqlite

from astrbot.api import logger

from .config import PluginConfig
from .utils import parse_bool


def connection_locked(method):
    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        async with self._write_lock:
            # 保护完整读改写，包括提交后发布缓存；外层取消不得取消 SQLite future。
            task = asyncio.create_task(method(self, *args, **kwargs))
            cancelled = False
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
                except BaseException:
                    break
            if cancelled:
                if not task.cancelled() and task.exception() is not None:
                    logger.error("取消期间数据库操作失败: %s", type(task.exception()).__name__)
                raise asyncio.CancelledError
            return task.result()
    return wrapped


class QQAdminDB:
    """
    群管插件数据库（极简 API + 动态字段 + 自动补齐）
    """

    # ====================== 字段中英文映射 ======================
    FIELD_MAP = {
        "join_switch": "进群审核",
        "join_min_level": "进群等级门槛",
        "join_max_time": "进群尝试次数",
        "join_accept_words": "进群白词",
        "join_reject_words": "进群黑词",
        "join_no_match_reject": "未中白词拒绝",
        "reject_word_block": "命中黑词拉黑",
        "block_ids": "进群黑名单",
        "join_welcome": "进群欢迎词",
        "join_welcome_cq_mention": "欢迎 CQ 提及",
        "join_welcome_cq_image": "欢迎 CQ 图片",
        "join_ban_time": "进群禁言时长",
        "leave_notify": "主动退群通知",
        "leave_block": "主动退群拉黑",
        "builtin_ban": "启用内置禁词",
        "custom_ban_words": "自定义违禁词",
        "word_ban_time": "禁词禁言时长",
        "spamming_ban_time": "刷屏禁言时长",
    }

    REVERSE_FIELD_MAP = {v: k for k, v in FIELD_MAP.items()}
    FOLLOW_DEFAULT_MARKER = "__follow_default__"

    # ================================================================

    def __init__(self, config: PluginConfig):
        self.db_path = config.db_path

        # 默认字段（动态配置核心）
        self.default_cfg: dict = config.default

        self._conn = None
        self._cache = {}
        self._initialized = False
        self._write_lock = asyncio.Lock()

    # ============================== 初始化 ==============================

    @connection_locked
    async def init(self):
        if self._initialized:
            return

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = aiosqlite.Row

        try:
            await self._execute_write("""
            CREATE TABLE IF NOT EXISTS groups (
                group_id TEXT PRIMARY KEY,
                data TEXT NOT NULL
            );
            """)
            cache = {}
            async with self._conn.execute("SELECT group_id, data FROM groups;") as cur:
                async for row in cur:
                    try:
                        cache[row["group_id"]] = json.loads(row["data"])
                    except Exception:
                        logger.exception("解析 group 数据失败: %s", row["group_id"])
        except BaseException:
            if self._conn:
                await self._finish_cleanup(self._conn.close())
            self._conn = None
            raise

        self._cache = cache
        self._initialized = True
        logger.info("QQAdminDB initialized (%d groups)", len(self._cache))

    async def _save_to_db(self, gid: str, data):
        """调用方必须持有连接锁；缓存只在本方法成功后发布。"""
        await self._execute_write(
            """
            INSERT INTO groups(group_id, data)
            VALUES (?, ?)
            ON CONFLICT(group_id) DO UPDATE SET data=excluded.data;
            """,
            (gid, json.dumps(data, ensure_ascii=False)),
        )

    async def _execute_write(self, sql, params=()):
        if not self._conn:
            raise RuntimeError("请先 init()")
        try:
            await self._conn.execute("BEGIN")
            await self._conn.execute(sql, params)
            await self._conn.commit()
        except BaseException:
            # aiosqlite 工作线程仍可能在执行被取消的 SQL；排队回滚并等待完成后才释放锁。
            try:
                await self._finish_cleanup(self._conn.rollback())
            except BaseException:
                await self._finish_cleanup(self._conn.close())
                self._conn = None
                self._initialized = False
                raise
            raise

    @staticmethod
    async def _finish_cleanup(operation):
        task = asyncio.ensure_future(operation)
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        return task.result()

    async def _publish(self, gid, candidate):
        await self._save_to_db(gid, candidate)
        self._cache[gid] = candidate

    def _strip_meta_fields(self, data: dict | None) -> dict | None:
        if data is None:
            return None
        return {
            key: copy.deepcopy(value)
            for key, value in data.items()
            if key != self.FOLLOW_DEFAULT_MARKER
        }

    def _is_follow_default_data(self, data: dict | None) -> bool:
        if data is None:
            return True

        marker = data.get(self.FOLLOW_DEFAULT_MARKER)
        if marker is False:
            return False

        clean = self._strip_meta_fields(data) or {}
        if not clean:
            return True

        for key, value in clean.items():
            if key not in self.default_cfg:
                return False
            if value != self.default_cfg[key]:
                return False
        return True

    def is_group_follow_default(self, gid: str) -> bool:
        return self._is_follow_default_data(self._cache.get(gid))

    def _build_explicit_group_record(self, data: dict | None = None) -> dict:
        base = copy.deepcopy(data if data is not None else self.default_cfg)
        base[self.FOLLOW_DEFAULT_MARKER] = False
        return base

    # ============================== 基础：确保配置存在 ==============================

    @connection_locked
    async def ensure_group(self, gid: str):
        """确保存在群配置，若没有则按 default_cfg 初始化"""
        if gid not in self._cache or self._is_follow_default_data(self._cache.get(gid)):
            await self._publish(gid, self._build_explicit_group_record(self.get_group_snapshot(gid)))

    def list_group_ids(self) -> list[str]:
        return sorted(
            self._cache.keys(),
            key=lambda gid: (
                not str(gid).isdigit(),
                int(gid) if str(gid).isdigit() else str(gid),
            ),
        )

    def get_group_snapshot(self, gid: str) -> dict:
        raw = self._cache.get(gid)
        if self._is_follow_default_data(raw):
            data = copy.deepcopy(self.default_cfg)
        else:
            data = self._strip_meta_fields(raw) or {}
        for key, value in self.default_cfg.items():
            if key not in data:
                data[key] = copy.deepcopy(value)
        return data

    # ============================== API ==============================

    @connection_locked
    async def all(self, gid: str) -> dict:
        """
        获取整个配置，并自动补齐 default_cfg 的字段
        """
        if self.is_group_follow_default(gid):
            return self.get_group_snapshot(gid)

        data = copy.deepcopy(self._cache[gid])

        changed = False
        for k, v in self.default_cfg.items():
            if k not in data:
                data[k] = json.loads(json.dumps(v))
                changed = True

        if data.get(self.FOLLOW_DEFAULT_MARKER) is not False:
            data[self.FOLLOW_DEFAULT_MARKER] = False
            changed = True

        if changed:
            await self._publish(gid, data)

        return self.get_group_snapshot(gid)

    @connection_locked
    async def get(self, gid: str, field: str, default=None):
        """
        读字段，不存在则补齐 default
        """
        if self.is_group_follow_default(gid):
            snapshot = self.get_group_snapshot(gid)
            if field in snapshot:
                return snapshot[field]
            return json.loads(json.dumps(default))

        data = copy.deepcopy(self._cache[gid])

        if field not in data:
            data[field] = json.loads(json.dumps(default))
            await self._publish(gid, data)

        return copy.deepcopy(data[field])

    @connection_locked
    async def set(self, gid: str, field: str, value):
        """
        写入字段
        """
        candidate = self._build_explicit_group_record(self.get_group_snapshot(gid))
        candidate[field] = copy.deepcopy(value)
        await self._publish(gid, candidate)

    @connection_locked
    async def replace_group(self, gid: str, data: dict):
        await self._publish(gid, self._build_explicit_group_record(data))

    @connection_locked
    async def add(self, gid: str, field: str, value):
        """
        列表字段追加（自动创建列表）
        """
        candidate = self._build_explicit_group_record(self.get_group_snapshot(gid))
        lst = list(candidate.get(field, []))
        if value not in lst:
            lst.append(value)
            candidate[field] = copy.deepcopy(lst)
            await self._publish(gid, candidate)

    @connection_locked
    async def remove(self, gid: str, field: str, value):
        """
        列表字段删除（自动创建列表）
        """
        candidate = self._build_explicit_group_record(self.get_group_snapshot(gid))
        candidate[field] = [i for i in candidate.get(field, []) if i != value]
        await self._publish(gid, candidate)

    # ============================== 删除群配置 ==============================

    @connection_locked
    async def delete_group(self, gid: str):
        """彻底删除群配置"""
        await self._execute_write("DELETE FROM groups WHERE group_id = ?", (gid,))
        self._cache.pop(gid, None)

    # ============================== 关闭 ==============================

    @connection_locked
    async def close(self):
        if self._conn:
            try:
                await self._finish_cleanup(self._conn.close())
            finally:
                self._conn = None
                self._initialized = False

    # ====================== 中文展示、读回 ======================

    async def export_cn_lines(self, gid: str) -> str:
        """
        以中文键名 + 多行文本形式输出群配置。
        - 列表字段：用空格分隔
        - 布尔：开 / 关
        - 其它类型按原样输出
        """
        data = await self.all(gid)
        lines = []

        for eng_key, value in data.items():
            cn_key = self.FIELD_MAP.get(eng_key, eng_key)

            # 列表字段 => 用空格分隔
            if isinstance(value, list):
                val_str = " ".join(map(str, value))

            # 布尔字段 => 显示 为“开 / 关”
            elif isinstance(value, bool):
                val_str = "开" if value else "关"

            # 其他字段 => 按原样
            else:
                val_str = str(value)

            lines.append(f"{cn_key}: {val_str}")

        return "\n".join(lines)

    @connection_locked
    async def import_cn_lines(self, gid: str, text: str) -> dict:
        """
        解析用户提交的中文多行文本并写回 DB
        - 列表字段：空格分隔
        - 布尔：开/关/开启/on/off/true/false/1/0
        - 数字：自动转 int
        - 字符串：原样保存
        """
        data = self._build_explicit_group_record(self.get_group_snapshot(gid))

        for line in text.splitlines():
            if ":" not in line:
                continue

            cn_key, raw_v = line.split(":", 1)
            cn_key = cn_key.strip()
            raw_v = raw_v.strip()

            eng_key = self.REVERSE_FIELD_MAP.get(cn_key)
            if not eng_key:
                continue

            old_val = data.get(eng_key)

            # 如果原字段是 bool，则优先进行布尔解析
            if isinstance(old_val, bool):
                parsed = parse_bool(raw_v)
                if parsed is not None:
                    data[eng_key] = parsed
                    continue
                # 若解析失败，退回默认字面处理（防错）

            # 列表字段：按空格拆
            if isinstance(old_val, list):
                value = [x for x in raw_v.split() if x]

            # 数字字段：自动转 int
            elif isinstance(old_val, int):
                try:
                    value = int(raw_v)
                except ValueError:
                    value = old_val  # 防错保底

            # 字符串字段
            else:
                value = raw_v

            data[eng_key] = value

        await self._publish(gid, data)
        return self.get_group_snapshot(gid)

    @connection_locked
    async def follow_default(self, gid: str | None = None):
        """让指定群（或全部群）重新跟随默认群配置"""
        if gid is None:
            await self._execute_write("DELETE FROM groups")
            self._cache.clear()
            logger.info("所有群聊的群管配置已重新跟随默认值")
            return

        normalized_gid = str(gid)
        await self._execute_write("DELETE FROM groups WHERE group_id = ?", (normalized_gid,))
        self._cache.pop(normalized_gid, None)
        logger.info(f"群聊{normalized_gid}的群管配置已重新跟随默认值")

    async def reset_to_default(self, gid: str | None = None):
        """把指定群（或全部群）配置恢复成 default_cfg"""
        await self.follow_default(gid)
