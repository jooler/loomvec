"""Celery 应用：broker/result = Redis。

队列拓扑（P1-WRK-01；P3 扩展）：
- `pipeline`       默认管线队列（parse→chunk→graph→embed→index 整链任务）；
- `pipeline_high`  交互优先队列（单步重跑 / 懒转码等用户等待场景）；
- `pipeline_low`   低优先级队列（实体合并、社区检测等空间级周期任务）。

超时：管线任务 soft 3500s / hard 3600s（大 PDF + 云端分片上限）。
指标：worker.metrics_port 开启 Prometheus 端口（prefork 多进程聚合，见 worker/metrics.py）。
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

from celery import Celery
from celery.signals import import_modules, worker_init
from kombu import Queue

from loomvec.core.config import get_settings
from loomvec.core.constants import QUEUE_PIPELINE, QUEUE_PIPELINE_HIGH, QUEUE_PIPELINE_LOW
from loomvec.core.logging import get_logger, setup_logging

settings = get_settings()
setup_logging(settings.log_level, json_output=settings.log_json)
logger = get_logger("loomvec.worker.celery_app")

# 内部标记 "<pid>:<目录>"：本进程自建的多进程计数目录，用于区分外部预设的 PROMETHEUS_MULTIPROC_DIR
_OWNED_MULTIPROC_DIR_ENV = "_LOOMVEC_WORKER_PROM_MULTIPROC_OWNER"


def _owned_multiproc_dir() -> str | None:
    """本进程自建且仍在用的计数目录。

    execv 不改 PID，SIGHUP 重启后仍认得出；继承了环境变量的其他进程 PID 不同，按外部预设处理。
    """
    pid, sep, path = os.environ.get(_OWNED_MULTIPROC_DIR_ENV, "").partition(":")
    if sep and pid == str(os.getpid()) and path == os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        return path
    return None


def _enable_metrics_multiproc(**_kwargs) -> None:
    """导入任务模块前开启 prometheus 多进程模式（子进程计数落盘到本次启动新建的目录）。

    prometheus_client 首次导入即按 PROMETHEUS_MULTIPROC_DIR 定死计数实现，而 celery 先导入
    include 任务模块（tasks → worker/metrics.py）再发 worker_init，故挂在更早的 import_modules。
    该信号的处理器异常会中止进程启动（worker / beat / shell 同），此处只做建目录与设环境变量。

    外部预设的 PROMETHEUS_MULTIPROC_DIR 原样沿用，不建不删。SIGHUP 重启经 execv 原地重入：
    atexit 清理没执行、环境变量被继承，按自建标记认出旧目录后删掉重建，保证重启即清零。
    """
    if not settings.worker.metrics_port:
        return
    owned = _owned_multiproc_dir()
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR") and not owned:
        return
    if owned:
        shutil.rmtree(owned, ignore_errors=True)
    multiproc_dir = tempfile.mkdtemp(prefix="loomvec-prom-")
    atexit.register(shutil.rmtree, multiproc_dir, ignore_errors=True)
    os.environ["PROMETHEUS_MULTIPROC_DIR"] = multiproc_dir
    os.environ[_OWNED_MULTIPROC_DIR_ENV] = f"{os.getpid()}:{multiproc_dir}"


def _start_metrics_server(**_kwargs) -> None:
    """主进程启动指标端口，经 MultiProcessCollector 聚合各子进程计数。"""
    multiproc_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if not settings.worker.metrics_port or not multiproc_dir:
        return
    from prometheus_client import CollectorRegistry, start_http_server, values
    from prometheus_client.multiprocess import MultiProcessCollector

    if values.ValueClass is values.MutexValue:
        logger.warning(
            "worker_metrics_not_multiprocess",
            detail="prometheus_client 先于 import_modules 被导入，子进程计数不落盘，端口无数据",
        )
    registry = CollectorRegistry()
    MultiProcessCollector(registry, path=multiproc_dir)
    start_http_server(settings.worker.metrics_port, registry=registry)


import_modules.connect(_enable_metrics_multiproc)
worker_init.connect(_start_metrics_server)

celery_app = Celery(
    "loomvec",
    broker=settings.redis.url,
    backend=settings.redis.url,
    include=["loomvec.worker.tasks", "loomvec.worker.webhooks"],
)

celery_app.conf.update(
    task_queues=(
        Queue(QUEUE_PIPELINE),
        Queue(QUEUE_PIPELINE_HIGH),
        Queue(QUEUE_PIPELINE_LOW),
    ),
    task_default_queue=QUEUE_PIPELINE,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,  # 重任务：处理完再取
    result_expires=7 * 24 * 3600,
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_soft_time_limit=settings.worker.task_soft_time_limit,
    task_time_limit=settings.worker.task_time_limit,
    beat_schedule={
        "worker-heartbeat": {
            "task": "worker.heartbeat",
            "schedule": settings.worker.heartbeat_interval_seconds,
        },
        # P4-API-08 Webhook 派发与补投
        "webhook-dispatch": {
            "task": "webhook.dispatch",
            "schedule": 30.0,
        },
        "webhook-retry-due": {
            "task": "webhook.retry_due",
            "schedule": 60.0,
        },
        # P3-CORE-03/WK-02 空间级周期任务（合并 / 社区）
        "graph-merge-scan": {
            "task": "graph.merge_scan",
            "schedule": settings.graph.merge_interval_seconds,
        },
        "graph-community-scan": {
            "task": "graph.community_scan",
            "schedule": settings.graph.community_interval_seconds,
        },
        # 目录监听自动入库（ingest_watch.dir 为空时任务内 no-op）
        "ingest-watch-scan": {
            "task": "ingest.watch_scan",
            "schedule": settings.ingest_watch.poll_interval_seconds,
            # 队列积压时过期丢弃，不堆积重复的扫描任务（下一周期会重新投递）
            "options": {"expires": settings.ingest_watch.poll_interval_seconds},
        },
    },
)
