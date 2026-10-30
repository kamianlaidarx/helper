"""群聊生图插件：群里真实艾特机器人并附描述，调用生图模型并把图片发回原群。"""
import hashlib
import math
from contextlib import contextmanager
from pathlib import Path
import threading
import time
import uuid

from wxbot_framework.image_intent import wants_image
from wxbot_framework.mentions import strip_at_labels

from .provider import ImageGenCancelled, ImageGenError, ImageProvider

STALE_SECONDS = 60


def _number(value, name, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'image_gen.{name} 应为 {low:g}–{high:g} 之间的数值')
    return value


class Plugin:
    def on_load(self, ctx):
        options = ctx.config
        config_file = options.get('image_config_file', 'image_config.json')
        if (not isinstance(config_file, str) or not 0 < len(config_file) <= 120 or
                Path(config_file).name != config_file):
            raise ValueError('image_gen.image_config_file 应为仓库根目录下的文件名')
        self.config_file = config_file
        self.cooldown = _number(options.get('cooldown_seconds', 20), 'cooldown_seconds', 0, 300)
        self.max_prompt = _number(options.get('max_prompt_length', 300), 'max_prompt_length', 1, 800)
        if type(self.max_prompt) is not int:
            raise ValueError('image_gen.max_prompt_length 应为整数')
        acknowledge = options.get('acknowledge', True)
        if type(acknowledge) is not bool:
            raise ValueError('image_gen.acknowledge 必须是布尔值')
        self.acknowledge = acknowledge
        self.members = ctx.members
        root = getattr(ctx, 'project_root', None) or Path(__file__).resolve().parents[2]
        self.directory = root / '.runtime/image_gen' / hashlib.sha256(
            self.members.account.encode('ascii')).hexdigest()[:16]
        self.directory.mkdir(parents=True, exist_ok=True)
        self.provider = ImageProvider.from_file(root / config_file)
        self.stopped = threading.Event()
        self.last_request = {}
        self.session_id = None

    def on_unload(self, ctx):
        self.stopped.set()
        self.last_request.clear()

    def _current(self, message, ctx):
        service = getattr(ctx, '_service', None)
        return (not self.stopped.is_set() and ctx.plugin_is_active('image_gen') and
                message.chat_id in self.members.groups and
                (service is None or service.session_is_current(message.session_id)))

    @contextmanager
    def _guard(self, message, ctx):
        if not self._current(message, ctx):
            raise ImageGenCancelled('生图回复已取消')
        yield

    def _say(self, message, ctx, text):
        if self._current(message, ctx):
            try:
                ctx.reply(message, text, delivery_guard=lambda: self._guard(message, ctx))
            except (ImageGenCancelled, RuntimeError):
                pass

    def on_message(self, message, ctx):
        if (message.type != 1 or not message.is_group or message.is_self or
                message.data.get('bot_generated') or not message.data.get('is_at_self') or
                not self._current(message, ctx)):
            return
        prompt = strip_at_labels(message.text)
        if not wants_image(prompt):
            # 普通聊天或看图提问交给 mention_chat，两个插件不抢同一条艾特。
            return
        if len(prompt) > self.max_prompt:
            self._say(message, ctx, f'描述请控制在 {self.max_prompt} 字以内。')
            return
        if self.session_id != message.session_id:
            self.last_request.clear()
            self.session_id = message.session_id
        now = time.monotonic()
        received = message.received_at
        if (type(received) not in (int, float) or not math.isfinite(received) or
                not 0 <= now - received <= STALE_SECONDS):
            return
        previous = self.last_request.get(message.chat_id)
        if previous is not None and received - previous < self.cooldown:
            return
        self.last_request[message.chat_id] = received
        if self.acknowledge:
            self._say(message, ctx, '收到，正在生成图片…')
        current = lambda: self._current(message, ctx)
        try:
            data = self.provider.generate(prompt, current=current)
        except ImageGenCancelled:
            return
        except ImageGenError as error:
            ctx.logger.warning('生图失败：%s', error)
            self._say(message, ctx, str(error))
            return
        except Exception as error:
            ctx.logger.warning('生图失败（%s），不自动重试', type(error).__name__)
            self._say(message, ctx, '生成失败，请稍后再试。')
            return
        if not self._current(message, ctx):
            return
        path = self.directory / f'{uuid.uuid4().hex}.jpg'
        try:
            path.write_bytes(data)
        except OSError as error:
            ctx.logger.warning('生图结果落盘失败（%s）', type(error).__name__)
            self._say(message, ctx, '生成失败，请稍后再试。')
            return
        try:
            delivered = ctx.reply_image(message, path, delivery_guard=lambda: self._guard(message, ctx))
        except ImageGenCancelled:
            path.unlink(missing_ok=True)
            return
        except Exception as error:
            ctx.logger.warning('生图图片发送未确认（%s），保留缓存，不自动重发', type(error).__name__)
            return
        if delivered:
            path.unlink(missing_ok=True)
        else:
            ctx.logger.warning('生图图片发送结果未确认，保留单次缓存，不自动重发')
