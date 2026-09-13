"""P0-INF-04 OpenTelemetry trace 骨架。

未配置 `LOOMVEC_otel__ENDPOINT` 时仅安装必要的传播器（noop provider），
不引入导出开销；api/worker 在启动时调用 `setup_tracing`。
instrumentation（FastAPI/SQLAlchemy/httpx）由 api 侧按依赖安装。
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from loomvec.core.config import Settings


def setup_tracing(settings: Settings) -> trace.TracerProvider:
    resource = Resource.create(
        {"service.name": settings.service_name, "deployment.environment": settings.env.value}
    )
    provider = TracerProvider(resource=resource)

    if settings.otel.endpoint:
        exporter = OTLPSpanExporter(endpoint=settings.otel.endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)
    return provider
