"""P0-INF-04 Prometheus 指标。

- api 进程：HTTP 指标 + 运行时 gauge（队列深度/组件健康/空间配额用量，
  由 api/metrics_runtime.py 在抓取时刷新）；`/metrics` 经 app.py 暴露；
- worker 进程：管线任务计数见 worker/metrics.py（多进程模式聚合导出）。
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, make_asgi_app

registry = CollectorRegistry()

http_requests_total = Counter(
    "loomvec_http_requests_total",
    "HTTP 请求总数",
    ["method", "path", "status"],
    registry=registry,
)
http_request_duration_seconds = Histogram(
    "loomvec_http_request_duration_seconds",
    "HTTP 请求耗时",
    ["method", "path"],
    registry=registry,
)

# ---- 运行时 gauge（api /metrics 抓取时刷新；告警规则见 deploy/monitoring/alerts.yml） ----
queue_length = Gauge(
    "loomvec_queue_length",
    "任务队列深度（Redis LLEN）",
    ["queue"],
    registry=registry,
)
dead_letter_size = Gauge(
    "loomvec_dead_letter_size",
    "死信任务列表长度",
    registry=registry,
)
component_up = Gauge(
    "loomvec_component_up",
    "组件健康（1 健康 / 0 降级）",
    ["component"],
    registry=registry,
)
space_usage_ratio = Gauge(
    "loomvec_space_usage_ratio",
    "空间配额用量比（存储量/文件数取较大者；0=不限不计）",
    ["space", "kind"],
    registry=registry,
)

metrics_asgi_app = make_asgi_app(registry=registry)
