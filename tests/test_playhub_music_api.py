"""点歌接 xiaoapi（`/detail/19`）：provider 契约 + 引擎卡面。

钉死三件事，任何一条被"顺手优化"掉都要让这个文件先红：
1. **`tips` 不出现**在任何输出里（接口自带的推广文案，用户 2026-11-13 要求处理掉）；
2. **没 key 就离线**（`build_music_provider` 失败关闭），离线卡面保持原样；
3. **`search()` 永不抛异常**，任何异常/非 JSON/非 200 一律 None。
"""
from __future__ import annotations

import gzip
import json
import os
import pathlib
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from plugins.playhub.adapters import xiaoapi
from plugins.playhub.core import Store
from plugins.playhub.core.clock import CN
from plugins.playhub.engines import GameRuntime
from plugins.playhub.engines.music import MusicEngine
from plugins.playhub.router import route

ROOM = "12345678@chatroom"
UID_A = "wxid_a"
NAME_A = "小明"

OK_PAYLOAD = {
    "code": 200,
    "cover": "https://y.qq.com/music/photo_new/T002R300x300M000002Neh8l0uciQZ_3.jpg",
    "name": "晴天",
    "singer": "周杰伦",
    "quality": "SQ无损",
    "url": "http://aqqmusic.tc.qq.com/amobile.music.tc.qq.com/F0000020wJDo3cx0j3.flac?vkey=abc",
    "tips": "慕名API：http://xiaoapi.cn",
}


class FakeMsg:
    def __init__(self, text: str = "", *, sender_id: str = UID_A, nickname: str = NAME_A):
        self.text = text
        self.chat_id = ROOM
        self.sender_id = sender_id
        self.is_self = False
        self.data = {"sender_wechat_name": nickname}


class FakeCtx:
    def __init__(self) -> None:
        self.sent: list[tuple[object, str]] = []
        self.members = self._Members()

    class _Members:
        def resolve(self, name, room=None):
            raise KeyError(name)

    def reply(self, msg, text: str) -> None:
        self.sent.append((msg, text))


class FakeClock:
    def __init__(self) -> None:
        self.ts = int(datetime.fromisoformat("2026-11-01T12:00:00").replace(tzinfo=CN).timestamp())

    def __call__(self) -> int:
        return self.ts

    def advance(self, seconds: int = 1) -> None:
        self.ts += seconds


class FakeProvider:
    enabled = True

    def __init__(self, result=None) -> None:
        self.result = result
        self.calls: list[str] = []

    def search(self, song: str, *, n: int = 1):
        self.calls.append(song)
        return self.result


class PayloadTests(unittest.TestCase):
    """`_to_song` 只认成功码 + 合法 url，其余一律 None。"""

    def test_success_keeps_fields_and_drops_tips(self) -> None:
        song = xiaoapi._to_song(json.dumps(OK_PAYLOAD).encode())
        self.assertIsNotNone(song)
        self.assertEqual(song.name, "晴天")
        self.assertEqual(song.singer, "周杰伦")
        self.assertEqual(song.quality, "SQ无损")
        self.assertTrue(song.url.startswith("http://aqqmusic"))
        self.assertFalse(hasattr(song, "tips"), "tips 不该成为 Song 的字段")

    def test_error_code_is_none(self) -> None:
        payload = {"code": 0, "msg": "密钥错误", "errcode": 11002}
        self.assertIsNone(xiaoapi._to_song(json.dumps(payload).encode()))

    def test_non_http_url_is_none(self) -> None:
        self.assertIsNone(xiaoapi._to_song(json.dumps({**OK_PAYLOAD, "url": "ftp://x/y"}).encode()))

    def test_missing_name_is_none(self) -> None:
        self.assertIsNone(xiaoapi._to_song(json.dumps({**OK_PAYLOAD, "name": ""}).encode()))

    def test_broken_json_is_none(self) -> None:
        self.assertIsNone(xiaoapi._to_song(b"<html>502</html>"))

    def test_gzip_body_is_decoded(self) -> None:
        raw = gzip.compress(json.dumps(OK_PAYLOAD).encode())
        song = xiaoapi._to_song(raw)
        self.assertIsNotNone(song)
        self.assertEqual(song.name, "晴天")


class ProviderTests(unittest.TestCase):
    def test_request_carries_key_msg_and_n(self) -> None:
        seen: list[str] = []

        def transport(url: str, timeout: float):
            seen.append(url)
            return json.dumps(OK_PAYLOAD).encode()

        provider = xiaoapi.XiaoApiMusicProvider("sk-test", transport=transport)
        song = provider.search("晴天")
        self.assertEqual(song.name, "晴天")
        self.assertTrue(seen[0].startswith(xiaoapi.ENDPOINT))
        self.assertIn("key=sk-test", seen[0])
        self.assertIn("msg=%E6%99%B4%E5%A4%A9", seen[0])
        self.assertIn("n=1", seen[0])

    def test_transport_exception_returns_none(self) -> None:
        def boom(url: str, timeout: float):
            raise TimeoutError("slow")

        provider = xiaoapi.XiaoApiMusicProvider("sk-test", transport=boom)
        self.assertIsNone(provider.search("晴天"))

    def test_empty_song_skips_the_network(self) -> None:
        calls: list[str] = []

        def transport(url: str, timeout: float):
            calls.append(url)
            return b""

        provider = xiaoapi.XiaoApiMusicProvider("sk-test", transport=transport)
        self.assertIsNone(provider.search("   "))
        self.assertEqual(calls, [])


class FactoryTests(unittest.TestCase):
    def test_without_key_is_offline(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            provider = xiaoapi.build_music_provider(None)
        self.assertFalse(provider.enabled)

    def test_blank_key_is_offline(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            provider = xiaoapi.build_music_provider({"xiaoapi_key": "   "})
        self.assertFalse(provider.enabled)

    def test_config_key_turns_it_on(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            provider = xiaoapi.build_music_provider({"xiaoapi_key": " sk-abc "})
        self.assertTrue(provider.enabled)
        self.assertEqual(provider._key, "sk-abc")

    def test_env_key_is_the_fallback(self) -> None:
        with mock.patch.dict(os.environ, {xiaoapi.KEY_ENV: "sk-env"}, clear=True):
            provider = xiaoapi.build_music_provider({})
        self.assertTrue(provider.enabled)
        self.assertEqual(provider._key, "sk-env")

    def test_bad_timeout_falls_back_to_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            provider = xiaoapi.build_music_provider(
                {"xiaoapi_key": "sk-abc", "xiaoapi_timeout_seconds": 999}
            )
        self.assertEqual(provider._timeout, xiaoapi.DEFAULT_TIMEOUT)


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.store = Store(self.tmp / "playhub.sqlite3", account="acct")
        self.store.migrate()
        self.clock = FakeClock()
        self.game = GameRuntime(self.store)
        self.ctx = FakeCtx()

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def engine(self, provider) -> MusicEngine:
        return MusicEngine(now_fn=self.clock, provider=provider)

    def ask(self, engine, text: str = "点歌 晴天", *, uid: str = UID_A):
        cmd = route(text)
        self.assertIsNotNone(cmd, f"「{text}」应当被路由成命令")
        return engine.on_command(cmd, FakeMsg(text, sender_id=uid), self.ctx, self.game)

    def test_found_card_shows_fields_and_no_tips(self) -> None:
        song = xiaoapi._to_song(json.dumps(OK_PAYLOAD).encode())
        reply = self.ask(self.engine(FakeProvider(song)))
        self.assertIsNotNone(reply)
        text = reply.text
        self.assertIn("晴天", text)
        self.assertIn("周杰伦", text)
        self.assertIn("SQ无损", text)
        self.assertIn("aqqmusic", text)
        self.assertNotIn("tips", text.lower())
        self.assertNotIn("慕名", text)
        self.assertNotIn("xiaoapi", text.lower())

    def test_missing_song_says_not_found(self) -> None:
        reply = self.ask(self.engine(FakeProvider(None)))
        self.assertIn("没找到《晴天》", reply.text)
        self.assertNotIn("慕名", reply.text)

    def test_cooldown_applies_to_online_path(self) -> None:
        song = xiaoapi._to_song(json.dumps(OK_PAYLOAD).encode())
        engine = self.engine(FakeProvider(song))
        self.ask(engine)
        second = self.ask(engine, "点歌 七里香")
        self.assertIn("缓一缓", second.text)
        self.clock.advance(31)
        third = self.ask(engine, "点歌 七里香")
        self.assertIn("晴天", third.text)

    def test_online_path_skips_the_offline_queue(self) -> None:
        song = xiaoapi._to_song(json.dumps(OK_PAYLOAD).encode())
        engine = self.engine(FakeProvider(song))
        for i in range(6):                       # 离线路径第 6 次会「点歌台满了」
            self.clock.advance(31)
            reply = self.ask(engine, f"点歌 歌{i}", uid=f"wxid_{i}")
            self.assertNotIn("点歌台满了", reply.text)

    def test_offline_provider_keeps_old_behaviour(self) -> None:
        reply = self.ask(self.engine(xiaoapi.OfflineMusicProvider()))
        self.assertIn("已点：《晴天》", reply.text)
        self.assertIn("不发音频", reply.text)

    def test_executor_path_replies_later(self) -> None:
        song = xiaoapi._to_song(json.dumps(OK_PAYLOAD).encode())
        jobs = []

        class FakeExecutor:
            def submit(self, fn, *args, **kwargs):
                jobs.append((fn, args))
                return True

        engine = MusicEngine(now_fn=self.clock, provider=FakeProvider(song), executor=FakeExecutor())
        self.assertIsNone(self.ask(engine))       # 结果稍后由 worker 直接发
        self.assertEqual(self.ctx.sent, [])
        fn, args = jobs[0]
        fn(*args)
        self.assertEqual(len(self.ctx.sent), 1)
        self.assertIn("周杰伦", self.ctx.sent[0][1])
        self.assertNotIn("慕名", self.ctx.sent[0][1])


if __name__ == "__main__":
    unittest.main()
