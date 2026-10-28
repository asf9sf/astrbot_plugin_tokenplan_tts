"""AstrBot 插件：调用阿里云百炼 Token Plan 的 Qwen TTS，让 Bot 发出 QQ 语音。

三种触发方式：
1. LLM 工具 send_voice（推荐）—— AI 自己结合当前人格与上下文写台词，再合成语音；
2. 命令 /语音 <文本> —— 手动合成；
3. 自动语音模式 —— 每条回复都额外合成一条语音。
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools

from .tts_client import TTSError, TokenPlanTTSClient

# 修改这里可以更换 LLM 工具名（改完需在 AstrBot 人格设置里重新勾选，并重载插件）
TTS_TOOL_NAME = "send_voice"

# ---------- 朗读文本清洗 ----------
_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+|www\.\S+")
# 斜体 / 删除线：成对出现时才剥离，避免误伤 send_voice 这类单词内下划线
_ITALIC = re.compile(r"(?<!\w)_{1,2}([^_\n]+)_{1,2}(?!\w)")
_STRIKE = re.compile(r"~{1,2}([^~\n]+)~{1,2}")
# 剩下的孤立 Markdown 标记
_MD_MARK = re.compile(r"[*#>|\\]")
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF"
    "\u2190-\u21FF\uFE00-\uFE0F\u200d\u20e3]",
)
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_MULTI_NL = re.compile(r"\n{3,}")

# 工具已经主动发过语音的标记，避免自动模式重复合成
_EXTRA_KEY_SENT = "tokenplan_tts_sent"


def clean_for_speech(text: str) -> str:
    """把 Markdown / 链接 / emoji 等不适合朗读的内容清理掉。"""
    if not text:
        return ""
    s = _CODE_BLOCK.sub("", text)
    s = _INLINE_CODE.sub(r"\1", s)
    s = _MD_LINK.sub(r"\1", s)
    s = _URL.sub("", s)
    s = _EMOJI.sub("", s)
    s = _ITALIC.sub(r"\1", s)
    s = _STRIKE.sub(r"\1", s)
    s = _MD_MARK.sub("", s)
    s = s.replace("\t", " ")
    s = _MULTI_SPACE.sub(" ", s)
    s = _MULTI_NL.sub("\n\n", s)
    return s.strip()


class TokenPlanTTSPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}

        def cfg(key: str, default):
            value = self.config.get(key, default)
            return default if value is None else value

        self.api_key: str = cfg("api_key", "")
        self.enable_tool: bool = bool(cfg("enable_tool", True))
        self.inject_prompt: bool = bool(cfg("inject_prompt", True))
        self.style_hint: str = str(cfg("style_hint", "") or "").strip()
        self.auto_voice: bool = bool(cfg("auto_voice", False))
        self.auto_keep_text: bool = bool(cfg("auto_keep_text", True))
        self.auto_min_chars: int = int(cfg("auto_min_chars", 4) or 4)
        self.auto_max_chars: int = int(cfg("auto_max_chars", 150) or 150)
        self.max_chars: int = int(cfg("max_chars", 300) or 300)
        self.clean_text: bool = bool(cfg("clean_text", True))
        self.cooldown: int = int(cfg("cooldown", 3) or 0)
        self.max_cache_files: int = int(cfg("max_cache_files", 300) or 300)

        self.client = TokenPlanTTSClient(
            api_key=self.api_key,
            api_base=str(cfg("api_base", "https://token-plan.cn-beijing.maas.aliyuncs.com")),
            model=str(cfg("model", "qwen-audio-3.0-tts-plus")),
            voice=str(cfg("voice", "longanhuan_v3.6")),
            audio_format=str(cfg("audio_format", "wav")),
            sample_rate=int(cfg("sample_rate", 24000) or 24000),
            instruction=str(cfg("instruction", "") or ""),
            timeout=int(cfg("timeout", 60) or 60),
        )

        try:
            self.audio_dir: Path = Path(StarTools.get_data_dir()) / "audio"
        except Exception:  # noqa: BLE001
            self.audio_dir = Path("data/plugin_data/astrbot_plugin_tokenplan_tts/audio")
        self.audio_dir.mkdir(parents=True, exist_ok=True)

        self._last_call: dict[str, float] = {}
        self._cleanup_task: asyncio.Task | None = None

        logger.info(
            f"[TokenPlanTTS] 已加载：模型={self.client.model} 音色={self.client.voice} "
            f"格式={self.client.audio_format} 工具={'开' if self.enable_tool else '关'} "
            f"自动={'开' if self.auto_voice else '关'}",
        )

    # ---------------- 生命周期 ----------------

    async def initialize(self) -> None:
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def terminate(self) -> None:
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
        await self.client.close()

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(300)
            try:
                self._cleanup_cache()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[TokenPlanTTS] 清理缓存失败: {exc}")

    def _cleanup_cache(self) -> None:
        """清理超量 / 过期（>24h）的音频缓存。"""
        try:
            files = [f for f in self.audio_dir.iterdir() if f.is_file()]
        except OSError:
            return
        now = time.time()
        expired = [f for f in files if now - f.stat().st_mtime > 86400]
        for f in expired:
            try:
                f.unlink()
            except OSError:
                pass
        remain = [f for f in self.audio_dir.iterdir() if f.is_file()]
        if len(remain) > self.max_cache_files:
            remain.sort(key=lambda p: p.stat().st_mtime)
            for f in remain[: len(remain) - self.max_cache_files]:
                try:
                    f.unlink()
                except OSError:
                    pass

    # ---------------- 通用逻辑 ----------------

    def _session_id(self, event: AstrMessageEvent) -> str:
        try:
            return event.unified_msg_origin or event.get_session_id()
        except Exception:  # noqa: BLE001
            return event.get_session_id()

    def _in_cooldown(self, event: AstrMessageEvent) -> bool:
        if self.cooldown <= 0:
            return False
        sid = self._session_id(event)
        last = self._last_call.get(sid, 0.0)
        return time.time() - last < self.cooldown

    def _mark_call(self, event: AstrMessageEvent) -> None:
        self._last_call[self._session_id(event)] = time.time()

    def _mark_sent(self, event: AstrMessageEvent) -> None:
        """标记本轮已由工具发过语音，供自动模式跳过。"""
        try:
            event.set_extra(_EXTRA_KEY_SENT, True)
        except Exception:  # noqa: BLE001
            pass

    def _already_sent(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.get_extra(_EXTRA_KEY_SENT))
        except Exception:  # noqa: BLE001
            return False

    def _prepare(self, text: str) -> str:
        return clean_for_speech(text) if self.clean_text else (text or "").strip()

    # ---------------- 1. LLM 工具 ----------------

    @filter.llm_tool(TTS_TOOL_NAME)
    async def send_voice(
        self,
        event: AstrMessageEvent,
        text: str,
        emotion: str = "",
    ):
        """把一句话合成语音并作为 QQ 语音消息发送给用户。

        当用户要求「发语音」「说给我听」「语音回复」「能不能听到你的声音」，或者当前情境下用语音比文字更合适时，调用本工具。

        调用前必须遵守以下规则：
        1. text 必须是「要亲口念出来的话」，必须严格使用你当前所扮演角色的口吻、称谓、口头禅和说话习惯，并紧密结合上文对话语境；
        2. 只写台词本身，禁止写旁白、动作描写、括号说明、心理活动、表情符号、Markdown 标记、列表符号、emoji；
        3. 用第一人称口语表达，句子短、自然，像真人说话，控制在 120 字以内；
        4. 调用成功后不要再把这些台词以文字形式重复输出一遍。

        Args:
            text(string): 要念出来的台词，纯口语文本，不含任何格式标记
            emotion(string): 情绪或语气指令，如「温柔」「生气」「兴奋」「撒娇」「低沉」，不需要时留空
        """
        if not self.api_key:
            yield "语音发送失败：插件未配置 Token Plan API Key，请让管理员在插件配置中填写。"
            return

        content = self._prepare(text)
        if not content:
            yield "语音发送失败：台词为空。"
            return

        if len(content) > self.max_chars:
            yield (
                f"语音发送失败：台词 {len(content)} 字，超过上限 {self.max_chars} 字。"
                "请压缩到更短的句子后重试，或放弃发送语音。"
            )
            return

        if self._in_cooldown(event):
            yield "语音发送失败：请求过于频繁，请稍后再试。"
            return

        self._mark_call(event)
        try:
            path = await self.client.synthesize(
                text=content,
                save_dir=self.audio_dir,
                instruction=emotion.strip() or None,
            )
        except TTSError as exc:
            logger.warning(f"[TokenPlanTTS] 合成失败: {exc}")
            yield f"语音发送失败：{exc}。请改用文字回复，不要反复重试语音。"
            return
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[TokenPlanTTS] 合成异常: {exc}")
            yield f"语音发送失败：{exc}。请改用文字回复。"
            return

        try:
            yield event.chain_result([Record.fromFileSystem(str(path))])
            self._mark_sent(event)
            yield (
                "语音已作为 QQ 语音消息发送成功。不要再输出这段台词，"
                "直接继续对话即可。"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[TokenPlanTTS] 发送语音失败: {exc}")
            yield f"语音已合成但发送失败：{exc}。请改用文字回复。"

    # ---------------- 2. 提示词注入 ----------------

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if not (self.enable_tool and self.inject_prompt):
            return

        hint = (
            f"你具备发送语音的能力：调用工具 `{TTS_TOOL_NAME}` 即可把你的一句话变成语音发给用户。"
            "当用户想听你说话、要求发语音，或你觉得语音更有表现力时调用它。"
            "台词必须完全贴合你当前的人设（口吻、称谓、口头禅）与上下文语境，"
            "只写要念出来的口语，不要旁白、括号、动作描写、emoji 和 Markdown，尽量控制在 120 字内。"
        )
        if self.style_hint:
            hint += f"补充要求：{self.style_hint}"

        # 优先走临时 user content（不进历史、不破坏 prompt 缓存）
        try:
            from astrbot.core.agent.message import TextPart

            part = TextPart(text=hint)
            if hasattr(part, "mark_as_temp"):
                part = part.mark_as_temp()
            req.extra_user_content_parts.append(part)
            return
        except Exception:  # noqa: BLE001
            pass

        try:
            req.system_prompt = f"{req.system_prompt or ''}\n{hint}"
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[TokenPlanTTS] 注入提示失败: {exc}")

    # ---------------- 3. 自动语音 ----------------

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        if not self.auto_voice:
            return
        if self._already_sent(event):
            return

        result = event.get_result()
        if not result or not getattr(result, "chain", None):
            return

        plains = [seg for seg in result.chain if isinstance(seg, Plain)]
        if not plains:
            return

        text = "".join(seg.text or "" for seg in plains).strip()
        content = self._prepare(text)
        if not (self.auto_min_chars <= len(content) <= self.auto_max_chars):
            return
        if self._in_cooldown(event):
            return

        self._mark_call(event)
        try:
            path = await self.client.synthesize(text=content, save_dir=self.audio_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[TokenPlanTTS] 自动语音合成失败: {exc}")
            return

        if not self.auto_keep_text:
            for seg in plains:
                try:
                    result.chain.remove(seg)
                except ValueError:
                    pass
        result.chain.append(Record.fromFileSystem(str(path)))
        self._mark_sent(event)

    # ---------------- 4. 命令 ----------------

    @filter.command("语音")
    async def tts_command(self, event: AstrMessageEvent):
        """手动合成语音：/语音 你好呀"""
        raw = (event.get_message_str() or "").strip()
        text = re.sub(r"^(语音|tts|say)\s*", "", raw, count=1, flags=re.I).strip()

        if not text:
            yield event.plain_result(
                "用法：/语音 要念出来的内容\n"
                f"当前模型 {self.client.model}，音色 {self.client.voice}",
            )
            return

        content = self._prepare(text)
        if len(content) > self.max_chars:
            yield event.plain_result(f"文本过长（{len(content)} 字），上限 {self.max_chars} 字。")
            return

        try:
            path = await self.client.synthesize(text=content, save_dir=self.audio_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[TokenPlanTTS] 命令合成失败: {exc}")
            yield event.plain_result(f"合成失败：{exc}")
            return

        self._mark_call(event)
        yield event.chain_result([Record.fromFileSystem(str(path))])

    @filter.command("语音测试")
    async def tts_test(self, event: AstrMessageEvent):
        """测试 Token Plan TTS 是否配置正确"""
        if not self.api_key:
            yield event.plain_result("未配置 API Key，请先在插件配置中填写。")
            return
        try:
            path = await self.client.synthesize(
                text="语音通道测试成功，我现在可以开口说话了。",
                save_dir=self.audio_dir,
            )
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"测试失败：{exc}")
            return
        yield event.chain_result([Record.fromFileSystem(str(path))])
