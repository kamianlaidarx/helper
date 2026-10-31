# 当前群人格

发送 `/切换人格 如烟` 或 `/切换人格 取消`。可选人格名由 `wxbot_framework/personality.py` 的 `PROMPTS` 决定，`/help` 和用法提示会自动跟随，不要在文案里写死名字。

人格是一层表达风格，作用于当前群的模型对话、个人总结、全群总结和接龙判定。事实和接龙规则仍按任务要求处理。每个群单独保存，重启和清理聊天文本不会重置人格。

“如烟”由 `PROMPTS["如烟"]` 的长设定加 `FEW_SHOTS["如烟"]` 的语感示例拼成；发送前由 `personality_text` 去掉括号旁白和 Markdown 排版符号，正文为空时抛 `LLMError` 让调用方重试。取消后恢复普通任务提示。

提示词位于 `wxbot_framework/personality.py`。新增人格时在 `PROMPTS` 加键，并在 `switch_ack` 补一条对应确认语（`switch_ack` 按名字直接索引，漏补会抛 `KeyError`）。
