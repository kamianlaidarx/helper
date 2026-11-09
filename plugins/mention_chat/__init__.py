"""真正艾特机器人后，用本群人格回复当前发言者；刚发过的图片会一起交给视觉模型。"""
import hashlib
import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from wxbot_framework import humanize
from wxbot_framework.dialogue import Dialogue
from wxbot_framework.image_intent import wants_image
from wxbot_framework.images import image_data_url
from wxbot_framework.llm import LLMError
from wxbot_framework.members import MemberError, SnapshotExpired
from wxbot_framework.mentions import strip_at_labels
from wxbot_framework.personality import personality_text
from wxbot_framework.speech import deliver_model_reply

from . import portrait

IMAGE_WINDOW_SECONDS = 300
ROOM_IMAGE_SECONDS = 600
MAX_REMEMBERED = 32
MAX_IMAGES = 3
# 同一条消息被投递两次（桥接重放、两个 worker 之一重复收）时，画像只做一次：
# 评价要花一次模型调用，画图还要花钱，重复一次就是双份。
REPEAT_WINDOW_SECONDS = 15


def _number(value, name, low, high):
    if type(value) not in (int, float) or not low <= value <= high:
        raise ValueError(f"mention_chat.{name} 应为 {low} 到 {high} 之间的数字")
    return int(value)


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
        options = ctx.config
        enabled = options.get("portrait_enabled", True)
        if type(enabled) is not bool:
            raise ValueError("mention_chat.portrait_enabled 必须是布尔值")
        self.portrait_enabled = enabled
        self.portrait_days = _number(options.get("portrait_days", portrait.DAYS), "portrait_days", 1, 365)
        self.portrait_limit = _number(options.get("portrait_limit", portrait.LIMIT), "portrait_limit", 5, 200)
        config_file = options.get("image_config_file", "image_config.json")
        if (not isinstance(config_file, str) or not 0 < len(config_file) <= 120 or
                Path(config_file).name != config_file):
            raise ValueError("mention_chat.image_config_file 应为仓库根目录下的文件名")
        self.image_config_file = config_file
        self.project_root = getattr(ctx, "project_root", None) or Path(__file__).resolve().parents[2]
        self.portrait_directory = self.project_root / ".runtime/portrait" / hashlib.sha256(
            self.members.account.encode("ascii")).hexdigest()[:16]
        self.portraits = {}  # 群 → 当天已画几张，只防账单，不落库。
        self.recent_requests = {}  # 消息指纹 → 处理时间，只防同一条消息被处理两次。
        self.provider = None
        # 生图要十几秒，绝不能占住插件唯一的回调线程。
        self.portrait_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="portrait")

    def on_unload(self, ctx):
        self.portrait_pool.shutdown(wait=False, cancel_futures=True)

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
        targets = [uid for uid in message.data.get("at_members", ())
                   if isinstance(uid, str) and uid and uid != self.members.account]
        if self.portrait_enabled and targets:
            request = portrait.intent(strip_at_labels(message.text))
            if request is not None:
                # 「画像」里也带「画」字，必须排在 image_gen 那条判断前面，否则会被当成画图吞掉。
                if self._repeat(message):
                    ctx.logger.warning("画像请求重复投递，忽略第二条：room=%s", message.chat_id)
                    return
                self.reply_portrait(message, ctx, targets[0], request)
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

    def reply_portrait(self, message, ctx, wxid, request):
        """拿这个人自己的发言写一段评价；明确要画像时再排一张图。"""
        room = message.chat_id
        display = self.members.display_name(wxid, room) or "这位群友"
        try:
            rows = self.members.member_messages(room, wxid, days=self.portrait_days,
                                                limit=self.portrait_limit)
        except MemberError as error:
            ctx.logger.warning("画像取样失败：%s", error)
            return
        lines = portrait.sample(rows)
        if not lines:
            self._portrait_say(message, ctx, f"我还没听到 {display} 在群里说过几句话，等聊起来再问我。")
            return
        personality = ctx.personality(message)
        speaker = ctx.speaker(message)
        system, user = portrait.build_prompt(personality, speaker, display, lines)
        try:
            payload = ctx.llm_for(message, personality=personality,
                                  speaker=speaker).complete_json(system, user)
        except SnapshotExpired:
            return
        except LLMError as error:
            ctx.logger.warning("画像生成失败：%s", error)
            self._portrait_say(message, ctx, "我这会儿有点卡壳，等下手再问我。")
            return
        verdict = portrait.parse_reply(payload)
        bullets = verdict.lines
        if not bullets:
            self._portrait_say(message, ctx, "我看了他这几句话，还想不好怎么说，换个人或者晚点再问？")
            return
        if request.wants_image:
            # 要图的时候只发图：群里再贴一段评价既啰嗦，也把"看画像"变成了念稿。
            self._queue_portrait(message, ctx, bullets, verdict.gender)
        else:
            body = "\n".join([f"{display} 给我的感觉是——", *bullets, portrait.DISCLAIMER])
            self._portrait_say(message, ctx, body)
        ctx.logger.info("画像完成：room=%s lines=%d gender=%s image=%s", room, len(lines),
                        verdict.gender, request.wants_image)

    def _repeat(self, message):
        """同一条消息是不是刚处理过：能拦进程内重复投递，跨进程只能靠单实例运行。"""
        now = time.monotonic()
        key = (message.session_id, message.chat_id, message.sender_id,
               getattr(message, "sequence", 0), message.text.strip()[:40])
        for old, at in list(self.recent_requests.items()):
            if now - at > REPEAT_WINDOW_SECONDS:
                self.recent_requests.pop(old, None)
        if key in self.recent_requests:
            return True
        self.recent_requests[key] = now
        return False

    def _portrait_say(self, message, ctx, text):
        try:
            ctx.reply(message, text)
        except SnapshotExpired:
            pass
        except Exception as error:  # 发送失败不能把插件线程带崩
            ctx.logger.warning("画像消息发送失败（%s）", type(error).__name__)

    def _queue_portrait(self, message, ctx, bullets, gender="不确定"):
        """每人每天最多画 IMAGE_PER_DAY 张：图比文字贵，也比文字难收回。"""
        day = time.strftime("%Y-%m-%d")
        stamp, used = self.portraits.get(message.chat_id, (day, 0))
        used = 0 if stamp != day else used
        if used >= portrait.IMAGE_PER_DAY:
            self._portrait_say(message, ctx, f"今天已经画过 {used} 张画像了，明天再画。")
            return
        self.portraits[message.chat_id] = (day, used + 1)
        prompt = portrait.image_prompt(bullets, gender)
        try:
            self.portrait_pool.submit(self._draw_portrait, message, ctx, prompt)
        except RuntimeError:
            ctx.logger.warning("画像线程池已关闭，这次不画图")

    def _draw_portrait(self, message, ctx, prompt):
        """在工作线程里画图并发送；发送被限流卡住时也不会拖住插件回调。"""
        provider = self._image_provider(ctx)
        if provider is None:
            # 要图的人等着的是一张图：画不出来就说一句，别让他对着空聊天框猜。
            self._portrait_say(message, ctx, "我这边还画不了图，生图服务没配好。")
            return
        try:
            data = provider.generate(prompt)
        except Exception as error:
            ctx.logger.warning("画像生成失败（%s）", type(error).__name__)
            self._portrait_say(message, ctx, "这张没画出来，过会儿再试一次？")
            return
        try:
            self.portrait_directory.mkdir(parents=True, exist_ok=True)
            path = self.portrait_directory / f"{uuid.uuid4().hex}.jpg"
            path.write_bytes(data)
        except OSError as error:
            ctx.logger.warning("画像落盘失败（%s）", type(error).__name__)
            self._portrait_say(message, ctx, "这张没画出来，过会儿再试一次？")
            return
        try:
            delivered = ctx.reply_image(message, path)
        except Exception as error:
            ctx.logger.warning("画像发送失败（%s）", type(error).__name__)
            return
        if delivered:
            path.unlink(missing_ok=True)

    def _image_provider(self, ctx):
        if self.provider is not None:
            return self.provider
        provider = portrait.load_provider(self.project_root / "plugins/image_gen/provider.py",
                                          self.project_root / self.image_config_file)
        if provider is None:
            ctx.logger.warning("画像：读不到生图配置 %s，这次画不了图", self.image_config_file)
            return None
        self.provider = provider
        return provider

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
        for index, bubble in enumerate(bubbles):
            if bubble.delay > 0:
                self.sleep(bubble.delay)
            # 只有第一条艾特人；后面几条像真人接着说，不重复提醒对方。
            deliver_model_reply(ctx, message, bubble.text,
                                display_name=display if index == 0 else None,
                                delivery_guard=delivery_guard)

    def bubbles(self, reply, snapshot):
        """排成要发的气泡；拟人层出任何问题都退回一口气说完，绝不吞掉回复。"""
        try:
            return humanize.plan(reply, is_owner=bool(snapshot.is_owner), is_at_self=True,
                                 rng=getattr(self, "rng", None))
        except Exception:
            return [humanize.Bubble(reply, 0.0)]
