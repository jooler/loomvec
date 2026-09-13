"""P1-QA-02 管线集成测试：文本摄取 → parse/chunk/embed/index → 混合检索命中。

依赖 compose 基础设施栈（PG 5433 / Milvus 19530 / RustFS 9000 / Redis 6379）
与已执行迁移（make migrate）。AI 网关强制 mock（离线确定性）。Docker 不可用自动跳过。
"""

from __future__ import annotations

import socket
import uuid

import pytest

from loomvec.core.config import Settings
from loomvec.core.db.models import Asset, AssetStatus, AssetVersion
from loomvec.core.pipeline import PipelineDeps, PipelineRunner
from loomvec.core.retrieval import Retriever, SemanticHit

INTEGRATION_ENV = [
    ("127.0.0.1", 5433),  # postgres
    ("127.0.0.1", 19530),  # milvus
    ("127.0.0.1", 9000),  # rustfs
]


def _stack_ready() -> bool:
    return all(_port_open(h, p) for h, p in INTEGRATION_ENV)


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _stack_ready(), reason="compose 基础设施栈未运行"),
]

SAMPLE_MD = """# 采集器操作手册（集成测试样例）

## 设备启动
QX-7 型采集器开机前需确认电源电压 220V，接地良好。\n首次启动会进行自检，指示灯呈绿色为正常。

## 校准流程
每 90 天执行一次零点校准：进入维护菜单，选择"零点校准"，等待 3 分钟提示完成。校准期间禁止采样。

## 故障排查
错误码 E-41 表示温度传感器断路；E-52 表示存储卡写入失败，需更换工业级 SD 卡。

<table><tr><th>错误码</th><th>含义</th></tr><tr><td>E-41</td><td>温度传感器断路</td></tr><tr><td>E-52</td><td>存储卡写入失败</td></tr></table>

## 维护周期
滤芯每 6 个月更换一次；密封圈每年检查；整机大修周期为 3 年。
"""


@pytest.fixture
def deps() -> PipelineDeps:
    settings = Settings()
    settings.env = settings.env.__class__.TEST
    settings.postgres.url = "postgresql+asyncpg://loomvec:loomvec@localhost:5433/loomvec"
    settings.ai.mock = True
    from loomvec.core.ai import AiGateway
    from loomvec.core.db.base import create_engine_and_sessionmaker
    from loomvec.core.mineru_client import MineruClient
    from loomvec.core.retrieval import MilvusStore
    from loomvec.core.storage import ObjectStorage

    _engine, session_factory = create_engine_and_sessionmaker(settings.postgres)
    return PipelineDeps(
        settings=settings,
        session_factory=session_factory,
        storage=ObjectStorage(settings.storage),
        ai=AiGateway(settings.ai),
        mineru=MineruClient(settings.mineru),
        milvus=MilvusStore(settings.milvus, settings.ai),
    )


async def test_full_pipeline_and_retrieval(deps: PipelineDeps):
    pytest.importorskip("sqlalchemy")
    DEFAULT_SPACE = uuid.UUID("0198bec0-0000-7000-8000-000000000001")

    # ---- 登记：文本资产（inline_text）----
    async with deps.session_factory() as session, session.begin():
        try:
            await session.execute(__import__("sqlalchemy").text("SELECT 1 FROM asset LIMIT 1"))
        except Exception as e:
            pytest.skip(f"迁移未执行（先 make migrate）：{e}")
        asset = Asset(
            space_id=DEFAULT_SPACE,
            name="QX-7采集器操作手册.md",
            mime_type="text/markdown",
            ext=".md",
            size_bytes=len(SAMPLE_MD.encode()),
        )
        session.add(asset)
        await session.flush()
        session.add(
            AssetVersion(
                asset_id=asset.id,
                version=1,
                mime_type=asset.mime_type,
                size_bytes=asset.size_bytes,
                inline_text=SAMPLE_MD,
            )
        )
        asset_id = asset.id

    # ---- 全链路：parse → chunk → embed → index ----
    runner = PipelineRunner(deps)
    results = await runner.run(asset_id)
    # 样例切分预期：正文文本片 ≥2（表格前后）+ 表格独立片 1
    assert results[-1]["indexed"] == results[-2]["embedded"] >= 3
    async with deps.session_factory() as session:
        from sqlalchemy import select

        from loomvec.core.db.models import SemanticUnit, UnitType

        units = (
            (await session.execute(select(SemanticUnit).where(SemanticUnit.asset_id == asset_id)))
            .scalars()
            .all()
        )
        assert any(u.unit_type == UnitType.TABLE for u in units), "表格未独立成片"
        assert all(u.locator.get("start_line") for u in units), "locator 缺失行号"

    async with deps.session_factory() as session:
        from sqlalchemy import select

        asset = (await session.execute(select(Asset).where(Asset.id == asset_id))).scalar_one()
        assert asset.status == AssetStatus.READY

    # ---- 混合检索：语义 + 精确词两路 ----
    retriever = Retriever(deps.milvus, deps.ai, deps.settings.search)
    hits: list[SemanticHit] = await retriever.search(
        space_ids=[DEFAULT_SPACE], query="采集器校准周期是多久"
    )
    assert hits, "检索无结果"
    assert any("零点校准" in h.text for h in hits), "语义命中失败"

    hits_bm25 = await retriever.search(space_ids=[DEFAULT_SPACE], query="E-41")
    assert any("E-41" in h.text for h in hits_bm25), "BM25 精确词命中失败"

    # locator 由 API 服务层从 PG 补全（unit 行已断言 start_line 存在，见上）

    # rerank 开关 A/B：两种模式均有结果（质量对比在 evals 回归中量化）
    hits_norr = await retriever.search(space_ids=[DEFAULT_SPACE], query="校准流程", rerank=False)
    assert hits_norr
