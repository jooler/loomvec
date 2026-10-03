"""process_asset 遇 MinerU 名额满：延后作为新任务重新入队，不计失败、不占 autoretry 次数。"""

from __future__ import annotations

import uuid

from loomvec.core.constants import QUEUE_PIPELINE
from loomvec.core.mineru_slots import MineruBusyError
from loomvec.worker import tasks


def test_process_asset_defers_when_mineru_busy(monkeypatch):
    class _Deps:
        redis = None

    async def fake_build_deps():
        return _Deps()

    async def fake_dispose(_deps):
        return None

    class _BusyRunner:
        def __init__(self, _deps):
            pass

        async def run(self, *_args, **_kwargs):
            raise MineruBusyError("MinerU 解析名额已满")

    sent: list[dict] = []
    monkeypatch.setattr(tasks, "build_deps", fake_build_deps)
    monkeypatch.setattr(tasks, "_dispose_deps", fake_dispose)
    monkeypatch.setattr(tasks, "PipelineRunner", _BusyRunner)
    monkeypatch.setattr(tasks.process_asset, "apply_async", lambda **kw: sent.append(kw))

    asset_id = str(uuid.uuid4())
    result = tasks.process_asset.apply(args=[asset_id, "parse"]).get()

    assert result == {"asset_id": asset_id, "from_step": "parse", "deferred": "mineru_busy"}
    assert len(sent) == 1
    assert sent[0]["args"] == [asset_id, "parse"]
    assert sent[0]["queue"] == QUEUE_PIPELINE
    assert sent[0]["countdown"] == tasks.get_settings().mineru.busy_retry_seconds


def test_process_asset_forwards_run_overrides(monkeypatch):
    # retry 带入的一次性覆盖（chunk_strategy/graph_extraction）原样透传 PipelineRunner.run，
    # 名额满延后重入队时也不丢
    class _Deps:
        redis = None

    async def fake_build_deps():
        return _Deps()

    async def noop(*_args, **_kwargs):
        return None

    calls: list[dict] = []

    class _Runner:
        busy = False

        def __init__(self, _deps):
            pass

        async def run(self, asset_id, from_step, **kwargs):
            calls.append({"from_step": from_step, **kwargs})
            if _Runner.busy:
                raise MineruBusyError("MinerU 解析名额已满")
            return []

    sent: list[dict] = []
    monkeypatch.setattr(tasks, "build_deps", fake_build_deps)
    monkeypatch.setattr(tasks, "_dispose_deps", noop)
    monkeypatch.setattr(tasks, "_maybe_trigger_merge", noop)
    monkeypatch.setattr(tasks, "PipelineRunner", _Runner)
    monkeypatch.setattr(tasks.process_asset, "apply_async", lambda **kw: sent.append(kw))

    asset_id = str(uuid.uuid4())
    overrides = {"chunk_strategy": "llm", "graph_extraction": "on"}
    tasks.process_asset.apply(args=[asset_id, "chunk"], kwargs=overrides).get()
    assert calls[-1] == {"from_step": "chunk", "attempt": 1, **overrides}

    _Runner.busy = True
    tasks.process_asset.apply(args=[asset_id, "chunk"], kwargs=overrides).get()
    assert sent[-1]["args"] == [asset_id, "chunk"]
    assert sent[-1]["kwargs"] == overrides
