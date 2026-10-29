# 群内艾特生图

在群里真正艾特机器人并附上一段画面描述，插件把描述交给生图模型，生成的图片由绑定账号直接发出。默认每个群 20 秒内只处理一次，群之间分别计时；不处理私聊、自身消息、普通文字（未艾特）和过期的排队消息。

描述为空时提示用法，超过 `max_prompt_length` 时提示缩短。默认先回一句“收到，正在生成图片…”，生成成功后发送图片本身；发送前取消或插件停用会丢弃结果并清理临时文件。

## 什么样的艾特会生图

不是每条艾特都生图：`wxbot_framework/image_intent.py` 用纯规则判断这句话是不是在要图，命中才处理，其余艾特留给 `mention_chat` 聊天或看图，两个插件不会同时回话。

- 以这些词开头：`画`、`画图`、`画张`、`画个`、`画只`、`画幅`、`生图`、`生成图`、`出图`、`draw`、`imagine`、`paint`
- 或者说法像在要图：「帮我画张夕阳」「想看你睡觉的照片」「来张猫的图」「生成一张赛博朋克街道」
- 识图类提问一律不算：「图里是什么」「这张照片里是谁」「这是什么图」，这些走 `mention_chat` 的视觉回复

规则是纯正则，不消耗模型调用；要改说法改 `image_intent.py` 里的 `TRIGGERS` 和 `REQUESTS` 两处常量，`mention_chat` 用同一份规则判断让位，两边不会打架。


## 配置

两项配置分开存放：开关和节奏在 `bot_config.json` 的 `plugins.image_gen`，密钥和模型在仓库根目录的 `image_config.json`。

```json
{
  "enabled": true,
  "image_config_file": "image_config.json",
  "cooldown_seconds": 20,
  "max_prompt_length": 300,
  "acknowledge": true
}
```

`image_config.json` 从 `image_config.example.json` 复制，只放本地，不提交：

```json
{
  "provider": "openai",
  "api_key": "sk-...",
  "base_url": "https://api.openai.com/v1",
  "model": "gpt-image-1",
  "size": "1024x1024",
  "timeout_seconds": 90
}
```

## 三种接口形态

| `provider` | 请求 | 图片位置 |
| --- | --- | --- |
| `openai`（默认） | `POST {base_url}/images/generations` | `data[0].b64_json` 或 `data[0].url` |
| `dashscope` | 阿里云百炼异步任务，轮询 `/api/v1/tasks/{id}` | `output.results[0].url` |
| `openai-chat` | `POST {base_url}/chat/completions` | `choices[0].message.images[0].image_url.url` |

`openai-chat` 用于只把出图能力挂在对话接口上的网关和模型（例如 Gemini 的 `*-image` 系列）：请求体只有 `model` 和 `messages`，不发送 `size`、`response_format`。图片可以是 `data:image/...;base64,` 内联数据，也可以是 HTTPS 地址。

配置里可选 `response_format`（`b64_json` 或 `url`）；不填则不发送该字段，兼容不接受它的模型（如 `gpt-image-1`）。`base_url` 必须是 HTTPS，只有本机回环地址允许 HTTP；下载 CDN 图片时不携带密钥，且只接受 HTTPS 的公网地址。

## 排错

接口返回非 2xx 时，回复里会带上服务端给的原因（截断到 120 字，且不含密钥），例如"HTTP 503 …（服务端：no auth available (providers=codex, model=gpt-image-2.5)）"——这通常表示网关没有为该模型开通上游账号，而不是密钥写错，换一个能出图的模型名即可。

连接在握手阶段被重置会自动重试两次；已经开始读取响应后不再重试，避免重复计费。

## 图片处理与缓存

生成结果会校验格式、像素数和大小（单张不超过 20 MiB），移除原始元数据并转成普通 JPEG。临时文件放在 `.runtime/image_gen/` 的账号散列目录，发送成功后立即删除；原生发送结果未确认时保留该次文件，不自动重发。该目录沿用 `.runtime/` 的忽略规则，不应提交到公开仓库。
