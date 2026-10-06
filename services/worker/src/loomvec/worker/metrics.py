"""Worker 侧 Prometheus 指标（prefork 多进程模式聚合导出）。

Celery prefork 下任务在子进程执行，进程内计数对主进程不可见；celery_app.py 在
worker 导入本模块之前设置 PROMETHEUS_MULTIPROC_DIR（默认每次启动新建目录，重启即清零；
外部预设则沿用），子进程计数落盘，主进程的指标端口经 MultiProcessCollector 聚合读取。
worker 进程在该环境变量设置前不得导入 prometheus_client（含本模块），
否则计数退回进程内、聚合恒为空。
"""

from __future__ import annotations

from prometheus_client import Counter

from loomvec.core.logging import get_logger

logger = get_logger("loomvec.worker.metrics")

pipeline_jobs_total = Counter(
    "loomvec_pipeline_jobs_total",
    "管线任务单次执行结果计数（succeeded/failed/retrying；PipelineTask 任务：入库管线与懒转码）",
    ["status"],
)


def record_pipeline_job(status: str) -> None:
    """计数一次管线任务执行结果。多进程模式下计数写盘，失败只告警，不改变任务结果。"""
    try:
        pipeline_jobs_total.labels(status=status).inc()
    except Exception as e:
        logger.warning("pipeline_metric_record_failed", status=status, error=str(e))
