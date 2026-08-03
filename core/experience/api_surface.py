"""
VE5 Experience API Surface — 受限 API 清单 + SafeContext
=========================================================
这是生成代码的唯一数据通道。生成的 Python 代码通过 ctx 对象
访问白名单 API，无法直接访问文件系统、网络或任意 Python 模块。

两重用途：
1. get_api_surface_text() → 注入 LLM prompt，告知可用的 API
2. SafeContext → 运行时提供受限的数据访问接口
"""

import json
import logging
import sqlite3
from typing import Dict, Any, List, Optional
from datetime import datetime

logger = logging.getLogger("ve5.experience.api_surface")


# ════════════════════════════════════════════════
# API Surface 文本（注入 LLM prompt）
# ════════════════════════════════════════════════

_API_SURFACE_TEXT = """## 可用 API（通过 ctx 对象调用）

### 数据加载
- ctx.load_goals() → List[Dict]
  返回用户目标列表，每个目标包含: name, target_amount, current_amount,
  progress_pct, status, monthly_target, months_needed, estimated_cost

- ctx.load_financial() → Dict
  返回财务概览: total_assets, monthly_income, monthly_expense,
  monthly_savings, savings_rate, holdings_count,
  monthly_data_month (实际数据月份，当月无数据时回退到最近月份),
  ytd_income, ytd_expense, ytd_savings, ytd_investment_return (年度累计)

- ctx.load_holdings() → List[Dict]
  返回持仓明细，每条包含: name, code, current_value, asset_class,
  holding_ratio, cost_basis, holding_days

- ctx.load_transactions(month=None) → List[Dict]
  返回交易记录，每条包含: date, type, amount, category, description
  month 参数格式: "2026-01"，默认当月（无数据时自动回退到最近月份）

- ctx.load_goals_detail() → List[Dict]
  返回完整目标详情（含 remaining_amount, goal_type, in_progress_goals_count）

### 计算工具
- ctx.calculate_progress(goal_name=None) → Dict
  返回目标进度: goal_name, progress_pct, current_amount, target_amount,
  gap, months_needed, remaining_amount, monthly_target, goal_type
  积累型目标(名称含"增加"/"积累"/"储蓄"/"攒")用YTD储蓄计算进度
  金额型目标用总资产计算进度

- ctx.calculate_savings_rate() → float
  返回当前储蓄率 (0~1)

- ctx.calculate_asset_allocation() → Dict
  返回资产配置: {aggressive: {amount, ratio}, stable: {...}, ...}

### 格式化
- ctx.format_currency(amount) → str   # "¥X,XXX"
- ctx.format_percent(value) → str     # "XX.X%"
- ctx.format_number(value, decimals=0) → str

### 上下文
- ctx.get(key, default=None) → 获取预加载的上下文值
- ctx.user_input → str  # 用户原始输入

## 代码约束
1. 必须定义 execute(ctx) 函数作为入口
2. 返回格式: {"reply": str, "data": dict, "sections": list}
   - reply: 给用户的回复文本（Markdown格式，可含表格/表情）
   - data: 结构化数据（供前端展示）
   - sections: 页面分区 [{"id","label","content","type?"}]
3. 不可导入 os, sys, subprocess, shutil, pathlib, socket, http 等系统模块
4. 不可使用 open(), exec(), eval(), __import__()
5. 不可访问文件系统、网络
6. 只能通过 ctx 对象获取数据
7. 可以使用: json, math, datetime, re, typing
8. 不要硬编码任何数值，所有数据通过 ctx API 获取
"""


def get_api_surface_text() -> str:
    """返回 API Surface 文本，供 LLM prompt 注入"""
    return _API_SURFACE_TEXT


# ════════════════════════════════════════════════
# SafeContext — 受限执行上下文
# ════════════════════════════════════════════════

class SafeContext:
    """
    受限的执行上下文，只暴露白名单 API。
    生成的 Python 代码通过此对象访问数据，无法触碰文件系统/网络。
    """

    def __init__(self, state: Dict, experience: Dict):
        self._state = state
        self._experience = experience
        self._cache: Dict = {}

        # 懒加载缓存
        self._financial: Optional[Dict] = None
        self._goals: Optional[List[Dict]] = None
        self._holdings: Optional[List[Dict]] = None
        self._transactions: Dict[str, List[Dict]] = {}

        # 暴露用户输入
        self.user_input = (
            state.get("raw_input", "")
            or state.get("user_input", "")
        )

    # ── 数据加载 API ──

    def load_goals(self) -> List[Dict]:
        """加载用户目标列表"""
        if self._goals is None:
            self._goals = self._fetch_goals()
        return self._goals

    def load_financial(self) -> Dict:
        """加载财务概览数据"""
        if self._financial is None:
            self._financial = self._fetch_financial()
        return self._financial

    def load_holdings(self) -> List[Dict]:
        """加载持仓明细"""
        if self._holdings is None:
            self._holdings = self._fetch_holdings()
        return self._holdings

    def load_transactions(self, month: str = None) -> List[Dict]:
        """加载交易记录"""
        if not month:
            month = datetime.now().strftime("%Y-%m")
        if month not in self._transactions:
            self._transactions[month] = self._fetch_transactions(month)
        return self._transactions[month]

    def load_goals_detail(self) -> List[Dict]:
        """加载完整目标详情（含 remaining_amount, goal_type）"""
        from core.ve5_chatbot.financial_data import calculate_goal_progress
        goals = self.load_goals()
        fin = self.load_financial()
        result = []
        for g in goals:
            progress_data = calculate_goal_progress(g, fin)
            detail = dict(g)
            detail["remaining_amount"] = progress_data.get("remaining_amount", 0)
            detail["goal_type"] = progress_data.get("goal_type", "")
            detail["progress_pct"] = progress_data.get("progress_pct", 0)
            detail["current_amount"] = progress_data.get("current_amount", 0)
            detail["months_needed"] = progress_data.get("months_needed", 0)
            detail["status"] = progress_data.get("status", "")
            result.append(detail)
        return result

    # ── 计算工具 ──

    def calculate_progress(self, goal_name: str = None) -> Dict:
        """计算目标进度（积累型用YTD储蓄，金额型用总资产）"""
        from core.ve5_chatbot.financial_data import calculate_goal_progress
        goals = self.load_goals()
        if not goals:
            return {}

        target = None
        if goal_name:
            target = next((g for g in goals if g.get("name") == goal_name), None)
        if not target:
            active = [g for g in goals if g.get("status") != "已达成"]
            target = max(active, key=lambda g: g.get("estimated_cost", 0), default=goals[0])

        # 使用共享模块的智能进度计算
        fin = self.load_financial()
        result = calculate_goal_progress(target, fin)

        return {
            "goal_name": result.get("name", ""),
            "progress_pct": result.get("progress_pct", 0),
            "current_amount": result.get("current_amount", 0),
            "target_amount": result.get("target_amount", 0),
            "remaining_amount": result.get("remaining_amount", 0),
            "gap": max(0, 100 - result.get("progress_pct", 0)),
            "months_needed": result.get("months_needed", 0),
            "monthly_target": target.get("monthly_target", 0),
            "goal_type": result.get("goal_type", ""),
            "ytd_savings": result.get("ytd_savings", 0) if result.get("goal_type") == "accumulation" else 0,
        }

    def calculate_savings_rate(self) -> float:
        """计算储蓄率 (0~1)"""
        fin = self.load_financial()
        income = fin.get("monthly_income", 0)
        if income > 0:
            return fin.get("monthly_savings", 0) / income
        return 0.0

    def calculate_asset_allocation(self) -> Dict:
        """计算资产配置"""
        holdings = self.load_holdings()
        total = sum(h.get("current_value", 0) for h in holdings)

        classes = {"aggressive": 0.0, "stable": 0.0, "liquid": 0.0, "protection": 0.0}
        for h in holdings:
            cls = h.get("asset_class", "unknown")
            if cls in classes:
                classes[cls] += h.get("current_value", 0)
            else:
                classes[cls] = classes.get(cls, 0.0) + h.get("current_value", 0)

        result = {}
        for cls, amount in classes.items():
            result[cls] = {
                "amount": round(amount, 2),
                "ratio": round(amount / total, 4) if total > 0 else 0,
            }
        return result

    # ── 格式化 ──

    def format_currency(self, amount: float) -> str:
        return f"¥{amount:,.0f}"

    def format_percent(self, value: float) -> str:
        return f"{value:.1f}%"

    def format_number(self, value: float, decimals: int = 0) -> str:
        if decimals == 0:
            return f"{value:,.0f}"
        return f"{value:,.{decimals}f}"

    # ── 上下文访问 ──

    def get(self, key: str, default=None):
        """获取预加载的上下文值"""
        if key in self._state:
            return self._state[key]
        for sub in ("entities", "user_state"):
            sub_dict = self._state.get(sub, {})
            if key in sub_dict:
                return sub_dict[key]
        return default

    # ════════════════════════════════════════════════
    # 内部数据获取（不暴露给生成代码，以 _ 开头）
    # ════════════════════════════════════════════════

    def _fetch_goals(self) -> List[Dict]:
        """从 goals.json 加载目标"""
        try:
            from app_paths import DATA_DIR
            gf = DATA_DIR / "goals.json"
            if not gf.exists():
                return []
            data = json.loads(gf.read_text(encoding="utf-8"))
            return data.get("goals", [])
        except Exception as e:
            logger.debug(f"[API_SURFACE] 加载目标失败: {e}")
            return []

    def _fetch_financial(self) -> Dict:
        """从数据库加载财务数据（含月份回退 + YTD）"""
        try:
            from app_paths import DB_PATH
            if not DB_PATH.exists():
                return self._empty_financial()

            # 使用共享模块（统一逻辑：月份回退 + YTD）
            from core.ve5_chatbot.financial_data import load_financial_summary
            fin = load_financial_summary()
            return {
                "total_assets": fin.get("total_assets", 0),
                "monthly_income": fin.get("monthly_income", 0),
                "monthly_expense": fin.get("monthly_expense", 0),
                "monthly_savings": fin.get("monthly_savings", 0),
                "savings_rate": fin.get("savings_rate", 0),
                "holdings_count": fin.get("holdings_count", 0),
                "monthly_data_month": fin.get("monthly_data_month", ""),
                "ytd_income": fin.get("ytd_income", 0),
                "ytd_expense": fin.get("ytd_expense", 0),
                "ytd_savings": fin.get("ytd_savings", 0),
                "ytd_investment_return": fin.get("ytd_investment_return", 0),
                "transaction_count": fin.get("transaction_count", 0),
            }
        except Exception as e:
            logger.debug(f"[API_SURFACE] 加载财务数据失败: {e}")
            return self._empty_financial()

    def _fetch_holdings(self) -> List[Dict]:
        """从数据库加载持仓"""
        try:
            from app_paths import DB_PATH
            if not DB_PATH.exists():
                return []

            from core.ve5_chatbot.financial_data import _check_column_exists, _get_superseded_filter
            conn = sqlite3.connect(str(DB_PATH))
            sup_filter = _get_superseded_filter(conn)
            rows = conn.execute(
                f"SELECT name, code, current_value, asset_class, cost_basis, holding_days "
                f"FROM asset_holdings WHERE {sup_filter}current_value > 0 "
                f"ORDER BY current_value DESC"
            ).fetchall()
            conn.close()

            total = sum(r[2] or 0 for r in rows)
            result = []
            for r in rows:
                result.append({
                    "name": r[0] or "",
                    "code": r[1] or "",
                    "current_value": float(r[2] or 0),
                    "asset_class": r[3] or "unknown",
                    "holding_ratio": round((r[2] or 0) / total, 4) if total > 0 else 0,
                    "cost_basis": float(r[4] or 0),
                    "holding_days": int(r[5] or 0),
                })
            return result
        except Exception as e:
            logger.debug(f"[API_SURFACE] 加载持仓失败: {e}")
            return []

    def _fetch_transactions(self, month: str) -> List[Dict]:
        """从数据库加载交易记录"""
        try:
            from app_paths import DB_PATH
            if not DB_PATH.exists():
                return []

            conn = sqlite3.connect(str(DB_PATH))
            rows = conn.execute(
                "SELECT transaction_date, transaction_type, amount, category, description "
                "FROM transactions WHERE transaction_date LIKE ? "
                "ORDER BY transaction_date DESC LIMIT 100",
                (f"{month}%",),
            ).fetchall()
            conn.close()

            result = []
            for r in rows:
                result.append({
                    "date": r[0] or "",
                    "type": r[1] or "",
                    "amount": abs(float(r[2] or 0)),
                    "category": r[3] or "",
                    "description": r[4] or "",
                })
            return result
        except Exception as e:
            logger.debug(f"[API_SURFACE] 加载交易失败: {e}")
            return []

    @staticmethod
    def _empty_financial() -> Dict:
        return {
            "total_assets": 0,
            "monthly_income": 0,
            "monthly_expense": 0,
            "monthly_savings": 0,
            "savings_rate": 0,
            "holdings_count": 0,
            "monthly_data_month": "",
            "ytd_income": 0,
            "ytd_expense": 0,
            "ytd_savings": 0,
            "ytd_investment_return": 0,
            "transaction_count": 0,
        }
