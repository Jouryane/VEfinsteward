"""
通用报告存储模块
================
为所有 VE 管家 skill 提供统一的版本化报告存储。

存储结构：
  {DATA_DIR}/chatbot/reports/
    ├── index.json          # 所有报告的索引列表
    ├── {report_id}.json    # 单个报告完整数据
    └── latest/             # 各类型最新报告软链接（兼容用）
"""

import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional
from app_paths import DATA_DIR

logger = logging.getLogger("ve5.chatbot.report_store")

_REPORTS_DIR = DATA_DIR / "chatbot" / "reports"
_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
_INDEX_FILE = _REPORTS_DIR / "index.json"
_LATEST_DIR = _REPORTS_DIR / "latest"
_LATEST_DIR.mkdir(parents=True, exist_ok=True)


def _load_index() -> List[Dict]:
    """加载报告索引"""
    if _INDEX_FILE.exists():
        try:
            return json.loads(_INDEX_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_index(index: List[Dict]):
    """保存报告索引"""
    _INDEX_FILE.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def ve5_report_save(report_type: str, data: Dict[str, Any], metadata: Optional[Dict[str, Any]] = None) -> str:
    """
    保存一份报告，返回 report_id。

    :param report_type: 报告类型标识，如 "life_plan", "spending", "goal" 等
    :param data: 报告的完整数据（会被原样存入文件）
    :param metadata: 用于列表展示的元数据，如 title, summary 等
    :return: report_id
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_id = f"{report_type}_{ts}"
    report_file = _REPORTS_DIR / f"{report_id}.json"

    record = {
        "report_id": report_id,
        "type": report_type,
        "created_at": datetime.now().isoformat(),
        "data": data,
        "metadata": metadata or {},
    }
    report_file.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    # 更新索引（避免重复）
    index = _load_index()
    index = [e for e in index if e.get("report_id") != report_id]
    index_entry = {
        "report_id": report_id,
        "type": report_type,
        "created_at": record["created_at"],
    }
    if metadata:
        index_entry.update(metadata)
    index.insert(0, index_entry)
    _save_index(index)

    # 同时更新 latest/{type}.json 供兼容
    latest_file = _LATEST_DIR / f"{report_type}.json"
    latest_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info(f"[REPORT_STORE] 已保存报告 {report_id} (type={report_type})")
    return report_id


def ve5_report_list(report_type: Optional[str] = None) -> List[Dict]:
    """
    列出报告。可指定 type 过滤。
    返回按 created_at 降序排列的报告索引列表。
    """
    index = _load_index()
    if report_type:
        index = [e for e in index if e.get("type") == report_type]
    return index


def ve5_report_get(report_id: str) -> Optional[Dict[str, Any]]:
    """
    获取单个报告的完整数据（包含 data 和 metadata）。
    如果找不到返回 None。
    """
    report_file = _REPORTS_DIR / f"{report_id}.json"
    if not report_file.exists():
        return None
    try:
        return json.loads(report_file.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"[REPORT_STORE] 读取报告 {report_id} 失败：{e}")
        return None


def ve5_report_delete(report_id: str) -> bool:
    """删除单个报告。成功返回 True。"""
    report_file = _REPORTS_DIR / f"{report_id}.json"
    deleted = False
    if report_file.exists():
        report_file.unlink()
        deleted = True

    # 更新索引
    index = _load_index()
    index = [e for e in index if e.get("report_id") != report_id]
    _save_index(index)

    if deleted:
        logger.info(f"[REPORT_STORE] 已删除报告 {report_id}")
    return deleted
