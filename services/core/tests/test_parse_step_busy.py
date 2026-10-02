"""ParseStep 在 MinerU 名额满时的早退路径：不下载原文件（P1-WRK-02 效率修复）。

64 个 worker 进程抢 1 个 MinerU 名额、每 30s 一轮重试的场景下，若每轮都先把原文件
（可达数 MB）从对象存储拉一遍再去发现名额已满，纯属浪费 I/O——ParseStep 改为先按
asset.mime_type 探测 deps.mineru.likely_busy()，命中直接抛 MineruBusyError，
不触达 storage.get_object。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from loomvec.core.config import Settings
from loomvec.core.db.models import Asset, AssetVersion
from loomvec.core.mineru_slots import MineruBusyError
from loomvec.core.pipeline.base import PipelineDeps
from loomvec.core.pipeline.steps import ParseStep


class _BusyMineru:
    async def likely_busy(self) -> bool:
        return True


class _IdleMineru:
    async def likely_busy(self) -> bool:
        return False


class _SpyStorage:
    def __init__(self) -> None:
        self.get_object_calls = 0

    async def get_object(self, *_args, **_kwargs):
        self.get_object_calls += 1
        raise AssertionError("不应在名额探测为 busy 时下载原文件")


class _FakeSession:
    """inline_text 路径末尾的 session.flush()：本测试不落库，占位即可。"""

    async def flush(self) -> None:
        return None


def _deps(mineru, storage) -> PipelineDeps:
    return PipelineDeps(
        settings=Settings(_env_file=None),
        session_factory=None,
        storage=storage,
        ai=None,
        mineru=mineru,
        milvus=None,
    )


def _pdf_version() -> tuple[Asset, AssetVersion]:
    asset = Asset(
        space_id=uuid.uuid4(),
        name="busy.pdf",
        mime_type="application/pdf",
        ext=".pdf",
        size_bytes=1,
    )
    version = AssetVersion(
        asset_id=asset.id,
        version=1,
        mime_type="application/pdf",
        size_bytes=1,
        storage_key="raw/busy.pdf",
    )
    return asset, version


def test_parse_step_skips_download_when_mineru_busy():
    storage = _SpyStorage()
    asset, version = _pdf_version()
    step = ParseStep()
    step._deps = _deps(_BusyMineru(), storage)

    with pytest.raises(MineruBusyError):
        asyncio.run(step.run(_FakeSession(), asset, version))
    assert storage.get_object_calls == 0


def test_parse_step_busy_check_skipped_for_non_mineru_mime():
    # inline_text 路径（如纯文本直读）无需 MinerU，不受名额探测影响
    storage = _SpyStorage()
    asset = Asset(
        space_id=uuid.uuid4(),
        name="x.md",
        mime_type="text/markdown",
        ext=".md",
        size_bytes=1,
        asset_meta={},
    )
    version = AssetVersion(
        asset_id=asset.id,
        version=1,
        mime_type="text/markdown",
        size_bytes=1,
        inline_text="正文",
        version_meta={},
    )
    step = ParseStep()
    step._deps = _deps(_BusyMineru(), storage)

    result = asyncio.run(step.run(_FakeSession(), asset, version))
    assert result["lines"] == 1
    assert storage.get_object_calls == 0
