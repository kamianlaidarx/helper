"""旁听群消息，攒够一批就抽出事实写进长期记忆。

只读不写群：不发送任何消息，也不拦别的插件。所有失败都吞掉，
记忆是加分项，坏了也不能影响机器人正常回话。
"""

from __future__ import annotations

import logging
import threading

from wxbot_framework import memory, memory_extract

LOGGER = logging.getLogger("group_memory")

BATCH_SIZE = 30
MAX_BUFFER = 400
MIN_TEXT = 2
MAX_TEXT = 500


class Plugin:
    def on_load(self, ctx):
        self.llm = getattr(ctx, "llm", None)
        self.buffers = {}
        self.busy = set()
        self.pending = set()

    def on_message(self, message, ctx):
        if self.llm is None:
            return
        if message.is_self or not message.is_group:
            return
        if message.type != 1 or (message.data or {}).get("bot_generated"):
            return
        if message.chat_id not in getattr(ctx.members, "groups", {}):
            return
        text = (message.text or "").strip()
        if len(text) < MIN_TEXT or len(text) > MAX_TEXT or text.startswith("/"):
            return
        buffer = self.buffers.setdefault(message.chat_id, [])
        buffer.append({"speaker": self._name(message, ctx), "sender_id": message.sender_id, "text": text})
        if len(buffer) > MAX_BUFFER:
            del buffer[: len(buffer) - MAX_BUFFER]
        if len(buffer) >= BATCH_SIZE:
            self._kick(message.chat_id)

    def _name(self, message, ctx):
        try:
            return ctx.members.display_name(message.sender_id, message.chat_id) or message.sender_id
        except Exception:
            return message.sender_id or "?"

    def _kick(self, chat_id):
        if chat_id in self.busy:
            self.pending.add(chat_id)
            return
        rows = self.buffers.pop(chat_id, [])
        if not rows:
            return
        self.busy.add(chat_id)
        threading.Thread(target=self._run, args=(chat_id, rows), daemon=True).start()

    def _run(self, chat_id, rows):
        try:
            state = memory.load(chat_id)
            for row in rows:
                memory.bump_affinity(state, row.get("sender_id") or "", row.get("speaker") or "")
                memory.update_emotion(state, row.get("text") or "")
            memory.apply_decay(state)
            memory.apply_affinity_decay(state)
            facts = memory_extract.extract(self.llm, rows)
            if facts:
                memory.merge_facts(state, facts)
            memory.save(chat_id, state)
        except Exception as error:
            LOGGER.warning("记忆整理失败 %s: %s", chat_id, error)
        finally:
            self.busy.discard(chat_id)
            if self.pending.discard(chat_id) and self.buffers.get(chat_id):
                self._kick(chat_id)
