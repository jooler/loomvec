"""本地模型推理（vLLM/Infinity）运行状态探测与每通道建议配置。

服务由 scripts/start-models-gpu.sh 在宿主机起（docs/16-本地模型推理.md），端口
单源为其 env 约定（MODELS_LLM_PORT / MODELS_INFINITY_PORT，缺省 38010/38011）。
探测只认 127.0.0.1——本地推理与 api 同机是该方案的部署前提；纯云端部署的
ai.* 不指向本地端口，system status 的 local_models 段 enabled=false，前端不展示。

- probe_services()：对两个服务做 /health + /v1/models 探测（短超时，随
  /admin/system/status 15s 轮询反复调用）；
- channel_status()：按生效 AI 配置判定各通道 configured_local（base_url 指向
  本地端口）与 available（服务健康且该通道模型在 served 列表）；
- channel_suggestions()：本地可用通道的建议写入值（AI 供方表单「选本地」
  一键填充，api_key 统一 local，clip 协议为 infinity）。
"""

from __future__ import annotations

import os
from typing import Any

from loomvec.core.config import AiSettings

# 与 scripts/start-models-gpu.sh 相同的 env 覆盖（裸 env，非 LOOMVEC_ 前缀）
LLM_PORT_ENV = "MODELS_LLM_PORT"
INFINITY_PORT_ENV = "MODELS_INFINITY_PORT"
LLM_PORT_DEFAULT = 38010
INFINITY_PORT_DEFAULT = 38011

# Infinity 侧固定 served 名（scripts/start-models-gpu.sh 单源）
INFINITY_CHANNEL_MODELS: dict[str, str] = {
    "embedding": "bge-m3",
    "rerank": "bge-reranker-v2-m3",
    "clip": "jina-clip-v2",
}
# vLLM 的 deepseek-chat 是 agent/dsh 兼容别名，不作为 ai.llm.model 推荐
LLM_ALIAS_EXCLUDE = ("deepseek-chat",)
LLM_MODEL_FALLBACK = "qwen2.5-1.5b"

PROBE_TIMEOUT_SECONDS = 1.5
LOCAL_HOSTS = ("127.0.0.1", "localhost")


def local_ports() -> tuple[int, int]:
    """（llm, infinity）本地推理端口：env 覆盖，缺省 38010/38011。"""

    def _int(env_key: str, default: int) -> int:
        raw = (os.environ.get(env_key) or "").strip()
        try:
            return int(raw) if raw else default
        except ValueError:
            return default

    return _int(LLM_PORT_ENV, LLM_PORT_DEFAULT), _int(INFINITY_PORT_ENV, INFINITY_PORT_DEFAULT)


def is_local_base_url(base_url: str | None, port: int) -> bool:
    """base_url 是否指向本机给定端口（host 限 127.0.0.1/localhost，忽略路径与协议尾斜杠）。"""
    if not base_url:
        return False
    rest = base_url.split("://", 1)[-1].rstrip("/")
    host_part = rest.split("/", 1)[0]
    host, _, port_part = host_part.partition(":")
    if host not in LOCAL_HOSTS:
        return False
    try:
        return int(port_part) == port if port_part else port in (80, 443)
    except ValueError:
        return False


async def _probe_service(client: Any, port: int) -> dict[str, Any]:
    base = f"http://127.0.0.1:{port}"
    try:
        resp = await client.get(f"{base}/health")
        ok = resp.status_code < 500
    except Exception as e:
        return {"ok": False, "models": [], "error": f"{type(e).__name__}: {e}"}
    models: list[str] = []
    if ok:
        # vLLM 为 /v1/models，Infinity（0.0.77）无此端点、模型列表在 /models
        for path in ("/v1/models", "/models"):
            try:
                r2 = await client.get(f"{base}{path}")
            except Exception:
                break
            if r2.status_code == 200:
                try:
                    data = r2.json().get("data") or []
                    models = [m["id"] for m in data if isinstance(m, dict) and m.get("id")]
                except Exception:
                    pass
                break
    return {"ok": ok, "models": models}


async def probe_services() -> dict[str, Any]:
    """探测本地推理两服务（vLLM / Infinity），返回 {vllm, infinity} 各含 ok/port/models。"""
    import httpx

    llm_port, inf_port = local_ports()
    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS, trust_env=False) as client:
        vllm = await _probe_service(client, llm_port)
        infinity = await _probe_service(client, inf_port)
    vllm["port"] = llm_port
    infinity["port"] = inf_port
    return {"vllm": vllm, "infinity": infinity}


def channel_status(ai: AiSettings, probe: dict[str, Any]) -> dict[str, dict[str, bool]]:
    """各通道 {configured_local, available}：配置是否指向本地、本地是否真可用。

    vlm 未本地化恒 (False, False)；llm available 看 vLLM 健康，embedding/rerank/clip
    还要求对应模型在 Infinity 的 served 列表（部分加载时单通道降级）。
    """
    vllm_ok = bool(probe.get("vllm", {}).get("ok"))
    inf_models = set(probe.get("infinity", {}).get("models") or [])
    llm_port, inf_port = local_ports()
    out: dict[str, dict[str, bool]] = {}
    for channel in ("llm", "embedding", "rerank", "vlm", "clip"):
        base_url = getattr(ai, channel).base_url
        if channel == "vlm":
            out[channel] = {"configured_local": False, "available": False}
        elif channel == "llm":
            out[channel] = {
                "configured_local": is_local_base_url(base_url, llm_port),
                "available": vllm_ok,
            }
        else:
            out[channel] = {
                "configured_local": is_local_base_url(base_url, inf_port),
                "available": INFINITY_CHANNEL_MODELS[channel] in inf_models,
            }
    return out


def channel_suggestions(probe: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """每通道「选本地」的建议写入值；available=false 的通道前端据此禁选。"""
    vllm = probe.get("vllm", {})
    inf = probe.get("infinity", {})
    llm_port, inf_port = local_ports()
    llm_models = [m for m in vllm.get("models") or [] if m not in LLM_ALIAS_EXCLUDE]
    suggestions: dict[str, dict[str, Any]] = {
        "llm": {
            "available": bool(vllm.get("ok")),
            "base_url": f"http://127.0.0.1:{llm_port}/v1",
            "model": llm_models[0] if llm_models else LLM_MODEL_FALLBACK,
            "api_key": "local",
            "api_style": "openai",
        },
        "vlm": {"available": False},  # VLM 未本地化（docs/16），表单不提供本地档
    }
    for channel, model in INFINITY_CHANNEL_MODELS.items():
        suggestion: dict[str, Any] = {
            "available": bool(inf.get("ok")) and model in set(inf.get("models") or []),
            "base_url": f"http://127.0.0.1:{inf_port}",
            "model": model,
            "api_key": "local",
        }
        if channel in ("rerank", "clip"):
            suggestion["api_style"] = "infinity" if channel == "clip" else "openai"
        suggestions[channel] = suggestion
    return suggestions
