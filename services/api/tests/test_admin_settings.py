"""admin 动态配置注册表契约：AI 供方键位完整性 / 生效方式 / 写入校验。

AI 供方（ai 组）是本地模型与云端供方的统一配置入口（docs/16-本地模型推理.md）：
- 五通道（llm/embedding/rerank/vlm/clip）×（base_url/api_key/model）+
  rerank/clip 的 api_style 全部在册，且与 core.config.AI_OVERRIDE_KEYS
  （api/worker 网关启动时的合并白名单）严格一致；
- ai 组全部 effect=restart（网关启动时快照，改完需重启）；
- base_url / api_style 写入前校验——坏值落库会让 api 启动 fail-fast，
  必须挡在写入口；
- 本地推理探测（settings/ai/local-runtime）为分层表单提供本地档可用性。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from loomvec.api.app import create_app
from loomvec.api.services import admin_settings
from loomvec.api.services.local_models import (
    channel_status,
    channel_suggestions,
    is_local_base_url,
)
from loomvec.core.config import AI_OVERRIDE_KEYS, AiSettings, Settings
from loomvec.core.errors import ValidationError


def test_ai_registry_keys_complete_and_restart():
    registered = {k for k in admin_settings.SETTING_REGISTRY if k.startswith("ai.")}
    assert registered == set(AI_OVERRIDE_KEYS)
    for key in registered:
        d = admin_settings.SETTING_REGISTRY[key]
        assert d.group == "ai"
        assert d.admin_only
        assert d.effect == "restart"
        if key.endswith("api_key"):
            assert d.sensitive


def test_base_url_validator():
    d = admin_settings.SETTING_REGISTRY["ai.clip.base_url"]
    d.validate("http://127.0.0.1:38011")
    d.validate("https://dashscope.aliyuncs.com/api/v1")
    d.validate("")  # 留空 = 回落文件配置
    d.validate(None)
    with pytest.raises(ValidationError):
        d.validate("ftp://127.0.0.1:38011")


def test_api_style_validator_per_channel():
    rr = admin_settings.SETTING_REGISTRY["ai.rerank.api_style"]
    clip = admin_settings.SETTING_REGISTRY["ai.clip.api_style"]
    rr.validate("openai")
    rr.validate("dashscope")
    clip.validate("infinity")  # 本地 Infinity 协议仅 clip 通道有
    with pytest.raises(ValidationError):
        rr.validate("infinity")
    with pytest.raises(ValidationError):
        clip.validate("bogus")


# ---------------------------------------------------------------- 本地推理探测派生
def test_is_local_base_url():
    assert is_local_base_url("http://127.0.0.1:38010/v1", 38010)
    assert is_local_base_url("http://localhost:38011", 38011)
    assert is_local_base_url("http://127.0.0.1:38011/", 38011)
    assert not is_local_base_url("http://127.0.0.1:38010/v1", 38011)  # 端口不匹配
    assert not is_local_base_url("https://api.deepseek.com", 38010)  # 非本机
    assert not is_local_base_url("", 38010)
    assert not is_local_base_url(None, 38010)


_PROBE_UP = {
    "vllm": {"ok": True, "port": 38010, "models": ["qwen2.5-1.5b", "deepseek-chat"]},
    "infinity": {"ok": True, "port": 38011, "models": ["bge-m3", "bge-reranker-v2-m3"]},
}

_PROBE_DOWN = {
    "vllm": {"ok": False, "port": 38010, "models": [], "error": "ConnectError"},
    "infinity": {"ok": False, "port": 38011, "models": [], "error": "ConnectError"},
}


def test_channel_suggestions_available_and_alias_excluded():
    s = channel_suggestions(_PROBE_UP)
    # deepseek-chat 是 agent 别名，不作为 ai.llm.model 推荐
    assert s["llm"]["available"] and s["llm"]["model"] == "qwen2.5-1.5b"
    assert s["llm"]["base_url"] == "http://127.0.0.1:38010/v1"
    assert s["embedding"]["available"] and s["embedding"]["model"] == "bge-m3"
    assert s["rerank"]["api_style"] == "openai"
    # jina-clip-v2 未在 Infinity served 列表 → clip 本地档不可用（部分加载语义）
    assert not s["clip"]["available"]
    assert not s["vlm"]["available"]  # VLM 未本地化


def test_channel_suggestions_all_down():
    s = channel_suggestions(_PROBE_DOWN)
    assert all(not s[c]["available"] for c in ("llm", "embedding", "rerank", "clip", "vlm"))


def test_channel_status_local_and_cloud_mix():
    ai = AiSettings()
    ai.llm.base_url = "http://127.0.0.1:38010/v1"
    ai.embedding.base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ai.clip.base_url = "http://127.0.0.1:38011"
    st = channel_status(ai, _PROBE_UP)
    assert st["llm"] == {"configured_local": True, "available": True}
    assert st["embedding"]["configured_local"] is False
    assert st["clip"]["configured_local"] and not st["clip"]["available"]  # 指向本地但模型未加载
    assert not st["vlm"]["configured_local"]


def test_local_runtime_endpoint_contract(monkeypatch):
    """local-runtime 探测端点：返回 services + suggestions，权限 admin:read。"""
    from loomvec.api.routes.admin import settings as settings_route

    async def _fake_probe():
        return _PROBE_UP

    monkeypatch.setattr(settings_route, "probe_services", _fake_probe)
    settings = Settings(_env_file=None, auth={"dev_mode": True})
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        token = c.post(
            "/api/v1/auth/dev/token", json={"username": "lr-user", "roles": ["super_admin"]}
        ).json()["access_token"]
        resp = c.get(
            "/api/v1/admin/settings/ai/local-runtime",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["services"]["vllm"]["ok"] is True
        assert body["suggestions"]["llm"]["model"] == "qwen2.5-1.5b"
        # operator（admin:read）可读
        token2 = c.post(
            "/api/v1/auth/dev/token", json={"username": "lr-op", "roles": ["operator"]}
        ).json()["access_token"]
        assert (
            c.get(
                "/api/v1/admin/settings/ai/local-runtime",
                headers={"Authorization": f"Bearer {token2}"},
            ).status_code
            == 200
        )
        # 未认证 401
        assert c.get("/api/v1/admin/settings/ai/local-runtime").status_code == 401
