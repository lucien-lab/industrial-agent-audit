"""ASGI 入口：`uvicorn app.main:app`。"""

from app.api.app import app, create_app

__all__ = ["app", "create_app"]
