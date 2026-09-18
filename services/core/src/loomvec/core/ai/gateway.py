"""P1-CORE-02 AI 供方网关（OpenAI 兼容）。

一切 AI 调用统一经此网关（06 文档 · 工程约定 5）：
- 业务代码禁止直连供应商 SDK，密钥只经环境变量注入，日志中不得出现；
- embed：批量 + 429/5xx 指数退避重试（tenacity）；
- rerank：`/rerank`（Cohere/Jina 风格），供方不配置时返回 None 由调用方跳过精排；
- complete：chat completions，支持 JSON 输出模式与解析失败重试；
- 调用计量：每次调用输出 structlog `ai_call` 事件（token / 耗时 / 是否成功）；
- `ai.mock=true` 时全部通道委托 MockBackend（见 mock.py）。

扩展点：新增通道行为在此加方法；新增本地供方参照 ai/mock.py 的接口。
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from loomvec.core.ai.mock import MockBackend
from loomvec.core.config import AiProviderConfig, AiSettings
from loomvec.core.errors import UpstreamUnavailableError, ValidationError

logger = structlog.get_logger("loomvec.ai")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class AiJsonError(Exception):
    """LLM 输出无法解析为期望 JSON（触发重试或兜底）。"""


def extract_json(text: str) -> dict[str, Any]:
    """宽容解析：直接 json.loads，失败时截取首个 {...} 代码块重试。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, flags=re.S)
    if m:
        try:
            result = json.loads(m.group(0))
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass
    raise AiJsonError(f"无法解析为 JSON 对象：{text[:200]!r}")


def _retryable(e: BaseException) -> bool:
    if isinstance(e, (httpx.TransportError, httpx.HTTPStatusError)):
        if isinstance(e, httpx.HTTPStatusError):
            return e.response.status_code in RETRYABLE_STATUS
        return True
    return False


@dataclass
class Usage:
    """单次调用计量。"""

    channel: str
    model: str
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    ok: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def log(self) -> None:
        logger.info(
            "ai_call",
            channel=self.channel,
            model=self.model,
            latency_ms=self.latency_ms,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            ok=self.ok,
            **self.extra,
        )


class AiGateway:
    """五通道：embedding / rerank / vlm / clip / llm（rerank·clip 支持
    api_style=dashscope 走百炼原生协议，其余 OpenAI 兼容）。"""

    def __init__(self, settings: AiSettings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._mock = MockBackend(settings) if settings.mock else None

    # ---------- HTTP 基建 ----------

    def _provider(self, channel: str) -> AiProviderConfig:
        cfg: AiProviderConfig = getattr(self._settings, channel)
        if not cfg.base_url or not cfg.model:
            raise ValidationError(
                f"AI 通道 {channel} 未配置（base_url/model），"
                "请检查 config/loomvec.json 的 ai 段（交互式配置可运行 ./deploy.sh）",
                channel=channel,
            )
        return cfg

    def _http(self, channel: str) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        if channel not in self._clients:
            cfg = self._provider(channel)
            self._clients[channel] = httpx.AsyncClient(timeout=cfg.timeout_seconds)
        return self._clients[channel]

    async def _post(self, channel: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        cfg = self._provider(channel)
        headers = {}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"

        @retry(
            retry=retry_if_exception(_retryable),
            wait=wait_exponential_jitter(initial=1, max=30),
            stop=stop_after_attempt(5),
            reraise=True,
        )
        async def _do() -> dict[str, Any]:
            resp = await self._http(channel).post(
                f"{cfg.base_url.rstrip('/')}{path}", json=payload, headers=headers
            )
            resp.raise_for_status()
            return resp.json()

        try:
            return await _do()
        except httpx.HTTPError as e:
            raise UpstreamUnavailableError(upstream=f"ai:{channel}", reason=str(e)) from e

    def _usage(self, channel: str, cfg: AiProviderConfig, started: float, resp: dict) -> Usage:
        u = resp.get("usage") or {}
        usage = Usage(
            channel=channel,
            model=cfg.model or "",
            latency_ms=int((time.monotonic() - started) * 1000),
            prompt_tokens=int(u.get("prompt_tokens") or 0),
            completion_tokens=int(u.get("completion_tokens") or 0),
            ok=True,
        )
        usage.log()
        return usage

    # ---------- 通道实现 ----------

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化：内部按 embedding.batch_size 分批；空输入返回空。"""
        if not texts:
            return []
        if self._mock is not None:
            return self._mock.embed(texts)

        cfg = self._provider("embedding")
        batch_size = max(1, getattr(cfg, "batch_size", 16))
        payload_extra: dict[str, Any] = {}
        dimensions = getattr(cfg, "dimensions", None)
        if dimensions:
            payload_extra["dimensions"] = dimensions
        vectors: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            started = time.monotonic()
            resp = await self._post(
                "embedding", "/embeddings", {"model": cfg.model, "input": batch, **payload_extra}
            )
            self._usage("embedding", cfg, started, resp)
            data = sorted(resp.get("data", []), key=lambda d: d.get("index", 0))
            if len(data) != len(batch):
                raise UpstreamUnavailableError(
                    upstream="ai:embedding",
                    reason=f"返回向量数 {len(data)} != 输入 {len(batch)}",
                )
            vectors.extend(d["embedding"] for d in data)
        return vectors

    async def rerank(
        self, query: str, documents: list[str], *, top_n: int | None = None
    ) -> list[dict[str, Any]] | None:
        """精排：返回 [{index, relevance_score}]（按分数降序）；未配置 rerank 通道返回 None。"""
        if not documents:
            return []
        if self._mock is not None:
            scores = self._mock.rerank(query, documents)
        elif self._settings.rerank.base_url and self._settings.rerank.model:
            cfg = self._settings.rerank
            started = time.monotonic()
            if cfg.api_style == "dashscope":
                resp = await self._post(
                    "rerank",
                    "/services/rerank/text-rerank/text-rerank",
                    {
                        "model": cfg.model,
                        "input": {"query": query, "documents": documents},
                        "parameters": {
                            "return_documents": False,
                            **({"top_n": top_n} if top_n else {}),
                        },
                    },
                )
                self._usage("rerank", cfg, started, resp)
                return [
                    {"index": r["index"], "relevance_score": float(r["relevance_score"])}
                    for r in (resp.get("output") or {}).get("results", [])
                ]
            payload: dict[str, Any] = {"model": cfg.model, "query": query, "documents": documents}
            if top_n:
                payload["top_n"] = top_n
            resp = await self._post("rerank", "/rerank", payload)
            self._usage("rerank", cfg, started, resp)
            return [
                {"index": r["index"], "relevance_score": float(r["relevance_score"])}
                for r in resp.get("results", [])
            ]
        else:
            return None  # 供方未配置：调用方降级为 RRF 序
        ranked = sorted(
            ({"index": i, "relevance_score": s} for i, s in enumerate(scores)),
            key=lambda r: r["relevance_score"],
            reverse=True,
        )
        return ranked[:top_n] if top_n else ranked

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """LLM chat；json_mode=True 时请求 JSON 输出。返回 assistant 文本。"""
        if self._mock is not None:
            return self._mock.complete("\n".join(m.get("content", "") for m in messages))

        cfg = self._settings.llm
        payload: dict[str, Any] = {
            "model": cfg.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else cfg.temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        started = time.monotonic()
        resp = await self._post("llm", "/chat/completions", payload)
        self._usage("llm", cfg, started, resp)
        choices = resp.get("choices") or []
        if not choices:
            raise UpstreamUnavailableError(upstream="ai:llm", reason="响应无 choices")
        return str(choices[0].get("message", {}).get("content", ""))

    async def complete_json(self, messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
        """JSON 输出模式 + 解析失败重试（llm.json_retries 次）；仍失败抛 AiJsonError。"""
        last_err: Exception | None = None
        for _attempt in range(1 + self._settings.llm.json_retries):
            text = await self.complete(messages, json_mode=True, **kwargs)
            try:
                return extract_json(text)
            except AiJsonError as e:
                last_err = e
        raise last_err or AiJsonError("LLM JSON 输出解析失败")

    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ):
        """LLM 流式输出（chat completions stream=true）→ 异步迭代 delta 文本。

        mock 模式：把 complete() 的整段回答按小块切分 yield（行为确定性）。
        """
        if self._mock is not None:
            import asyncio

            text = self._mock.complete("\n".join(m.get("content", "") for m in messages))
            for i in range(0, len(text), 8):
                yield text[i : i + 8]
                await asyncio.sleep(0)
            return

        cfg = self._settings.llm
        payload: dict[str, Any] = {
            "model": cfg.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else cfg.temperature,
            "stream": True,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        cfg_provider = self._provider("llm")
        headers = {}
        if cfg_provider.api_key:
            headers["Authorization"] = f"Bearer {cfg_provider.api_key}"
        started = time.monotonic()
        try:
            async with self._http("llm").stream(
                "POST",
                f"{cfg_provider.base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers=headers,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        if data == "[DONE]":
                            break
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        yield str(delta)
        except httpx.HTTPError as e:
            raise UpstreamUnavailableError(upstream="ai:llm", reason=str(e)) from e
        Usage(
            channel="llm",
            model=cfg.model or "",
            latency_ms=int((time.monotonic() - started) * 1000),
            ok=True,
        ).log()

    # ---------- clip / vlm（P2-CORE-05 图片管线通道） ----------

    def clip_available(self) -> bool:
        """clip 通道是否可用（mock 模式恒可用；真实供方需配置 base_url+model）。"""
        if self._mock is not None:
            return True
        return bool(self._settings.clip.base_url and self._settings.clip.model)

    def llm_available(self) -> bool:
        """LLM 通道是否可用（问答入口降级判定；mock 模式恒可用）。"""
        if self._mock is not None:
            return True
        return bool(self._settings.llm.base_url and self._settings.llm.model)

    def clip_dim(self) -> int:
        return self._settings.clip.dim

    async def clip_embed_texts(self, texts: list[str]) -> list[list[float]]:
        """图文向量·文本侧：以文搜图时把查询文本嵌入 clip 空间。"""
        if not texts:
            return []
        if self._mock is not None:
            return self._mock.clip_text(texts)
        return await self._clip_request([{"text": t} for t in texts])

    async def clip_embed_images(
        self, images: list[bytes], captions: list[str | None] | None = None
    ) -> list[list[float]]:
        """图文向量·图片侧：bytes（JPEG/PNG）嵌入 clip 空间。"""
        if not images:
            return []
        if self._mock is not None:
            return self._mock.clip_image(images, captions or [None] * len(images))
        if self._settings.clip.api_style == "dashscope":
            # 百炼原生 multimodal-embedding 每次请求最多 1 张图：逐张请求
            vectors: list[list[float]] = []
            for img, cap in zip(images, captions or [None] * len(images), strict=False):
                item: dict[str, str] = {
                    "image": f"data:image/jpeg;base64,{base64.b64encode(img).decode()}"
                }
                if cap:
                    item["text"] = cap
                vectors.extend(await self._clip_request_dashscope([item]))
            return vectors
        items: list[dict[str, str]] = []
        for img, cap in zip(images, captions or [None] * len(images), strict=False):
            item: dict[str, str] = {"image": base64.b64encode(img).decode()}
            if cap:
                item["text"] = cap
            items.append(item)
        return await self._clip_request(items)

    async def _clip_request(self, items: list[dict[str, str]]) -> list[list[float]]:
        """Jina 风格多模态 embeddings：input 为 [{"text"|"image", ...}] 混合列表。"""
        cfg = self._provider("clip")
        if cfg.api_style == "dashscope":
            return await self._clip_request_dashscope(items)
        started = time.monotonic()
        resp = await self._post("clip", "/embeddings", {"model": cfg.model, "input": items})
        self._usage("clip", cfg, started, resp)
        data = sorted(resp.get("data", []), key=lambda d: d.get("index", 0))
        if len(data) != len(items):
            raise UpstreamUnavailableError(
                upstream="ai:clip", reason=f"返回向量数 {len(data)} != 输入 {len(items)}"
            )
        return [d["embedding"] for d in data]

    async def _clip_request_dashscope(self, contents: list[dict[str, str]]) -> list[list[float]]:
        """百炼原生 multimodal-embedding：POST .../services/embeddings/multimodal-embedding/
        multimodal-embedding，input.contents 为 [{"text"}|{"image": URL/dataURI}]。"""
        cfg = self._provider("clip")
        started = time.monotonic()
        resp = await self._post(
            "clip",
            "/services/embeddings/multimodal-embedding/multimodal-embedding",
            {"model": cfg.model, "input": {"contents": contents}},
        )
        self._usage("clip", cfg, started, resp)
        embeddings = (resp.get("output") or {}).get("embeddings") or []
        if len(embeddings) != len(contents):
            raise UpstreamUnavailableError(
                upstream="ai:clip",
                reason=f"返回向量数 {len(embeddings)} != 输入 {len(contents)}",
            )
        return [e["embedding"] for e in embeddings]

    async def vlm(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        """视觉问答/caption：chat completions 多模态消息（content 含 image_url）。"""
        if self._mock is not None:
            texts = []
            for m in messages:
                content = m.get("content") or ""
                if isinstance(content, list):  # 多模态 content parts
                    texts.extend(str(p.get("text", "")) for p in content if isinstance(p, dict))
                else:
                    texts.append(str(content))
            return self._mock.complete("\n".join(texts))

        cfg = self._settings.vlm
        payload: dict[str, Any] = {
            "model": cfg.model,
            "messages": messages,
            "temperature": kwargs.get("temperature", 0.2),
        }
        started = time.monotonic()
        resp = await self._post("vlm", "/chat/completions", payload)
        self._usage("vlm", cfg, started, resp)
        choices = resp.get("choices") or []
        if not choices:
            raise UpstreamUnavailableError(upstream="ai:vlm", reason="响应无 choices")
        return str(choices[0].get("message", {}).get("content", ""))

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
