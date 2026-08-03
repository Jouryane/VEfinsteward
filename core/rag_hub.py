"""VE5 RAG Hub 统一管理模块
========================
集中管理所有 RAG 子系统，使用 SQLite + FTS5 替代 ChromaDB。

存储后端：core/rag_sqlite_store.py
同步引擎：core/rag_sync_engine.py
"""

import sys
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger("ve5.rag_hub")

from app_paths import DATA_DIR


# ── RAG 模块注册表 ──
_RAG_MODULES: List[Dict[str, Any]] = []


def _register_modules():
    global _RAG_MODULES
    if _RAG_MODULES:
        return _RAG_MODULES

    _RAG_MODULES = [
        {
            "id": "financial_rag",
            "name": "财务隐私 RAG",
            "icon": "\U0001f4b0",
            "description": "存储 OCR 截图的原始文本和解析元数据，供后续 AI 分析用户持仓偏好、账户风格和资产迁移趋势",
            "status": "active",
            "privacy_level": "high",
            "storage_backend": "sqlite+fts5",
            "storage_path": "ve5.db / rag_financial_texts",
            "collection_name": "rag_financial_texts",
            "embedding_model": "无（FTS5 全文检索）",
            "data_sources": ["截图 OCR（自动）"],
            "consumers": ["预留：AI 偏好分析", "预留：风格迁移检测", "预留：资产变化追踪"],
            "logic": (
                "数据源：\n"
                "  1. userdata/screenshot_descriptions/*.txt + logs/screenshot_descriptions.jsonl（OCR 原始文本）\n"
                "  2. ve5.db / transactions 表中 transaction_type='expense' 的记录（消费支出流水）\n"
                "同步引擎分别扫描上述数据源，将 OCR 文本写入 SQLite rag_financial_texts 表，"
                "将消费支出流水（商户、金额、分类、必需/弹性标记）写入 rag_expense_records 表。\n"
                "FTS5 全文索引支持按 source_file、ocr_text、description 检索财务文本，"
                "按 counterparty、category_primary、description 检索消费支出。\n"
                "搜索时可调用 LLM 联想扩展关键词（如'可用余额'→'账户余额/活期/零钱'）。\n"
                "完全独立于截图处理 workflow，workflow 只负责提取数据写 SQLite，"
                "RAG 同步引擎定时扫描已留存的数据并入库。"
            ),
        },
        {
            "id": "consumer_price_rag",
            "name": "消费价格 RAG",
            "icon": "\U0001f6cd\ufe0f",
            "description": "存储用户的实际购物价格记录，为生活规划提供真实的区域物价参考",
            "status": "active",
            "privacy_level": "medium",
            "storage_backend": "sqlite+fts5",
            "storage_path": "ve5.db / rag_price_records",
            "collection_name": "rag_price_records",
            "embedding_model": "无（FTS5 全文检索）",
            "data_sources": ["手动录入", "小票 OCR（规划中）", "政府公开物价（自动采集）"],
            "consumers": ["life_planner 生活规划", "regional_price 区域物价上下文"],
            "logic": (
                "数据源：userdata/consumer_prices.jsonl（API 写入时同步落盘）\n"
                "同步引擎扫描 JSONL，将每条价格记录写入 SQLite rag_price_records 表。\n"
                "FTS5 支持按商品名、商家、区域、分类检索。\n"
                "life_planner 生成食谱和购物清单时，调用 price_recent() 获取最近价格作为参考。\n"
                "完全独立于价格录入 API，API 只负责写入 JSONL，RAG 同步引擎定时扫描并入库。"
            ),
        },
        {
            "id": "report_rag",
            "name": "基本面研报 RAG",
            "icon": "\U0001f4d8",
            "description": "存储研报的结构化知识，为投资分析提供语义检索能力",
            "status": "active",
            "privacy_level": "low",
            "storage_backend": "sqlite+fts5",
            "storage_path": "ve5.db / rag_reports",
            "collection_name": "rag_reports",
            "embedding_model": "无（FTS5 全文检索）",
            "data_sources": ["研报 URL/PDF/文本解析（手动导入）"],
            "consumers": ["investment_advisor 投资顾问", "life_planner 用户习惯检索", "API /tactical/fundamental/chat"],
            "logic": (
                "数据源：userdata/rag_reports.jsonl（研报导入时写入）\n"
                "同步引擎扫描 JSONL，将研报标题和内容写入 SQLite rag_reports 表。\n"
                "FTS5 支持按标题和正文全文检索，rank 排序。\n"
                "研报导入 API 只负责写入 JSONL，RAG 同步引擎定时扫描并入库。"
            ),
        },
        {
            "id": "behavior_rag",
            "name": "用户行为习惯 RAG",
            "icon": "\U0001f9e0",
            "description": "从消费记录和持仓变化中自动学习用户的消费模式、投资偏好和生活习惯",
            "status": "planned",
            "privacy_level": "high",
            "storage_backend": "tbd",
            "storage_path": "",
            "collection_name": "",
            "embedding_model": "",
            "data_sources": ["transactions 表（自动）", "asset_holdings 变更记录（自动）"],
            "consumers": ["life_planner 个性化建议", "asset_doctor 风格诊断", "spending_analyst 消费预测"],
            "logic": "规划逻辑：从 transactions 表按时间序列提取消费模式，从 asset_holdings 提取投资偏好。定期生成用户画像摘要供各 chatbot skill 检索。",
        },
        {
            "id": "policy_rag",
            "name": "政策法规 RAG",
            "icon": "\U0001f3db\ufe0f",
            "description": "存储财税政策、监管动态、市场规则等公开信息，为投资决策提供合规参考",
            "status": "planned",
            "privacy_level": "low",
            "storage_backend": "tbd",
            "storage_path": "",
            "collection_name": "",
            "embedding_model": "",
            "data_sources": ["政策文件爬取（规划中）", "用户手动导入"],
            "consumers": ["investment_advisor 政策影响评估", "asset_doctor 合规检查"],
            "logic": "规划逻辑：通过搜索或用户导入获取政策文件，解析后按主题分块入库。投资顾问分析持仓时检索相关政策变动评估影响。",
        },
        {
            "id": "community_rag",
            "name": "社区知识 RAG",
            "icon": "\U0001f91d",
            "description": "用户贡献的物价信息、商家评价、消费体验，形成社区化的区域消费知识库",
            "status": "planned",
            "privacy_level": "low",
            "storage_backend": "tbd",
            "storage_path": "",
            "collection_name": "",
            "embedding_model": "",
            "data_sources": ["用户主动贡献（规划中）"],
            "consumers": ["life_planner 社区物价参考", "regional_price 多源融合"],
            "logic": "规划逻辑：用户贡献区域物价信息，经验证后入库。其他用户的生活规划可检索社区数据作为辅助参考。",
        },
    ]
    return _RAG_MODULES


def rag_hub_list_modules() -> List[Dict[str, Any]]:
    """获取所有 RAG 模块列表（含实时状态）"""
    from core.rag_sqlite_store import fin_stats, price_stats, report_stats
    from core.rag_sync_engine import rag_get_sync_status

    modules = _register_modules()
    sync_status = rag_get_sync_status()
    result = []
    for m in modules:
        info = {k: v for k, v in m.items() if k != "logic"}
        stats = _get_module_stats(m["id"])
        info["stats"] = stats
        info["record_count"] = stats.get("total_records", 0)
        info["is_available"] = stats.get("available", False)
        # 同步状态
        sync = sync_status.get(m["id"])
        if sync:
            info["last_sync"] = sync.get("last_sync", "")
            info["last_sync_new"] = sync.get("new_count", 0)
        else:
            info["last_sync"] = ""
            info["last_sync_new"] = 0
        result.append(info)
    return result


def rag_hub_get_module(module_id: str) -> Optional[Dict[str, Any]]:
    """获取单个 RAG 模块的完整信息"""
    modules = _register_modules()
    for m in modules:
        if m["id"] == module_id:
            info = dict(m)
            info["stats"] = _get_module_stats(module_id)
            info["record_count"] = info["stats"].get("total_records", 0)
            info["is_available"] = info["stats"].get("available", False)
            return info
    return None


def rag_hub_get_records(module_id: str, limit: int = 20, offset: int = 0) -> Dict[str, Any]:
    """获取指定 RAG 模块的记录列表"""
    from core.rag_sqlite_store import fin_list, price_list, report_list, expense_list
    if module_id == "financial_rag":
        fin = fin_list(limit, offset)
        exp = expense_list(limit, offset)
        records = fin.get("records", []) + exp.get("records", [])
        total = fin.get("total", 0) + exp.get("total", 0)
        return {"records": records, "total": total, "module_id": module_id}
    elif module_id == "consumer_price_rag":
        return price_list(limit, offset)
    elif module_id == "report_rag":
        return report_list(limit, offset)
    return {"records": [], "total": 0, "module_id": module_id}


def rag_hub_get_vectors(module_id: str, query: str = "", limit: int = 5) -> List[Dict[str, Any]]:
    """语义检索（FTS5 全文检索）"""
    from core.rag_sqlite_store import fin_search, price_search, report_search, expense_search
    if module_id == "financial_rag":
        results = fin_search(query, limit)
        out = [{"id": f"vec_{r.get('id', i)}", "text": (r.get("ocr_text") or "")[:300],
                "source_file": r.get("source_file", ""),
                "snapshot_type": r.get("snapshot_type", "")} for i, r in enumerate(results)]
        exp_results = expense_search(query, limit)
        out += [{"id": f"exp_{r.get('id', i)}", "type": "expense",
                 "counterparty": r.get("counterparty", ""),
                 "amount": r.get("amount", 0), "category": r.get("category_primary", ""),
                 "is_essential": r.get("is_essential", 0)} for i, r in enumerate(exp_results)]
        return out[:limit * 2]
    elif module_id == "consumer_price_rag":
        results = price_search(query, limit)
        return [{"id": f"vec_{r.get('id', i)}", "item": r.get("item", ""),
                 "price": r.get("price", 0), "merchant": r.get("merchant", ""),
                 "location": r.get("location", ""), "source": r.get("source", "")}
                for i, r in enumerate(results)]
    elif module_id == "report_rag":
        results = report_search(query, limit)
        return [{"id": f"vec_{r.get('id', i)}", "title": r.get("title", ""),
                 "content": (r.get("content") or "")[:300]} for i, r in enumerate(results)]
    return []


def rag_hub_delete_record(module_id: str, record_id: str) -> bool:
    """删除指定记录"""
    from core.rag_sqlite_store import fin_delete, price_delete, report_delete
    try:
        rid = int(record_id)
        if module_id == "financial_rag":
            return fin_delete(rid)
        elif module_id == "consumer_price_rag":
            return price_delete(rid)
        elif module_id == "report_rag":
            return report_delete(rid)
    except (ValueError, Exception) as e:
        logger.error(f"[RAG-HUB] 删除失败: {e}")
    return False


def rag_hub_get_storage_info() -> Dict[str, Any]:
    """获取所有 RAG 存储的磁盘占用信息"""
    from core.rag_sqlite_store import fin_stats, price_stats, report_stats
    info = {}
    # SQLite 表大小（从 ve5.db 整体估算）
    db_path = DATA_DIR / "ve5.db"
    if db_path.exists():
        info["ve5_db"] = {"path": str(db_path), "size_bytes": db_path.stat().st_size}
    # JSONL 文件大小
    for name, p in [
        ("screenshot_descriptions_jsonl", DATA_DIR / "logs" / "screenshot_descriptions.jsonl"),
        ("consumer_prices_jsonl", DATA_DIR / "consumer_prices.jsonl"),
        ("rag_reports_jsonl", DATA_DIR / "rag_reports.jsonl"),
    ]:
        if p.exists():
            info[name] = {"path": str(p), "size_bytes": p.stat().st_size}
        else:
            info[name] = {"path": str(p), "size_bytes": 0, "exists": False}
    # 各表记录数
    info["table_counts"] = {
        "financial": fin_stats().get("total_records", 0),
        "price": price_stats().get("total_records", 0),
        "report": report_stats().get("total_records", 0),
    }
    return info


def _get_module_stats(module_id: str) -> Dict[str, Any]:
    """获取模块实时统计信息"""
    from core.rag_sqlite_store import fin_stats, price_stats, report_stats, expense_stats
    try:
        if module_id == "financial_rag":
            fin = fin_stats()
            exp = expense_stats()
            total = fin.get("total_records", 0) + exp.get("total_records", 0)
            return {
                "available": True,
                "total_records": total,
                "backend": "sqlite+fts5",
                "financial_texts": fin.get("total_records", 0),
                "expense_records": exp.get("total_records", 0),
                "essential_expenses": exp.get("essential_count", 0),
                "elastic_expenses": exp.get("elastic_count", 0),
            }
        elif module_id == "consumer_price_rag":
            return price_stats()
        elif module_id == "report_rag":
            return report_stats()
        # planned 模块无实时统计
        return {"available": False, "total_records": 0, "backend": "none"}
    except Exception as e:
        logger.debug(f"[RAG-HUB] 获取 {module_id} 统计失败: {e}")
        return {"available": False, "total_records": 0, "error": str(e)}
