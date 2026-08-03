"""
VE管家 子skill — 生活规划师 🍽
================================
两步工作流：
  Step1: LLM 输出温暖的人话总结（直接展示给用户）
  Step2: LLM 输出结构化 JSON（存入文件，供结果页读取，不展示给用户）

读取用户的总资产、收入、支出等财务信息，
获取用户的"目标捕获"（目标及资金需求），
结合当前情况为用户规划生活开支计划。
"""

import json
import re
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, List
from core.ai_gateway import ve4_ai_call
from app_paths import DATA_DIR, DB_PATH
from core.ve5_chatbot.report_store import ve5_report_save

logger = logging.getLogger("ve5.chatbot.life_planner")

_OUTPUT_DIR = DATA_DIR / "chatbot" / "skills"
_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
_LIFE_PLAN_FILE = _OUTPUT_DIR / "life_plan.json"


def _load_financial_summary() -> Dict[str, Any]:
    """加载财务摘要（含支出明细目录）— 使用共享模块 + 月份回退"""
    try:
        # 使用共享模块获取基础财务数据（含月份回退 + YTD）
        from core.ve5_chatbot.financial_data import load_financial_summary as _shared_load
        base = _shared_load()

        data_month = base.get("monthly_data_month", datetime.now().strftime("%Y-%m"))

        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row

        # ── 支出明细目录（按 category_primary 分组，使用回退后的月份）──
        expense_cats = conn.execute(
            "SELECT category_primary, SUM(amount) as total, COUNT(*) as cnt "
            "FROM transactions WHERE transaction_type='expense' AND transaction_date LIKE ? "
            "GROUP BY category_primary ORDER BY total DESC",
            (f"{data_month}%",)
        ).fetchall()
        expense_breakdown = [
            {"category": r["category_primary"], "amount": r["total"], "count": r["cnt"]}
            for r in expense_cats
        ]

        # ── 上月支出总额（用于环比对比）──
        prev_month = (datetime.now().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        prev_expense = conn.execute(
            "SELECT SUM(amount) FROM transactions WHERE transaction_type='expense' AND transaction_date LIKE ?",
            (f"{prev_month}%",)
        ).fetchone()[0] or 0

        conn.close()
        return {
            "total_assets": base.get("total_assets", 0),
            "monthly_income": base.get("monthly_income", 0),
            "monthly_expense": base.get("monthly_expense", 0),
            "monthly_savings": base.get("monthly_savings", 0),
            "ytd_savings": base.get("ytd_savings", 0),
            "expense_breakdown": expense_breakdown,
            "prev_month_expense": prev_expense,
            "month": data_month,
        }
    except Exception as e:
        logger.warning(f"[LIFE_PLANNER] 财务数据加载失败：{e}")
        return {"total_assets": 0, "monthly_income": 0, "monthly_expense": 0,
                "monthly_savings": 0, "ytd_savings": 0,
                "expense_breakdown": [], "prev_month_expense": 0,
                "month": datetime.now().strftime("%Y-%m")}


def _load_goals() -> List[Dict]:
    """加载用户目标"""
    goals_file = DATA_DIR / "goals.json"
    if not goals_file.exists():
        return []
    try:
        return json.loads(goals_file.read_text(encoding="utf-8")).get("goals", [])
    except Exception:
        return []


def _load_rag_habits() -> Dict[str, Any]:
    """从RAG系统读取用户习惯"""
    try:
        from tactical.fundamental.knowledge.vector_store import ve4_kb_search
        results = ve4_kb_search("饮食 购物 习惯 生活方式 食谱", top_k=5)
        habits = []
        for r in results:
            habits.append({
                "title": r.get("title", ""),
                "summary": r.get("summary", ""),
                "source": r.get("source", ""),
            })
        return {"habits": habits}
    except Exception as e:
        logger.warning(f"[LIFE_PLANNER] RAG习惯读取失败：{e}")
        return {"habits": []}


def _build_context() -> str:
    """构建用户上下文（财务 + 目标 + 习惯 + 区域物价）"""
    finance = _load_financial_summary()
    goals = _load_goals()
    habits = _load_rag_habits()

    # ── 区域物价上下文（三重组合）──
    regional = ""
    try:
        from core.regional_price import build_regional_context
        regional = build_regional_context()
    except Exception as e:
        logger.warning(f"[LIFE_PLANNER] 区域物价上下文获取失败：{e}")

    context = f"""用户财务概况：
- 总资产：¥{finance['total_assets']:,.0f}
- 本月收入：¥{finance['monthly_income']:,.0f}（数据月份：{finance['month']}）
- 本月支出：¥{finance['monthly_expense']:,.0f}
- 月结余：¥{finance.get('monthly_savings', 0):,.0f}
- 年度累计储蓄(YTD)：¥{finance.get('ytd_savings', 0):,.0f}
- 上月支出：¥{finance.get('prev_month_expense', 0):,.0f}（用于环比参考）

本月支出明细（按分类）：
"""
    for cat in finance.get("expense_breakdown", []):
        context += f"- {cat['category']}：¥{cat['amount']:,.0f}（{cat['count']}笔）\n"

    context += f"""
用户目标：
"""
    for g in goals[:4]:
        context += f"- {g.get('icon','')} {g.get('name','')}（预估¥{g.get('estimated_cost',0):,.0f}，{g.get('horizon','')}）\n"

    if habits["habits"]:
        context += "\n用户习惯参考：\n"
        for h in habits["habits"][:3]:
            context += f"- {h['title']}: {h['summary'][:80]}...\n"

    if regional:
        context += f"\n{regional}\n"

    return context


# ─── Step1: 人话回复 ───

_STEP1_SYSTEM = """你是VE管家的生活规划师。你刚为用户完成了一周的生活规划（食谱、购物清单、娱乐安排）。

你的任务：用温暖、简洁的语气告诉用户规划已经完成，引导用户查看详情。

要求：
- 像朋友一样说话，不要机械
- 不要给用户起任何昵称，直接用"你"称呼
- 不要编造具体的预算金额或购物数量（这些会在详情页中展示，你不需要在这里说）
- 可以提到"食谱""购物清单""娱乐安排"等概念
- 不要输出任何JSON、代码块、markdown标记
- 控制在60字以内
- 结尾用一句引导语，如"点击下方按钮查看食谱和购物清单吧" """

_STEP1_PROMPT = """{context}

用户需求：{user_message}

规划已完成，请用温暖的语气告诉用户。"""


# ─── Step2: 结构化JSON ───

_STEP2_SYSTEM = """你是数据结构化引擎。请根据用户的财务状况、所在区域物价和真实世界物价，生成一份合理的生活规划JSON。

用户的财务数据（总资产、月收入、月支出、支出明细目录）已在prompt中提供。此外，prompt中还会包含以下参考信息：
- 用户所在区域（城市、周边商铺）
- 用户近期购物价格（实际支付记录，最准确）
- 政府批发市场参考价格（批发价，可作为底价）
- 季节价格波动系数（当前月份哪些商品便宜、哪些偏贵）
- 消费档次模型（用户的价格系数和消费特征）

你的任务是：
1. 根据用户的月收入、月支出和**支出明细目录**，推导一个合理的**周生活预算**（weekly_budget）
   - 支出明细中包含餐饮、居住、交通、日用等各类目的的金额和笔数
   - 周预算应反映用户真实的消费水平，而不是凭空设定
   - 居住类（房租/房贷）属于固定支出，不应计入周生活预算
   - 周预算 ≈ (月支出 - 居住类固定支出) / 4.33，但你可以根据自己的理解适当调整
2. 将周预算拆分为合理的分项：伙食费、日用品、娱乐休闲、机动灵活
3. 为每一天设计具体的食谱，价格必须符合用户所在城市的真实物价和消费档次
4. 生成对应的购物清单，价格同样需符合真实物价和消费档次
5. 设计合理的娱乐安排

**价格确定规则（优先级从高到低）：**
1. 优先使用 prompt 中"用户近期购物价格"的数据（这是用户实际支付的价格，最准确）
2. 其次参考"政府批发市场参考价格"（批发价通常低于零售价，可作为底价参考）
3. 参考"季节价格波动系数"：当前月份便宜的商品优先推荐，偏贵的商品减少使用
4. 参考"消费档次"：价格系数决定整体价格水平（经济型×0.7，标准型×1.0，优质型×1.3，轻奢型×1.8）
5. 如果以上都没有某商品的价格，使用你对中国城市物价的常识推断
6. 必须考虑用户所在城市的物价水平（一线城市 > 二线城市 > 三四线城市）

**通用价格参考（2026年中国日常物价，当区域数据缺失时备用）：**
- 早餐（包子/豆浆/鸡蛋/牛奶等）：8-18元
- 午餐（面食/米饭+菜/快餐等）：18-35元
- 晚餐（家常菜/汤/米饭等）：15-30元
- 生鲜食材：鸡蛋约1-1.5元/枚，猪肉约25-35元/斤，蔬菜约3-8元/斤，大米约3-5元/斤
- 日用品：洗洁精约8-15元，纸巾约15-25元/提，洗发水约20-40元

输出格式（只输出JSON，不要有其他文字）：
{{
  "weekly_budget": <整数，根据用户财务状况和消费档次合理推导>,
  "budget_breakdown": {{
    "伙食费": <整数，约占总预算50-70%>,
    "日用品": <整数，约占总预算10-20%>,
    "娱乐休闲": <整数，约占总预算5-15%>,
    "机动灵活": <整数，约占总预算5-15%>
  }},
  "recipes": [
    {{"day": "周一", "breakfast": "具体食物", "lunch": "具体食物", "dinner": "具体食物", "estimated_cost": <当天三餐总花费>}}
    ...共7天，每餐写具体食物，不要写"..."
  ],
  "shopping_list": [
    {{"item": "具体物品名称（带数量/规格）", "category": "食材/日用品", "priority": "高/中/低", "estimated_price": <整数>}}
    ...至少10项
  ],
  "entertainment": [
    {{"activity": "活动名称", "day": "周几", "budget": <整数，可为0表示免费>, "reason": "推荐理由"}}
    ...至少2项
  ],
  "disclaimer": "本规划中的价格参考了历史季节波动数据和所在城市物价水平，为历史回测结果，可能与实时价格存在差异。实际价格受天气、运输、供需等多因素影响，仅供参考。"
}}

约束：
- budget_breakdown各项之和 = weekly_budget
- 7天recipes的estimated_cost之和 ≈ budget_breakdown中的伙食费
- shopping_list各项estimated_price之和 ≈ budget_breakdown中的日用品 + 机动灵活
- entertainment各项budget之和 ≈ budget_breakdown中的娱乐休闲
- 所有价格必须符合真实物价，不要虚构不合理的价格
- 所有价格用整数数字（不要带¥符号）
- 优先推荐当前季节便宜/时令的食材
- 消费档次系数必须体现在价格中（如经济型用户的三餐价格应低于标准型）
- disclaimer字段必须包含，说明价格可能存在偏差"""

_STEP2_PROMPT = """{context}

用户需求：{user_message}

请输出完整的生活规划JSON。"""


# ─── Step2 精简版（重试用）───

_STEP2_SYSTEM_LITE = """你是数据结构化引擎。生成一份精简的生活规划JSON。

输出格式（只输出JSON）：
{{
  "weekly_budget": <整数>,
  "budget_breakdown": {{
    "伙食费": <整数>,
    "日用品": <整数>,
    "娱乐休闲": <整数>,
    "机动灵活": <整数>
  }},
  "recipes": [
    {{"day": "周一", "breakfast": "食物", "lunch": "食物", "dinner": "食物", "estimated_cost": <整数>}},
    {{"day": "周二", "breakfast": "食物", "lunch": "食物", "dinner": "食物", "estimated_cost": <整数>}},
    {{"day": "周三", "breakfast": "食物", "lunch": "食物", "dinner": "食物", "estimated_cost": <整数>}}
  ],
  "shopping_list": [
    {{"item": "物品", "category": "食材", "priority": "高", "estimated_price": <整数>}},
    {{"item": "物品", "category": "食材", "priority": "中", "estimated_price": <整数>}},
    {{"item": "物品", "category": "日用品", "priority": "低", "estimated_price": <整数>}},
    {{"item": "物品", "category": "食材", "priority": "高", "estimated_price": <整数>}},
    {{"item": "物品", "category": "日用品", "priority": "中", "estimated_price": <整数>}}
  ],
  "entertainment": [
    {{"activity": "活动", "day": "周六", "budget": <整数>, "reason": "理由"}}
  ],
  "disclaimer": "本规划价格参考历史数据，可能与实时价格存在差异，仅供参考。"
}}

约束：budget_breakdown各项之和 = weekly_budget。所有价格为整数。"""


def ve5_skill_life_planner(user_message: str, history: List[Dict]) -> Dict[str, Any]:
    """生活规划skill主入口 — 两步工作流"""
    context = _build_context()

    # ── Step1: 人话回复 ──
    reply = ""
    reasoning = ""
    try:
        result = ve4_ai_call(
            task_type="life_planning_chat",
            system=_STEP1_SYSTEM,
            prompt=_STEP1_PROMPT.format(context=context, user_message=user_message),
            format_type="text",
            complexity="medium",
            max_tokens=256,
            contains_privacy_data=True,
        )
        reply = result.text.strip() if result.success else ""
        reasoning = result.reasoning.strip() if result.success else ""
        if not reply and reasoning:
            reply = "本周生活规划已生成，点击下方按钮查看详情。"
    except Exception as e:
        logger.warning(f"[LIFE_PLANNER] step1失败：{e}")
        reply = "本周生活规划已生成，点击下方按钮查看详情。"

    # ── Step2: 结构化JSON（存文件，不展示给用户） ──
    data = {}
    report_id = ""
    raw_text = ""  # 统一在 try/except 外读取，供修复路径使用
    try:
        result2 = ve4_ai_call(
            task_type="life_planning_structured",
            system=_STEP2_SYSTEM,
            prompt=_STEP2_PROMPT.format(context=context, user_message=user_message),
            format_type="json",
            complexity="high",
            max_tokens=8192,
            contains_privacy_data=True,
        )
        if result2.success:
            raw_text = result2.text.strip()
            # gateway 已做归一化：推理模型 content 为空时 is_truncated=True
            # 不再从 reasoning 暴力提取 JSON（reasoning 中的 JSON 不可信）
            if result2.is_truncated:
                logger.info(f"[LIFE_PLANNER] step2 gateway标记截断，直接进入修复流程 (text长度={len(raw_text)})")

        if raw_text:
            # 移除 markdown 代码块包裹
            if raw_text.startswith("```"):
                m = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw_text)
                if m:
                    raw_text = m.group(1).strip()
            # 如果 gateway 标记截断，直接走修复流程
            if result2.is_truncated:
                data = _repair_broken_json(raw_text, _skip_llm=False)
            else:
                data = _parse_json_robust(raw_text)

            if data and data.get("weekly_budget"):
                context_summary = _build_context_summary()
                metadata = {
                    "title": datetime.now().strftime("%m月%d日 生活规划"),
                    "weekly_budget": data.get("weekly_budget", 0),
                    "recipes_count": len(data.get("recipes", [])),
                    "shopping_count": len(data.get("shopping_list", [])),
                    "entertainment_count": len(data.get("entertainment", [])),
                }
                if context_summary.get("location"):
                    metadata["location"] = context_summary["location"]
                if context_summary.get("tier"):
                    metadata["tier"] = context_summary["tier"]
                if context_summary.get("season_month"):
                    metadata["season_month"] = context_summary["season_month"]

                report_id = ve5_report_save("life_plan", data, metadata)
                _LIFE_PLAN_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

                # ── 触发 Bot 云端渠道 ──
                _trigger_life_plan_bots(data, metadata)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"[LIFE_PLANNER] step2 JSON解析失败：{e}")
        # 尝试用更强的修复手段
        if raw_text:
            data = _repair_broken_json(raw_text)
        if not data:
            # 回退到已有文件
            if _LIFE_PLAN_FILE.exists():
                try:
                    data = json.loads(_LIFE_PLAN_FILE.read_text(encoding="utf-8"))
                    logger.info("[LIFE_PLANNER] step2 回退到已有文件")
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"[LIFE_PLANNER] step2失败：{e}")
        if _LIFE_PLAN_FILE.exists():
            try:
                data = json.loads(_LIFE_PLAN_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass

    # ── Step2 重试：精简版 prompt（仅在 data 仍为空时触发）──
    if not data or not data.get("weekly_budget"):
        logger.info("[LIFE_PLANNER] step2 首次生成失败/数据不完整，启动精简版重试...")
        try:
            result_lite = ve4_ai_call(
                task_type="life_planning_structured",
                system=_STEP2_SYSTEM_LITE,
                prompt=_STEP2_PROMPT.format(context=context, user_message=user_message),
                format_type="json",
                complexity="medium",
                max_tokens=4096,
                contains_privacy_data=True,
            )
            if result_lite.success:
                lite_text = result_lite.text.strip()
                # gateway 已做归一化，不再从 reasoning 提取
                if result_lite.is_truncated:
                    logger.info(f"[LIFE_PLANNER] step2重试 gateway标记截断 (text长度={len(lite_text)})")
                if lite_text:
                    if lite_text.startswith("```"):
                        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", lite_text)
                        if m:
                            lite_text = m.group(1).strip()
                    if result_lite.is_truncated:
                        data = _repair_broken_json(lite_text)
                    else:
                        data = _parse_json_robust(lite_text)
                    if data and data.get("weekly_budget"):
                        logger.info("[LIFE_PLANNER] step2 精简版重试成功")

            if data and data.get("weekly_budget"):
                context_summary = _build_context_summary()
                metadata = {
                    "title": datetime.now().strftime("%m月%d日 生活规划"),
                    "weekly_budget": data.get("weekly_budget", 0),
                    "recipes_count": len(data.get("recipes", [])),
                    "shopping_count": len(data.get("shopping_list", [])),
                    "entertainment_count": len(data.get("entertainment", [])),
                    "retry": True,
                }
                if context_summary.get("location"):
                    metadata["location"] = context_summary["location"]
                if context_summary.get("tier"):
                    metadata["tier"] = context_summary["tier"]
                if context_summary.get("season_month"):
                    metadata["season_month"] = context_summary["season_month"]

                report_id = ve5_report_save("life_plan", data, metadata)
                _LIFE_PLAN_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                _trigger_life_plan_bots(data, metadata)
            else:
                logger.warning("[LIFE_PLANNER] step2 精简版重试也失败")
        except Exception as e:
            logger.warning(f"[LIFE_PLANNER] step2 精简版重试异常：{e}")

    # ── 构建上下文摘要卡片（让用户看到后端逻辑）──
    context_summary = _build_context_summary()

    # ── 构建卡片和操作按钮 ──
    cards = []
    if context_summary:
        cards.append({
            "type": "context_summary",
            "title": "规划上下文",
            "data": context_summary,
        })
    if data:
        budget = data.get("weekly_budget", 0)
        shopping_count = len(data.get("shopping_list", []))
        entertainment_count = len(data.get("entertainment", []))
        disclaimer = data.get("disclaimer", "")
        cards.append({
            "type": "life_plan_summary",
            "title": "本周生活规划",
            "data": {
                "weekly_budget": budget,
                "shopping_count": shopping_count,
                "entertainment_count": entertainment_count,
                "disclaimer": disclaimer,
            }
        })

    actions = [
        {"label": "查看食谱详情", "action": "navigate", "url": f"chatbot-result.html?type=life_plan&tab=recipes&report_id={report_id}", "icon": "📅"},
        {"label": "查看购物清单", "action": "navigate", "url": f"chatbot-result.html?type=life_plan&tab=shopping&report_id={report_id}", "icon": "🛒"},
        {"label": "重新规划", "action": "rerun_skill", "skill": "life_planner", "icon": "🔄"},
    ]

    # 如果没有experience则添加保存按钮
    has_experience = False
    if report_id:
        try:
            from core.experience_store import exp_list
            existing = exp_list()
            has_experience = any(e.get("source_report_id") == report_id for e in existing)
        except Exception:
            pass
    if not has_experience:
        actions.insert(0, {
            "label": "保存为长期经验",
            "action": "save_experience",
            "skill": "life_planner",
            "report_id": report_id,
            "report_type": "life_plan",
            "icon": "⚡",
        })

    return {"reply": reply, "reasoning": reasoning, "data": data, "cards": cards, "actions": actions, "report_id": report_id}


def _build_context_summary() -> Dict[str, Any]:
    """构建上下文摘要，供前端展示后端workflow状态"""
    summary = {}

    # 位置信息
    try:
        from core.regional_price import get_location_settings
        loc = get_location_settings()
        province = loc.get("province", "")
        city = loc.get("city", "")
        district = loc.get("district", "") or loc.get("county", "")
        if city:
            summary["location"] = f"{province}{city}{district}".strip() if province else f"{city}{district}".strip()
            summary["location_set"] = True
        else:
            summary["location"] = "未设置（请在设置中选择所在区域）"
            summary["location_set"] = False
    except Exception:
        summary["location"] = "未设置"
        summary["location_set"] = False

    # 消费档次
    try:
        from core.consumption_tier import get_tier_status
        tier_status = get_tier_status(summary.get("location", ""))
        config = tier_status.get("config", {})
        summary["tier"] = config.get("name", "标准型")
        summary["tier_multiplier"] = config.get("multiplier", 1.0)
        summary["tier_is_manual"] = tier_status.get("is_manual", False)
        summary["tier_auto"] = tier_status.get("auto_detected_tier") or tier_status.get("effective_tier", "standard")
    except Exception:
        summary["tier"] = "标准型"
        summary["tier_multiplier"] = 1.0
        summary["tier_is_manual"] = False

    # 季节信息
    try:
        from core.seasonal_price import get_season_context
        season_text = get_season_context()
        import re
        m = re.search(r"(\d+)月", season_text)
        summary["season_month"] = int(m.group(1)) if m else 0
        m2 = re.search(r"当前时令/便宜商品：(.+)", season_text)
        if m2:
            summary["season_cheap"] = m2.group(1)[:60] + "..." if len(m2.group(1)) > 60 else m2.group(1)
        else:
            summary["season_cheap"] = ""
    except Exception:
        summary["season_month"] = 0
        summary["season_cheap"] = ""

    # 数据源状态
    summary["data_sources"] = []
    try:
        from core.regional_price import _get_user_location
        loc = _get_user_location()
        amap_key = loc.get("amap_key", "")
        summary["data_sources"].append({
            "name": "周边商铺",
            "status": "已加载" if amap_key else "未配置高德Key",
        })
    except Exception:
        summary["data_sources"].append({"name": "周边商铺", "status": "未加载"})

    try:
        from core.consumer_price_rag import ve5_price_rag_stats
        stats = ve5_price_rag_stats()
        summary["data_sources"].append({
            "name": "用户购物价格",
            "status": f"{stats.get('total_records', 0)}条记录" if stats.get("available") else "暂无记录",
        })
    except Exception:
        summary["data_sources"].append({"name": "用户购物价格", "status": "暂无记录"})

    summary["data_sources"].append({"name": "季节波动系数", "status": "已加载"})
    summary["data_sources"].append({"name": "消费档次模型", "status": "已加载"})

    return summary


def _trigger_life_plan_bots(data: Dict, metadata: Dict):
    """当生活计划生成后，推送到所有已配置的 life_planner bot"""
    try:
        from core.cloud_bot_manager import bot_list, bot_send_message
        bots = bot_list()
        for bot in bots:
            if not bot.get("is_active"):
                continue
            if bot.get("source_module") != "life_planner":
                continue
            rules = bot.get("trigger_rules", {})
            if not rules.get("on_plan_created"):
                continue
            # 格式化消息
            content = _format_life_plan_message(data, metadata)
            result = bot_send_message(bot["bot_id"], content, "life_plan_created")
            if result.get("success"):
                logger.info(f"[LIFE_PLANNER] Bot推送成功: {bot['bot_id']}")
            else:
                logger.warning(f"[LIFE_PLANNER] Bot推送失败: {bot['bot_id']}: {result.get('error', '')}")
    except Exception as e:
        logger.warning(f"[LIFE_PLANNER] Bot推送异常: {e}")


def _format_life_plan_message(data: Dict, metadata: Dict) -> str:
    """格式化生活管家播报消息"""
    lines = ["**本周生活计划** (" + metadata.get("title", "") + ")\n"]
    weekly = data.get("weekly_budget", 0)
    lines.append(f"**预算**: ¥{weekly}/周\n")

    recipes = data.get("recipes", [])
    if recipes:
        lines.append("**食谱菜单**:")
        for r in recipes[:7]:
            if isinstance(r, dict):
                # 探测字段名：优先 day+三餐 结构，其次 recipe/name/meal/dish/title
                rkeys = set(r.keys())
                logger.debug(f"[LIFE_PLANNER] recipe keys: {rkeys}")

                name = ""
                cost = r.get("estimated_cost") or r.get("cost") or r.get("budget") or 0

                # 模式1: day + breakfast/lunch/dinner
                day = r.get("day", "")
                bf = r.get("breakfast", "")
                lh = r.get("lunch", "")
                dn = r.get("dinner", "")
                if day and (bf or lh or dn):
                    parts = [day]
                    if bf: parts.append(f"早餐: {bf}")
                    if lh: parts.append(f"午餐: {lh}")
                    if dn: parts.append(f"晚餐: {dn}")
                    name = " | ".join(parts)
                # 模式2: 通用名称字段
                if not name:
                    for k in ("recipe", "name", "meal", "dish", "title", "description", "day"):
                        v = r.get(k, "")
                        if v and isinstance(v, str) and v.strip():
                            name = v.strip()
                            break
                # 模式3: 如果连 value 字段都没有，拼所有 text 字段
                if not name:
                    text_parts = []
                    for k, v in r.items():
                        if k in ("estimated_cost", "cost", "budget"):
                            continue
                        if isinstance(v, str) and v.strip():
                            text_parts.append(f"{k}: {v}")
                    name = ", ".join(text_parts)

                if not name:
                    name = "(无名称)"

                lines.append(f"- {name} (约¥{cost})")
            else:
                lines.append(f"- {str(r)}")
        lines.append("")

    shopping = data.get("shopping_list", [])
    if shopping:
        lines.append("**购物清单**:")
        for s in shopping[:10]:
            if isinstance(s, dict):
                item = s.get("item") or s.get("name") or s.get("product") or ""
                qty = s.get("quantity") or s.get("qty") or 1
                price = s.get("estimated_price") or s.get("price") or s.get("unit_price") or 0
                priority = s.get("priority", "")
                cat = s.get("category", "")

                desc = item
                if qty and qty != 1:
                    desc += f" x{qty}"
                if price:
                    desc += f" 约¥{price}"
                if priority:
                    desc += f" [{priority}优先]"
                if cat:
                    desc += f" ({cat})"
                lines.append(f"- {desc}")
            else:
                lines.append(f"- {str(s)}")
        lines.append("")

    entertainment = data.get("entertainment", [])
    if entertainment:
        lines.append("**娱乐安排**:")
        for e in entertainment[:5]:
            if isinstance(e, dict):
                # 探测所有可能的字段
                ekeys = set(e.keys())
                activity = e.get("activity") or e.get("name") or e.get("title") or ""
                day = e.get("day", "")
                budget = e.get("budget") or e.get("estimated_cost") or e.get("price") or 0
                reason = e.get("reason", "")

                parts = [activity] if activity else []
                if day:
                    parts.append(day)
                if budget:
                    parts.append(f"预算¥{budget}")
                if reason:
                    parts.append(reason)
                if not parts:
                    # 终极兜底：显示所有 text 字段
                    for k, v in e.items():
                        if isinstance(v, str) and v.strip():
                            parts.append(f"{k}: {v}")
                lines.append(f"- {' | '.join(parts) if parts else '(无信息)'}")
            else:
                lines.append(f"- {str(e)}")
        lines.append("")

    if data.get("tips"):
        lines.append(f"\n**小贴士**: {data['tips']}")

    logger.debug(f"[LIFE_PLANNER] 格式化播报完成, 行数={len(lines)}")
    return "\n".join(lines)


def _parse_json_robust(raw: str) -> Dict:
    """健壮 JSON 解析：先直接解析，失败后逐级修复"""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    return _repair_broken_json(raw)


def _repair_truncated_json(raw: str) -> Dict:
    """修复被截断的 JSON — 在最后一个完整元素处截断，补全括号"""
    text = raw.strip()

    # 从第一个 { 开始
    start = text.find('{')
    if start < 0:
        return {}
    text = text[start:]

    # 如果括号已平衡，不是截断问题
    if text.count('{') == text.count('}') and text.count('[') == text.count(']'):
        return {}

    # 扫描找所有"安全截断点"：字符串外的逗号或右括号位置
    safe_points = []  # [(截断位置, 需要补全的括号列表)]
    in_str = False
    esc = False
    stack = []

    for i, ch in enumerate(text):
        if esc:
            esc = False
            continue
        if ch == '\\':
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == '{':
            stack.append('}')
        elif ch == '[':
            stack.append(']')
        elif ch in ('}', ']'):
            if stack and stack[-1] == ch:
                stack.pop()
                safe_points.append((i, list(stack)))
        elif ch == ',':
            # 逗号前是一个完整元素
            safe_points.append((i, list(stack)))

    # 从后向前尝试每个安全截断点
    for pos, st in reversed(safe_points):
        truncated = text[:pos]  # 不包含逗号/右括号本身
        # 移除尾部不完整的 key-value（如 "key": 后面没有值）
        truncated = re.sub(r'"[^"]*"\s*:\s*$', '', truncated.rstrip())
        # 移除尾部逗号和空白
        truncated = re.sub(r',\s*$', '', truncated).rstrip()
        if not truncated or not (truncated[-1] in '"}]el' or truncated[-1].isdigit()):
            # 确保截断点在一个完整值之后
            continue
        # 补全括号（栈底→栈顶 = 外→内，反转后从内到外闭合）
        closing = ''.join(reversed(st))
        candidate = truncated + closing
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                logger.info(f"[LIFE_PLANNER] step2 截断JSON修复成功 (原{len(raw)}→修{len(candidate)}字符)")
                return result
        except (json.JSONDecodeError, ValueError):
            continue

    return {}


def _repair_broken_json(raw: str, _skip_llm: bool = False) -> Dict:
    """逐级修复损坏的 JSON"""
    fixes = []
    text = raw.strip()

    # 提取最外层 {...}
    m = re.search(r'\{[\s\S]*\}', text)
    if m:
        text = m.group(0)

    fixes.append(text)

    # 修复1：去除尾部逗号
    text2 = re.sub(r',(\s*[}\]])', r'\1', text)
    fixes.append(text2)

    # 修复2：补全括号
    pairs = [
        ('{', '}'), ('[', ']'),
    ]
    for op, cl in pairs:
        diff = text2.count(op) - text2.count(cl)
        if diff > 0:
            text2 += cl * diff
    fixes.append(text2)

    # 修复3：LLM 有时在字符串内插入了未转义的换行（常见在多行描述中）
    text3 = re.sub(r'([^\\\n])"\s*\n\s*"', r'\1\\n"', text2)
    fixes.append(text3)

    # 尝试逐个修复版本
    for i, f in enumerate(fixes):
        try:
            result = json.loads(f)
            if i > 0:
                logger.info(f"[LIFE_PLANNER] step2 JSON修复成功（策略{i}）")
            return result
        except (json.JSONDecodeError, ValueError):
            continue

    # 修复4：AST 策略
    try:
        import ast
        py_text = re.sub(r'\bnull\b', 'None', text2)
        py_text = re.sub(r'\btrue\b', 'True', py_text)
        py_text = re.sub(r'\bfalse\b', 'False', py_text)
        result = ast.literal_eval(py_text)
        if isinstance(result, dict):
            logger.info("[LIFE_PLANNER] step2 JSON修复成功（ast策略）")
            return result
    except Exception:
        pass

    # 修复5：截断修复 — 专为 LLM 输出被截断的场景设计
    truncated_result = _repair_truncated_json(raw)
    if truncated_result:
        return truncated_result

    # 修复6：让 LLM 自我修复（跳过递归调用）
    if not _skip_llm:
        try:
            fixed = _llm_fix_json(raw)
            if fixed:
                return fixed
        except Exception:
            pass

    logger.warning("[LIFE_PLANNER] step2 所有JSON修复策略均失败")
    return {}


def _llm_fix_json(broken_text: str) -> Dict:
    """让 LLM 修复损坏的 JSON（仅限严重损坏时）"""
    prompt = f"""以下 JSON 有语法错误或被截断，请修复后只返回修复后的完整合法 JSON。

要求：
1. 保持原有数据和结构不变
2. 如果 JSON 被截断，补全缺失的部分（如 recipes 数组补到7天，shopping_list 至少10项）
3. 只输出 JSON，不要加任何解释、markdown标记或代码块
4. 确保所有括号和引号都正确闭合
5. 直接以 {{ 开头，以 }} 结尾

待修复的 JSON：
{broken_text[:6000]}

修复后的完整 JSON："""
    try:
        from core.ai_gateway import ve4_ai_call
        result = ve4_ai_call(
            task_type="json_repair",
            system="你是一个JSON修复工具。只输出修复后的合法JSON，不加任何解释或markdown标记。直接以 { 开头，以 } 结尾。",
            prompt=prompt,
            format_type="json",
            complexity="medium",
            max_tokens=8192,
            contains_privacy_data=True,
        )
        if result.success:
            text = result.text.strip()
            # gateway 已做归一化，不再从 reasoning 提取
            # 如果 is_truncated=True 且 text 为空，直接返回空让上游处理
            if result.is_truncated and not text:
                logger.info("[LIFE_PLANNER] step2 LLM修复也被截断（content为空）")
                return {}
            if text:
                # 移除 markdown 代码块包裹
                if text.startswith("```"):
                    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
                    if m:
                        text = m.group(1).strip()
                # 尝试直接解析
                try:
                    data = json.loads(text)
                    logger.info(f"[LIFE_PLANNER] step2 LLM修复成功 ({len(broken_text)}→{len(text)}字符)")
                    return data
                except (json.JSONDecodeError, ValueError):
                    # 如果直接解析失败，尝试修复（跳过LLM递归）
                    data = _repair_broken_json(text, _skip_llm=True)
                    if data:
                        logger.info("[LIFE_PLANNER] step2 LLM修复后二次修复成功")
                        return data
    except Exception as e:
        logger.warning(f"[LIFE_PLANNER] LLM修复失败: {e}")
    return {}
