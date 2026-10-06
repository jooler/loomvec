"""worker 指标：多进程模式须在任务模块导入前开启，子进程计数经主进程端口聚合导出。"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from loomvec.worker import celery_app as celery_app_module
from loomvec.worker import metrics

_DIR_PREFIX = "MULTIPROC_DIR="

# prometheus_client 首次导入即定死计数实现、进程级不可逆，故在独立子进程里走真实启动路径
_SCRIPT = textwrap.dedent(
    f"""
    import os
    import sys
    import urllib.request

    from loomvec.worker.celery_app import celery_app

    assert "prometheus_client" not in sys.modules, "celery_app 不得提前导入 prometheus_client"
    # 与 `celery worker` 同一构造路径（import_modules → 导入任务模块 → worker_init），不连 broker
    celery_app.WorkController(pool_cls="solo", without_mingle=True, without_gossip=True)

    from prometheus_client import values

    from loomvec.worker.metrics import pipeline_jobs_total

    assert values.ValueClass is not values.MutexValue, "任务模块导入时多进程模式未开启"
    print("{_DIR_PREFIX}" + os.environ["PROMETHEUS_MULTIPROC_DIR"], flush=True)

    pid = os.fork()  # 模拟 prefork 子进程执行任务
    if pid == 0:
        code = 1
        try:
            pipeline_jobs_total.labels(status="succeeded").inc()
            code = 0
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0, "子进程计数失败"

    port = os.environ["LOOMVEC_WORKER__METRICS_PORT"]
    body = urllib.request.urlopen(f"http://127.0.0.1:{{port}}/metrics", timeout=5).read().decode()
    assert 'loomvec_pipeline_jobs_total{{status="succeeded"}} 1.0' in body, body
    """
)


def _free_port() -> int:
    # 与 start_http_server 的默认监听地址一致
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def test_child_process_counts_exported_on_metrics_port():
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("PROMETHEUS_MULTIPROC_DIR", celery_app_module._OWNED_MULTIPROC_DIR_ENV)
    }
    env["LOOMVEC_WORKER__METRICS_PORT"] = str(_free_port())

    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT], env=env, capture_output=True, text=True, timeout=60
    )

    # worker_init 处理器的异常被 celery 吞掉只写日志（stdout），根因要看 stdout
    assert proc.returncode == 0, proc.stdout + proc.stderr
    dirs = [ln[len(_DIR_PREFIX) :] for ln in proc.stdout.splitlines() if ln.startswith(_DIR_PREFIX)]
    assert len(dirs) == 1, proc.stdout
    multiproc_dir = Path(dirs[0])
    assert multiproc_dir.name.startswith("loomvec-prom-")
    assert not multiproc_dir.exists(), "进程退出后多进程计数目录应被清理"


@pytest.fixture
def metrics_enabled(monkeypatch):
    monkeypatch.setattr(celery_app_module.settings.worker, "metrics_port", 39808)


def test_sighup_reexec_replaces_inherited_owned_dir(metrics_enabled, monkeypatch, tmp_path):
    """SIGHUP 经 execv 原地重启：继承下来的自建目录删掉重建，计数不跨重启累加。"""
    stale = tmp_path / "loomvec-prom-stale"
    stale.mkdir()
    (stale / "counter_1.db").write_bytes(b"")
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(stale))
    monkeypatch.setenv(celery_app_module._OWNED_MULTIPROC_DIR_ENV, f"{os.getpid()}:{stale}")

    celery_app_module._enable_metrics_multiproc()

    fresh = Path(os.environ["PROMETHEUS_MULTIPROC_DIR"])
    try:
        assert fresh != stale and fresh.is_dir() and not any(fresh.iterdir())
        assert os.environ[celery_app_module._OWNED_MULTIPROC_DIR_ENV] == f"{os.getpid()}:{fresh}"
        assert not stale.exists()
    finally:
        fresh.rmdir()


def test_external_multiproc_dir_kept_as_is(metrics_enabled, monkeypatch, tmp_path):
    (tmp_path / "counter_1.db").write_bytes(b"")
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    monkeypatch.delenv(celery_app_module._OWNED_MULTIPROC_DIR_ENV, raising=False)

    celery_app_module._enable_metrics_multiproc()

    assert os.environ["PROMETHEUS_MULTIPROC_DIR"] == str(tmp_path)
    assert celery_app_module._OWNED_MULTIPROC_DIR_ENV not in os.environ
    assert (tmp_path / "counter_1.db").exists()


def test_inherited_marker_from_other_process_kept_as_external(
    metrics_enabled, monkeypatch, tmp_path
):
    """继承了两个环境变量的其他进程（PID 不同）不得删掉正在运行的 worker 的计数目录。"""
    (tmp_path / "counter_1.db").write_bytes(b"")
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    monkeypatch.setenv(celery_app_module._OWNED_MULTIPROC_DIR_ENV, f"{os.getpid() + 1}:{tmp_path}")

    celery_app_module._enable_metrics_multiproc()

    assert os.environ["PROMETHEUS_MULTIPROC_DIR"] == str(tmp_path)
    assert (tmp_path / "counter_1.db").exists()


def test_marker_without_multiproc_dir_never_deletes_target(metrics_enabled, monkeypatch, tmp_path):
    """只有标记、没有 PROMETHEUS_MULTIPROC_DIR（如误配）：标记指向的目录不得被删，另建新目录。"""
    (tmp_path / "data.bin").write_bytes(b"x")
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    monkeypatch.setenv(celery_app_module._OWNED_MULTIPROC_DIR_ENV, f"{os.getpid()}:{tmp_path}")

    celery_app_module._enable_metrics_multiproc()

    fresh = Path(os.environ["PROMETHEUS_MULTIPROC_DIR"])
    try:
        assert (tmp_path / "data.bin").exists()
        assert fresh != tmp_path and fresh.name.startswith("loomvec-prom-")
    finally:
        fresh.rmdir()


def test_metric_write_failure_does_not_raise(monkeypatch):
    """计数写盘失败（如多进程目录被外部清掉）不得把任务结果改成失败。"""

    class _Broken:
        def labels(self, **_kwargs):
            raise FileNotFoundError("multiproc dir gone")

    monkeypatch.setattr(metrics, "pipeline_jobs_total", _Broken())

    metrics.record_pipeline_job("succeeded")
