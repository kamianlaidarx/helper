"""真正艾特机器人后，用本群人格回复当前发言者；刚发过的图片会一起交给视觉模型。"""
import time

from wxbot_framework.dialogue import Dialogue
from wxbot_framework.images import image_data_url
from wxbot_framework.llm import LLMError
from wxbot_framework.members import SnapshotExpired
from wxbot_framework.personality import personality_text
from wxbot_framework.speech import deliver_model_reply

IMAGE_WINDOW_SECONDS = 300
ROOM_IMAGE_SECONDS = 60
MAX_REMEMBERED = 32


class Plugin:
    def on_load(self, ctx):
        self.members = ctx.members
        if not self.members.history_enabled:
            raise ValueError("艾特上下文对话需要 history_enabled: true")
        ctx.llm  # 启动时核对配置，不请求接口。
        self.images = {}
        self.latest = {}
        self.pending_image = None

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
        personality = ctx.personality(message)
        try:
            snapshot = self.members.conversation_snapshot(message)
            guard = lambda: ctx.response_guard(snapshot, personality)
            self.pending_image = self.recent_image(message)
            try:
                self.reply_to_member(message, ctx, snapshot, personality, guard)
            finally:
                attached, self.pending_image = bool(self.pending_image), None
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
        """记住上一张可用的图，只留路径和本地到达时间，不落库。"""
        if message.data.get("bot_generated") or message.data.get("image_status") not in (None, "ready"):
            return
        path = message.image_path
        if not path:
            return
        stamp = time.monotonic()
        self.images[(message.chat_id, message.sender_id)] = (path, stamp)
        self.latest[message.chat_id] = (path, stamp)
        while len(self.images) > MAX_REMEMBERED:
            self.images.pop(next(iter(self.images)))
        while len(self.latest) > MAX_REMEMBERED:
            self.latest.pop(next(iter(self.latest)))

    def recent_image(self, message):
        """同一个人刚发的图优先；其次是本群 60 秒内的最后一张图。"""
        now = time.monotonic()
        candidates = (((message.chat_id, message.sender_id), self.images, IMAGE_WINDOW_SECONDS),
                      (message.chat_id, self.latest, ROOM_IMAGE_SECONDS))
        for key, store, window in candidates:
            row = store.get(key)
            if row is None:
                continue
            path, stamp = row
            if not 0 <= now - stamp <= window:
                store.pop(key, None)
                continue
            try:
                return image_data_url(path)
            except ValueError:
                store.pop(key, None)
        return None

    def reply_to_member(self, message, ctx, snapshot, personality, delivery_guard):
        """上下文已准备好，在这里生成并发送给群友的回复。"""
        reply = personality_text(Dialogue(ctx.llm_for(message, personality=personality)).reply(
            snapshot, image=self.pending_image), personality.name)
        display = self.members.display_name(message.sender_id, message.chat_id) or snapshot.speaker_name
        deliver_model_reply(ctx, message, reply, display_name=display, delivery_guard=delivery_guard)
