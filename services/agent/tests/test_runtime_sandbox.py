"""P5.5a 沙箱单测（docs/Research/01）：docker provider argv 组装、fail-closed、
envs.py 按 provider 渲染 MCP url、项目目录入参校验、project_path 注入前缀。

路径恒等原则是本模块的第一断言：cwd/runtime_cwd/挂载/DSH_HOME 全部为宿主真实
路径且容器内值 == 宿主值（F13/F14 零改动的前提）。
"""

from __future__ import annotations

import asyncio
import types
from pathlib import Path

import pytest

from loomvec.agent.config import AgentRuntimeConfig
from loomvec.agent.routes import normalize_project_path
from loomvec.agent.runtime import docker as docker_mod
from loomvec.agent.runtime import envs as env_layout
from loomvec.agent.runtime.manager import build_provider
from loomvec.agent.runtime.provider import SandboxUnavailableError
from loomvec.agent.sessions.orchestrator import PromptContext, PromptOrchestrator
from loomvec.core.config import AgentSettings, Settings
from loomvec.core.errors import ValidationError


def _cfg(tmp_path: Path, provider: str = "docker") -> AgentRuntimeConfig:
    return AgentRuntimeConfig(
        settings=Settings(),
        agent=AgentSettings(storage_root=str(tmp_path / "storage"), **{
            "runtime": {"provider": provider},
        }),
    )


# ---------------------------------------------------------------------------
# provider 工厂
# ---------------------------------------------------------------------------


def test_build_provider_local_and_docker(tmp_path):
    from loomvec.agent.runtime.docker import DockerSandboxProvider
    from loomvec.agent.runtime.local import LocalRuntimeProvider

    assert isinstance(build_provider(_cfg(tmp_path, "local")), LocalRuntimeProvider)
    assert isinstance(build_provider(_cfg(tmp_path, "docker")), DockerSandboxProvider)
    with pytest.raises(ValueError):
        build_provider(_cfg(tmp_path, "e2b"))


# ---------------------------------------------------------------------------
# DockerSandboxProvider.spawn：argv 组装与路径恒等
# ---------------------------------------------------------------------------


@pytest.fixture()
def _fake_harness(monkeypatch):
    captured: dict = {}

    class FakeConfig:
        def __init__(self, **kwargs):
            captured["config"] = types.SimpleNamespace(**kwargs)

    class FakeHarness:
        def __init__(self, config, *, _launch_args=None):
            captured["argv"] = list(_launch_args or [])

        def start(self):
            captured["started"] = True

        def close(self):
            pass

    monkeypatch.setattr(docker_mod, "_harness_cls", lambda: (FakeHarness, FakeConfig))
    monkeypatch.setattr(
        docker_mod, "_docker", lambda *a, **k: asyncio.sleep(0, result=(0, ""))
    )
    return captured


def test_spawn_argv_path_identity_and_hardening(tmp_path, _fake_harness, monkeypatch):
    monkeypatch.setattr(docker_mod.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(docker_mod.os, "getgid", lambda: 20, raising=False)
    cfg = _cfg(tmp_path)
    env_id = "0f" * 16
    handle = asyncio.run(
        docker_mod.DockerSandboxProvider().spawn(cfg, env_id, {"LOOMVEC_AGENT_TOKEN": "jwt"})
    )
    assert _fake_harness["started"] is True
    argv = _fake_harness["argv"]
    env_dir = cfg.env_dir(env_id)

    # 路径恒等：唯一挂载 env 子目录自身（:z 仅 SELinux 重标，不改路径），
    # 容器内路径 = 宿主路径
    assert f"-v {env_dir}:{env_dir}:z" in " ".join(argv)
    assert f"DSH_HOME={env_dir / 'dsh-home'}" in argv
    # 硬边界与统一 uid（MVP）
    for flag in ("--read-only", "no-new-privileges", "--cap-drop", "ALL", "--pids-limit"):
        assert flag in argv
    assert argv[argv.index("--user") + 1] == "501:20"
    assert argv[argv.index("--network") + 1] == "agent-sandbox"
    assert argv[argv.index("--add-host") + 1] == "host.docker.internal:host-gateway"
    assert argv[-1] == "sdk" and "loomvec/agent-sandbox:stable" in argv
    # stdio 桥：-i（无 -t），容器即 runtime
    assert "docker run --rm -i" in " ".join(argv[:6])
    # F10：runtime_cwd = docker CLI 的宿主 cwd；cwd = initialize payload
    assert str(_fake_harness["config"].runtime_cwd) == str(env_dir)
    assert str(_fake_harness["config"].cwd) == str(env_dir / "workspace")
    # terminate 兜底所需的 env 元数据
    assert handle.env_id == env_id


def test_spawn_fail_closed_without_docker_cli(tmp_path, monkeypatch):

    monkeypatch.setattr(docker_mod.shutil, "which", lambda name: None)
    with pytest.raises(SandboxUnavailableError):
        asyncio.run(
            docker_mod.DockerSandboxProvider().spawn(
                _cfg(tmp_path), "ab" * 16, {"LOOMVEC_AGENT_TOKEN": "jwt"}
            )
        )


# ---------------------------------------------------------------------------
# envs.py：mcp url 按 provider 渲染（patch 恒重渲染，切换无残留）
# ---------------------------------------------------------------------------


def test_envs_renders_mcp_url_by_provider(tmp_path):
    cfg = _cfg(tmp_path, provider="local")
    env_id = "cd" * 16
    env_layout.ensure_env_layout(cfg, env_id, tenant_name="T", user_display_name="U")
    patch = (cfg.dsh_home(env_id) / "cordis.patch.yml").read_text(encoding="utf-8")
    assert "http://127.0.0.1:38080/api/v1/mcp" in patch  # local → url

    # 切 docker：同 env 重渲染出 url_sandbox（patchReload=startup 随下次 spawn 生效）
    cfg.agent.runtime.provider = "docker"
    env_layout.ensure_env_layout(cfg, env_id)
    patch = (cfg.dsh_home(env_id) / "cordis.patch.yml").read_text(encoding="utf-8")
    assert "http://host.docker.internal:38080/api/v1/mcp" in patch
    assert "127.0.0.1" not in patch

    # sessions root 渲染零改动（路径恒等，F13）：root 仍是宿主绝对路径
    assert f"root: {cfg.dsh_home(env_id) / 'sessions'}" in patch


# ---------------------------------------------------------------------------
# 项目目录入参校验（workspace 相对路径、防穿越、目录可建）
# ---------------------------------------------------------------------------


def test_normalize_project_path(tmp_path):
    cfg = _cfg(tmp_path)
    env_id = "ef" * 16

    assert normalize_project_path(cfg, env_id, None) == ""
    assert normalize_project_path(cfg, env_id, "  ") == ""
    assert normalize_project_path(cfg, env_id, "projects/report-2026Q3") == "projects/report-2026Q3"
    assert normalize_project_path(cfg, env_id, "/leading/slash/") == "leading/slash"
    assert (cfg.workspace(env_id) / "projects" / "report-2026Q3").is_dir()

    for bad in ("../escape", "a/../../b", ".hidden/x", "/.."):
        with pytest.raises(ValidationError):
            normalize_project_path(cfg, env_id, bad)


# ---------------------------------------------------------------------------
# orchestrator：project_path 前缀注入（与重放前缀同一机制）
# ---------------------------------------------------------------------------


def test_build_blocks_injects_project_prefix(tmp_path):
    orch = PromptOrchestrator(None, None, None)
    ctx = PromptContext(
        env_id="e",
        tenant_id=None,
        tenant_name="T",
        user_id="u",
        user_display_name="U",
        session_id="s",
        session_title="t",
        scope_space_ids=[],
        question="帮我写个周报",
        attachments=[],
        project_path="projects/report",
    )
    blocks = orch._build_blocks(ctx, replay_note="历史")
    kinds = [b["type"] for b in blocks]
    assert kinds == ["text", "text", "text"]
    assert "历史" in blocks[0]["text"]
    assert "projects/report" in blocks[1]["text"]
    assert blocks[2]["text"] == "帮我写个周报"

    ctx.project_path = ""
    assert [b["text"] for b in orch._build_blocks(ctx, "")] == ["帮我写个周报"]
