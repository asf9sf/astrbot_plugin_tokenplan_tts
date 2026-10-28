"""Token Plan (阿里云百炼) 语音合成 HTTP 客户端。

接口：POST {api_base}/api/v1/services/audio/tts/SpeechSynthesizer
鉴权：Authorization: Bearer sk-sp-xxxx
请求体：{"model": ..., "input": {"text": ..., "voice": ..., "format": ..., "sample_rate": ...}}

响应主体通常直接是音频二进制；为兼容服务端改版，这里同时支持返回 JSON
（内含音频 URL 或 base64）的情况。
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import logger

ENDPOINT_PATH = "/api/v1/services/audio/tts/SpeechSynthesizer"

# 常见音频格式的 magic bytes，用于纠正实际返回的文件后缀
_MAGIC = (
    (b"RIFF", "wav"),
    (b"ID3", "mp3"),
    (b"OggS", "ogg"),
    (b"fLaC", "flac"),
)


class TTSError(Exception):
    """语音合成失败。"""


def _sniff_ext(data: bytes, declared: str) -> str:
    """根据文件头判断真实音频格式，声明格式与实际不符时以实际为准。"""
    declared = (declared or "wav").lower().strip(".")
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    # MP3 裸帧：帧同步 0xFFEx
    if len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"
    return declared


def _walk_audio(obj: Any) -> tuple[str, str] | None:
    """在 JSON 响应里递归查找音频。

    Returns:
        (kind, value)，kind 为 "url" 或 "b64"；找不到返回 None。
    """
    if isinstance(obj, dict):
        # 优先显式字段
        for key in ("audio_url", "url", "audio", "download_url"):
            if key in obj:
                val = obj[key]
                if isinstance(val, str) and val.startswith(("http://", "https://")):
                    return "url", val
                if isinstance(val, str) and len(val) > 256:
                    return "b64", val
        for key in ("audio_data", "data", "base64", "audio_base64", "content"):
            if key in obj:
                val = obj[key]
                if isinstance(val, str) and len(val) > 256:
                    return "b64", val
        for val in obj.values():
            found = _walk_audio(val)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _walk_audio(item)
            if found:
                return found
    return None


class TokenPlanTTSClient:
    def __init__(
        self,
        api_key: str,
        api_base: str,
        model: str,
        voice: str,
        audio_format: str = "wav",
        sample_rate: int = 24000,
        instruction: str = "",
        instruction_field: str = "parameters",
        timeout: int = 60,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.api_base = (api_base or "").strip().rstrip("/")
        self.model = model
        self.voice = voice
        self.audio_format = (audio_format or "wav").lower()
        self.sample_rate = int(sample_rate or 24000)
        self.instruction = (instruction or "").strip()
        # instruction 的官方位置是顶层 parameters；早期实现误放进 input 导致不生效
        self.instruction_field = (instruction_field or "parameters").strip().lower()
        self.timeout = int(timeout or 60)
        self._session: aiohttp.ClientSession | None = None

    # ---------- 基础设施 ----------

    @property
    def endpoint(self) -> str:
        if self.api_base.endswith(ENDPOINT_PATH):
            return self.api_base
        return self.api_base + ENDPOINT_PATH

    @property
    def is_openai_compat(self) -> bool:
        """base_url 里带 compatible-mode 就按 OpenAI 协议走，否则走 DashScope 原生接口。"""
        return "/compatible-mode" in self.api_base.lower()

    @property
    def openai_endpoint(self) -> str:
        return self.api_base.rstrip("/") + "/audio/speech"

    @property
    def native_base(self) -> str:
        """从 OpenAI 兼容地址倒推业务空间根地址，用于失败回退。"""
        base = self.api_base.rstrip("/")
        idx = base.lower().find("/compatible-mode")
        return base[:idx] if idx > 0 else base

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ---------- 核心 ----------

    async def synthesize(
        self,
        text: str,
        save_dir: Path,
        voice: str | None = None,
        instruction: str | None = None,
        audio_format: str | None = None,
    ) -> Path:
        """合成语音并落盘，返回音频文件绝对路径。"""
        if not self.api_key:
            raise TTSError("未配置 Token Plan API Key")

        text = (text or "").strip()
        if not text:
            raise TTSError("待合成文本为空")

        fmt = (audio_format or self.audio_format).lower()
        voice_name = voice or self.voice

        audio: bytes | None = None

        # base_url 指向业务空间的 /compatible-mode/v1 时先走 OpenAI 协议
        if self.is_openai_compat:
            try:
                audio = await self._synth_openai(text, voice_name, fmt)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"[TokenPlanTTS] OpenAI 兼容端点失败，回退原生接口: {exc}",
                )
                audio = None

        if not audio:
            audio = await self._synth_native(text, voice_name, fmt, instruction)

        if not audio:
            raise TTSError("TTS 接口未返回有效的音频数据")

        ext = _sniff_ext(audio, fmt)
        save_dir.mkdir(parents=True, exist_ok=True)
        file_path = save_dir / f"tts_{uuid.uuid4().hex[:12]}.{ext}"
        file_path.write_bytes(audio)
        logger.debug(f"[TokenPlanTTS] 已保存音频 {file_path} ({len(audio)} bytes)")
        return file_path

    async def _synth_openai(self, text: str, voice_name: str, fmt: str) -> bytes | None:
        """OpenAI 兼容协议：POST {base}/audio/speech，直接返回音频二进制。"""
        payload: dict[str, Any] = {
            "model": self.model,
            "input": text,
            "voice": voice_name,
            "response_format": (
                fmt if fmt in ("wav", "mp3", "opus", "pcm", "aac", "flac") else "wav"
            ),
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "*/*",
        }
        session = self._ensure_session()
        async with session.post(
            self.openai_endpoint, json=payload, headers=headers
        ) as resp:
            raw = await resp.read()
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if resp.status != 200:
                raise TTSError(
                    f"OpenAI 兼容 TTS 返回 {resp.status}: "
                    f"{raw[:400].decode('utf-8', errors='ignore')}",
                )
            return await self._parse_audio(resp, raw, ctype, session)

    async def _synth_native(
        self,
        text: str,
        voice_name: str,
        fmt: str,
        instruction: str | None,
    ) -> bytes | None:
        """DashScope 原生协议：POST {根地址}/api/v1/services/aigc/multimodal-generation/generation。"""
        payload: dict[str, Any] = {
            "model": self.model,
            "input": {
                "text": text,
                "voice": voice_name,
                "format": fmt,
                "sample_rate": self.sample_rate,
            },
        }
        # 情感 / 风格指令（qwen3-tts-flash 不支持，留空即不传）
        ins = instruction if instruction is not None else self.instruction
        if ins and self.instruction_field != "none":
            if self.instruction_field == "input":
                payload["input"]["instruction"] = ins
            else:
                payload.setdefault("parameters", {})["instruction"] = ins

        url = self.native_base.rstrip("/") + ENDPOINT_PATH
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "*/*",
        }

        session = self._ensure_session()
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                raw = await resp.read()
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if resp.status != 200:
                    raise TTSError(
                        f"TTS 接口返回 {resp.status}: {raw[:500].decode('utf-8', errors='ignore')}",
                    )
                return await self._parse_audio(resp, raw, ctype, session)
        except aiohttp.ClientError as exc:
            raise TTSError(f"请求 TTS 接口失败: {exc}") from exc
        except TTSError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TTSError(f"合成语音时发生异常: {exc}") from exc

    async def _parse_audio(
        self,
        resp: aiohttp.ClientResponse,
        raw: bytes,
        ctype: str,
        session: aiohttp.ClientSession,
    ) -> bytes | None:
        """把接口响应统一解析成音频字节。"""
        # 1) 看起来像 JSON：尝试提取音频 URL 或 base64
        if "json" in ctype or raw[:1] in (b"{", b"["):
            try:
                data = json.loads(raw.decode("utf-8", errors="ignore"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                data = None

            if data is not None:
                found = _walk_audio(data)
                if found:
                    kind, value = found
                    if kind == "url":
                        async with session.get(value) as r:
                            if r.status != 200:
                                raise TTSError(f"下载音频失败: HTTP {r.status}")
                            return await r.read()
                    try:
                        return base64.b64decode(value)
                    except (binascii.Error, ValueError) as exc:
                        raise TTSError(f"音频 base64 解码失败: {exc}") from exc
                raise TTSError(
                    f"TTS 接口返回 JSON 但找不到音频字段: {json.dumps(data, ensure_ascii=False)[:500]}",
                )

        # 2) 直接是音频二进制
        if raw:
            return raw
        return None
