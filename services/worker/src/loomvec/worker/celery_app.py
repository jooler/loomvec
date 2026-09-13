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
from celery.signals import worker_init
from kombu import Queue

from loomvec.core.config import get_settings
from loomvec.core.constants import QUEUE_PIPELINE, QUEUE_PIPELINE_HIGH, QUEUE_PIPELINE_LOW
from loomvec.core.logging import setup_logging

settings = get_settings()
setup_logging(settings.log_level, json_output=settings.log_json)


def _start_metrics_server(**_kwargs) -> None:
    """主进程启动指标端口；子进程计数经 PROMETHEUS_MULTIPROC_DIR 聚合。"""
    port = settings.worker.metrics_port
    if not port:
        return
    multiproc_dir = tempfile.mkdtemp(prefix="loomvec-prom-")
    os.environ["PROMETHEUS_MULTIPROC_DIR"] = multiproc_dir
    from prometheus_client import CollectorRegistry, MultiProcessCollector, start_http_server

    registry = CollectorRegistry()
    MultiProcessCollector(registry)
    start_http_server(port, registry=registry)
    atexit.register(shutil.rmtree, multiproc_dir, ignore_errors=True)


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
    },
)
