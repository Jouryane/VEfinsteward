"""
VE5 RAG 同步引擎
================
从已有数据源（workflow 产出物）扫描并同步到 SQLite RAG 存储。
完全独立于 workflow，定时触发 + 手动触发。

数据源：
    - 财务OCR：userdata/screenshot_descriptions/*.txt + screenshot_descriptions.jsonl
    - 消费价格：userdata/consumer_prices.jsonl（新增的降级存储）
    - 研报：userdata/rag_reports.jsonl
"""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Dict, Any, List
from datetime import datetime

logger = logging.getLogger("ve5.rag_sync")

from app_paths import DATA_DIR, LOG_DIR
from core.rag_sqlite_store import (
    fin_store, fin_stats, fin_list,
    price_store, price_stats, price_list,
    report_store, report_stats, report_list,
    expense_store, expense_stats,
    log_sync_start, log_sync_finish, get_last_sync, get_all_sync_status,
    rag_sqlite_init,
)

# 确保表存在
rag_sqlite_init()

# 同步间隔（秒）
SYNC_INTERVAL = 300  # 5 分钟

# 数据源路径
_DESC_DIR = DATA_DIR / "screenshot_descriptions"
_DESC_JSONL = LOG_DIR / "screenshot_descriptions.jsonl"
_PRICE_JSONL = DATA_DIR / "consumer_prices.jsonl"
_REPORT_JSONL = DATA_DIR / "rag_reports.jsonl"


def _sync_financial() -> Dict[str, Any]:
    """扫描截图描述文件，同步到 SQLite"""
    log_id = log_sync_start("financial_rag")
    new_count = 0
    error = ""

    try:
        # 优先读 JSONL（结构化）
        sources = []

        if _DESC_JSONL.exists():
            for line in _DESC_JSONL.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                    sources.append(obj)
                except json.JSONDecodeError:
                    continue

        # 补充扫描 txt 文件（可能 JSONL 未覆盖）
        if _DESC_DIR.exists():
            for txt_file in sorted(_DESC_DIR.glob("*.txt")):
                # 避免重复：检查是否已有同 source_file 的 JSONL 条目
                fname = txt_file.name
                if any(s.get("file_name", "").endswith(fname) or s.get("source_file", "").endswith(fname)
                       for s in sources):
                    continue
                text = txt_file.read_text(encoding="utf-8").strip()
                if text:
                    sources.append({
                        "source_file": fname,
                        "description": text,
                        "ocr_text": text,
                    })

        # 写入 SQLite
        for s in sources:
            ok = fin_store(
                source_file=s.get("source_file") or s.get("file_name", "unknown"),
                ocr_text=s.get("ocr_text") or s.get("description") or "",
                description=s.get("description") or "",
                snapshot_type=s.get("snapshot_type", ""),
                holdings_count=s.get("holdings_count", 0),
                total_assets=s.get("total_assets", 0),
                ocr_engine=s.get("ocr_engine", ""),
                parsed_at=s.get("parsed_at") or s.get("timestamp", ""),
            )
            if ok:
                new_count += 1

    except Exception as e:
        error = str(e)
        logger.error(f"[RAG-SYNC] 财务同步失败: {e}")

    stats = fin_stats()
    total = stats.get("total_records", 0)

    # ── 结构化持仓补充：将 asset_holdings 中的产品名注入 RAG 搜索索引 ──
    try:
        import sqlite3
        from app_paths import DB_PATH
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        products = conn.execute(
            "SELECT DISTINCT product_name FROM asset_holdings WHERE current_value > 0 AND is_superseded=0"
        ).fetchall()
        conn.close()
        if products:
            names = " | ".join(r["product_name"] for r in products if r["product_name"])
            # 更新最后一条 RAG 记录的 ocr_text 以包含产品名
            from core.rag_sqlite_store import _get_conn as _rag_conn
            rc = _rag_conn()
            last = rc.execute("SELECT id, ocr_text FROM rag_financial_texts ORDER BY id DESC LIMIT 1").fetchone()
            if last and names:
                merged = (last["ocr_text"] or "") + "\n[持仓产品] " + names
                rc.execute("UPDATE rag_financial_texts SET ocr_text=? WHERE id=?", (merged, last["id"]))
                # 同步 FTS
                from core.rag_sqlite_store import _build_fts_query as _bfq
                rc.execute("DELETE FROM rag_financial_fts WHERE rowid=?", (last["id"],))
                rc.execute("INSERT INTO rag_financial_fts(rowid, source_file, ocr_text, description) VALUES (?,?,?,?)",
                           (last["id"], "products_summary", merged, "持仓产品汇总"))
                rc.commit()
            rc.close()
            logger.info(f"[RAG-SYNC] 已注入 {len(products)} 个持仓产品名到 RAG 索引")
    except Exception as e:
        logger.warning(f"[RAG-SYNC] 持仓名注入失败（非关键）: {e}")

    log_sync_finish(log_id, new_count, total, error)
    logger.info(f"[RAG-SYNC] 财务RAG同步完成: 新增 {new_count} 条, 总计 {total} 条")
    return {"module": "financial_rag", "new_count": new_count, "total": total, "error": error}


def _sync_prices() -> Dict[str, Any]:
    """扫描消费价格 JSONL，同步到 SQLite"""
    log_id = log_sync_start("consumer_price_rag")
    new_count = 0
    error = ""

    try:
        if not _PRICE_JSONL.exists():
            # 确保 JSONL 文件存在（供 API 写入）
            _PRICE_JSONL.parent.mkdir(parents=True, exist_ok=True)
            _PRICE_JSONL.touch()

        for line in _PRICE_JSONL.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                p = json.loads(line)
                ok = price_store(
                    item=p.get("item", ""),
                    price=p.get("price", 0),
                    spec=p.get("spec", ""),
                    merchant=p.get("merchant", ""),
                    location=p.get("location", ""),
                    address=p.get("address", ""),
                    category=p.get("category", ""),
                    source=p.get("source", "manual"),
                    record_date=p.get("date") or p.get("record_date", ""),
                )
                if ok:
                    new_count += 1
            except json.JSONDecodeError:
                continue

    except Exception as e:
        error = str(e)
        logger.error(f"[RAG-SYNC] 价格同步失败: {e}")

    stats = price_stats()
    total = stats.get("total_records", 0)
    log_sync_finish(log_id, new_count, total, error)
    logger.info(f"[RAG-SYNC] 价格RAG同步完成: 新增 {new_count} 条, 总计 {total} 条")
    return {"module": "consumer_price_rag", "new_count": new_count, "total": total, "error": error}


def _sync_reports() -> Dict[str, Any]:
    """扫描研报 JSONL，同步到 SQLite"""
    log_id = log_sync_start("report_rag")
    new_count = 0
    error = ""

    try:
        if not _REPORT_JSONL.exists():
            logger.info("[RAG-SYNC] 研报JSONL不存在，跳过")
        else:
            for line in _REPORT_JSONL.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                    ok = report_store(
                        report_id=r.get("report_id") or r.get("id", f"rpt_{hash(line) % 100000}"),
                        title=r.get("title", ""),
                        content=r.get("content") or r.get("summary") or "",
                        chunk_type=r.get("chunk_type", "full"),
                        source_url=r.get("source_url", ""),
                        parsed_at=r.get("parsed_at") or r.get("created_at", ""),
                    )
                    if ok:
                        new_count += 1
                except json.JSONDecodeError:
                    continue

    except Exception as e:
        error = str(e)
        logger.error(f"[RAG-SYNC] 研报同步失败: {e}")

    stats = report_stats()
    total = stats.get("total_records", 0)
    log_sync_finish(log_id, new_count, total, error)
    logger.info(f"[RAG-SYNC] 研报RAG同步完成: 新增 {new_count} 条, 总计 {total} 条")
    return {"module": "report_rag", "new_count": new_count, "total": total, "error": error}


def _sync_expenses() -> Dict[str, Any]:
    """扫描 transactions 表中的消费支出，同步到 SQLite"""
    log_id = log_sync_start("financial_rag_expense")
    new_count = 0
    error = ""

    try:
        import sqlite3
        from app_paths import DB_PATH
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM transactions WHERE transaction_type='expense' ORDER BY transaction_date DESC"
        ).fetchall()
        conn.close()

        for r in rows:
            ok = expense_store(
                transaction_date=r["transaction_date"] or r["transaction_time"] or "",
                transaction_type=r["transaction_type"] or "expense",
                amount=r["amount"] or 0,
                counterparty=r["counterparty"] or "",
                category_primary=r["category_primary"] or "",
                category_secondary=r["category_secondary"] or "",
                is_essential=bool(r["is_essential"]),
                description=r["description"] or "",
                source_file=r["source_file"] or "",
            )
            if ok:
                new_count += 1

    except Exception as e:
        error = str(e)
        logger.error(f"[RAG-SYNC] 消费支出同步失败: {e}")

    stats = expense_stats()
    total = stats.get("total_records", 0)
    log_sync_finish(log_id, new_count, total, error)
    logger.info(f"[RAG-SYNC] 消费支出同步完成: 新增 {new_count} 条, 总计 {total} 条")
    return {"module": "financial_rag_expense", "new_count": new_count, "total": total, "error": error}


# ════════════════════════════════════════════════
# 公共 API
# ════════════════════════════════════════════════

def rag_sync_all() -> List[Dict[str, Any]]:
    """同步所有活跃模块（手动触发用）"""
    results = []
    results.append(_sync_financial())
    results.append(_sync_expenses())
    results.append(_sync_prices())
    results.append(_sync_reports())
    return results


def rag_sync_module(module_id: str) -> Dict[str, Any]:
    """同步单个模块"""
    if module_id == "financial_rag":
        _sync_financial()
        return _sync_expenses()
    elif module_id == "consumer_price_rag":
        return _sync_prices()
    elif module_id == "report_rag":
        return _sync_reports()
    return {"module": module_id, "error": "未知的模块ID"}


def rag_get_sync_status() -> Dict[str, Any]:
    """获取所有模块的同步状态"""
    statuses = get_all_sync_status()
    result = {}
    for s in statuses:
        result[s["module_id"]] = {
            "last_sync": s.get("finished_at", ""),
            "new_count": s.get("new_count", 0),
            "total_count": s.get("total_count", 0),
            "status": s.get("status", ""),
            "error": s.get("error", ""),
        }
    return result


# ════════════════════════════════════════════════
# 后台定时同步线程
# ════════════════════════════════════════════════

_sync_thread = None
_sync_running = False


def _bg_sync_loop():
    """后台同步循环"""
    global _sync_running
    logger.info(f"[RAG-SYNC] 后台同步线程启动，间隔 {SYNC_INTERVAL}s")
    while _sync_running:
        try:
            rag_sync_all()
        except Exception as e:
            logger.error(f"[RAG-SYNC] 后台同步异常: {e}")
        # 等待下一次
        for _ in range(SYNC_INTERVAL):
            if not _sync_running:
                break
            time.sleep(1)


def rag_start_bg_sync():
    """启动后台同步线程"""
    global _sync_thread, _sync_running
    if _sync_thread and _sync_thread.is_alive():
        return
    _sync_running = True
    _sync_thread = threading.Thread(target=_bg_sync_loop, daemon=True, name="rag-sync")
    _sync_thread.start()
    logger.info("[RAG-SYNC] 后台同步线程已启动")


def rag_stop_bg_sync():
    """停止后台同步线程"""
    global _sync_running
    _sync_running = False


# LLM 关键词联想（搜索时扩展同义词）
def rag_llm_expand_keywords(query: str, module_id: str = "") -> List[str]:
    """用 LLM 扩展搜索关键词的同义词"""
    try:
        from core.llm_gateway import ve5_llm_chat
        prompt = f"""你是金融术语专家。用户搜索"{query}"，请给出3-5个同义或近义的表达（用于全文检索）。
只返回词语，用逗号分隔，不要解释。例如：可用余额,账户余额,活期余额,零钱"""
        resp = ve5_llm_chat(prompt, max_tokens=100)
        if resp:
            keywords = [k.strip() for k in resp.split(",，") if k.strip()]
            keywords.insert(0, query)
            return keywords[:8]
    except Exception:
        pass
    return [query]
