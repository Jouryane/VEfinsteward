"""
VE5 RAG SQLite 存储层
=====================
替代 ChromaDB，使用 SQLite + FTS5 实现全文检索。
所有 RAG 模块的数据统一存储在 ve5.db 中，表名以 rag_ 前缀。

设计原则：
    - 零外部依赖（sqlite3 是 Python 内置）
    - exe 完美兼容
    - FTS5 全文检索替代向量语义搜索
    - 搜索时可选 LLM 关键词联想扩展
"""

import sqlite3
import json
import logging
import hashlib
import re
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger("ve5.rag_sqlite")

from app_paths import DATA_DIR, DB_PATH


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def rag_sqlite_init():
    """初始化所有 RAG 表（幂等）"""
    conn = _get_conn()
    try:
        # ── 财务隐私 RAG ──
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rag_financial_texts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_file TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                ocr_text TEXT,
                description TEXT,
                snapshot_type TEXT DEFAULT '',
                holdings_count INTEGER DEFAULT 0,
                total_assets REAL DEFAULT 0,
                ocr_engine TEXT DEFAULT '',
                parsed_at TEXT DEFAULT '',
                synced_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_fin_hash ON rag_financial_texts(content_hash)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fin_source ON rag_financial_texts(source_file)")
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS rag_financial_fts USING fts5(source_file, ocr_text, description)")

        # ── 消费价格 RAG ──
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rag_price_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_hash TEXT NOT NULL,
                item TEXT NOT NULL,
                price REAL DEFAULT 0,
                spec TEXT DEFAULT '',
                merchant TEXT DEFAULT '',
                location TEXT DEFAULT '',
                address TEXT DEFAULT '',
                category TEXT DEFAULT '',
                source TEXT DEFAULT 'manual',
                record_date TEXT DEFAULT '',
                synced_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_price_hash ON rag_price_records(content_hash)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_price_item ON rag_price_records(item)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_price_date ON rag_price_records(record_date)")
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS rag_price_fts USING fts5(item, merchant, location, category, spec)")

        # ── 研报 RAG ──
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rag_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id TEXT NOT NULL,
                title TEXT DEFAULT '',
                content TEXT,
                chunk_type TEXT DEFAULT 'full',
                source_url TEXT DEFAULT '',
                parsed_at TEXT DEFAULT '',
                synced_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_report_rid ON rag_reports(report_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_report_title ON rag_reports(title)")
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS rag_reports_fts USING fts5(title, content)")

        # ── 消费支出 RAG（并入财务隐私 High 等级） ──
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rag_expense_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_hash TEXT NOT NULL,
                transaction_date TEXT DEFAULT '',
                transaction_type TEXT DEFAULT 'expense',
                amount REAL DEFAULT 0,
                counterparty TEXT DEFAULT '',
                category_primary TEXT DEFAULT '',
                category_secondary TEXT DEFAULT '',
                is_essential INTEGER DEFAULT 0,
                description TEXT DEFAULT '',
                source_file TEXT DEFAULT '',
                synced_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_exp_hash ON rag_expense_records(content_hash)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_date ON rag_expense_records(transaction_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_cat ON rag_expense_records(category_primary)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_essential ON rag_expense_records(is_essential)")
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS rag_expense_fts USING fts5(counterparty, category_primary, description)")

        # ── 同步日志 ──
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rag_sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                module_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT DEFAULT 'running',
                new_count INTEGER DEFAULT 0,
                total_count INTEGER DEFAULT 0,
                error TEXT DEFAULT ''
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_module ON rag_sync_log(module_id)")
        conn.commit()
        logger.info("[RAG-SQLite] 表初始化完成")
    finally:
        conn.close()


def _build_fts_query(query: str) -> str:
    """将用户查询转为 FTS5 MATCH 语法"""
    tokens = re.findall(r'[\w\u4e00-\u9fff]+', query)
    if not tokens:
        return query
    return " OR ".join(f'"{t}"' for t in tokens)


# ════════════════════════════════════════════════
# 财务隐私 RAG
# ════════════════════════════════════════════════

def fin_store(source_file: str, ocr_text: str, description: str = "",
              snapshot_type: str = "", holdings_count: int = 0,
              total_assets: float = 0, ocr_engine: str = "",
              parsed_at: str = "") -> bool:
    content_hash = hashlib.md5((source_file + (ocr_text or "")[:500]).encode()).hexdigest()
    now = datetime.now().isoformat()
    conn = _get_conn()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO rag_financial_texts
            (source_file, content_hash, ocr_text, description, snapshot_type,
             holdings_count, total_assets, ocr_engine, parsed_at, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (source_file, content_hash, ocr_text, description, snapshot_type,
              holdings_count, total_assets, ocr_engine, parsed_at or now, now))
        row_id = conn.execute(
            "SELECT id FROM rag_financial_texts WHERE content_hash=?", (content_hash,)
        ).fetchone()
        if row_id:
            conn.execute("DELETE FROM rag_financial_fts WHERE rowid=?", (row_id[0],))
            conn.execute(
                "INSERT INTO rag_financial_fts(rowid, source_file, ocr_text, description) VALUES (?, ?, ?, ?)",
                (row_id[0], source_file, ocr_text or "", description or "")
            )
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"[RAG-SQLite] 财务记录写入失败: {e}")
        return False
    finally:
        conn.close()


def fin_search(query: str, limit: int = 5) -> List[Dict]:
    conn = _get_conn()
    try:
        if not query or not query.strip():
            rows = conn.execute(
                "SELECT * FROM rag_financial_texts ORDER BY synced_at DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            fts_q = _build_fts_query(query)
            rows = conn.execute("""
                SELECT t.* FROM rag_financial_fts f
                JOIN rag_financial_texts t ON t.id = f.rowid
                WHERE rag_financial_fts MATCH ?
                ORDER BY rank LIMIT ?
            """, (fts_q, limit)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"[RAG-SQLite] 财务检索失败: {e}")
        return []
    finally:
        conn.close()


def fin_stats() -> Dict[str, Any]:
    conn = _get_conn()
    try:
        count = conn.execute("SELECT COUNT(*) FROM rag_financial_texts").fetchone()[0]
        return {"available": True, "total_records": count, "backend": "sqlite+fts5"}
    finally:
        conn.close()


def fin_list(limit: int = 20, offset: int = 0) -> Dict[str, Any]:
    conn = _get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM rag_financial_texts").fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM rag_financial_texts ORDER BY synced_at DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["text_preview"] = (d.get("ocr_text") or "")[:200]
            items.append(d)
        return {"records": items, "total": total}
    finally:
        conn.close()


def fin_delete(record_id: int) -> bool:
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM rag_financial_fts WHERE rowid=?", (record_id,))
        conn.execute("DELETE FROM rag_financial_texts WHERE id=?", (record_id,))
        conn.commit()
        return True
    finally:
        conn.close()


def fin_clear():
    """清空全部财务隐私 RAG 数据"""
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM rag_financial_fts")
        conn.execute("DELETE FROM rag_financial_texts")
        conn.commit()
    finally:
        conn.close()


def fin_delete_by_source(source_pattern: str):
    """按 source_file 删除 RAG 记录。source_pattern 如 '%Screenshot_2026_0710%'"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id FROM rag_financial_texts WHERE source_file LIKE ?",
            (source_pattern,)
        ).fetchall()
        for r in rows:
            conn.execute("DELETE FROM rag_financial_fts WHERE rowid=?", (r[0],))
        conn.execute("DELETE FROM rag_financial_texts WHERE source_file LIKE ?", (source_pattern,))
        conn.commit()
    finally:
        conn.close()


# ════════════════════════════════════════════════
# 消费价格 RAG
# ════════════════════════════════════════════════

def price_store(item: str, price: float, spec: str = "", merchant: str = "",
                location: str = "", address: str = "", category: str = "",
                source: str = "manual", record_date: str = "") -> bool:
    content_hash = hashlib.md5(f"{item}_{merchant}_{location}_{record_date}".encode()).hexdigest()
    now = datetime.now().isoformat()
    conn = _get_conn()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO rag_price_records
            (content_hash, item, price, spec, merchant, location, address,
             category, source, record_date, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (content_hash, item, price, spec, merchant, location, address,
              category, source, record_date or datetime.now().strftime("%Y-%m-%d"), now))
        row_id = conn.execute(
            "SELECT id FROM rag_price_records WHERE content_hash=?", (content_hash,)
        ).fetchone()
        if row_id:
            conn.execute("DELETE FROM rag_price_fts WHERE rowid=?", (row_id[0],))
            conn.execute(
                "INSERT INTO rag_price_fts(rowid, item, merchant, location, category, spec) VALUES (?, ?, ?, ?, ?, ?)",
                (row_id[0], item, merchant or "", location or "", category or "", spec or "")
            )
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"[RAG-SQLite] 价格记录写入失败: {e}")
        return False
    finally:
        conn.close()


def price_search(query: str, limit: int = 5, location: str = None,
                 category: str = None, source: str = None) -> List[Dict]:
    conn = _get_conn()
    try:
        if not query or not query.strip():
            sql = "SELECT * FROM rag_price_records WHERE 1=1"
            params: list = []
        else:
            fts_q = _build_fts_query(query)
            sql = """SELECT t.* FROM rag_price_fts f
                     JOIN rag_price_records t ON t.id = f.rowid
                     WHERE rag_price_fts MATCH ?"""
            params = [fts_q]
        if location:
            sql += " AND t.location LIKE ?"
            params.append(f"%{location}%")
        if category:
            sql += " AND t.category = ?"
            params.append(category)
        if source:
            sql += " AND t.source = ?"
            params.append(source)
        sql += " ORDER BY t.record_date DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"[RAG-SQLite] 价格检索失败: {e}")
        return []
    finally:
        conn.close()


def price_stats() -> Dict[str, Any]:
    conn = _get_conn()
    try:
        count = conn.execute("SELECT COUNT(*) FROM rag_price_records").fetchone()[0]
        return {"available": True, "total_records": count, "backend": "sqlite+fts5"}
    finally:
        conn.close()


def price_list(limit: int = 20, offset: int = 0) -> Dict[str, Any]:
    conn = _get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM rag_price_records").fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM rag_price_records ORDER BY record_date DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
        return {"records": [dict(r) for r in rows], "total": total}
    finally:
        conn.close()


def price_recent(n: int = 10, location: str = None) -> List[Dict]:
    conn = _get_conn()
    try:
        if location:
            rows = conn.execute(
                "SELECT * FROM rag_price_records WHERE location LIKE ? ORDER BY record_date DESC LIMIT ?",
                (f"%{location}%", n)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM rag_price_records ORDER BY record_date DESC LIMIT ?", (n,)
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def price_delete(record_id: int) -> bool:
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM rag_price_fts WHERE rowid=?", (record_id,))
        conn.execute("DELETE FROM rag_price_records WHERE id=?", (record_id,))
        conn.commit()
        return True
    finally:
        conn.close()


# ════════════════════════════════════════════════
# 研报 RAG
# ════════════════════════════════════════════════

def report_store(report_id: str, title: str, content: str,
                 chunk_type: str = "full", source_url: str = "",
                 parsed_at: str = "") -> bool:
    now = datetime.now().isoformat()
    conn = _get_conn()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO rag_reports
            (report_id, title, content, chunk_type, source_url, parsed_at, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (report_id, title, content, chunk_type, source_url,
              parsed_at or now, now))
        row_id = conn.execute(
            "SELECT id FROM rag_reports WHERE report_id=?", (report_id,)
        ).fetchone()
        if row_id:
            conn.execute("DELETE FROM rag_reports_fts WHERE rowid=?", (row_id[0],))
            conn.execute(
                "INSERT INTO rag_reports_fts(rowid, title, content) VALUES (?, ?, ?)",
                (row_id[0], title or "", content or "")
            )
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"[RAG-SQLite] 研报写入失败: {e}")
        return False
    finally:
        conn.close()


def report_search(query: str, limit: int = 5) -> List[Dict]:
    conn = _get_conn()
    try:
        if not query or not query.strip():
            rows = conn.execute(
                "SELECT * FROM rag_reports ORDER BY parsed_at DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            fts_q = _build_fts_query(query)
            rows = conn.execute("""
                SELECT t.* FROM rag_reports_fts f
                JOIN rag_reports t ON t.id = f.rowid
                WHERE rag_reports_fts MATCH ?
                ORDER BY rank LIMIT ?
            """, (fts_q, limit)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"[RAG-SQLite] 研报检索失败: {e}")
        return []
    finally:
        conn.close()


def report_stats() -> Dict[str, Any]:
    conn = _get_conn()
    try:
        count = conn.execute("SELECT COUNT(*) FROM rag_reports").fetchone()[0]
        return {"available": True, "total_records": count, "backend": "sqlite+fts5"}
    finally:
        conn.close()


def report_list(limit: int = 20, offset: int = 0) -> Dict[str, Any]:
    conn = _get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM rag_reports").fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM rag_reports ORDER BY parsed_at DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["text_preview"] = (d.get("content") or "")[:200]
            items.append(d)
        return {"records": items, "total": total}
    finally:
        conn.close()


def report_delete(record_id: int) -> bool:
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM rag_reports_fts WHERE rowid=?", (record_id,))
        conn.execute("DELETE FROM rag_reports WHERE id=?", (record_id,))
        conn.commit()
        return True
    finally:
        conn.close()


# ════════════════════════════════════════════════
# 同步日志
# ════════════════════════════════════════════════

def log_sync_start(module_id: str) -> int:
    conn = _get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO rag_sync_log (module_id, started_at, status) VALUES (?, ?, 'running')",
            (module_id, datetime.now().isoformat())
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def log_sync_finish(log_id: int, new_count: int, total_count: int, error: str = ""):
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE rag_sync_log SET finished_at=?, status=?, new_count=?, total_count=?, error=? WHERE id=?",
            (datetime.now().isoformat(), "error" if error else "done",
             new_count, total_count, error, log_id)
        )
        conn.commit()
    finally:
        conn.close()


def get_last_sync(module_id: str) -> Optional[Dict]:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM rag_sync_log WHERE module_id=? AND status='done' ORDER BY finished_at DESC LIMIT 1",
            (module_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_all_sync_status() -> List[Dict]:
    conn = _get_conn()
    try:
        rows = conn.execute("""
            SELECT s.* FROM rag_sync_log s
            INNER JOIN (
                SELECT module_id, MAX(finished_at) as max_time
                FROM rag_sync_log WHERE status='done'
                GROUP BY module_id
            ) latest ON s.module_id = latest.module_id AND s.finished_at = latest.max_time
        """).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ════════════════════════════════════════════════
# 消费支出 RAG（并入财务隐私 High 等级）
# ════════════════════════════════════════════════

def expense_store(transaction_date: str, transaction_type: str, amount: float,
                  counterparty: str, category_primary: str, category_secondary: str = "",
                  is_essential: bool = False, description: str = "", source_file: str = "") -> bool:
    content_hash = hashlib.md5(
        f"{transaction_date}_{counterparty}_{amount}_{description}".encode()
    ).hexdigest()
    now = datetime.now().isoformat()
    conn = _get_conn()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO rag_expense_records
            (content_hash, transaction_date, transaction_type, amount, counterparty,
             category_primary, category_secondary, is_essential, description, source_file, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (content_hash, transaction_date, transaction_type, amount, counterparty,
              category_primary, category_secondary, 1 if is_essential else 0,
              description, source_file, now))
        row_id = conn.execute(
            "SELECT id FROM rag_expense_records WHERE content_hash=?", (content_hash,)
        ).fetchone()
        if row_id:
            conn.execute("DELETE FROM rag_expense_fts WHERE rowid=?", (row_id[0],))
            conn.execute(
                "INSERT INTO rag_expense_fts(rowid, counterparty, category_primary, description) VALUES (?, ?, ?, ?)",
                (row_id[0], counterparty or "", category_primary or "", description or "")
            )
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"[RAG-SQLite] 消费支出记录写入失败: {e}")
        return False
    finally:
        conn.close()


def expense_search(query: str, limit: int = 5, is_essential: bool = None) -> List[Dict]:
    conn = _get_conn()
    try:
        # 检测 support_date 列（兼容旧 schema 可能用 transaction_time）
        cols = [c[1] for c in conn.execute("PRAGMA table_info(rag_expense_records)").fetchall()]
        date_col = "transaction_date" if "transaction_date" in cols else "transaction_time"
        if not query or not query.strip():
            sql = f"SELECT * FROM rag_expense_records WHERE 1=1"
            params: list = []
        else:
            fts_q = _build_fts_query(query)
            sql = f"""SELECT r.* FROM rag_expense_fts f
                     JOIN rag_expense_records r ON r.id = f.rowid
                     WHERE rag_expense_fts MATCH ?"""
            params = [fts_q]
        if is_essential is not None:
            sql += " AND is_essential = ?"
            params.append(1 if is_essential else 0)
        sql += f" ORDER BY {date_col} DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"[RAG-SQLite] 消费支出检索失败: {e}")
        return []
    finally:
        conn.close()


def expense_stats() -> Dict[str, Any]:
    conn = _get_conn()
    try:
        count = conn.execute("SELECT COUNT(*) FROM rag_expense_records").fetchone()[0]
        essential = conn.execute("SELECT COUNT(*) FROM rag_expense_records WHERE is_essential=1").fetchone()[0]
        return {"available": True, "total_records": count, "backend": "sqlite+fts5", "essential_count": essential, "elastic_count": count - essential}
    finally:
        conn.close()


def expense_list(limit: int = 20, offset: int = 0) -> Dict[str, Any]:
    conn = _get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM rag_expense_records").fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM rag_expense_records ORDER BY transaction_date DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
        return {"records": [dict(r) for r in rows], "total": total}
    finally:
        conn.close()


def expense_delete(record_id: int) -> bool:
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM rag_expense_fts WHERE rowid=?", (record_id,))
        conn.execute("DELETE FROM rag_expense_records WHERE id=?", (record_id,))
        conn.commit()
        return True
    finally:
        conn.close()


# 模块加载时自动初始化
rag_sqlite_init()
