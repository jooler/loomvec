"""uvicorn 入口：`uvicorn loomvec.api.main:app`。"""

from loomvec.api.app import app

__all__ = ["app"]
