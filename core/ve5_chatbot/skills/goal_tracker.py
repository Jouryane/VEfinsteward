"""
VE管家 子skill — 目标追踪师 🎯
================================
追踪用户目标的进度、规划达成路径。
读取goals.json和目标相关财务数据，
计算每个目标的完成度和所需时间。

关键修复：
  1. 使用共享 financial_data 模块，含月份回退 + YTD 计算
  2. 积累型目标进度 = YTD储蓄 / 目标额（而非 总资产 / 目标额）
  3. 注入导航上下文，确保 LLM 拿到权威数据
"""

import json
import logging
from datetime import datetime
from typing import Dict, Any, List
from core.ai_gateway import ve4_ai_call
from app_paths import DATA_DIR
from core.ve5_chatbot.report_store import ve5_report_save
from core.ve5_chatbot.financial_data import (
    load_financial_summary,
    calculate_goal_progress,
    load_goals_with_progress,
)

logger = logging.getLogger("ve5.chatbot.goal_tracker")

_OUTPUT_DIR = DATA_DIR / "chatbot" / "skills"
_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
_GOAL_TRACK_FILE = _OUTPUT_DIR / "goal_tracking.json"


def _load_goals() -> List[Dict]:
    """加载用户目标"""
    goals_file = DATA_DIR / "goals.json"
    if not goals_file.exists():
        return []
    try:
        data = json.loads(goals_file.read_text(encoding="utf-8"))
        return data.get("goals", [])
    except Exception:
        return []


def ve5_skill_goal_tracker(user_message: str, history: List[Dict]) -> Dict[str, Any]:
    """目标追踪skill主入口"""
    goals = _load_goals()
    if not goals:
        return {
            "reply": "🎯 还没有设定任何目标。你可以前往「资产配置 → 目标规划」页面设定你的财务目标，比如旅行、购房、教育基金等。",
            "data": {},
            "cards": [],
            "actions": [
                {"label": "去设定目标", "action": "navigate", "url": "allocation-profile.html", "icon": "🎯"},
                {"label": "生成目标建议", "action": "rerun_skill", "skill": "goal_tracker", "icon": "✨"},
            ],
        }

    # ── 使用共享模块加载财务数据（含月份回退 + YTD）──
    finance = load_financial_summary()

    # ── 使用智能进度计算（积累型用 YTD储蓄，金额型用总资产）──
    goal_progress = [calculate_goal_progress(g, finance) for g in goals]

    savings = finance.get("monthly_savings", 0)
    total = finance.get("total_assets", 0)
    data_month = finance.get("monthly_data_month", "无数据")
    ytd_savings = finance.get("ytd_savings", 0)
    ytd_income = finance.get("ytd_income", 0)
    ytd_expense = finance.get("ytd_expense", 0)
    ytd_return = finance.get("ytd_investment_return", 0)

    # ── 构建包含 YTD 数据的上下文 ──
    context = f"""用户财务概况：
- 总资产：¥{total:,.0f}
- 持仓数：{finance.get('holdings_count', 0)} 条
- 月度数据月份：{data_month}
- 月收入：¥{finance.get('monthly_income', 0):,.0f}
- 月支出：¥{finance.get('monthly_expense', 0):,.0f}
- 月结余：¥{savings:,.0f}
- 年度累计收入(YTD)：¥{ytd_income:,.0f}
- 年度累计支出(YTD)：¥{ytd_expense:,.0f}
- 年度累计储蓄(YTD)：¥{ytd_savings:,.0f}
- 年度投资收益(YTD)：¥{ytd_return:,.0f}

目标进度：
"""
    for gp in goal_progress:
        goal_type_label = "积累型" if gp.get("goal_type") == "accumulation" else "金额型"
        context += (
            f"- {gp['icon']} {gp['name']} [{goal_type_label}]: "
            f"目标¥{gp['target_amount']:,.0f}，"
            f"当前¥{gp['current_amount']:,.0f}，"
            f"进度{gp['progress_pct']}%，"
            f"剩余¥{gp['remaining_amount']:,.0f}，"
            f"预计还需{gp['months_needed']}个月\n"
        )

    # ── 注入导航上下文（权威数据 + 导航指引）──
    from core.ve5_chatbot.context_navigator import build_navigation_context
    nav_ctx = build_navigation_context("goal_tracker", user_message)

    system = f"""你是VE管家的目标追踪师。根据用户的财务目标和当前资产状况，分析目标达成路径。

{nav_ctx}

分析维度：
1. 每个目标的当前进度和预计达成时间（进度已由系统计算，请引用百分比）
2. 月度结余对目标达成的影响
3. 年度累计储蓄(YTD)对年度积累目标的贡献
4. 目标之间的优先级建议
5. 如果月度结余不足，给出具体的节流或开源建议
6. 达成路径的关键里程碑

重要规则：
- 积累型目标进度 = 年度累计储蓄 / 目标额 × 100%
- 金额型目标进度 = 当前可调配资产 / 目标额 × 100%
- 进度百分比已由系统动态计算，请直接引用，不要自行重新推算
- 如需年度投资收益数据，请引用上方 YTD 数据

风格：鼓励性、务实、给出清晰的时间线。"""

    prompt = f"""{context}

用户需求：{user_message}

请进行目标追踪分析。"""

    result = ve4_ai_call(
        task_type="goal_tracking",
        system=system,
        prompt=prompt,
        format_type="text",
        complexity="high",
        max_tokens=2048,
        contains_privacy_data=True,
    )

    reply = result.text if result.success else "抱歉，目标分析失败。"

    data = {"goals": goal_progress, "finance": finance}
    _GOAL_TRACK_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 多版本存储 ──
    achieved = sum(1 for g in goal_progress if g["status"] == "已达成")
    in_progress = sum(1 for g in goal_progress if g["status"] == "进行中")
    metadata = {
        "title": f"{datetime.now().strftime('%m月%d日')} 目标追踪",
        "goal_count": len(goal_progress),
        "achieved": achieved,
        "in_progress": in_progress,
        "total_assets": total,
        "monthly_savings": savings,
        "ytd_savings": ytd_savings,
    }
    report_id = ve5_report_save("goal", data, metadata)

    # ── 检查是否已有同类型经验（避免重复创建） ──
    has_experience = False
    try:
        from core.experience_store import exp_list
        existing = exp_list(exp_type="goal_tracking")
        has_experience = len(existing) > 0
    except Exception:
        pass

    cards = []
    for gp in goal_progress:
        cards.append({
            "type": "goal_progress",
            "title": f"{gp['icon']} {gp['name']}",
            "data": gp,
        })

    actions = [
        {"label": "目标规划详情", "action": "navigate", "url": "allocation-profile.html", "icon": "🎯"},
        {"label": "资产配置", "action": "navigate", "url": "asset-allocation.html", "icon": "📊"},
        {"label": "重新分析", "action": "rerun_skill", "skill": "goal_tracker", "icon": "🔄"},
    ]

    # 如果没有经验则添加"保存为长期经验"按钮
    if not has_experience:
        actions.insert(0, {
            "label": "保存为长期经验",
            "action": "save_experience",
            "skill": "goal_tracker",
            "report_id": report_id,
            "report_type": "goal",
            "icon": "⚡",
        })

    return {"reply": reply, "reasoning": result.reasoning.strip() if result.success else "", "data": data, "cards": cards, "actions": actions, "report_id": report_id}
