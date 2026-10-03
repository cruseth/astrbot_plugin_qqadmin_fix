"""受信欢迎模板与受限图片加载。"""
from __future__ import annotations

import asyncio
import ctypes
import io
import ipaddress
import os
import re
import socket
import stat
from pathlib import Path, PurePosixPath

import aiohttp
from aiocqhttp import Message
from PIL import Image as PILImage
from astrbot.api import logger
from astrbot.core.message.components import At, Image, Plain
from yarl import URL

MAX_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_IMAGES = 3
TIMEOUT = 10
FORMATS = {
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".gif": "GIF",
    ".webp": "WEBP",
    ".bmp": "BMP",
}
CQ_LEXEME = re.compile(r"\[CQ:[^\]]*\]")
PLACEHOLDER = re.compile(r"(\{at\}|\{qq\}|\{nickname\})")
DEFAULT_UPLOAD_PREFIX = ("files", "default", "items", "join_welcome_image")


def validate_image(data: bytes, expected: str | None = None) -> bytes:
    if not data or len(data) > MAX_BYTES:
        raise ValueError("image size")
    with PILImage.open(io.BytesIO(data)) as image:
        if image.format not in FORMATS.values() or (
            expected is not None and image.format != expected
        ):
            raise ValueError("image format")
        if image.width * image.height > MAX_PIXELS:
            raise ValueError("image pixels")
        image.verify()
    with PILImage.open(io.BytesIO(data)) as image:
        pixels = 0
        for frame in range(getattr(image, "n_frames", 1)):
            image.seek(frame)
            pixels += image.width * image.height
            if pixels > MAX_PIXELS:
                raise ValueError("image pixels")
            image.load()
    return data


def _handle_path(handle) -> Path:
    # 校验实际打开的文件，而不是仅校验可在打开前被替换的路径名。
    if os.name == "nt":
        import msvcrt

        fn = ctypes.windll.kernel32.GetFinalPathNameByHandleW
        fn.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        fn.restype = ctypes.c_uint32
        buffer = ctypes.create_unicode_buffer(32768)
        size = fn(msvcrt.get_osfhandle(handle.fileno()), buffer, len(buffer), 0)
        if not size or size >= len(buffer):
            raise ValueError("file handle")
        path = buffer.value
        if path.startswith("\\\\?\\UNC\\"):
            path = "\\\\" + path[8:]
        elif path.startswith("\\\\?\\"):
            path = path[4:]
        return Path(path).resolve(strict=True)
    fd_path = Path(f"/proc/self/fd/{handle.fileno()}")
    if not fd_path.exists():
        fd_path = Path(f"/dev/fd/{handle.fileno()}")
    return fd_path.resolve(strict=True)


def _read_regular_image(path: Path, root: Path, expected: str | None = None) -> bytes:
    root = Path(root).resolve(strict=True)
    fd = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0),
    )
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("image file")
        if not _handle_path(handle).is_relative_to(root):
            raise ValueError("image path")
        data = handle.read(MAX_BYTES + 1)
    return validate_image(data, expected)


def read_local_image(source: str, directory: Path) -> bytes:
    directory = Path(directory)
    root = directory.resolve(strict=True)
    if root != directory.parent.resolve(strict=True) / "welcome_images":
        raise ValueError("image directory")
    relative = Path(source)
    if relative.is_absolute() or relative.drive or ":" in source:
        raise ValueError("image path")
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root) or path.suffix.lower() not in FORMATS:
        raise ValueError("image path")
    if not path.is_file():
        raise ValueError("image file")
    return _read_regular_image(path, root, FORMATS[path.suffix.lower()])


def _legacy_image_dir(data_dir: Path) -> Path:
    path = Path(data_dir)
    # 兼容旧调用方直接传入 welcome_images 目录的写法。
    return path if path.name == "welcome_images" else path / "welcome_images"


def _safe_upload_parts(source: str, group_id: str | None) -> tuple[str, ...]:
    if not isinstance(source, str) or not source or source != source.strip():
        raise ValueError("image path")
    if any(ord(char) < 32 or ord(char) == 127 for char in source):
        raise ValueError("image path")
    if "\\" in source or ":" in source or source.startswith(("/", "//")):
        raise ValueError("image path")

    parts = tuple(source.split("/"))
    if len(parts) != 5 or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("image path")
    filename = parts[4]
    if PurePosixPath(filename).name != filename:
        raise ValueError("image path")

    if parts[:4] == DEFAULT_UPLOAD_PREFIX:
        return parts
    if (
        group_id is not None
        and str(group_id).isdigit()
        and parts[:3] == ("files", "groups", str(group_id))
        and parts[3] == "join_welcome_image"
    ):
        return parts
    raise ValueError("image path")


def resolve_uploaded_image(
    source: str, data_dir: Path, group_id: str | None = None
) -> Path:
    """把显式欢迎图片相对路径解析为数据目录内的真实文件。"""
    parts = _safe_upload_parts(source, group_id)
    if Path(parts[4]).suffix.lower() not in FORMATS:
        raise ValueError("image extension")

    root = Path(data_dir).resolve(strict=True)
    allowed_dir = (root / Path(*parts[:4])).resolve(strict=True)
    candidate = (root / Path(*parts)).resolve(strict=True)
    if not allowed_dir.is_relative_to(root):
        raise ValueError("image path")
    if candidate == allowed_dir or not candidate.is_file():
        raise ValueError("image file")
    if not candidate.is_relative_to(root) or not candidate.is_relative_to(
        allowed_dir
    ):
        raise ValueError("image path")

    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("image symlink")
    return candidate


def read_uploaded_image(
    source: str, data_dir: Path, group_id: str | None = None
) -> bytes:
    path = resolve_uploaded_image(source, data_dir, group_id)
    return _read_regular_image(
        path,
        path.parent.resolve(strict=True),
        FORMATS[path.suffix.lower()],
    )


async def load_uploaded_image(
    source: str, data_dir: Path, group_id: str | None = None
):
    data = await asyncio.to_thread(read_uploaded_image, source, data_dir, group_id)
    return Image.fromBytes(data)


def public_ip(host: str) -> bool:
    address = ipaddress.ip_address(host)
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return False
        if address.sixtofour is not None or address.teredo is not None:
            return False
        if any(
            address in ipaddress.ip_network(network)
            for network in ("64:ff9b::/96", "64:ff9b:1::/48")
        ):
            return False
    return address.is_global and not address.is_multicast and not address.is_reserved


class PublicResolver(aiohttp.abc.AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        if host.rstrip(".").lower() == "localhost":
            raise ValueError("private host")
        records = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM
        )
        if not records or any(not public_ip(record[4][0]) for record in records):
            raise ValueError("private address")
        return [
            {
                "hostname": host,
                "host": record[4][0],
                "port": port,
                "family": record[0],
                "proto": record[2],
                "flags": socket.AI_NUMERICHOST,
            }
            for record in records
        ]

    async def close(self):
        pass


def validate_url(source: str) -> URL:
    url = URL(source)
    if (
        url.scheme not in {"http", "https"}
        or not url.host
        or url.user is not None
        or url.password is not None
        or url.port not in {80, 443}
    ):
        raise ValueError("image URL")
    host = url.host
    if host.rstrip(".").lower() == "localhost" or "%" in host:
        raise ValueError("private host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        # aiohttp 对 IP 字面量绕过 resolver，因此此处必须独立校验。
        if not public_ip(host):
            raise ValueError("private address")
    return url


async def read_remote_image(source: str) -> bytes:
    url = validate_url(source)
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False)
    async with aiohttp.ClientSession(
        connector=connector,
        trust_env=False,
        timeout=aiohttp.ClientTimeout(total=TIMEOUT),
    ) as session:
        for redirects in range(4):
            async with session.get(url, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    if redirects == 3 or not response.headers.get("Location"):
                        raise ValueError("image redirect")
                    url = validate_url(
                        str(url.join(URL(response.headers["Location"])))
                    )
                    continue
                if response.status != 200:
                    raise ValueError("image response")
                if (
                    response.content_length is not None
                    and response.content_length > MAX_BYTES
                ):
                    raise ValueError("image size")
                data = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    if len(data) + len(chunk) > MAX_BYTES:
                        raise ValueError("image size")
                    data.extend(chunk)
                return await asyncio.to_thread(validate_image, bytes(data))
    raise ValueError("image redirect")


async def load_image(source: str, directory: Path):
    if any(token in source for token in ("{qq}", "{nickname}", "{at}")):
        raise ValueError("image placeholder")

    # 覆盖 DNS、全部重定向以及文件/图像校验的整体等待时间。
    async def read():
        if source.lower().startswith(("http://", "https://")):
            return await read_remote_image(source)
        return await asyncio.to_thread(read_local_image, source, directory)

    data = await asyncio.wait_for(read(), timeout=TIMEOUT)
    return Image.fromBytes(data)


async def load_legacy_cq_image(source: str, welcome_image_dir: Path):
    return await load_image(source, welcome_image_dir)


def _text(chain: list, text: str, uid: str, nickname: str, mention: bool):
    for part in PLACEHOLDER.split(text):
        if part == "{at}":
            if not (mention and re.fullmatch(r"[0-9]+", str(uid))):
                chain.append(Plain(text="{at}"))
        elif part:
            chain.append(
                Plain(
                    text={
                        "{qq}": str(uid),
                        "{nickname}": nickname,
                    }.get(part, part)
                )
            )


class _ImageBudget:
    def __init__(self):
        self.used = 0

    def take(self) -> bool:
        if self.used >= MAX_IMAGES:
            return False
        self.used += 1
        return True


async def _render_explicit_images(
    images: list[str],
    data_dir: Path,
    group_id: str | None,
    budget: _ImageBudget,
) -> list:
    chain = []
    for source in images:
        if not budget.take():
            chain.append(Plain(text="[欢迎图片数量超限]"))
            continue
        try:
            chain.append(await load_uploaded_image(source, data_dir, group_id))
        except Exception as exc:
            logger.warning("欢迎图片加载失败 (%s)", type(exc).__name__)
            chain.append(Plain(text="[欢迎图片不可用]"))
    return chain


async def _render_welcome_text(
    template: str,
    uid: str,
    nickname: str,
    legacy_image_dir: Path,
    mention: bool,
    budget: _ImageBudget,
    legacy_images_enabled: bool,
) -> list:
    chain = []
    cursor = 0
    for match in CQ_LEXEME.finditer(template):
        _text(
            chain,
            Message(template[cursor : match.start()]).extract_plain_text(),
            uid,
            nickname,
            mention,
        )
        raw = match.group()
        segments = Message(raw)
        segment = segments[0] if len(segments) == 1 else None
        if (
            segment is not None
            and segment.type == "at"
            and set(segment.data) == {"qq"}
        ):
            target = segment.data["qq"]
            target = uid if target == "{qq}" else target
            target = str(target)
            if re.fullmatch(r"[0-9]+", str(uid)) and target == str(uid):
                if mention:
                    pass
                else:
                    chain.append(Plain(text=raw))
            elif re.fullmatch(r"[0-9]+", target):
                chain.append(At(qq=target))
            else:
                chain.append(Plain(text=raw))
        elif (
            segment is not None
            and segment.type == "image"
            and set(segment.data) in ({"file"}, {"url"})
        ):
            if not legacy_images_enabled:
                chain.append(Plain(text=raw))
            elif not budget.take():
                chain.append(Plain(text="[欢迎图片数量超限]"))
            else:
                try:
                    chain.append(
                        await load_legacy_cq_image(
                            next(iter(segment.data.values())), legacy_image_dir
                        )
                    )
                except Exception as exc:
                    logger.warning("欢迎图片加载失败 (%s)", type(exc).__name__)
                    chain.append(Plain(text="[欢迎图片不可用]"))
        else:
            chain.append(Plain(text=raw))
        cursor = match.end()
    _text(
        chain,
        Message(template[cursor:]).extract_plain_text(),
        uid,
        nickname,
        mention,
    )
    return chain


async def build_welcome(
    template: str,
    uid: str,
    nickname: str,
    data_dir: Path,
    *,
    mention: bool = True,
    images: list[str] | None = None,
    image_before: bool = False,
    group_id: str | None = None,
    **legacy_kwargs,
) -> list:
    """组装欢迎消息链，显式 @ 永远位于最前。"""
    if "cq_mention" in legacy_kwargs:
        mention = bool(legacy_kwargs.pop("cq_mention"))
    legacy_images_enabled = bool(legacy_kwargs.pop("cq_image", True))
    if legacy_kwargs:
        unexpected = ", ".join(sorted(legacy_kwargs))
        raise TypeError(f"unexpected keyword argument(s): {unexpected}")

    template = template if isinstance(template, str) else str(template or "")
    image_list = list(images or [])
    if not template and not image_list:
        return []

    data_dir = Path(data_dir)
    legacy_image_dir = _legacy_image_dir(data_dir)
    budget = _ImageBudget()
    chain: list = []
    if mention and re.fullmatch(r"[0-9]+", str(uid)):
        chain.append(At(qq=str(uid)))

    if image_before:
        chain.extend(
            await _render_explicit_images(image_list, data_dir, group_id, budget)
        )
        chain.extend(
            await _render_welcome_text(
                template,
                uid,
                nickname,
                legacy_image_dir,
                mention,
                budget,
                legacy_images_enabled,
            )
        )
    else:
        chain.extend(
            await _render_welcome_text(
                template,
                uid,
                nickname,
                legacy_image_dir,
                mention,
                budget,
                legacy_images_enabled,
            )
        )
        chain.extend(
            await _render_explicit_images(image_list, data_dir, group_id, budget)
        )
    return chain
