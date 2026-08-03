"""
VE5 共享财务数据工具
====================
统一的财务数据加载逻辑，解决：
  1. 当月无交易数据时回退到最近有数据的月份
  2. YTD（年度累计）收支计算
  3. 积累目标的进度计算（YTD储蓄 / 目标额，而非 总资产 / 目标额）

所有需要财务数据的模块统一调用此模块，避免逻辑重复。
"""

import json
import sqlite3
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

from app_paths import DB_PATH, DATA_DIR

logger = logging.getLogger("ve5.chatbot.financial_data")

# alternative → protection 映射
_ASSET_CLASS_MAP = {"alternative": "protection"}


def _check_column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """检测表是否存在某列"""
    try:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        return column in cols
    except Exception:
        return False


def _get_superseded_filter(conn: sqlite3.Connection) -> str:
    """返回 is_superseded 过滤子句（如列存在）"""
    if _check_column_exists(conn, "asset_holdings", "is_superseded"):
        return "is_superseded=0 AND "
    return ""


def find_latest_month_with_data(conn: sqlite3.Connection, txn_type: str = "income") -> Optional[str]:
    """
    查找最近有交易数据的月份（YYYY-MM 格式）。
    如果当月有数据，返回当月；否则返回最近有数据的月份。
    """
    current_month = datetime.now().strftime("%Y-%m")
    # 先查当月
    r = conn.execute(
        "SELECT SUM(amount) FROM transactions WHERE transaction_type=? AND transaction_date LIKE ?",
        (txn_type, f"{current_month}%")
    ).fetchone()
    if r and abs(float(r[0] or 0)) > 0:
        return current_month

    # 当月无数据 → 查最近有数据的月份
    r = conn.execute(
        "SELECT DISTINCT SUBSTR(transaction_date, 1, 7) AS month "
        "FROM transactions WHERE transaction_type=? AND amount > 0 "
        "ORDER BY month DESC LIMIT 1",
        (txn_type,)
    ).fetchone()
    if r:
        return r[0]
    return None


def load_financial_summary() -> Dict[str, Any]:
    """
    统一的财务数据加载（含月份回退 + YTD）。

    返回:
      {
        total_assets, holdings_count,
        total_aggressive, total_stable, total_liquid, total_protection,
        monthly_income, monthly_expense, monthly_savings, savings_rate,
        monthly_data_month,           # 实际使用的月份
        ytd_income, ytd_expense, ytd_savings,  # 年度累计
        ytd_investment_return,        # 年度投资收益（如有）
        transaction_count,
      }
    """
    result = {
        "total_assets": 0.0,
        "holdings_count": 0,
        "total_aggressive": 0.0,
        "total_stable": 0.0,
        "total_liquid": 0.0,
        "total_protection": 0.0,
        "monthly_income": 0.0,
        "monthly_expense": 0.0,
        "monthly_savings": 0.0,
        "savings_rate": 0.0,
        "monthly_data_month": "",
        "ytd_income": 0.0,
        "ytd_expense": 0.0,
        "ytd_savings": 0.0,
        "ytd_investment_return": 0.0,
        "transaction_count": 0,
    }

    if not DB_PATH.exists():
        return result

    try:
        conn = sqlite3.connect(str(DB_PATH))
        sup_filter = _get_superseded_filter(conn)

        # ── 总资产 + 持仓数 + 分类汇总 ──
        r = conn.execute(
            f"SELECT SUM(current_value) FROM asset_holdings WHERE {sup_filter}current_value > 0"
        ).fetchone()
        result["total_assets"] = float(r[0] or 0) if r else 0.0

        r = conn.execute(
            f"SELECT COUNT(*) FROM asset_holdings WHERE {sup_filter}current_value > 0"
        ).fetchone()
        result["holdings_count"] = int(r[0] if r else 0)

        rows = conn.execute(
            f"SELECT asset_class, SUM(current_value) FROM asset_holdings "
            f"WHERE {sup_filter}current_value > 0 GROUP BY asset_class"
        ).fetchall()
        for row in rows:
            cls_name = row[0] or "unknown"
            mapped = _ASSET_CLASS_MAP.get(cls_name, cls_name)
            result[f"total_{mapped}"] = float(row[1] or 0)

        # ── 月度收支（含回退）──
        tcols = [r[1] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()]
        has_txn = "transaction_type" in tcols and "transaction_date" in tcols

        if has_txn:
            # 找到最近有收入数据的月份
            data_month = find_latest_month_with_data(conn, "income")
            if data_month:
                result["monthly_data_month"] = data_month

                r = conn.execute(
                    "SELECT SUM(amount) FROM transactions WHERE transaction_type='income' AND transaction_date LIKE ?",
                    (f"{data_month}%",)
                ).fetchone()
                income = abs(float(r[0] or 0)) if r else 0.0
                result["monthly_income"] = income

                r = conn.execute(
                    "SELECT SUM(amount) FROM transactions WHERE transaction_type='expense' AND transaction_date LIKE ?",
                    (f"{data_month}%",)
                ).fetchone()
                expense = abs(float(r[0] or 0)) if r else 0.0
                result["monthly_expense"] = expense

                result["monthly_savings"] = max(0, income - expense)
                result["savings_rate"] = round(result["monthly_savings"] / income, 4) if income > 0 else 0.0

                r = conn.execute(
                    "SELECT COUNT(*) FROM transactions WHERE transaction_date LIKE ?",
                    (f"{data_month}%",)
                ).fetchone()
                result["transaction_count"] = int(r[0] if r else 0)

            # ── YTD（年度累计）──
            year = datetime.now().strftime("%Y")
            r = conn.execute(
                "SELECT SUM(amount) FROM transactions WHERE transaction_type='income' AND transaction_date LIKE ?",
                (f"{year}%",)
            ).fetchone()
            result["ytd_income"] = abs(float(r[0] or 0)) if r else 0.0

            r = conn.execute(
                "SELECT SUM(amount) FROM transactions WHERE transaction_type='expense' AND transaction_date LIKE ?",
                (f"{year}%",)
            ).fetchone()
            result["ytd_expense"] = abs(float(r[0] or 0)) if r else 0.0

            result["ytd_savings"] = max(0, result["ytd_income"] - result["ytd_expense"])

            # 投资收益（如有 investment_return 类型）
            if "investment_return" in [
                r[1] for r in conn.execute(
                    "SELECT DISTINCT transaction_type FROM transactions WHERE transaction_type LIKE 'return%'"
                ).fetchall()
            ]:
                r = conn.execute(
                    "SELECT SUM(amount) FROM transactions WHERE transaction_type LIKE 'return%' AND transaction_date LIKE ?",
                    (f"{year}%",)
                ).fetchone()
                result["ytd_investment_return"] = abs(float(r[0] or 0)) if r else 0.0

        conn.close()
    except Exception as e:
        logger.warning(f"[FIN_DATA] 加载财务数据失败: {e}")

    return result


def calculate_goal_progress(goal: Dict[str, Any], finance: Dict[str, Any]) -> Dict[str, Any]:
    """
    智能计算目标进度。

    积累型目标（名称含"增加"/"积累"/"储蓄"/"攒"）：
      进度 = YTD储蓄 / 目标额 * 100

    金额型目标（如购房）：
      进度 = 总资产 / 目标额 * 100（如果当前资产可覆盖）

    返回: {name, progress_pct, current_amount, target_amount, remaining_amount, months_needed, status}
    """
    name = goal.get("name", "")
    target = float(goal.get("estimated_cost", 0) or goal.get("target_amount", 0) or 0)
    icon = goal.get("icon", "")

    # 判断是否为积累型目标
    is_accumulation = any(kw in name for kw in ["增加", "积累", "储蓄", "攒", "存"])

    if is_accumulation:
        # 积累型：用 YTD 储蓄计算
        ytd_savings = finance.get("ytd_savings", 0) + finance.get("ytd_investment_return", 0)
        progress_pct = min(100, (ytd_savings / target * 100)) if target > 0 else 0
        current_amount = ytd_savings
        remaining = max(0, target - ytd_savings)

        # 预估剩余月份
        monthly_savings = finance.get("monthly_savings", 0)
        months_needed = (remaining / monthly_savings) if monthly_savings > 0 and remaining > 0 else 0

        return {
            "name": name,
            "icon": icon,
            "target_amount": target,
            "estimated_cost": target,
            "current_amount": round(current_amount, 2),
            "progress_pct": round(progress_pct, 1),
            "remaining_amount": round(remaining, 2),
            "months_needed": round(months_needed, 1) if months_needed > 0 else 0,
            "status": "已达成" if progress_pct >= 100 else ("进行中" if progress_pct > 0 else "未开始"),
            "goal_type": "accumulation",
            "ytd_savings": round(ytd_savings, 2),
        }
    else:
        # 金额型：用总资产计算（可覆盖时视为可达成）
        total_assets = finance.get("total_assets", 0)
        progress_pct = min(100, (total_assets / target * 100)) if target > 0 else 0
        current_amount = min(total_assets, target)
        remaining = max(0, target - total_assets)

        monthly_savings = finance.get("monthly_savings", 0)
        months_needed = (remaining / monthly_savings) if monthly_savings > 0 and remaining > 0 else 0

        return {
            "name": name,
            "icon": icon,
            "target_amount": target,
            "estimated_cost": target,
            "current_amount": round(current_amount, 2),
            "progress_pct": round(progress_pct, 1),
            "remaining_amount": round(remaining, 2),
            "months_needed": round(months_needed, 1) if months_needed > 0 else 0,
            "status": "已达成" if progress_pct >= 100 else ("进行中" if progress_pct > 0 else "未开始"),
            "goal_type": "amount",
        }


def load_goals_with_progress() -> List[Dict[str, Any]]:
    """加载目标并计算进度（使用统一的财务数据）"""
    gf = DATA_DIR / "goals.json"
    if not gf.exists():
        return []

    try:
        goals = json.loads(gf.read_text(encoding="utf-8")).get("goals", [])
    except Exception:
        return []

    finance = load_financial_summary()
    return [calculate_goal_progress(g, finance) for g in goals]
