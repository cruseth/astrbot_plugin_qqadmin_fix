"""真实 CQ/Pillow 与隔离 SDK 下的欢迎安全契约。"""
import asyncio
import importlib
import importlib.util
import io
import logging
import socket
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from PIL import Image as PILImage

ROOT = Path(__file__).resolve().parents[1]


class Plain:
    def __init__(self, text):
        self.text = text


class At:
    def __init__(self, qq):
        self.qq = qq


class Image:
    @staticmethod
    def fromBytes(data):
        image = Image()
        image.data = data
        return image


@pytest.fixture
def code(monkeypatch):
    def module(name, **attrs):
        value = types.ModuleType(name)
        value.__dict__.update(attrs)
        value.__path__ = []
        monkeypatch.setitem(sys.modules, name, value)
        return value

    # 先隔离其他测试的 aiocqhttp 替身，再加载已安装的真实 SDK。
    for name in list(sys.modules):
        if name == "aiocqhttp" or name.startswith("aiocqhttp."):
            monkeypatch.delitem(sys.modules, name)
    real_cq = importlib.import_module("aiocqhttp")
    monkeypatch.setitem(sys.modules, "aiocqhttp", real_cq)
    module("astrbot.api", logger=logging.getLogger("welcome-test"))
    module("astrbot.core.message.components", Plain=Plain, At=At, Image=Image)
    module("astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event",
           AiocqhttpMessageEvent=object)
    module("welcome_test", __path__=[str(ROOT)])
    module("welcome_test.core", __path__=[str(ROOT / "core")])
    module("welcome_test.config", PluginConfig=object)
    module("welcome_test.data", QQAdminDB=object)
    module("welcome_test.utils", get_nickname=AsyncMock(return_value="[CQ:at,qq=all]"),
           get_reply_message_str=lambda e: "", parse_bool=lambda x: x)

    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        result = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, result)
        spec.loader.exec_module(result)
        return result

    welcome = load("welcome_test.core.welcome", ROOT / "core/welcome.py")
    join = load("welcome_test.core.join_handle", ROOT / "core/join_handle.py")
    return types.SimpleNamespace(w=welcome, join=join, cq=real_cq)


def run(coro):
    return asyncio.run(coro)


def png():
    buffer = io.BytesIO()
    PILImage.new("RGB", (4, 4), "red").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def directory(tmp_path):
    path = tmp_path / "welcome_images"
    path.mkdir()
    return path


def text(chain):
    return "".join(p.text for p in chain if isinstance(p, Plain))


def test_real_cq_nickname_and_unknown(code, directory):
    assert code.cq.Message("[CQ:at,qq={qq}]")[0].type == "at"
    nickname = "[CQ:image,file=https://evil.test/a]{at}{qq}"
    raw = "[CQ:unknown,x=a&#44;b,] [CQ:at,qq=all] [CQ:at,qq=bad] [CQ:at,qq=1,broken]"
    chain = run(code.w.build_welcome(
        "{nickname}{at}[CQ:at,qq={qq}][CQ:at,qq=123]&#91;CQ:at,qq=all&#93;" + raw,
        "20", nickname, directory))
    assert [p.qq for p in chain if isinstance(p, At)] == ["20", "20", "123"]
    assert nickname in text(chain) and raw in text(chain)
    assert "[CQ:at,qq=all]" in text(chain)
    assert not any(isinstance(p, Image) for p in chain)


def test_local_real_image_and_bytes(code, directory):
    path = directory / "ok.png"
    path.write_bytes(png())
    chain = run(code.w.build_welcome("[CQ:image,file=ok.png]", "20", "", directory))
    assert isinstance(chain[0], Image)
    path.write_bytes(b"replaced")
    assert chain[0].data == png()


@pytest.mark.parametrize("source", [
    "../outside.png", "/outside.png", "C:/secret.png", "file:///secret.png",
    "{qq}.png", "{nickname}.png", "base64://abc", "missing.png",
])
def test_local_rejections(code, directory, source):
    chain = run(code.w.build_welcome(
        "before{at}[CQ:image,file=" + source + "]after", "20", "", directory))
    assert text(chain) == "before[欢迎图片不可用]after"
    assert isinstance(chain[1], At)
    assert source not in text(chain)


def test_format_size_pixels_and_directory(code, directory, monkeypatch):
    (directory / "wrong.jpg").write_bytes(png())
    (directory / "fake.png").write_bytes(b"not image")
    for source in ("wrong.jpg", "fake.png", "."):
        with pytest.raises((ValueError, OSError)):
            code.w.read_local_image(source, directory)
    with pytest.raises(ValueError):
        code.w.validate_image(b"x" * (code.w.MAX_BYTES + 1))
    monkeypatch.setattr(code.w, "MAX_PIXELS", 15)
    with pytest.raises(ValueError):
        code.w.validate_image(png())


def test_local_symlink(code, directory, tmp_path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(png())
    link = directory / "link.png"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("当前系统无软链权限")
    with pytest.raises(ValueError):
        code.w.read_local_image("link.png", directory)


def test_local_open_race(code, directory, tmp_path, monkeypatch):
    outside = tmp_path / "outside.png"
    outside.write_bytes(png())
    safe = directory / "safe.png"
    safe.write_bytes(png())
    real_open = code.w.os.open
    def race(path, flags):
        return real_open(outside, flags)
    monkeypatch.setattr(code.w.os, "open", race)
    with pytest.raises(ValueError):
        code.w.read_local_image("safe.png", directory)


def test_local_size_and_frame_pixels(code, directory, monkeypatch):
    (directory / "large.png").write_bytes(b"x" * (code.w.MAX_BYTES + 1))
    with pytest.raises(ValueError):
        code.w.read_local_image("large.png", directory)
    buffer = io.BytesIO()
    first = PILImage.new("RGB", (4, 4), "red")
    first.save(buffer, "GIF", save_all=True,
               append_images=[PILImage.new("RGB", (4, 4), "blue")])
    monkeypatch.setattr(code.w, "MAX_PIXELS", 20)
    with pytest.raises(ValueError):
        code.w.validate_image(buffer.getvalue())


def test_invalid_cq_keeps_original_without_substitution(code, directory):
    raw = "[CQ:at,qq={nickname}][CQ:image,file=ok.png,extra=1][CQ:thing,x={at}&#44;x]"
    chain = run(code.w.build_welcome(raw, "20", "123", directory))
    assert text(chain) == raw
    assert all(isinstance(p, Plain) for p in chain)


@pytest.mark.parametrize("url", [
    "http://localhost/a", "http://127.0.0.1/a", "http://10.0.0.1/a",
    "http://169.254.169.254/a", "http://100.64.0.1/a", "http://192.168.1.1/a",
    "http://[::1]/a", "http://[::ffff:8.8.8.8]/a", "http://[fe80::1]/a",
    "http://[64:ff9b::a00:1]/a", "http://[2002:a00:1::]/a",
    "http://224.0.0.1/a", "http://240.0.0.1/a", "https://public.test:8080/a",
    "https://user:secret@public.test/a", "ftp://public.test/a",
])
def test_private_and_invalid_urls(code, url):
    with pytest.raises(ValueError):
        code.w.validate_url(url)


def test_resolver_all_results_and_connector(code, monkeypatch):
    async def check():
        loop = asyncio.get_running_loop()
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        lookup = AsyncMock(return_value=answers)
        monkeypatch.setattr(loop, "getaddrinfo", lookup)
        resolver = code.w.PublicResolver()
        assert (await resolver.resolve("public.test", 443))[0]["host"] == "8.8.8.8"
        # 实际 TCPConnector 使用该次安全 DNS 结果，不重新解析域名。
        connector = code.w.aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False)
        try:
            resolved = await connector._resolve_host("public.test", 443)
            assert resolved[0]["host"] == "8.8.8.8"
            assert lookup.await_count == 2
        finally:
            await connector.close()
        answers.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0)))
        with pytest.raises(ValueError):
            await resolver.resolve("mixed.test", 443)
    run(check())


def fake_http(code, monkeypatch, responses):
    seen = []
    class Content:
        async def iter_chunked(self, size):
            for chunk in self.chunks:
                if isinstance(chunk, Exception):
                    raise chunk
                yield chunk
    class Response:
        def __init__(self, status=200, location=None, chunks=None, length=None):
            self.status = status
            self.headers = {"Location": location} if location else {}
            self.content_length = length
            self.content = Content()
            self.content.chunks = chunks or [png()]
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    class Session:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            assert isinstance(kwargs["connector"]._resolver, code.w.PublicResolver)
            self.connector = kwargs["connector"]
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            await self.connector.close()
        def get(self, url, **kwargs):
            assert kwargs == {"allow_redirects": False}
            seen.append(str(url))
            return Response(**responses.pop(0))
    monkeypatch.setattr(code.w.aiohttp, "ClientSession", Session)
    return seen


def test_remote_success_relative_redirect(code, directory, monkeypatch):
    seen = fake_http(code, monkeypatch, [
        {"status": 302, "location": "/next"}, {"chunks": [png()[:10], png()[10:]]},
    ])
    result = run(code.w.load_image("https://public.test/first", directory))
    assert result.data == png()
    assert seen == ["https://public.test/first", "https://public.test/next"]


@pytest.mark.parametrize("responses", [
    [{"status": 302, "location": "http://127.0.0.1/secret"}],
    [{"status": 302, "location": "/again"}] * 4,
    [{"length": 5 * 1024 * 1024 + 1}],
    [{"chunks": [b"x" * (5 * 1024 * 1024), b"x"]}],
    [{"chunks": [asyncio.TimeoutError()]}],
    [{"chunks": [b"not image"]}],
    [{"status": 404}],
])
def test_remote_failures(code, directory, monkeypatch, responses):
    fake_http(code, monkeypatch, list(responses))
    chain = run(code.w.build_welcome(
        "hello{at}[CQ:image,file=https://public.test/a]bye", "20", "", directory))
    assert text(chain) == "hello[欢迎图片不可用]bye"
    assert [p.qq for p in chain if isinstance(p, At)] == ["20"]


def test_image_limit_and_timeout(code, directory, monkeypatch):
    loader = AsyncMock(side_effect=ValueError())
    monkeypatch.setattr(code.w, "load_image", loader)
    chain = run(code.w.build_welcome("[CQ:image,file=a.png]" * 5, "20", "", directory))
    assert loader.await_count == 3
    assert text(chain).count("数量超限") == 2


def test_overall_timeout(code, directory, monkeypatch):
    async def slow(_):
        await asyncio.sleep(1)
    monkeypatch.setattr(code.w, "read_remote_image", slow)
    monkeypatch.setattr(code.w, "TIMEOUT", 0.01)
    with pytest.raises(asyncio.TimeoutError):
        run(code.w.load_image("https://public.test/a", directory))


@pytest.mark.parametrize("send_fails", [False, True])
def test_failed_image_and_send_still_ban(code, directory, send_fails):
    async def get(gid, key):
        return {"join_welcome": "{at}[CQ:image,file=missing.png]tail",
                "join_ban_time": 60}[key]
    event = types.SimpleNamespace(
        message_obj=types.SimpleNamespace(raw_message={
            "notice_type": "group_increase", "group_id": 10, "user_id": 20}),
        bot=types.SimpleNamespace(set_group_ban=AsyncMock()),
        get_self_id=lambda: "99", chain_result=lambda chain: chain,
        send=AsyncMock(side_effect=RuntimeError("send") if send_fails else None))
    handler = code.join.JoinHandle(
        types.SimpleNamespace(welcome_image_dir=directory),
        types.SimpleNamespace(get=get))
    run(handler.event_monitoring(event))
    assert text(event.send.await_args.args[0]) == "[欢迎图片不可用]tail"
    event.bot.set_group_ban.assert_awaited_once_with(
        group_id=10, user_id=20, duration=60)


@pytest.mark.parametrize("cancel_welcome", [False, True])
def test_ban_precedes_slow_or_cancelled_welcome(code, directory, monkeypatch, cancel_welcome):
    async def check():
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_build(*args):
            started.set()
            await release.wait()
            return [Plain("welcome")]

        async def get(gid, key):
            return {"join_welcome": "welcome", "join_ban_time": 60}[key]

        monkeypatch.setattr(code.join, "build_welcome", slow_build)
        event = types.SimpleNamespace(
            message_obj=types.SimpleNamespace(raw_message={
                "notice_type": "group_increase", "group_id": 10, "user_id": 20}),
            bot=types.SimpleNamespace(set_group_ban=AsyncMock()),
            get_self_id=lambda: "99", chain_result=lambda chain: chain,
            send=AsyncMock())
        handler = code.join.JoinHandle(
            types.SimpleNamespace(welcome_image_dir=directory),
            types.SimpleNamespace(get=get))
        task = asyncio.create_task(handler.event_monitoring(event))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            assert not task.done()
            event.send.assert_not_awaited()
            event.bot.set_group_ban.assert_awaited_once_with(
                group_id=10, user_id=20, duration=60)
            if cancel_welcome:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                event.send.assert_not_awaited()
            else:
                release.set()
                await asyncio.wait_for(task, timeout=1)
                event.send.assert_awaited_once()
            assert event.bot.set_group_ban.await_count == 1
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
    run(check())


def test_ban_failure_still_sends_welcome(code, directory, monkeypatch):
    async def get(gid, key):
        return {"join_welcome": "welcome", "join_ban_time": 60}[key]

    builder = AsyncMock(return_value=[Plain("welcome")])
    monkeypatch.setattr(code.join, "build_welcome", builder)
    event = types.SimpleNamespace(
        message_obj=types.SimpleNamespace(raw_message={
            "notice_type": "group_increase", "group_id": 10, "user_id": 20}),
        bot=types.SimpleNamespace(set_group_ban=AsyncMock(side_effect=RuntimeError("ban"))),
        get_self_id=lambda: "99", chain_result=lambda chain: chain,
        send=AsyncMock())
    handler = code.join.JoinHandle(
        types.SimpleNamespace(welcome_image_dir=directory),
        types.SimpleNamespace(get=get))
    run(handler.event_monitoring(event))
    event.bot.set_group_ban.assert_awaited_once()
    builder.assert_awaited_once()
    event.send.assert_awaited_once()
