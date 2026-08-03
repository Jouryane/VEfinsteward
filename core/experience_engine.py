"""
VE5 Experience Engine — 经验运行时
====================================
在 skill 层之上构建 experience 层：
1. 从 LLM 生成的报告中蒸馏经验
2. 定时运行全局衰退
3. 为前端提供活跃经验列表

唤起流程：
    用户场景 → exp_hit(trigger, tags) → 返回命中经验列表
    → 前端选取最高分 → exp_execute(exp_id, context)
    → 如果 automatic 级别：纯程序执行，填充模板
    → 更新 confidence / frequency
"""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime

logger = logging.getLogger("ve5.experience_engine")

_DECAY_INTERVAL = 3600  # 每小时跑一次全局衰退


def exp_engine_from_goal_report(report_id: str, goal_data: Dict) -> Optional[str]:
    """
    从目标追踪报告中蒸馏为经验。
    用户点击"保存为长期经验"后调用。

    同时会通过 Episode Store 进行完整链路：
    goal_data → Episode → Compiler → Experience
    """
    goals = goal_data.get("goals", [])
    if not goals:
        return None

    # ── Step 0: 保存 Episode（如果还没保存过）──
    from core.episode_store import ep_save
    best = max(
        (g for g in goals if g.get("status") != "已达成"),
        key=lambda g: g.get("estimated_cost", 0),
        default=goals[0]
    )
    episode_id = ep_save(
        source_skill="goal_tracker",
        source_report_id=report_id,
        user_input="分析我的目标",
        context_json={
            "goals": goals,
            "best_goal": best.get("name", ""),
            "target_amount": best.get("estimated_cost", 0),
        },
        llm_reasoning=f"目标分析：{len(goals)}个目标，最高优先级{best.get('name', '')}",
        llm_action_steps=[
            "读取用户目标列表",
            "加载财务状况",
            "计算目标完成进度",
            "对比预算与结余",
            "生成进度报告",
        ],
        result_json=goal_data,
    )
    logger.info(f"[EXPERIENCE] Episode 已保存: {episode_id}")

    # ── Step 1: Compiler 蒸馏 Experience ──
    from core.episode_compiler import ep_compile
    from core.episode_store import ep_mark_compiled

    compiled = ep_compile(episode_id) if episode_id else None
    if compiled:
        from core.experience_store import exp_create
        # context_variables 如果 compiler 产出不全，补全默认值
        compiled_cvars = compiled.get("context_variables", [])
        if len(compiled_cvars) < 3:
            compiled_cvars = list(set(compiled_cvars + [
                "goal_name","progress","target_amount","current_amount",
                "monthly_target","monthly_savings","total_assets",
            ]))
        exp_id = exp_create(
            source_report_id=report_id,
            exp_type=_normalize_exp_type(compiled.get("exp_type", "goal_tracking")),
            name=compiled["name"],
            description=compiled.get("description", ""),
            trigger_event=compiled.get("trigger_event", "monthly_check"),
            workflow=compiled.get("workflow", []),
            template_json=compiled.get("template_json", {}),
            context_variables=compiled_cvars,
            tags=compiled.get("tags", []),
            llm_required=compiled.get("llm_required", True),
        )
        if exp_id:
            ep_mark_compiled(episode_id, exp_id)
            logger.info(f"[EXPERIENCE] Episode {episode_id} → Experience {exp_id}")
            return exp_id

    # ── Fallback: 原始规则构建（V0 兼容）──
    return _exp_from_goal_legacy(report_id, goal_data)


def _exp_from_goal_legacy(report_id: str, goal_data: Dict) -> Optional[str]:
    """原始规则构建（向后兼容）"""
    goals = goal_data.get("goals", [])
    if not goals:
        return None

    best = max(
        (g for g in goals if g.get("status") != "已达成"),
        key=lambda g: g.get("estimated_cost", 0),
        default=goals[0]
    )

    name = f"储蓄目标追踪: {best.get('name', '目标')}"
    description = (
        f"自动追踪 {best.get('name', '')} "
        f"(目标金额 ¥{best.get('estimated_cost', 0):,.0f}，"
        f"当前进度 {best.get('progress_pct', 0)}%)"
    )
    cost = best.get("estimated_cost", 0)

    workflow = [
        {"step": 1, "action": "load_goals", "description": "加载用户目标列表"},
        {"step": 2, "action": "load_financial", "description": "加载资产/收入/支出数据"},
        {"step": 3, "action": "calculate_progress", "description": "计算目标完成进度"},
        {"step": 4, "action": "compare_budget", "description": "对比月度预算与实际结余"},
        {"step": 5, "action": "generate_message", "description": "生成进度播报文本"},
    ]

    template_json = {
        "normal": "🎯 {goal_name}：已完成 {progress}%，当前 ¥{current_amount}/¥{target_amount}，月度计划 ¥{monthly_target}",
        "ahead": "🚀 {goal_name}：进度超前！已完成 {progress}%，预计提前 {months_early} 个月达成。当前 ¥{current_amount}/¥{target_amount}",
        "behind": "⚠️ {goal_name}：进度落后，已完成 {progress}%，差 {gap}%，需要加强储蓄。月度目标 ¥{monthly_target}，当前结余 ¥{monthly_savings}",
        "achieved": "🎉 {goal_name}：目标已达成！¥{current_amount}/¥{target_amount}",
    }

    tags = ["goal_tracking", "saving", "progress", "monthly_check",
            f"amount_{cost}", best.get("name", "").replace(" ", "_")]

    from core.experience_store import exp_create
    exp_id = exp_create(
        source_report_id=report_id,
        exp_type="goal_tracking",
        name=name,
        description=description,
        trigger_event="monthly_check",
        workflow=workflow,
        template_json=template_json,
        context_variables=["goal_name","progress","current_amount","target_amount","monthly_target","monthly_savings","total_assets","gap","months_needed","months_early"],
        tags=tags,
        llm_required=True,
    )

    if exp_id:
        logger.info(f"[EXPERIENCE] 目标追踪经验已创建: {exp_id} ({name})")

    return exp_id


# ════════════════════════════════════════════════
# LLM Experience Compiler
# ════════════════════════════════════════════════

_EXP_COMPILER_SYSTEM = """你是经验编译器。你的任务是将一段 LLM 对话分析结果编译成一个可复用的经验脚本。

经验脚本是一个 JSON 对象，结构如下：
{
  "name": "经验名称（简洁，不超过20字）",
  "description": "描述这个经验的作用（一句话）",
  "trigger_event": "触发事件（如 monthly_check / weekly_check / on_income / on_new_expense）",
  "trigger_frequency": "recurring 或 on_demand",
  "workflow": [
    {"step": 1, "action": "load_xxx", "description": "做什么"},
    ...
  ],
  "template_json": {
    "normal": "正常情况的回复模板，用 {variable_name} 占位",
    "warning": "预警情况的回复模板"
  },
  "decision_rules": [
    {"condition": "progress < expected", "action": "warning"},
    {"condition": "progress >= expected * 1.1", "action": "encouragement"}
  ],
  "exception_rules": [
    {"condition": "收入变化>30%", "action": "call_llm"}
  ],
  "context_variables": ["变量名1", "变量名2", ...],
  "tags": ["标签1", "标签2"],
  "result_page": {
    "title": "{goal_name} 运行结果",
    "description": "当前进度和财务状态",
    "sections": [
      {"id":"hero","label":"概览","template":"{goal_name}: {progress}% (¥{current_amount}/¥{target_amount})"},
      {"id":"details","label":"明细","template":"月结余 ¥{monthly_savings} | 月目标 ¥{monthly_target} | 总资产 ¥{total_assets}"}
    ]
  }
}

工作流程：
1. 阅读用户提供的对话数据（来自 VE 管家的分析结果）
2. 理解这个场景的核心逻辑和决策模式
3. 设计 workflow 步骤链（简洁，3-7步，每步一个动作）
4. 设计 template_json（至少包含 normal 模板，用 {变量名} 作为运行时填充的占位符）
5. 提取 context_variables（模板中使用的所有变量）
6. 设计 decision_rules（2-4条条件判定规则）
7. 设计 exception_rules（1-3条例外情况，交给 LLM 处理）
8. 确定合适的 trigger_event 和 tags
9. 输出完整的 JSON，不要其他文字。

重要：workflow 和 template 必须基于数据中的实际内容来设计，不要套用固定格式。"""


def exp_llm_compile_experience(report_data: dict, report_type: str = "goal") -> dict:
    """
    用 LLM 读取 report 数据，编译成经验脚本 schema。

    参数:
        report_data: VE 管家 report 的 data 字段（LLM 原始输出）
        report_type: "goal" | "life_plan"

    返回:
        完整的 experience schema dict（可直接传给 exp_create）
    """
    from core.ai_gateway import ve4_ai_call

    # 构建 prompt：把 report data 喂给 LLM
    prompt_parts = [
        "以下数据来自 VE 管家的分析结果，请将此编译为一个可复用的经验脚本。",
        "",
        f"报告类型: {report_type}",
        "",
        "数据内容:",
        json.dumps(report_data, ensure_ascii=False, indent=2)[:3000],
        "",
        "请输出完整的经验 JSON，不要其他文字。",
    ]

    try:
        result = ve4_ai_call(
            task_type="experience_compile",
            system=_EXP_COMPILER_SYSTEM,
            prompt="\n".join(prompt_parts),
            format_type="json",
            complexity="high",
            max_tokens=8192,
        )

        if result.success and result.text:
            schema = json.loads(result.text)
            if isinstance(schema, dict) and "name" in schema:
                logger.info(f"[EXPERIENCE] LLM 编译完成: {schema.get('name', '?')}")
                schema = _normalize_schema(schema)
                # ── V2: 生成受限 Python 代码 ──
                llm_output = json.dumps(report_data, ensure_ascii=False)[:3000]
                _try_generate_code(schema, llm_output)
                return schema
            else:
                logger.warning(f"[EXPERIENCE] LLM 返回无效 schema: {str(schema)[:100]}")
    except Exception as e:
        logger.error(f"[EXPERIENCE] LLM 编译失败: {e}")

    # Fallback: 规则编译
    logger.info("[EXPERIENCE] 回退到规则编译")
    return _rules_compile_fallback(report_data, report_type)


# ════════════════════════════════════════════════
# 类型规范化：统一 exp_type 命名
# ════════════════════════════════════════════════

_TYPE_NORMALIZE = {
    "goal_tracker": "goal_tracking",
    "goal": "goal_tracking",
    "goals": "goal_tracking",
    "goal_track": "goal_tracking",
    "life_plan": "life_planner",
    "life_planning": "life_planner",
    "life_planer": "life_planner",
    "budget_planner": "budget_planning",
    "budget": "budget_planning",
    "budget_plan": "budget_planning",
}


def _normalize_exp_type(raw_type: str) -> str:
    """规范化经验类型，确保全系统一致"""
    if not raw_type:
        return "goal_tracking"
    return _TYPE_NORMALIZE.get(raw_type, raw_type)


def _normalize_schema(schema: dict) -> dict:
    """规范化 LLM 输出的 schema，补全缺失字段"""
    # ── 类型规范化（最高优先级，确保 matcher/controller 能命中）──
    raw_type = schema.get("exp_type", schema.get("type", ""))
    schema["exp_type"] = _normalize_exp_type(raw_type)

    defaults = {
        "trigger_frequency": "recurring",
        "workflow": [],
        "template_json": {"normal": "经验已执行"},
        "decision_rules": [],
        "exception_rules": [],
        "context_variables": [],
        "tags": [],
        "result_page": {
            "title": "经验运行结果",
            "sections": [{"id":"output","label":"输出","template":"经验已执行"}]
        },
    }
    for k, v in defaults.items():
        if k not in schema or not schema[k]:
            schema[k] = v

    # 确保 workflow 每步有 step 序号
    for i, w in enumerate(schema.get("workflow", [])):
        if "step" not in w:
            w["step"] = i + 1
    return schema


def _rules_compile_fallback(report_data: dict, report_type: str) -> dict:
    """规则编译回退（LLM 不可用时）"""
    if report_type == "goal" or "goals" in report_data:
        goals = report_data.get("goals", [])
        if goals:
            best = max(goals, key=lambda g: g.get("estimated_cost", 0), default=goals[0])
            return {
                "name": f"储蓄追踪：{best.get('name', '目标')}",
                "description": f"自动追踪{best.get('name','')}进度（目标¥{best.get('estimated_cost',0):,.0f}）",
                "trigger_event": "monthly_check",
                "trigger_frequency": "recurring",
                "workflow": [
                    {"step":1,"action":"load_goals","description":"加载目标列表"},
                    {"step":2,"action":"load_financial","description":"加载财务数据"},
                    {"step":3,"action":"calculate_progress","description":"计算进度"},
                    {"step":4,"action":"compare_budget","description":"对比预算"},
                    {"step":5,"action":"generate_message","description":"生成播报"},
                ],
                "template_json": {
                    "normal": "目标 {goal_name}：进度 {progress}%，¥{current_amount}/¥{target_amount}，月度计划 ¥{monthly_target}",
                },
                "decision_rules": [
                    {"condition":"progress < expected","action":"warning"},
                ],
                "exception_rules": [
                    {"condition":"\u6536\u5165\u53d8\u5316>30%","action":"call_llm"},
                ],
                "context_variables": ["goal_name","progress","current_amount","target_amount","monthly_target","monthly_savings","gap","months_needed"],
                "tags": ["goal_tracking","saving","recurring"],
                "result_page": {
                    "title": "{goal_name} 进度报告",
                    "description": "储蓄目标和财务进度概览",
                    "sections": [
                        {"id":"hero","label":"目标概览","template":"{goal_name}: 已完成 {progress}% (¥{current_amount} / ¥{target_amount})"},
                        {"id":"monthly","label":"月度计划","template":"月目标 ¥{monthly_target} | 月结余 ¥{monthly_savings}"},
                        {"id":"gap","label":"差距分析","template":"距目标差 {gap}%，预计 {months_needed} 个月达成"},
                    ]
                },
            }

    # life_plan / default
    budget = report_data.get("weekly_budget", 0)
    return {
        "name": "每周生活规划",
        "description": f"自动生成每周食谱和购物清单（周预算¥{budget:,.0f}）",
        "trigger_event": "weekly_check",
        "trigger_frequency": "recurring",
        "workflow": [
            {"step":1,"action":"load_financial","description":"加载财务"},
            {"step":2,"action":"load_goals","description":"加载目标"},
            {"step":3,"action":"generate_plan","description":"生成规划"},
        ],
        "template_json": {
            "normal": "本周规划已生成，预算 ¥{weekly_budget}",
        },
        "context_variables": ["weekly_budget","recipes_count","shopping_count"],
        "tags": ["weekly","life"],
    }


# ════════════════════════════════════════════════
# V2: AI Coding — 代码生成辅助
# ════════════════════════════════════════════════

def _try_generate_code(schema: dict, llm_output: str) -> str:
    """
    尝试为 schema 生成受限 Python 代码。
    生成的 code_path 会写入 schema["code_path"]。

    使用 UUID 保证 temp_id 唯一性，避免并发编译时代码文件互相覆盖。

    参数:
        schema: 经验 schema dict
        llm_output: 原始 LLM 输出文本

    返回:
        code_path 字符串，失败返回空字符串
    """
    try:
        from core.experience.code_generator import generate_experience_code
        import uuid as _uuid

        # 使用 UUID 保证唯一性，避免并发覆盖
        temp_id = f"tmp_{_uuid.uuid4().hex[:12]}"
        code_path = generate_experience_code(temp_id, schema, llm_output)
        if code_path:
            schema["code_path"] = code_path
            schema["_code_temp_id"] = temp_id  # 供 exp_create 后重命名
            logger.info(f"[EXPERIENCE] 代码已生成: {code_path}")
            return code_path
    except Exception as e:
        logger.warning(f"[EXPERIENCE] 代码生成失败（不影响经验创建）: {e}")

    schema["code_path"] = ""
    return ""


def _try_generate_code_for_exp(exp_id: str, schema: dict, llm_output: str) -> str:
    """
    为已创建的经验生成代码，并更新数据库中的 code_path。

    参数:
        exp_id: 已创建的经验 ID
        schema: 经验 schema dict
        llm_output: 原始 LLM 输出文本

    返回:
        code_path 字符串，失败返回空字符串
    """
    try:
        from core.experience.code_generator import generate_experience_code
        from core.experience_store import exp_update

        code_path = generate_experience_code(exp_id, schema, llm_output)
        if code_path:
            exp_update(exp_id, code_path=code_path, code_generated_at=datetime.now().isoformat())
            logger.info(f"[EXPERIENCE] 代码已生成并更新: {exp_id} → {code_path}")
            return code_path
    except Exception as e:
        logger.warning(f"[EXPERIENCE] 代码生成失败 {exp_id}: {e}")

    return ""


def exp_engine_from_life_plan_report(report_id: str, plan_data: Dict) -> Optional[str]:
    """
    从生活规划报告中蒸馏为经验。
    """
    weekly_budget = plan_data.get("weekly_budget", 0)
    if not weekly_budget:
        return None

    name = f"每周生活规划"
    description = f"自动生成每周食谱和购物清单，周预算 ¥{weekly_budget:,.0f}"

    workflow = [
        {"step": 1, "action": "load_financial", "description": "加载收入/支出/资产"},
        {"step": 2, "action": "load_goals", "description": "加载目标"},
        {"step": 3, "action": "load_habits", "description": "加载用户习惯"},
        {"step": 4, "action": "load_price_context", "description": "加载区域物价"},
        {"step": 5, "action": "generate_plan", "description": "生成生活规划"},
    ]

    template_json = {
        "summary": f"🍽 本周生活规划 · 预算 ¥{weekly_budget:,.0f}",
        "normal": "📋 本周规划已生成: {recipes_count} 道食谱, {shopping_count} 项购物清单, 预算 ¥{weekly_budget}",
    }

    tags = ["weekly", "life_planning", "food", "shopping", f"budget_{weekly_budget}"]
    context_variables = ["weekly_budget", "recipes_count", "shopping_count"]

    from core.experience_store import exp_create
    exp_id = exp_create(
        source_report_id=report_id,
        exp_type="life_planner",
        name=name,
        description=description,
        origin={
            "type": "user_confirmed",
            "episode_ids": [],
            "created_reason": "用户保存生活规划为长期经验",
            "confirmed_at": datetime.now().isoformat(),
            "compiled_by": "V0_rules",
        },
        trigger_event="weekly_check",
        trigger_frequency="recurring",
        workflow=workflow,
        template_json=template_json,
        context_variables=context_variables,
        tags=tags,
        llm_required=True,
    )

    if exp_id:
        logger.info(f"[EXPERIENCE] 生活规划经验已创建: {exp_id} ({name})")

    return exp_id


def exp_engine_hit(trigger_event: str, context_tags: List[str] = None) -> List[Dict]:
    """快捷命中接口 (V1: 通过 Matcher)"""
    from core.experience import match
    state = {"intent": trigger_event, "entities": {}, "user_state": {}, "context": {}}
    return match(state)


def exp_engine_execute(exp_id: str, context: Dict = None) -> Dict:
    """快捷执行接口 — 通过 exp_execute 统一执行路径，自动更新 confidence
    
    前端"执行该程序"按钮调用此函数。
    执行后自动检测成功/失败并更新 confidence 统计。
    """
    from core.experience_store import exp_execute
    return exp_execute(exp_id, context)


def exp_engine_list(exp_type: str = None, level: str = None) -> List[Dict]:
    """获取经验列表"""
    from core.experience_store import exp_list
    return exp_list(exp_type, level)


def exp_engine_dispatch(user_input: str, context: Dict = None) -> Dict:
    """V1 统一调度入口"""
    from core.experience import exp_runtime_dispatch
    return exp_runtime_dispatch(user_input, context)


def exp_engine_get_runtime() -> Dict[str, Any]:
    """
    获取当前活跃的 experience runtime 快照（供前端展示）。
    返回 automatic 级别的经验及其状态。
    """
    from core.experience_store import exp_list, exp_hit
    automatics = exp_list(level="automatic")
    learnings = exp_list(level="learning", limit=3)
    raws = exp_list(level="raw", limit=2)

    # 检查 learning 中是否有即将跃迁的
    about_to_promote = [e for e in learnings if e.get("confidence", 0) >= 0.60]

    # 检查 automatic 中是否有即将衰退的
    at_risk = [e for e in automatics if e.get("confidence", 0) < 0.78]

    return {
        "automatic_count": len(automatics),
        "learning_count": len(learnings) + len(raws),
        "automatics": [
            {"exp_id": e["exp_id"], "name": e["name"], "confidence": e["confidence"],
             "frequency": e["frequency"], "last_used": e["last_used"],
             "description": e.get("description", ""), "type": e["type"]}
            for e in automatics[:5]
        ],
        "about_to_promote": [
            {"exp_id": e["exp_id"], "name": e["name"], "confidence": e["confidence"]}
            for e in about_to_promote
        ],
        "at_risk": [
            {"exp_id": e["exp_id"], "name": e["name"], "confidence": e["confidence"],
             "last_used": e.get("last_used", "")}
            for e in at_risk
        ],
    }


def exp_engine_monthly_check() -> Dict:
    """
    月度检查：触发所有 goal_tracking 类型的经验。
    由 VE5 启动时或定时调度调用。
    """
    from core.experience_store import exp_hit, exp_execute
    results = []
    hits = exp_hit("monthly_check", ["goal_tracking", "monthly_check", "saving"])

    # 加载当前财务上下文
    try:
        from core.ve5_chatbot.skills.goal_tracker import _load_goals, _load_financial
        goals = _load_goals()
        finance = _load_financial()
    except Exception:
        goals, finance = [], {}

    for exp in hits:
        context = {
            "triggered_by": "monthly_check",
            "tags": ["monthly_check", "saving", "progress"],
        }
        # 填充实际数据
        if goals:
            best = max((g for g in goals), key=lambda g: g.get("estimated_cost", 0), default=goals[0])
            context["goal_name"] = best.get("name", "")
            context["target_amount"] = str(best.get("estimated_cost", 0))
        context["current_amount"] = str(finance.get("total_assets", 0))
        context["monthly_savings"] = str(finance.get("monthly_savings", 0))

        result = exp_execute(exp["exp_id"], context)
        results.append(result)

    return {"checked": len(results), "results": results}


# ════════════════════════════════════════════════
# 后台衰退线程
# ════════════════════════════════════════════════

_decay_thread = None


def _decay_loop():
    while True:
        time.sleep(_DECAY_INTERVAL)
        try:
            from core.experience_store import exp_decay_tick
            exp_decay_tick()
        except Exception as e:
            logger.warning(f"[EXPERIENCE] 衰退tick异常: {e}")


def exp_engine_start():
    global _decay_thread
    if _decay_thread is None:
        _decay_thread = threading.Thread(target=_decay_loop, daemon=True, name="exp-decay")
        _decay_thread.start()
        logger.info("[EXPERIENCE] 衰退线程已启动")


# ════════════════════════════════════════════════
# Auto Formation: Episode 累积检测 → 自动编译
# ════════════════════════════════════════════════

_MIN_EPISODES_FOR_AUTO = 3    # 最少 Episode 数才触发自动编译
_SKILL_SIMILARITY_DAYS = 30   # 30 天内的 Episode 视为同一批


def _llm_batch_compile(episodes: list) -> Optional[dict]:
    """
    V1 LLM 批处理编译器：将多个 Episode 一起喂给 LLM，归纳通用经验。

    参数:
        episodes: 同类型 Episode 列表 [{episode_id, llm_reasoning, result_json, ...}, ...]

    返回:
        Experience schema dict，失败返回 None
    """
    from core.ai_gateway import ve4_ai_call

    # 构建批处理 prompt：汇总所有 Episode 的推理和结果
    batch_data = []
    for i, ep in enumerate(episodes[:5]):  # 最多取5个避免 prompt 过长
        batch_data.append({
            "episode": i + 1,
            "reasoning": (ep.get("llm_reasoning", "") or "")[:500],
            "steps": ep.get("llm_action_steps", [])[:10],
            "result_summary": json.dumps(ep.get("result_json", {}), ensure_ascii=False)[:500],
        })

    prompt_parts = [
        f"以下是从 {len(episodes)} 次相似对话中提取的数据。请从这些对话中归纳出一个通用的经验脚本。",
        "",
        "核心要求：识别所有对话中的共同模式，忽略一次性内容，提炼可复用的逻辑。",
        "",
        "对话数据:",
        json.dumps(batch_data, ensure_ascii=False, indent=2),
        "",
        "请输出完整的经验 JSON，不要其他文字。",
    ]

    try:
        result = ve4_ai_call(
            task_type="experience_batch_compile",
            system=_EXP_COMPILER_SYSTEM,
            prompt="\n".join(prompt_parts),
            format_type="json",
            complexity="high",
            max_tokens=8192,
        )

        if result.success and result.text:
            schema = json.loads(result.text)
            if isinstance(schema, dict) and "name" in schema:
                logger.info(f"[EXPERIENCE] LLM 批处理编译完成: {schema.get('name', '?')}")
                schema = _normalize_schema(schema)
                # ── V2: 为批处理编译结果生成代码 ──
                llm_ref = json.dumps(batch_data, ensure_ascii=False)[:2000]
                _try_generate_code(schema, llm_ref)
                return schema
            else:
                logger.warning(f"[EXPERIENCE] LLM 批处理返回无效 schema")
    except Exception as e:
        logger.error(f"[EXPERIENCE] LLM 批处理编译异常: {e}")

    return None


def exp_check_auto_compilation(source_skill: str) -> Optional[str]:
    """
    检查指定 skill 的未编译 Episode 累积是否达到自动编译阈值。

    如果 30 天内有 >= _MIN_EPISODES_FOR_AUTO 个未编译 Episode，
    自动触发 Compiler → Experience 创建。

    返回:
      新 Experience ID → 自动编译成功
      None            → 不需编译 / 编译失败
    """
    from core.episode_store import ep_list, ep_get
    from core.episode_compiler import ep_compile as _compile_ep
    from core.experience_store import exp_create as _exp_create, exp_list as _exp_list

    # 1. 获取未编译的 Episode
    raw_eps = ep_list(source_skill=source_skill, compiled=False, limit=10)

    # 2. 筛选最近 30 天内的
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=_SKILL_SIMILARITY_DAYS)).isoformat()
    recent = [e for e in raw_eps if e.get("created_at", "") >= cutoff]

    if len(recent) < _MIN_EPISODES_FOR_AUTO:
        return None

    # 3. 检查是否已有类似经验（避免重复创建）
    # 使用规范化后的类型查询（source_skill 可能是 goal_tracker，但 DB 中存的是 goal_tracking）
    normalized_type = _normalize_exp_type(source_skill)
    existing_exps = _exp_list(exp_type=normalized_type, limit=5)
    # 也查询原始 skill 名称，兼容旧数据
    if not existing_exps:
        existing_exps = _exp_list(exp_type=source_skill, limit=5)
    # 取最近创建的经验
    latest_exp = existing_exps[0] if existing_exps else None

    # 如果已有最近 7 天创建的同类型经验，跳过自动编译
    if latest_exp:
        latest_created = latest_exp.get("created_at", "")
        week_ago = (datetime.now() - timedelta(days=7)).isoformat()
        if latest_created >= week_ago:
            logger.info(f"[EXPERIENCE] 自动编译跳过：已有最近经验 {latest_exp['exp_id']}")
            return None

    # 4. 优先 V1 LLM 批处理编译（跨 Episode 归纳）
    compiled = None
    try:
        compiled = _llm_batch_compile(recent)
    except Exception as e:
        logger.warning(f"[EXPERIENCE] LLM 批处理编译失败: {e}，回退到 V0")

    # 5. 回退 V0 单条编译
    if not compiled:
        best_ep = max(recent, key=lambda e: len(e.get("llm_action_steps", [])))
        best_ep_id = best_ep["episode_id"]
        compiled = _compile_ep(best_ep_id)
        if not compiled:
            return None
    else:
        # LLM 编译成功，取第一个 Episode 作为 source
        best_ep = recent[0]
        best_ep_id = best_ep["episode_id"]

    logger.info(
        f"[EXPERIENCE] 自动编译触发: {source_skill} "
        f"({len(recent)} 个 Episode, 代表 {best_ep_id})"
    )

    # 6. 创建 Experience（标记为 auto_compiled）
    from core.episode_store import ep_mark_compiled

    # ── 类型优先级：episodes 的 source_skill 是真实来源 ──
    # LLM 编译可能归纳错误（如把 life_planner 写成 goal_tracking），
    # 因此以 source_skill 归一化结果为准，compiled.exp_type 仅作 fallback。
    norm_skill_type = _normalize_exp_type(source_skill)
    compiled_raw_type = compiled.get("exp_type", "") or compiled.get("type", "")
    compiled_type = _normalize_exp_type(compiled_raw_type) if compiled_raw_type else ""
    exp_type = norm_skill_type or compiled_type or "goal_tracking"

    exp_id = _exp_create(
        source_report_id=best_ep.get("source_report_id", ""),
        exp_type=exp_type,
        name=compiled["name"] + " (自动)",
        description=f"自动从 {len(recent)} 次交互中归纳: {compiled.get('description', '')}",
        origin={
            "type": "auto_compiled",
            "episode_ids": [e["episode_id"] for e in recent[:5]],
            "created_reason": f"连续 {len(recent)} 次 {source_skill} 交互结构相似，自动蒸馏",
        },
        trigger_event=compiled.get("trigger_event", ""),
        workflow=compiled.get("workflow", []),
        template_json=compiled.get("template_json", {}),
        decision_rules=compiled.get("decision_rules", []),
        exception_rules=compiled.get("exception_rules", []),
        context_variables=compiled.get("context_variables", []),
        tags=compiled.get("tags", []),
        llm_required=compiled.get("llm_required", True),
        code_path=compiled.get("code_path", ""),
    )

    if exp_id:
        ep_mark_compiled(best_ep_id, exp_id)
        logger.info(f"[EXPERIENCE] 自动编译完成: {exp_id} ({compiled['name']})")
        # ── V2: 代码文件处理 ──
        if compiled.get("code_path"):
            # 代码在编译阶段已生成（temp_id 命名），重命名为正式 exp_id
            from core.experience.code_generator import rename_code_file
            new_path = rename_code_file(compiled["code_path"], exp_id)
            if new_path != compiled["code_path"]:
                from core.experience_store import exp_update as _exp_update
                _exp_update(exp_id, code_path=new_path)
                logger.info(f"[EXPERIENCE] 代码文件已重命名: {exp_id}")
        else:
            # 编译阶段未生成代码，尝试为已创建经验生成
            # 注意：V0 fallback 时 batch_data 未定义，需从 recent episodes 构造 llm_ref
            try:
                llm_ref = json.dumps(
                    [
                        {
                            "reasoning": (e.get("llm_reasoning", "") or "")[:500],
                            "result_summary": json.dumps(
                                e.get("result_json", {}), ensure_ascii=False
                            )[:500],
                        }
                        for e in recent[:5]
                    ],
                    ensure_ascii=False,
                )[:2000]
            except Exception:
                llm_ref = ""
            _try_generate_code_for_exp(exp_id, compiled, llm_ref)
        return exp_id

    return None
