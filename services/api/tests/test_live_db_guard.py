"""线上库硬防护的判定逻辑（conftest.pytest_configure 调用，见 _live_db_guard）。"""

from __future__ import annotations

import pytest
from _live_db_guard import live_db_violation

LIVE = "postgresql+asyncpg://loomvec:loomvec@localhost:35433/loomvec"


def test_live_port_listening_is_rejected():
    reason = live_db_violation(LIVE, listening=lambda *_: True)
    assert reason is not None and "35433" in reason


def test_live_port_not_listening_has_nothing_to_pollute():
    assert live_db_violation(LIVE, listening=lambda *_: False) is None


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://loomvec:loomvec@127.0.0.1:49153/loomvec",  # testcontainers 随机端口
        "postgresql+asyncpg://x:x@127.0.0.1:1/x",  # 刻意不可达
    ],
)
def test_temp_db_allowed(url):
    assert live_db_violation(url, listening=lambda *_: True) is None


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://u:p@10.0.0.5:5432/loomvec",
        "postgresql+asyncpg://u:p@db.internal:35433/loomvec",
    ],
)
def test_remote_host_always_rejected(url):
    assert live_db_violation(url, listening=lambda *_: False) is not None
