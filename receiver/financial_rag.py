"""
VE5 财务隐私 RAG 模块（SQLite 路线）
=====================================
早期版本依赖 ChromaDB 向量存储，现已全面迁移到 SQLite+FTS5
方案（core/rag_sqlite_store.py + core/rag_sync_engine.py）。

本模块保留为兼容性薄封装，实际存储经过 rag_sync_engine
写入 ve5.db 的 rag_financial_texts 表。

用法不变（向后兼容）：
    from receiver.financial_rag import ve4_financial_rag_store
    ve4_financial_rag_store(ocr_text, source_file, parse_result, engine_info)
"""

import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict

logger = logging.getLogger("ve5.financial_rag")

from app_paths import DATA_DIR


def ve4_financial_rag_store(
    ocr_text: str,
    source_file: str,
    parse_result: Dict,
    engine_info: Dict,
):
    """
    存储 OCR 文本到 SQLite RAG。
    实际写入由 rag_sync_engine 后台扫描完成，
    这里只做 JSONL 日志留存。
    """
    try:
        log_dir = DATA_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "screenshot_descriptions.jsonl"

        record = {
            "source_file": source_file,
            "engine": engine_info.get("engine", "unknown"),
            "snapshot_type": parse_result.get("snapshot_type", "unknown"),
            "holdings_count": len(parse_result.get("holdings", [])),
            "total_assets": parse_result.get("account_summary", {}).get("total_assets", 0),
            "format": "ocr",
            "date": datetime.now().isoformat(),
        }
        with open(log_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"[FIN-RAG] 日志写入异常: {e}")


def ve4_financial_rag_search(query: str, n_results: int = 5):
    """已废弃：请使用 core.rag_sqlite_store.fin_search()"""
    return []


def ve4_financial_rag_stats():
    """已废弃：请使用 core.rag_sqlite_store.fin_stats()"""
    return {"available": False, "backend": "sqlite_deprecated_thin_wrapper"}


def ve4_financial_rag_clear():
    """已废弃：请通过 RAG 管理页面操作"""
    pass


def ve4_financial_rag_delete(file_name: str):
    """已废弃：请通过 RAG 管理页面操作"""
    pass
