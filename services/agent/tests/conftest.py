"""agent 测试与本机状态隔离：网关凭据文件回落读 $XDG_DATA_HOME/acp-gateway，
开发机上真有本地网关时不能让它的 key 渗进「未配置」的断言。"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_local_gateway_state(monkeypatch: pytest.MonkeyPatch, tmp_path_factory) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path_factory.mktemp("xdg")))
    # 线上配置若写了 agent.gateway.state_dir，同样不能指到真实目录
    from loomvec.core.config import get_app_config

    monkeypatch.setattr(get_app_config().agent.gateway, "state_dir", None)
