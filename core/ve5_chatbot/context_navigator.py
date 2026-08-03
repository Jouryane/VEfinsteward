"""
VE5 Context Navigator — 上下文导航系统
======================================
为 VEchatbot 所有 skill 提供统一的、权威的财务上下文。

设计理念：
  - "导航"：LLM 不再凭空猜测，而是读取系统已计算的权威结果
  - "不冲突"：注入推荐配比/评分/进度等业务逻辑结果，LLM 引用而非重新计算
  - "省 token"：紧凑格式，~400-600 tokens，本地纯代码计算，无额外 LLM 调用

数据来源（全部本地 Python 直连，不经 LLM）：
  1. allocation_engine — 推荐配比、实际配比、偏差、三维度评分
  2. goals.json + SQLite — 目标进度（动态计算，含月份回退 + YTD）
  3. SQLite asset_holdings/transactions — 财务概览

关键修复：
  - 使用共享 financial_data 模块（月份回退 + YTD）
  - 积累型目标进度 = YTD储蓄 / 目标额（而非 总资产 / 目标额）
"""

import json
import logging
import time
from typing import Dict, Any, Optional
from app_paths import DATA_DIR

logger = logging.getLogger("ve5.chatbot.context_navigator")

# ── 缓存（避免每次对话都重算 allocation_engine）──
_nav_cache: Dict[str, Any] = {"ts": 0, "data": None}
_NAV_CACHE_TTL = 60  # 60 秒缓存


def build_navigation_context(intent: str = "", user_message: str = "") -> str:
    """
    构建上下文导航文本，注入到 LLM prompt 中。

    Args:
        intent: 当前意图（asset_doctor/goal_tracker/general_chat 等）
        user_message: 用户原始输入

    Returns:
        紧凑的上下文字符串，约 500-700 tokens
    """
    parts = ["[系统导航上下文 — 以下为权威计算结果，请引用而非重新计算]"]

    # ── 1. 财务概览 ──
    _append_financial_overview(parts)

    # ── 2. 资产配置（推荐配比 + 实际配比 + 偏差 + 评分）──
    _append_allocation_navigation(parts)

    # ── 3. 目标进度（动态计算，含 YTD）──
    _append_goal_progress(parts)

    # ── 4. 数据可用性清单（告知 LLM 有哪些数据可用）──
    _append_data_surface(parts)

    # ── 5. 导航指引 ──
    _append_navigation_guide(parts, intent, user_message)

    return "\n".join(parts)


# ════════════════════════════════════════════════
# 1. 财务概览（使用共享 financial_data 模块）
# ════════════════════════════════════════════════

def _append_financial_overview(parts: list):
    """紧凑的财务概览（含 YTD 数据）"""
    try:
        from core.ve5_chatbot.financial_data import load_financial_summary
        fin = load_financial_summary()

        total_assets = fin.get("total_assets", 0)
        income = fin.get("monthly_income", 0)
        expense = fin.get("monthly_expense", 0)
        savings = fin.get("monthly_savings", 0)
        data_month = fin.get("monthly_data_month", "")

        ytd_income = fin.get("ytd_income", 0)
        ytd_expense = fin.get("ytd_expense", 0)
        ytd_savings = fin.get("ytd_savings", 0)
        ytd_return = fin.get("ytd_investment_return", 0)

        month_label = f"({data_month}数据)" if data_month else ""
        parts.append(
            f"总资产:¥{total_assets:,.0f} "
            f"月收入:¥{income:,.0f} 月支出:¥{expense:,.0f} 月结余:¥{savings:,.0f} {month_label}"
        )
        if ytd_income > 0 or ytd_expense > 0:
            parts.append(
                f"年度累计(YTD):收入¥{ytd_income:,.0f} 支出¥{ytd_expense:,.0f} "
                f"储蓄¥{ytd_savings:,.0f} 投资收益¥{ytd_return:,.0f}"
            )
    except Exception as e:
        logger.debug(f"[NAV] 财务概览失败: {e}")


# ════════════════════════════════════════════════
# 2. 资产配置导航（核心：从 allocation_engine 获取权威数据）
# ════════════════════════════════════════════════

def _append_allocation_navigation(parts: list):
    """注入推荐配比、实际配比、偏差和评分"""
    try:
        report = _get_allocation_report()
        if not report:
            return

        framework = report.get("framework", {})
        actual = report.get("actual", {})
        overall = report.get("overall", {})
        dimensions = report.get("dimensions", [])
        advices = report.get("advices", [])

        # 推荐配比（framework target）
        fw_liquid = framework.get("liquid_pct", 0) * 100
        fw_stable = framework.get("stable_pct", 0) * 100
        fw_aggr = framework.get("aggressive_pct", 0) * 100
        parts.append(
            f"推荐配比:活钱{fw_liquid:.0f}% 稳健{fw_stable:.0f}% 进取{fw_aggr:.0f}%"
        )

        # 实际配比
        ac_liquid = actual.get("liquid_pct", 0) * 100
        ac_stable = actual.get("stable_pct", 0) * 100
        ac_aggr = actual.get("aggressive_pct", 0) * 100
        ac_prot = actual.get("protection_pct", 0) * 100
        parts.append(
            f"实际配比:流动{ac_liquid:.0f}% 稳健{ac_stable:.0f}% 进取{ac_aggr:.0f}% 保障{ac_prot:.0f}%"
        )

        # 综合评分
        score = overall.get("total_score", 0)
        status = overall.get("status", "")
        if score > 0:
            parts.append(f"健康度:{score:.0f}分({status})")

        # 三维度评分（紧凑）
        if dimensions:
            dim_parts = []
            for d in dimensions:
                name = d.get("name", "")
                sc = d.get("score", 0)
                st = d.get("status", "")
                dim_parts.append(f"{name}{sc:.0f}分")
            parts.append(f"维度评分:{' '.join(dim_parts)}")

        # 关键偏差和建议（仅取前2条，控制 token）
        if advices:
            advice_parts = []
            for a in advices[:2]:
                msg = a.get("message", "")[:80]
                if msg:
                    advice_parts.append(msg)
            if advice_parts:
                parts.append(f"系统建议:{';'.join(advice_parts)}")

    except Exception as e:
        logger.debug(f"[NAV] 配置导航失败: {e}")


def _get_allocation_report() -> Optional[dict]:
    """获取 allocation_engine 报告（带缓存）"""
    now = time.time()
    if _nav_cache["data"] and (now - _nav_cache["ts"]) < _NAV_CACHE_TTL:
        return _nav_cache["data"]

    try:
        from core.allocation_engine import ve4_alloc_generate_report
        report = ve4_alloc_generate_report()
        if report:
            data = report.to_dict() if hasattr(report, "to_dict") else {}
            _nav_cache["data"] = data
            _nav_cache["ts"] = now
            return data
    except Exception as e:
        logger.debug(f"[NAV] allocation_engine 报告生成失败: {e}")

    return None


# ════════════════════════════════════════════════
# 3. 目标进度（使用共享模块，含 YTD 储蓄计算）
# ════════════════════════════════════════════════

def _append_goal_progress(parts: list):
    """动态计算目标进度（含 YTD 储蓄，积累型目标用 YTD 而非总资产）"""
    try:
        from core.ve5_chatbot.financial_data import load_goals_with_progress
        goals = load_goals_with_progress()
        if not goals:
            return

        active = [g for g in goals if g.get("status") != "已达成"]
        if not active:
            return

        goal_parts = []
        for g in active[:3]:
            name = g.get("name", "?")
            progress = g.get("progress_pct", 0)
            goal_type = g.get("goal_type", "")
            type_label = "积累" if goal_type == "accumulation" else "金额"

            if g.get("months_needed", 0) > 0:
                months = g["months_needed"]
                goal_parts.append(f"{name}[{type_label}]{progress:.0f}%(约{months:.0f}月)")
            else:
                goal_parts.append(f"{name}[{type_label}]{progress:.0f}%")

        if goal_parts:
            parts.append(f"目标进度:{' '.join(goal_parts)}")

    except Exception as e:
        logger.debug(f"[NAV] 目标进度计算失败: {e}")


# ════════════════════════════════════════════════
# 4. 数据可用性清单（告知 LLM 有哪些数据可用）
# ════════════════════════════════════════════════

def _append_data_surface(parts: list):
    """
    动态生成数据可用性清单，告知 LLM：
    - 哪些数据已预加载到上下文中（可直接引用）
    - 哪些数据可按需展开（引导用户查看）
    - 数据来源和格式说明
    """
    try:
        from core.ve5_chatbot.financial_data import load_financial_summary, load_goals_with_progress
        fin = load_financial_summary()

        # 已加载的数据点
        loaded = []
        expandable = []

        # 财务概览
        if fin.get("total_assets", 0) > 0:
            loaded.append(f"总资产¥{fin['total_assets']:,.0f}")
        if fin.get("holdings_count", 0) > 0:
            loaded.append(f"持仓{fin['holdings_count']}条")
            expandable.append("持仓明细(名称/代码/市值/分类/占比)")
        if fin.get("monthly_income", 0) > 0 or fin.get("monthly_expense", 0) > 0:
            data_month = fin.get("monthly_data_month", "")
            month_tag = f"({data_month}数据)" if data_month else ""
            loaded.append(f"月收入¥{fin['monthly_income']:,.0f}/支出¥{fin['monthly_expense']:,.0f}/结余¥{fin['monthly_savings']:,.0f}{month_tag}")
        if fin.get("ytd_savings", 0) > 0:
            loaded.append(f"YTD储蓄¥{fin['ytd_savings']:,.0f}(收入¥{fin['ytd_income']:,.0f}-支出¥{fin['ytd_expense']:,.0f}+收益¥{fin.get('ytd_investment_return',0):,.0f})")

        # 分类汇总
        for cls, label in [("aggressive", "进取"), ("stable", "稳健"), ("liquid", "流动"), ("protection", "保障")]:
            val = fin.get(f"total_{cls}", 0)
            if val > 0:
                loaded.append(f"{label}¥{val:,.0f}")

        # 目标进度
        goals = load_goals_with_progress()
        if goals:
            for g in goals:
                pct = g.get("progress_pct", 0)
                name = g.get("name", "?")
                gtype = g.get("goal_type", "")
                type_tag = "积累" if gtype == "accumulation" else "金额"
                loaded.append(f"目标[{name}]{pct:.0f}%[{type_tag}]")

        # 资产配置
        report = _get_allocation_report()
        if report and report.get("framework"):
            loaded.append("推荐配比/实际配比/三维度评分")

        # 可展开数据
        if fin.get("transaction_count", 0) > 0:
            expandable.append(f"交易明细({fin['transaction_count']}条)")
        expandable.append("用户画像(风险偏好/投资期限)")

        # 构建清单文本
        surface_parts = ["[数据可用性清单 — 以下数据已预加载，请直接引用]"]
        if loaded:
            surface_parts.append("✓已加载: " + " | ".join(loaded))
        if expandable:
            surface_parts.append("📋可展开: " + " / ".join(expandable) + " (引导用户查看对应页面)")

        parts.append("\n".join(surface_parts))
    except Exception as e:
        logger.debug(f"[NAV] 数据清单生成失败: {e}")


def get_data_surface() -> str:
    """
    公开接口：返回数据可用性清单文本。
    供 mother_skill 或其他模块单独调用。
    """
    parts = []
    _append_data_surface(parts)
    return "\n".join(parts) if parts else ""


# ════════════════════════════════════════════════
# 5. 导航指引（约束 LLM 行为）
# ════════════════════════════════════════════════

_NAV_GUIDE = """[导航指引]
- 以上"推荐配比"和"维度评分"由系统引擎计算，是权威结果，请引用而非自行推算
- 风险分析应基于"实际配比 vs 推荐配比"的偏差，不要自行提出不同的配比建议
- 目标进度已动态计算：积累型目标=YTD储蓄/目标额，金额型目标=总资产/目标额
- 月度数据可能来自最近有数据的月份（非当月），请引用数据月份
- 上方"数据可用性清单"列出了所有已加载的数据，请基于这些数据分析，不要遗漏
- 如需"可展开"的数据，引导用户前往对应页面查看，不要臆测数据
- 如需更多数据，可引导用户前往「资产配置」页面查看完整报告"""


def _append_navigation_guide(parts: list, intent: str, user_message: str):
    """注入导航指引，约束 LLM 不与业务逻辑冲突"""
    parts.append(_NAV_GUIDE)


# ════════════════════════════════════════════════
# 便捷函数
# ════════════════════════════════════════════════

def get_allocation_summary() -> dict:
    """获取配比摘要（供 skill 内部使用）"""
    report = _get_allocation_report()
    if not report:
        return {}
    return {
        "framework": report.get("framework", {}),
        "actual": report.get("actual", {}),
        "overall": report.get("overall", {}),
        "dimensions": report.get("dimensions", []),
        "advices": report.get("advices", []),
    }


def invalidate_cache():
    """清除缓存（数据更新后调用）"""
    _nav_cache["data"] = None
    _nav_cache["ts"] = 0
