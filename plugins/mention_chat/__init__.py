"""真正艾特机器人后，用本群人格回复当前发言者；刚发过的图片会一起交给视觉模型。"""
import random
import time

from wxbot_framework import humanize
from wxbot_framework.dialogue import Dialogue
from wxbot_framework.image_intent import wants_image
from wxbot_framework.images import image_data_url
from wxbot_framework.llm import LLMError
from wxbot_framework.members import SnapshotExpired
from wxbot_framework.mentions import strip_at_labels
from wxbot_framework.personality import personality_text
from wxbot_framework.speech import deliver_model_reply

IMAGE_WINDOW_SECONDS = 300
ROOM_IMAGE_SECONDS = 600
MAX_REMEMBERED = 32
MAX_IMAGES = 3


class Plugin:
    def on_load(self, ctx):
        self.members = ctx.members
        if not self.members.history_enabled:
            raise ValueError("艾特上下文对话需要 history_enabled: true")
        ctx.llm  # 启动时核对配置，不请求接口。
        self.images = {}
        self.latest = {}
        self.pending_images = None
        # 拟人层要随机和等待；测试里这两个都能换掉，免得跑一次测试等好几秒。
        self.sleep = time.sleep
        self.rng = random.Random()

    def on_message(self, message, ctx):
        if message.is_self or not message.is_group or message.chat_id not in self.members.groups:
            return
        if message.type == 3:
            self.remember(message)
            return
        if (message.type != 1 or "WXBOT_AT_CHAT_PROBE" in message.text or
                message.data.get("bot_generated") or not message.data.get("is_at_self") or
                self.members.account not in message.data.get("at_members", ())):
            return
        if ctx.plugin_is_active("image_gen") and wants_image(strip_at_labels(message.text)):
            # 这条艾特是要画图，交给 image_gen 插件，避免文本回复和图片同时出现。
            return
        personality = ctx.personality(message)
        try:
            snapshot = self.members.conversation_snapshot(message)
            guard = lambda: ctx.response_guard(snapshot, personality)
            self.pending_images = self.recent_images(message)
            try:
                self.reply_to_member(message, ctx, snapshot, personality, guard)
            finally:
                attached, self.pending_images = bool(self.pending_images), None
            ctx.logger.info("艾特对话完成：room=%s persona=%s context=%d image=%s", message.chat_id,
                            personality.name, len(snapshot.messages), attached)
        except SnapshotExpired:
            ctx.logger.info("艾特对话已取消：记录或人格状态已变更")
        except LLMError as error:
            ctx.logger.warning("艾特对话失败：%s", error)
            try:
                ctx.reply(message, "暂时无法回复，请稍后再艾特我。", delivery_guard=guard)
            except SnapshotExpired:
                pass

    def remember(self, message):
        """记住最近几张可用的图，只留路径和本地到达时间，不落库。"""
        if message.data.get("bot_generated") or message.data.get("image_status") not in (None, "ready"):
            return
        path = message.image_path
        if not path:
            return
        stamp = time.monotonic()
        self.images.setdefault((message.chat_id, message.sender_id), []).append((path, stamp))
        self.latest.setdefault(message.chat_id, []).append((path, stamp))
        for store, window in ((self.images, IMAGE_WINDOW_SECONDS), (self.latest, ROOM_IMAGE_SECONDS)):
            for key in list(store):
                rows = [row for row in store[key] if 0 <= stamp - row[1] <= window]
                if rows:
                    store[key] = rows[-MAX_IMAGES:]
                else:
                    store.pop(key, None)
        while len(self.images) > MAX_REMEMBERED:
            self.images.pop(next(iter(self.images)))
        while len(self.latest) > MAX_REMEMBERED:
            self.latest.pop(next(iter(self.latest)))

    def recent_images(self, message):
        """同一个人刚发的图优先；其次是本群 10 分钟内的最后几张图，最多 MAX_IMAGES 张。"""
        now = time.monotonic()
        candidates = (((message.chat_id, message.sender_id), self.images, IMAGE_WINDOW_SECONDS),
                      (message.chat_id, self.latest, ROOM_IMAGE_SECONDS))
        for key, store, window in candidates:
            rows = [row for row in store.get(key) or [] if 0 <= now - row[1] <= window]
            if not rows:
                store.pop(key, None)
                continue
            store[key] = rows
            urls = []
            for path, _stamp in rows[-MAX_IMAGES:]:
                try:
                    urls.append(image_data_url(path))
                except ValueError:
                    continue
            if urls:
                return urls
            store.pop(key, None)
        return None

    def reply_to_member(self, message, ctx, snapshot, personality, delivery_guard):
        """上下文已准备好，在这里生成并发送给群友的回复。"""
        reply = personality_text(Dialogue(ctx.llm_for(
            message, personality=personality, speaker=ctx.speaker(message))).reply(
            snapshot, image=self.pending_images), personality.name)
        display = self.members.display_name(message.sender_id, message.chat_id) or snapshot.speaker_name
        bubbles = self.bubbles(reply, snapshot)
        if not bubbles:
            ctx.logger.info("拟人层决定这次不回：room=%s", message.chat_id)
            return
        for bubble in bubbles:
            if bubble.delay > 0:
                self.sleep(bubble.delay)
            deliver_model_reply(ctx, message, bubble.text, display_name=display,
                                delivery_guard=delivery_guard)

    def bubbles(self, reply, snapshot):
        """排成要发的气泡；拟人层出任何问题都退回一口气说完，绝不吞掉回复。"""
        try:
            return humanize.plan(reply, is_owner=bool(snapshot.is_owner), is_at_self=True,
                                 rng=getattr(self, "rng", None))
        except Exception:
            return [humanize.Bubble(reply, 0.0)]
