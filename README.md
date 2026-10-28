# TokenPlan 语音合成插件（AstrBot）

接入阿里云百炼 **Token Plan** 套餐的 Qwen TTS，让 Bot 用角色音色发出 QQ 语音。

用户说一句「能不能发段语音给我」，Bot 会结合**当前人格（角色模板）**和**上下文对话**，自己写好台词，再合成语音发出来。

## 三种触发方式

| 方式 | 配置项 | 说明 |
| --- | --- | --- |
| **LLM 工具**（推荐） | `enable_tool` | AI 自主判断何时该说话，自己写台词，最贴合人设 |
| **命令** | 始终可用 | `/语音 要念的内容`、`/语音测试` |
| **自动语音** | `auto_voice` | 每条回复都额外合成一条语音，比较费额度 |

## 安装

把整个 `astrbot_plugin_tokenplan_tts` 目录放到 `AstrBot/data/plugins/` 下，在 WebUI 插件管理中重载/启用即可。依赖 `aiohttp`（AstrBot 自带）。

## 配置

必填只有一项：**API Key**（`sk-sp-` 开头，在阿里云百炼控制台的 Token Plan 套餐页获取）。

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `api_key` | — | Token Plan 套餐密钥 |
| `api_base` | `https://token-plan.cn-beijing.maas.aliyuncs.com` | 海外站用 `https://token-plan.ap-southeast-1.maas.aliyuncs.com` |
| `model` | `qwen-audio-3.0-tts-plus` | 也可 `qwen-audio-3.0-tts-flash` / `qwen-audio-3.1-tts-flash` / `qwen3-tts-flash` |
| `voice` | `longanhuan_v3.6` | 音色，**必须与模型版本匹配** |
| `audio_format` | `wav` | 推荐 wav，QQ 语音兼容性最好且无需 ffmpeg 转码 |
| `sample_rate` | `24000` | 16000 / 24000 |
| `instruction` | 空 | 情感风格指令，如「温柔撒娇」；`qwen3-tts-flash` 不支持 |
| `enable_tool` | `true` | 是否注册 LLM 工具 |
| `inject_prompt` | `true` | 是否每轮向 LLM 注入台词写作要求 |
| `style_hint` | 空 | 追加的角色说话风格，如「全程古风，自称本座」 |
| `voice_probability` | `35` | 想发语音时真正发出的概率。35 约三发一，100 想发就发，0 等于关闭 |
| `voice_cooldown_turns` | `2` | 发过语音后强制文字几轮，防止连续轰炸 |
| `auto_voice` / `auto_keep_text` | `false` / `true` | 自动语音模式及其是否保留原文（同样受概率和冷却约束） |
| `max_chars` | `300` | 单次合成字数上限 |
| `cooldown` | `3` | 同会话冷却秒数，防刷屏 |

### 语音不要每句都发

默认配置下语音是**稀缺的**，逻辑是两层：

1. AI 想发语音时，先看**上一轮发过没有**——发过就接下来两轮强制文字；
2. 冷却过了也不一定发，还要按 `voice_probability`（默认 35%）抽一次。

所以正常体验是「大部分时候打字，偶尔突然来一句语音」。这比每句都念更像真人，也省额度。

想让它更爱说话就调高概率；想几乎只听文字就调到 10 以下。**调到 0 等于彻底关闭语音，但命令 `/语音` 仍然可用。**

### 常用音色

- `longanhuan_v3.6` 龙安欢（女，默认）
- `longanlingxin` / `longanlingxin_v3.1` 龙安灵心（女）
- `longanfengyue_v3.1` 龙安风悦（女）
- `xunanchuan_v3.1` 许南川（男）
- `yuxiaoyun_v3.1` 于小云（女，元气亲切）
- `qiaoxiaojiao_v3.1` 乔小娇（女，俏丽可爱）
- `anmingyuan_v3.1` 安明远（男，清亮自然）

完整列表见阿里云百炼《Qwen-Audio-TTS 音色列表》文档；多语种/方言音色见该文档的「多语种与方言音色」章节。

## 重要：开启工具后还要勾选

`enable_tool` 打开后，需要到 AstrBot **人格管理 → 编辑人格 → 工具（Function Tools）** 里勾选 `send_voice`。没勾选 AI 就"看不见"这个能力。

工具描述里已经写死了要求：台词必须用当前人设的口吻、结合上下文、只写能念出来的口语、控制在 120 字内、不带 Markdown 和 emoji。`inject_prompt` 会在每轮请求里再提醒一次（走临时 user content，不进历史、不破坏 prompt 缓存）。

## 工作原理

1. 用户说话 → AstrBot 带上人格 + 上下文请求 LLM；
2. LLM 决定调用 `send_voice(text, emotion)`，其中 `text` 就是它按人设写好的台词；
3. 插件清洗台词（去 Markdown / 链接 / emoji）→ POST 到 Token Plan 同步合成接口；
4. 拿到音频二进制落盘 → `Record` 组件经 aiocqhttp 转成 base64 发给协议端（NapCat/Lagrange 负责转成 QQ 语音）；
5. 插件把成功/失败结果回报给 LLM，AI 继续对话，不会把台词再重复打一遍字。

## 常见问题

- **完全没反应**：先跑 `/语音测试`。若报 401/403 检查 Key 前缀是否为 `sk-sp-`；报 404 检查 `api_base` 地域。
- **AI 不会主动发语音**：确认人格里的 `send_voice` 已勾选，且该人格启用了工具调用。
- **语音发出但 QQ 里显示"语音解析失败"**：把 `audio_format` 改成 `wav`、`sample_rate` 改成 `16000` 试试；也可给协议端装好 ffmpeg。
- **音色报错**：模型与音色版本要匹配，例如 `qwen-audio-3.0-tts-plus` 用 `longanlingxin`，`*_v3.1` 音色配 `qwen-audio-3.1-tts-flash`。
- **额度**：自动语音模式每条回复都消耗额度，建议只在私聊或指定群开启。

## 文件结构

```
astrbot_plugin_tokenplan_tts/
├── main.py             # 插件入口：LLM 工具、提示注入、自动语音、命令
├── tts_client.py       # Token Plan TTS HTTP 客户端（含音频格式嗅探与响应容错）
├── _conf_schema.json   # 配置面板
├── metadata.yaml
├── requirements.txt
└── README.md
```

音频缓存在 `data/plugin_data/astrbot_plugin_tokenplan_tts/audio/`，每 5 分钟自动清理超过 24 小时或超出数量上限的文件。
