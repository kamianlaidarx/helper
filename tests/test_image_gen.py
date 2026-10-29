"""生图接口边界与插件触发条件；不连接外网或微信。"""
from contextlib import contextmanager, nullcontext
from io import BytesIO
import json
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error

from PIL import Image, PngImagePlugin

from plugins.image_gen import Plugin
from plugins.image_gen.provider import ImageGenCancelled, ImageGenError, ImageProvider, MAX_IMAGE_BYTES
from wxbot_framework.sdk import Message


def png(size=(24, 32)):
    data = BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text('private', 'test-metadata')
    Image.new('RGB', size, 'skyblue').save(data, 'PNG', pnginfo=metadata)
    return data.getvalue()


class Response(BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = {'Content-Length': str(len(body)), **(headers or {})}


def payload(value):
    return Response(json.dumps(value).encode('utf-8'))


def options(**overrides):
    values = {'provider': 'openai', 'api_key': 'TEST_SECRET', 'base_url': 'https://api.example.com/v1',
              'model': 'test-image', 'size': '1024x1024', 'timeout_seconds': 30}
    values.update(overrides)
    return values


class ProviderTests(unittest.TestCase):
    def provider(self, responses=(), **overrides):
        client = ImageProvider(options(**overrides))
        client.opener = Mock()
        client.opener.open.side_effect = list(responses)
        return client

    def test_base64_result_is_normalized_to_jpeg_without_metadata(self):
        import base64
        client = self.provider([payload({'data': [{'b64_json': base64.b64encode(png()).decode('ascii')}]})])
        data = client.generate('一只橘猫')
        with Image.open(BytesIO(data)) as decoded:
            self.assertEqual(decoded.format, 'JPEG')
            self.assertEqual(decoded.size, (24, 32))
            self.assertNotIn('private', decoded.info)
        request = client.opener.open.call_args_list[0].args[0]
        self.assertEqual(json.loads(request.data)['prompt'], '一只橘猫')
        self.assertNotIn('response_format', json.loads(request.data))
        self.assertEqual(request.get_header('Authorization'), 'Bearer TEST_SECRET')

    def test_chat_provider_reads_inline_image_data_url(self):
        import base64
        data_url = 'data:image/png;base64,' + base64.b64encode(png()).decode('ascii')
        client = self.provider(
            [payload({'choices': [{'message': {'content': None,
                                               'images': [{'type': 'image_url',
                                                           'image_url': {'url': data_url}}]}}]})],
            provider='openai-chat')
        data = client.generate('一只橘猫')
        with Image.open(BytesIO(data)) as decoded:
            self.assertEqual(decoded.format, 'JPEG')
        request = client.opener.open.call_args_list[0].args[0]
        self.assertTrue(request.full_url.endswith('/chat/completions'))
        body = json.loads(request.data)
        self.assertEqual(body['messages'], [{'role': 'user', 'content': '一只橘猫'}])
        self.assertNotIn('size', body)

    def test_chat_provider_without_images_is_reported(self):
        for result in ({'choices': [{'message': {'content': '我画不出来'}}]},
                       {'choices': [{'message': {'images': []}}]},
                       {'choices': []}):
            client = self.provider([payload(result)], provider='openai-chat')
            with self.subTest(result=result), self.assertRaises(ImageGenError) as caught:
                client.generate('x')
            self.assertEqual(str(caught.exception), '生图接口未返回图片')

    def test_server_reason_is_shown_but_credentials_are_not(self):
        reason = BytesIO(json.dumps({'error': {'message': 'auth_not_found: no auth available '
                                                         '(providers=codex, model=gpt-image-2.5)'}}).encode())
        error = urllib.error.HTTPError('https://api.example.com/v1/images/generations', 503,
                                       'Unavailable', {}, reason)
        client = self.provider([error])
        with self.assertRaises(ImageGenError) as caught:
            client.generate('x')
        self.assertIn('HTTP 503', str(caught.exception))
        self.assertIn('no auth available', str(caught.exception))
        self.assertNotIn('api.example.com', str(caught.exception))

        leaked = BytesIO(json.dumps({'error': {'message': 'bad token TEST_SECRET'}}).encode())
        client = self.provider([urllib.error.HTTPError('https://api.example.com/v1/images/generations',
                                                       401, 'Unauthorized', {}, leaked)])
        with self.assertRaises(ImageGenError) as caught:
            client.generate('x')
        self.assertEqual(str(caught.exception), '生图接口 HTTP 401；请核对密钥、余额和模型名')

    def test_connection_reset_before_response_is_retried_once(self):
        client = self.provider([urllib.error.URLError('reset'), payload({'data': [
            {'b64_json': __import__('base64').b64encode(png()).decode('ascii')}]})])
        with patch('plugins.image_gen.provider.time.sleep') as sleep:
            self.assertTrue(client.generate('x'))
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(client.opener.open.call_count, 2)

    def test_url_result_downloads_without_api_key(self):
        client = self.provider([payload({'data': [{'url': 'https://cdn.example.com/a.png'}]}), Response(png())])
        self.assertTrue(client.generate('x'))
        first, second = [row.args[0] for row in client.opener.open.call_args_list]
        self.assertEqual(second.get_method(), 'GET')
        self.assertNotIn('Authorization', second.headers)

    def test_http_error_and_bad_payload_hide_credentials(self):
        error = urllib.error.HTTPError('https://api.example.com/v1/images/generations', 401, 'Unauthorized',
                                      {}, None)
        client = self.provider([error])
        with self.assertRaises(ImageGenError) as caught:
            client.generate('x')
        self.assertIn('HTTP 401', str(caught.exception))
        self.assertNotIn('TEST_SECRET', str(caught.exception))
        self.assertNotIn('api.example.com', str(caught.exception))

    def test_unsafe_download_target_is_refused_before_second_request(self):
        for url in ('http://cdn.example.com/a.png', 'https://127.0.0.1/a.png',
                    'https://cdn.example.com/a.png?x=1#frag' and 'ftp://cdn.example.com/a.png'):
            client = self.provider([payload({'data': [{'url': url}]})])
            with self.subTest(url=url), self.assertRaises(ImageGenError):
                client.generate('x')
            self.assertEqual(client.opener.open.call_count, 1)

    def test_oversized_and_invalid_images_are_rejected(self):
        client = self.provider([Response(b'x' * (MAX_IMAGE_BYTES + 1))])
        with self.assertRaises(ImageGenError):
            client.generate('x')
        import base64
        client = self.provider([payload({'data': [{'b64_json': base64.b64encode(b'not an image').decode()}]})])
        with self.assertRaises(ImageGenError) as caught:
            client.generate('x')
        self.assertEqual(str(caught.exception), '生图模型未返回有效图片')

    def test_cancellation_stops_before_any_request(self):
        client = self.provider()
        with self.assertRaises(ImageGenCancelled):
            client.generate('x', current=lambda: False)
        self.assertEqual(client.opener.open.call_count, 0)

    def test_timeout_and_connection_failures_are_reported_as_one_sentence(self):
        for error in (urllib.error.URLError('boom'), TimeoutError('slow')):
            client = self.provider([error, error, error])
            with patch('plugins.image_gen.provider.time.sleep'), \
                    self.subTest(error=error), self.assertRaises(ImageGenError) as caught:
                client.generate('x')
            self.assertEqual(str(caught.exception), '生图接口连接失败或超时')
            self.assertEqual(client.opener.open.call_count, 3)

    def test_dashscope_task_is_polled_until_success(self):
        client = self.provider([
            payload({'output': {'task_id': 'ok-task'}}),
            payload({'output': {'task_status': 'RUNNING'}}),
            payload({'output': {'task_status': 'SUCCEEDED', 'results': [{'url': 'https://cdn.example.com/a.png'}]}}),
            Response(png())], provider='dashscope', model='wanx2.1-t2i-turbo')
        self.assertTrue(client.generate('x'))
        poll = client.opener.open.call_args_list[2].args[0]
        self.assertTrue(poll.full_url.endswith('/api/v1/tasks/ok-task'))

    def test_invalid_options_are_rejected(self):
        bad = [{'provider': 'other'}, {'api_key': ' '}, {'model': ''},
               {'base_url': 'http://api.example.com/v1'}, {'base_url': 'https://api.example.com/v1?k=1'},
               {'size': '1024 x 1024'}, {'response_format': 'xml'},
               {'timeout_seconds': 0}, {'timeout_seconds': 600}, {'timeout_seconds': True}]
        for values in bad:
            with self.subTest(values=values), self.assertRaises(ValueError):
                ImageProvider(options(**values))

    def test_missing_config_file_has_actionable_message(self):
        with self.assertRaises(ValueError) as caught:
            ImageProvider.from_file(Path(tempfile.gettempdir()) / 'missing-image_config.json')
        self.assertIn('image_config.example.json', str(caught.exception))


class Context:
    def __init__(self, root):
        self.config = {}
        self.project_root = Path(root)
        self.members = SimpleNamespace(account='wxid_test_robot', groups={'123@chatroom', '456@chatroom'})
        self.logger = logging.getLogger('ImageGenPluginTest')
        self.current = True
        self.sent = []
        self.image_result = True
        self.send_error = None
        self._service = SimpleNamespace(session_is_current=lambda session: self.current and session == 'one')

    def plugin_is_active(self, name):
        return name == 'image_gen' and self.current

    def reply(self, message, text, *, delivery_guard=None):
        with delivery_guard() if delivery_guard else nullcontext():
            self.sent.append(('text', message.chat_id, text))
        return True

    def reply_image(self, message, path, *, delivery_guard=None):
        with delivery_guard() if delivery_guard else nullcontext():
            assert Path(path).is_file()
            self.sent.append(('image', message.chat_id, Path(path)))
            if self.send_error:
                raise self.send_error
            return self.image_result


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'image_config.json').write_text(json.dumps(options()), encoding='utf-8')
        self.ctx = Context(self.root)
        self.now = 100.0
        timer = patch('plugins.image_gen.time.monotonic', side_effect=lambda: self.now)
        timer.start()
        self.addCleanup(timer.stop)
        self.plugin = Plugin()
        self.plugin.on_load(self.ctx)
        self.plugin.provider = Mock()
        self.plugin.provider.generate.side_effect = self.generate

    def generate(self, prompt, *, current):
        self.assert_is_generate_prompt(prompt)
        if not current():
            raise ImageGenCancelled('取消')
        return png()

    @staticmethod
    def assert_is_generate_prompt(prompt):
        assert prompt and not prompt.startswith('@')

    def send(self, text='@机器人\u2005画一只戴墨镜的橘猫', **kwargs):
        values = {'text': text, 'chat_id': '123@chatroom', 'sender_id': 'wxid_test_sender', 'is_group': True,
                  'session_id': 'one', 'received_at': self.now,
                  'data': {'is_at_self': True, 'at_members': ('wxid_test_robot',)}}
        values.update(kwargs)
        self.plugin.on_message(Message(**values), self.ctx)

    def test_mention_sends_image_then_removes_temp_file(self):
        self.send()
        self.assertEqual([row[0] for row in self.ctx.sent], ['text', 'image'])
        self.assertEqual(self.plugin.provider.generate.call_args.args[0], '画一只戴墨镜的橘猫')
        self.assertFalse(self.ctx.sent[1][2].exists())
        self.assertEqual(list(self.plugin.directory.iterdir()), [])

    def test_untriggered_chats_do_not_generate(self):
        cases = [{'chat_id': '789@chatroom'}, {'is_group': False}, {'is_self': True},
                 {'data': {'bot_generated': True, 'is_at_self': True}},
                 {'data': {'at_members': ('wxid_test_robot',)}},
                 {'data': {}}, {'received_at': 10.0}, {'type': 3},
                 {'text': '@机器人\u2005今天天气不错'}, {'text': '@机器人\u2005图里是什么'},
                 {'text': '@机器人\u2005'}, {'text': '@机器人\u2005帮我看看这张照片'}]
        for values in cases:
            self.send(**values)
        self.assertEqual(self.plugin.provider.generate.call_count, 0)
        self.assertEqual(self.ctx.sent, [])

    def test_overlong_prompt_gets_usage_without_generating(self):
        self.send(text='@机器人\u2005画' + '猫' * 400)
        self.assertEqual(self.plugin.provider.generate.call_count, 0)
        self.assertEqual([row[0] for row in self.ctx.sent], ['text'])
        self.assertIn('300', self.ctx.sent[0][2])

    def test_requests_phrased_as_wishes_do_generate(self):
        self.send(text='@机器人\u2005想看你睡觉的照片')
        self.now += 21
        self.send(text='@机器人\u2005来张猫的图')
        self.assertEqual(self.plugin.provider.generate.call_count, 2)

    def test_cooldown_is_per_chat_and_uses_arrival_time(self):
        self.send()
        self.now += 5
        self.send()
        self.assertEqual(self.plugin.provider.generate.call_count, 1)
        self.send(chat_id='456@chatroom')
        self.assertEqual(self.plugin.provider.generate.call_count, 2)
        self.now += 21
        self.send()
        self.assertEqual(self.plugin.provider.generate.call_count, 3)

    def test_cancel_during_generation_sends_nothing(self):
        def cancel(prompt, *, current):
            self.ctx.current = False
            return self.generate(prompt, current=current)
        self.plugin.provider.generate.side_effect = cancel
        self.send()
        self.assertEqual([row[0] for row in self.ctx.sent], ['text'])
        self.assertEqual(list(self.plugin.directory.iterdir()), [])

    def test_provider_failure_replies_once_without_leftover_file(self):
        self.plugin.provider.generate.side_effect = ImageGenError('生图接口连接失败或超时')
        self.send()
        self.assertEqual(self.ctx.sent[-1], ('text', '123@chatroom', '生图接口连接失败或超时'))
        self.assertEqual(list(self.plugin.directory.iterdir()), [])

    def test_unexpected_failure_does_not_retry(self):
        self.plugin.provider.generate.side_effect = RuntimeError('boom')
        self.send()
        self.assertEqual(self.ctx.sent[-1], ('text', '123@chatroom', '生成失败，请稍后再试。'))
        self.assertEqual(self.plugin.provider.generate.call_count, 1)

    def test_uncertain_send_keeps_file(self):
        self.ctx.image_result = False
        self.send()
        self.assertTrue(self.ctx.sent[1][2].is_file())
        self.assertEqual(self.plugin.provider.generate.call_count, 1)

    def test_send_timeout_keeps_file_and_does_not_resend(self):
        self.ctx.send_error = TimeoutError('native timeout')
        self.send()
        self.assertEqual(len(self.ctx.sent), 2)
        self.assertTrue(self.ctx.sent[1][2].is_file())

    def test_unload_prevents_more_requests(self):
        self.plugin.on_unload(self.ctx)
        self.send()
        self.assertEqual(self.plugin.provider.generate.call_count, 0)

    def test_invalid_options_do_not_load(self):
        for values in ({'cooldown_seconds': -1}, {'max_prompt_length': 0}, {'acknowledge': 1},
                       {'image_config_file': '../image_config.json'}):
            with self.subTest(values=values):
                self.ctx.config = values
                with self.assertRaises(ValueError):
                    Plugin().on_load(self.ctx)


if __name__ == '__main__':
    unittest.main()
