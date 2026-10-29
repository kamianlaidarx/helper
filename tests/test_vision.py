"""视觉链路：图片编码、多模态请求体和艾特时携带刚发的图；不连接外网或微信。"""
import base64
from datetime import datetime
from io import BytesIO
import json
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from PIL import Image, PngImagePlugin

from plugins.mention_chat import Plugin as ChatPlugin
from tools.probe_vision import payload as probe_payload, probe
from wxbot_framework.dialogue import Dialogue
from wxbot_framework.images import image_data_url
from wxbot_framework.llm import LLMClient, LLMError
from wxbot_framework.sdk import Message


def picture(size=(4000, 3000), mode="RGBA"):
    data = BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("private", "test-metadata")
    image = Image.new(mode, size, (10, 200, 30, 128) if mode == "RGBA" else "pink")
    image.save(data, "PNG", pnginfo=metadata)
    return data.getvalue()


class ImageDataUrlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def store(self, data, name="image.png"):
        path = self.directory / name
        path.write_bytes(data)
        return path

    def test_png_is_shrunk_flattened_and_stripped_to_jpeg(self):
        url = image_data_url(self.store(picture()))
        self.assertTrue(url.startswith("data:image/jpeg;base64,"))
        decoded = base64.b64decode(url.partition(",")[2])
        self.assertNotIn(b"test-metadata", decoded)
        with Image.open(BytesIO(decoded)) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.mode, "RGB")
            self.assertLessEqual(max(image.size), 1280)
            self.assertEqual(image.size, (1280, 960))

    def test_indivisible_bytes_budget_is_reported_instead_of_sent(self):
        with self.assertRaises(ValueError) as caught:
            image_data_url(self.store(picture()), max_bytes=1024)
        self.assertIn("4 MiB", str(caught.exception))

    def test_non_image_and_missing_files_are_rejected(self):
        for path in (self.store(b"not an image at all", "fake.png"), self.directory / "missing.png"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                image_data_url(path)


class VisionRequestTests(unittest.TestCase):
    def setUp(self):
        self.client = LLMClient({"api_key": "dummy-test-key", "model": "deepseek-flash"})

    def response(self, content):
        return BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())

    def test_image_uses_content_parts_without_response_format(self):
        with patch("wxbot_framework.llm.urlopen", return_value=self.response('{"reply":"一只猫"}')) as call:
            result = self.client.complete_json("人格", "{\"current\":\"图里是什么\"}",
                                               image="data:image/jpeg;base64,AAAA")
        self.assertEqual(result, {"reply": "一只猫"})
        body = json.loads(call.call_args.args[0].data)
        self.assertNotIn("response_format", body)
        self.assertIn("JSON 对象", body["messages"][0]["content"])
        self.assertEqual(body["messages"][1]["content"], [
            {"type": "text", "text": "{\"current\":\"图里是什么\"}"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}])

    def test_fenced_json_is_accepted_when_an_image_is_attached(self):
        with patch("wxbot_framework.llm.urlopen",
                   return_value=self.response('```json\n{"reply":"一只猫"}\n```')):
            self.assertEqual(self.client.complete_json("人格", "看图", image="data:image/png;base64,AAAA"),
                             {"reply": "一只猫"})

    def test_text_only_requests_keep_the_previous_payload(self):
        with patch("wxbot_framework.llm.urlopen", return_value=self.response('{"reply":"好"}')) as call:
            self.client.complete_json("人格", "你好")
        body = json.loads(call.call_args.args[0].data)
        self.assertEqual(body["messages"][1]["content"], "你好")
        self.assertEqual(body["response_format"], {"type": "json_object"})

    def test_non_data_url_image_is_refused_before_any_request(self):
        for value in ("https://example.com/a.png", "AAAA", b"binary", 42):
            with self.subTest(value=value), patch("wxbot_framework.llm.urlopen") as call:
                with self.assertRaises(ValueError):
                    self.client.complete_json("人格", "看图", image=value)
                self.assertEqual(call.call_count, 0)

    def test_parse_failure_names_the_exception_type_in_the_log(self):
        with patch("wxbot_framework.llm.urlopen", return_value=self.response("我看不到图片")), \
                self.assertLogs("LLM", level="WARNING") as logs, self.assertRaises(LLMError):
            self.client.complete_json("人格", "看图", image="data:image/png;base64,AAAA")
        self.assertIn("JSONDecodeError", "\n".join(logs.output))


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.config = {"model": "deepseek-vl", "api_key": "TEST_SECRET", "base_url": "https://probe.example.test/v1"}

    def opener(self, body):
        def open_url(request, timeout=None):
            open_url.request = request
            return BytesIO(json.dumps(body).encode())
        return open_url

    def test_payload_carries_a_data_url_and_no_response_format(self):
        body = probe_payload(self.config)
        parts = body["messages"][0]["content"]
        self.assertEqual(parts[0]["type"], "text")
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertNotIn("response_format", body)

    def test_payload_leaves_room_for_reasoning_models(self):
        self.assertGreaterEqual(probe_payload(self.config)["max_tokens"], 128)

    def test_truncated_reply_explains_the_missing_content(self):
        accepted, message = probe(self.config, open_url=self.opener(
            {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}))
        self.assertTrue(accepted)
        self.assertIn("finish_reason=length", message)
        self.assertIn("max_tokens", message)

    def test_accepting_endpoint_is_reported_as_supported(self):
        accepted, message = probe(self.config, open_url=self.opener(
            {"choices": [{"message": {"content": "红色"}}]}))
        self.assertTrue(accepted)
        self.assertIn("红色", message)

    def test_rejection_keeps_the_server_reason_but_not_the_key(self):
        def reject(request, timeout=None):
            raise HTTPError(request.full_url, 400, "Bad Request", {},
                            BytesIO('{"error":"model does not support image_url"}'.encode()))
        accepted, message = probe(self.config, open_url=reject)
        self.assertFalse(accepted)
        self.assertIn("HTTP 400", message)
        self.assertIn("image_url", message)
        self.assertNotIn("TEST_SECRET", message)


class Snapshot:
    def __init__(self, current="图里是什么？"):
        self.messages = []
        self.day = "2026-02-18"
        self.speaker_name = "小猫"
        self.current = current


class Members:
    account = "wxid_test_robot"
    history_enabled = True
    groups = {"123@chatroom"}

    def __init__(self):
        self.snapshots = {}

    def conversation_snapshot(self, message):
        return self.snapshots.setdefault(message.sequence, Snapshot())

    def display_name(self, sender_id, chat_id):
        return "小猫"


class Client:
    def __init__(self):
        self.calls = []

    def complete_json(self, system, user, image=None):
        self.calls.append({"system": system, "user": user, "image": image})
        return {"reply": "图里是一只戴墨镜的橘猫。"}


class Context:
    def __init__(self, members):
        self.members = members
        self.client = Client()
        self.logger = logging.getLogger("MentionChatVisionTest")
        self.sent = []
        self._speech = None
        self.active = {"mention_chat"}

    def plugin_is_active(self, name):
        return name in self.active

    def llm_for(self, message, personality=None):
        return self.client

    @property
    def llm(self):
        return self.client

    def personality(self, message):
        return SimpleNamespace(name="默认")

    def response_guard(self, snapshot, personality):
        return lambda: None

    def reply(self, message, text, *, delivery_guard=None):
        self.sent.append((message.chat_id, text))
        return True

    def reply_at(self, message, text, *, display_name=None, delivery_guard=None):
        self.sent.append((message.chat_id, text))
        return True


class MentionChatVisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.ctx = Context(Members())
        self.now = 1000.0
        timer = patch("plugins.mention_chat.time.monotonic", side_effect=lambda: self.now)
        timer.start()
        self.addCleanup(timer.stop)
        self.plugin = ChatPlugin()
        self.plugin.on_load(self.ctx)
        self.sequence = 1

    def image(self, path, **kwargs):
        values = {"text": "", "chat_id": "123@chatroom", "sender_id": "wxid_sender", "is_group": True,
                  "type": 3, "sequence": self.next_sequence(),
                  "data": {"image_path": str(path), "image_status": "ready"}}
        values.update(kwargs)
        return Message(**values)

    def mention(self, text="图里是什么？", **kwargs):
        values = {"text": "@机器人\u2005" + text, "chat_id": "123@chatroom", "sender_id": "wxid_sender",
                  "is_group": True, "type": 1, "sequence": self.next_sequence(),
                  "data": {"is_at_self": True, "at_members": ("wxid_test_robot",)}}
        values.update(kwargs)
        return Message(**values)

    def next_sequence(self):
        self.sequence += 1
        return self.sequence

    def store(self, data=None, name="image.png"):
        path = self.directory / name
        path.write_bytes(picture() if data is None else data)
        return path

    def test_picture_before_mention_reaches_the_model(self):
        self.plugin.on_message(self.image(self.store()), self.ctx)
        self.plugin.on_message(self.mention(), self.ctx)
        self.assertEqual(len(self.ctx.client.calls), 1)
        self.assertTrue(self.ctx.client.calls[0]["image"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(self.ctx.sent, [("123@chatroom", "图里是一只戴墨镜的橘猫。")])

    def test_picture_message_itself_is_not_answered(self):
        self.plugin.on_message(self.image(self.store()), self.ctx)
        self.assertEqual(self.ctx.client.calls, [])
        self.assertEqual(self.ctx.sent, [])

    def test_draw_request_is_left_to_the_image_plugin(self):
        self.ctx.active.add("image_gen")
        self.plugin.on_message(self.mention("画一只戴墨镜的橘猫"), self.ctx)
        self.assertEqual(self.ctx.client.calls, [])
        self.assertEqual(self.ctx.sent, [])

    def test_draw_request_still_gets_a_chat_reply_when_image_plugin_is_off(self):
        self.plugin.on_message(self.mention("画一只戴墨镜的橘猫"), self.ctx)
        self.assertEqual(len(self.ctx.client.calls), 1)
        self.assertEqual(len(self.ctx.sent), 1)

    def test_mention_without_picture_stays_text_only(self):
        self.plugin.on_message(self.mention(), self.ctx)
        self.assertIsNone(self.ctx.client.calls[0]["image"])

    def test_stale_picture_is_dropped_instead_of_attached(self):
        self.plugin.on_message(self.image(self.store()), self.ctx)
        self.now += 301
        self.plugin.on_message(self.mention(), self.ctx)
        self.assertIsNone(self.ctx.client.calls[0]["image"])

    def test_other_members_picture_only_counts_for_one_minute(self):
        self.plugin.on_message(self.image(self.store(), sender_id="wxid_other"), self.ctx)
        self.plugin.on_message(self.mention(), self.ctx)
        self.assertTrue(self.ctx.client.calls[0]["image"].startswith("data:image/jpeg;base64,"))
        self.now += 61
        self.plugin.on_message(self.mention(), self.ctx)
        self.assertIsNone(self.ctx.client.calls[1]["image"])

    def test_own_unready_or_bot_picture_is_ignored(self):
        for data in ({"image_status": "failed", "image_path": str(self.store())},
                     {"image_status": "ready", "image_path": str(self.store()), "bot_generated": True},
                     {"image_status": "ready", "image_path": ""}):
            with self.subTest(data=data):
                self.plugin.on_message(self.image("", data=data), self.ctx)
                self.plugin.on_message(self.mention(), self.ctx)
                self.assertIsNone(self.ctx.client.calls[-1]["image"])

    def test_broken_file_falls_back_to_text_and_is_forgotten(self):
        broken = self.directory / "broken.jpg"
        broken.write_bytes(b"definitely not a jpeg")
        self.plugin.on_message(self.image(broken, data={"image_status": "ready",
                                                       "image_path": str(broken)}), self.ctx)
        self.plugin.on_message(self.mention(), self.ctx)
        self.assertIsNone(self.ctx.client.calls[0]["image"])
        self.assertEqual(self.plugin.images, {})

    def test_model_failure_still_replies_with_the_apology(self):
        self.plugin.on_message(self.image(self.store()), self.ctx)
        self.ctx.client.complete_json = lambda system, user, image=None: (_ for _ in ()).throw(
            LLMError("大模型响应格式不正确，请重试"))
        self.plugin.on_message(self.mention(), self.ctx)
        self.assertEqual(self.ctx.sent, [("123@chatroom", "暂时无法回复，请稍后再艾特我。")])


if __name__ == "__main__":
    unittest.main()

class PlainReplyTests(unittest.TestCase):
    """网关忽略 response_format 时改用 reply_format=text。"""

    def test_reply_format_is_validated(self):
        for bad in ("xml", True, 1, None):
            if bad is None:
                continue
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "reply_format"):
                LLMClient({"api_key": "k", "reply_format": bad})

    def test_complete_text_sends_no_json_constraints(self):
        body = {"choices": [{"finish_reason": "stop", "message": {"content": "  图里是三个色块  "}}]}
        with patch("wxbot_framework.llm.urlopen", return_value=BytesIO(json.dumps(body).encode())) as call:
            client = LLMClient({"api_key": "k"})
            self.assertEqual(client.complete_text("系统规则", "用户输入"), "图里是三个色块")
        payload = json.loads(call.call_args.args[0].data)
        self.assertNotIn("response_format", payload)
        self.assertEqual(payload["messages"][0]["content"], "系统规则")

    def test_complete_text_attaches_the_image_as_content_parts(self):
        body = {"choices": [{"finish_reason": "stop", "message": {"content": "红绿蓝"}}]}
        with patch("wxbot_framework.llm.urlopen", return_value=BytesIO(json.dumps(body).encode())) as call:
            client = LLMClient({"api_key": "k"})
            self.assertEqual(client.complete_text("系统", "用户", image="data:image/png;base64,AAA"), "红绿蓝")
        parts = json.loads(call.call_args.args[0].data)["messages"][1]["content"]
        self.assertEqual([part["type"] for part in parts], ["text", "image_url"])

    def test_complete_text_still_reports_upstream_failures(self):
        for raw in (b'{"choices":[null]}', b"<html>bad gateway</html>"):
            with self.subTest(raw=raw[:12]), patch("wxbot_framework.llm.urlopen", return_value=BytesIO(raw)):
                with self.assertRaises(LLMError) as caught:
                    LLMClient({"api_key": "k"}).complete_text("系统", "用户")
                self.assertNotIn("html", str(caught.exception))


class TextClient:
    reply_format = "text"

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def complete_text(self, system, user, image=None):
        self.calls.append((system, user, image))
        return self.reply


class DialoguePlainTests(unittest.TestCase):
    def test_plain_reply_asks_for_prose_instead_of_json(self):
        client = TextClient("  图里从左到右是红圆、绿圆、蓝方  ")
        self.assertEqual(Dialogue(client).reply(Snapshot()), "图里从左到右是红圆、绿圆、蓝方")
        system, _, image = client.calls[0]
        self.assertIn("直接输出要说的那段话本身", system)
        self.assertNotIn("JSON 对象", system)
        self.assertIsNone(image)

    def test_plain_reply_strips_fences_and_quotes(self):
        for wrapped in ('```\n"图里有三个色块"\n```', '"图里有三个色块"'):
            with self.subTest(wrapped=wrapped[:8]):
                self.assertEqual(Dialogue(TextClient(wrapped)).reply(Snapshot()), "图里有三个色块")

    def test_plain_reply_passes_the_image_through(self):
        client = TextClient("一张白底图")
        Dialogue(client).reply(Snapshot(), image="data:image/png;base64,AAA")
        self.assertEqual(client.calls[0][2], "data:image/png;base64,AAA")

    def test_plain_reply_still_rejects_empty_or_broken_text(self):
        for bad in ("   ", "坏\x07字", "长" * 601):
            with self.subTest(bad=bad[:6]), self.assertRaises(LLMError):
                Dialogue(TextClient(bad)).reply(Snapshot())

class PersonalityWrapperTests(unittest.TestCase):
    """人格包装层不能挡住图片和纯文本回复：真实链路里 mention_chat 拿到的就是它。"""

    def test_reply_format_follows_the_wrapped_client(self):
        from wxbot_framework.personality import PersonalityLLM, PersonalityState
        inner = TextClient("图里是一只橘猫")
        client = PersonalityLLM(inner, PersonalityState(room="1@chatroom"), current=lambda: True)
        self.assertEqual(client.reply_format, "text")
        self.assertEqual(Dialogue(client).reply(Snapshot(), image="data:image/jpeg;base64,AAA"), "图里是一只橘猫")
        system, _, image = inner.calls[0]
        self.assertEqual(image, "data:image/jpeg;base64,AAA")
        self.assertIn("当前人格", system)

    def test_json_mode_client_still_receives_the_image(self):
        from wxbot_framework.personality import PersonalityLLM, PersonalityState
        seen = []

        class JsonClient:
            reply_format = "json"

            def complete_json(self, system, user, image=None):
                seen.append((system, user, image))
                return {"reply": "图里是一只橘猫"}

        client = PersonalityLLM(JsonClient(), PersonalityState(room="1@chatroom", name="嘴臭"), current=lambda: True)
        self.assertEqual(Dialogue(client).reply(Snapshot(), image="data:image/jpeg;base64,AAA"), "图里是一只橘猫")
        self.assertEqual(seen[0][2], "data:image/jpeg;base64,AAA")
        self.assertIn("当前人格", seen[0][0])

    def test_switch_during_the_request_discards_the_result(self):
        from wxbot_framework.personality import PersonalityExpired, PersonalityLLM, PersonalityState
        calls = []

        class Switching(TextClient):
            def complete_text(self, system, user, image=None):
                calls.append(1)
                alive[0] = False
                return super().complete_text(system, user, image)

        alive = [True]
        client = PersonalityLLM(Switching("图里是一只橘猫"), PersonalityState(room="1@chatroom"),
                                current=lambda: alive[0])
        with self.assertRaises(PersonalityExpired):
            client.complete_text("系统", "问题", image="data:image/jpeg;base64,AAA")
        self.assertEqual(calls, [1])
