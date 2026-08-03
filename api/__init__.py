"""
VE4 API 模块
===========
导出：
    from ve4.api import app  → FastAPI 应用实例
"""

from .ve4_api_server import app

__all__ = ["app"]
