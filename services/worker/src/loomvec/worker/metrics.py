"""Worker 侧 Prometheus 指标（prefork 多进程模式聚合导出）。

Celery prefork 下任务在子进程执行，进程内计数对主进程不可见；celery_app.py 在
worker 启动时设置 PROMETHEUS_MULTIPROC_DIR（每次启动独立目录，重启即清零），
子进程计数落盘，主进程的指标端口经 MultiProcessCollector 聚合读取。
"""

from __future__ import annotations

from prometheus_client import Counter

pipeline_jobs_total = Counter(
    "loomvec_pipeline_jobs_total",
    "管线任务执行结果计数（按最终状态；含转码/合并/社区等 PipelineTask 任务）",
    ["status"],
)
