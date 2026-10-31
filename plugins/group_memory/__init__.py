"""旁听消息，攒够一批就抽出事实写进长期记忆；群聊和私聊各记各的。

只读不写聊天：不发送任何消息，也不拦别的插件。所有失败都吞掉，
记忆是加分项，坏了也不能影响机器人正常回话。

这里是记忆状态的唯一写入者：面具共振和情绪都在内存里逐条累加，
攒批或超时才落盘一次。别的模块只读 memory.load()，不写。
"""

from __future__ import annotations

import logging
import threading
import time

from wxbot_framework import mask, memory, memory_extract

LOGGER = logging.getLogger("group_memory")

BATCH_SIZE = 30
MAX_BUFFER = 400
FLUSH_SECONDS = 600
MIN_TEXT = 2
MAX_TEXT = 500


class Plugin:
    def on_load(self, ctx):
        self.llm = getattr(ctx, "llm", None)
        self.buffers = {}
        self.states = {}
        self.last_flush = {}
        self.busy = set()
        self.pending = set()
        self.lock = threading.Lock()

    def on_message(self, message, ctx):
        if self.llm is None:
            return
        if message.is_self or message.type != 1 or (message.data or {}).get("bot_generated"):
            return
        if message.is_group and message.chat_id not in getattr(ctx.members, "groups", {}):
            return
        text = (message.text or "").strip()
        if len(text) < MIN_TEXT or len(text) > MAX_TEXT or text.startswith("/"):
            return
        state = self._state(message.chat_id)
        mask.update(state, text)
        buffer = self.buffers.setdefault(message.chat_id, [])
        buffer.append({"speaker": self._name(message, ctx), "sender_id": message.sender_id, "text": text})
        if len(buffer) > MAX_BUFFER:
            del buffer[: len(buffer) - MAX_BUFFER]
        if len(buffer) >= BATCH_SIZE or self._flush_due(message.chat_id):
            self._kick(message.chat_id)

    def _state(self, chat_id):
        """同一份 state 只从磁盘读一次；之后内存里这份就是权威副本。"""
        with self.lock:
            state = self.states.get(chat_id)
            if state is None:
                state = memory.load(chat_id)
                self.states[chat_id] = state
            return state

    def _flush_due(self, chat_id):
        """不到 30 条也要定期落盘，否则面具共振和情绪会一直停在内存里。"""
        stamp = self.last_flush.get(chat_id)
        return stamp is None or time.monotonic() - stamp >= FLUSH_SECONDS

    def _name(self, message, ctx):
        try:
            return ctx.members.display_name(message.sender_id, message.chat_id) or message.sender_id
        except Exception:
            return message.sender_id or "?"

    def _kick(self, chat_id):
        with self.lock:
            if chat_id in self.busy:
                self.pending.add(chat_id)
                return
            rows = self.buffers.pop(chat_id, [])
            self.busy.add(chat_id)
        threading.Thread(target=self._run, args=(chat_id, rows), daemon=True).start()

    def _run(self, chat_id, rows):
        try:
            state = self._state(chat_id)
            for row in rows:
                memory.bump_affinity(state, row.get("sender_id") or "", row.get("speaker") or "")
                memory.update_emotion(state, row.get("text") or "")
            memory.apply_decay(state)
            memory.apply_affinity_decay(state)
            mask.decay(state)
            facts = memory_extract.extract(self.llm, rows) if rows else []
            if facts:
                memory.merge_facts(state, facts)
            memory.save(chat_id, state)
            self.last_flush[chat_id] = time.monotonic()
        except Exception as error:
            LOGGER.warning("记忆整理失败 %s: %s", chat_id, error)
        finally:
            with self.lock:
                self.busy.discard(chat_id)
                again = self.pending.discard(chat_id) and self.buffers.get(chat_id)
            if again:
                self._kick(chat_id)
