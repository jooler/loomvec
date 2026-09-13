"""P0-INF-04 结构化日志（JSON）与请求 ID 贯穿。

- structlog 输出 JSON（`LOOMVEC_LOG_JSON=false` 时切 console 渲染）；
- `request_id` 通过 contextvar 绑定，API 中间件写入、日志与 trace 自动携带；
- 事件名取 `event` 字段，其余为结构化键值，供 Loki/采集器消费。
"""

from __future__ import annotations

import contextvars
import logging
import sys
import uuid

import structlog

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def new_request_id() -> str:
    return uuid.uuid4().hex


def bind_request_id(request_id: str | None = None) -> str:
    rid = request_id or new_request_id()
    request_id_var.set(rid)
    return rid


def _add_request_id(
    logger: structlog.types.WrappedLogger,
    method_name: str,
    event_dict: structlog.types.EventDict,
) -> structlog.types.EventDict:
    event_dict.setdefault("request_id", request_id_var.get())
    return event_dict


def setup_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=log_level)

    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _add_request_id,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json_output
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.stdlib.BoundLogger,
        # stdlib LoggerFactory：add_logger_name 需要 .name 属性，且与 uvicorn 日志统一
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # 让 uvicorn / sqlalchemy 等标准库日志也走统一格式
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = [logging.NullHandler()]
        logging.getLogger(name).propagate = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
