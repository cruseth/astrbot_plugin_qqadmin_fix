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
from pathlib import Path

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
FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG",
           ".gif": "GIF", ".webp": "WEBP", ".bmp": "BMP"}
CQ_LEXEME = re.compile(r"\[CQ:[^\]]*\]")
PLACEHOLDER = re.compile(r"(\{at\}|\{qq\}|\{nickname\})")


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
        fn.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                       ctypes.c_uint32, ctypes.c_uint32]
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
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("image file")
        if not _handle_path(handle).is_relative_to(root):
            raise ValueError("image path")
        data = handle.read(MAX_BYTES + 1)
    return validate_image(data, FORMATS[path.suffix.lower()])


def public_ip(host: str) -> bool:
    address = ipaddress.ip_address(host)
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return False
        if address.sixtofour is not None or address.teredo is not None:
            return False
        if any(address in ipaddress.ip_network(network) for network in (
            "64:ff9b::/96", "64:ff9b:1::/48",
        )):
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
            {"hostname": host, "host": record[4][0], "port": port,
             "family": record[0], "proto": record[2],
             "flags": socket.AI_NUMERICHOST}
            for record in records
        ]

    async def close(self):
        pass


def validate_url(source: str) -> URL:
    url = URL(source)
    if (url.scheme not in {"http", "https"} or not url.host
            or url.user is not None or url.password is not None
            or url.port not in {80, 443}):
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
        connector=connector, trust_env=False,
        timeout=aiohttp.ClientTimeout(total=TIMEOUT),
    ) as session:
        for redirects in range(4):
            async with session.get(url, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    if redirects == 3 or not response.headers.get("Location"):
                        raise ValueError("image redirect")
                    url = validate_url(str(url.join(URL(response.headers["Location"]))))
                    continue
                if response.status != 200:
                    raise ValueError("image response")
                if response.content_length is not None and response.content_length > MAX_BYTES:
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


def _text(
    chain: list, text: str, uid: str, nickname: str, cq_mention: bool = True
):
    for part in PLACEHOLDER.split(text):
        if part == "{at}" and cq_mention and re.fullmatch(r"[0-9]+", uid):
            chain.append(At(qq=uid))
        elif part:
            chain.append(Plain(text={"{qq}": uid, "{nickname}": nickname}.get(part, part)))


async def build_welcome(
    template: str, uid: str, nickname: str, directory: Path,
    cq_mention: bool = True, cq_image: bool = True,
) -> list:
    chain = []
    cursor = images = 0
    for match in CQ_LEXEME.finditer(template):
        _text(chain, Message(template[cursor:match.start()]).extract_plain_text(),
              uid, nickname, cq_mention)
        raw = match.group()
        segments = Message(raw)
        segment = segments[0] if len(segments) == 1 else None
        if (cq_mention and segment is not None and segment.type == "at"
                and set(segment.data) == {"qq"}):
            target = segment.data["qq"]
            target = uid if target == "{qq}" else target
            if re.fullmatch(r"[0-9]+", target):
                chain.append(At(qq=target))
            else:
                chain.append(Plain(text=raw))
        elif (cq_image and segment is not None and segment.type == "image"
              and set(segment.data) in ({"file"}, {"url"})):
            images += 1
            if images > MAX_IMAGES:
                chain.append(Plain(text="[欢迎图片数量超限]"))
            else:
                try:
                    chain.append(await load_image(next(iter(segment.data.values())), directory))
                except Exception as exc:
                    logger.warning("欢迎图片加载失败 (%s)", type(exc).__name__)
                    chain.append(Plain(text="[欢迎图片不可用]"))
        else:
            chain.append(Plain(text=raw))
        cursor = match.end()
    _text(chain, Message(template[cursor:]).extract_plain_text(), uid, nickname,
          cq_mention)
    return chain
