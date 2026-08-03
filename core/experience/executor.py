"""
VE5 Experience Layer — Executor
================================
问题: "怎么执行？"

所有级别统一执行:
  1. Context Builder — 从 State + DB 加载最新真实财务数据
  2. Template Filler — 用最新数据填充模板（所有级别都执行）
  3. Context Variables 注入 — 将 cvars 写入 output 供下游使用
  4. Last Report Merge — 加载上次 LLM 报告，标注数据变化

根据级别差异:
  - automatic: 直接返回填充结果，不调 LLM
  - learning: 填充结果 + LLM 微调提示
  - raw: 填充结果 + 完整 LLM 探索（不再返回空 output）
"""

import json
import logging
from typing import Dict, Any, List, Callable, Optional

logger = logging.getLogger("ve5.experience.executor")


# ════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════

def execute(experience: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    """
    执行经验。

    所有级别统一流程:
      1. 构建 exec_context（从数据库加载最新数据）
      2. 填充模板
      3. 注入 context_variables 到 output
      4. 加载上次 LLM 报告（如有），标注数据变化
      5. 按 level 设置 decision/needs_llm

    返回:
      {
        "decision": "execute" | "assist" | "delegate",
        "experience_id": str,
        "output": {...},            # 永远不为空
        "exec_context": {...},      # 完整上下文，供 API 层填充
        "needs_llm": bool,
        "origin": {...},
        "workflow_results": [...],
        "decisions_applied": [...],
        "code_executed": bool,
        "data_changes": [...],      # 相比上次报告的数据变化
      }
    """
    level = experience.get("level", "raw")
    confidence = experience.get("confidence", 0)
    template = experience.get("template_json", {})
    workflow = experience.get("workflow", [])
    decision_rules = experience.get("decision_rules", [])
    origin = experience.get("origin", {})
    code_path = experience.get("code_path", "")

    # ── 构建执行上下文 (含真实财务数据) ──
    exec_context = _build_context(state, experience)

    # ── 特殊路径：life_planner 经验 → 回放历史规划（"生活惯性"模式）──
    # 回放是低风险操作（仅展示历史数据），不要求高 confidence。
    # 只要类型是 life_planner 且有最近的生活规划报告，就回放。
    exp_type = experience.get("type", "") or experience.get("exp_type", "")
    if exp_type in ("life_planner", "life_planning", "life_plan"):
        replay = _replay_life_plan_history(experience, state)
        if replay:
            logger.info(
                f"[EXECUTOR] life_planner history replay: {experience['exp_id']} "
                f"(confidence={confidence:.3f}) → 回放历史规划 {replay.get('_source_report_id', 'unknown')}"
            )
            return {
                "decision": "execute",
                "experience_id": experience["exp_id"],
                "output": replay,
                "exec_context": exec_context,
                "needs_llm": False,
                "origin": origin,
                "workflow_results": [],
                "decisions_applied": [],
                "code_executed": False,
                "replay": True,
                "data_changes": [],
            }

    # ── 执行 Workflow ──
    workflow_results = _run_workflow(workflow, exec_context)

    # ── 求值 Decision Rules ──
    decisions = _evaluate_decision_rules(decision_rules, exec_context)

    # ── 加载上次 LLM 报告，检测数据变化 ──
    data_changes = _detect_data_changes(experience, exec_context)

    # ═══ 优先路径：代码执行 ═══
    if code_path:
        try:
            from core.experience.code_loader import load_and_execute
            code_result = load_and_execute(code_path, state, experience)
            if code_result:
                output = code_result
                output["_workflow_summary"] = {
                    "total": len(workflow_results),
                    "ok": len([r for r in workflow_results if r.get("status") == "ok"]),
                    "errors": len([r for r in workflow_results if r.get("status") == "error"]),
                    "skipped": len([r for r in workflow_results if r.get("status") == "skipped"]),
                }
                if decisions:
                    output["_decisions"] = decisions

                logger.info(
                    f"[EXECUTOR] code execute ({level}): {experience['exp_id']} "
                    f"({experience['name']}) conf={confidence:.3f}"
                )
                return {
                    "decision": "execute" if level == "automatic" else ("assist" if level == "learning" else "delegate"),
                    "experience_id": experience["exp_id"],
                    "output": output,
                    "exec_context": exec_context,
                    "needs_llm": level != "automatic",
                    "origin": origin,
                    "workflow_results": workflow_results,
                    "decisions_applied": decisions,
                    "code_executed": True,
                    "data_changes": data_changes,
                }
        except Exception as e:
            logger.warning(f"[EXECUTOR] 代码执行失败，回退模板: {e}")

    # ═══ 统一路径：模板填充 + cvars 注入（所有级别） ═══
    template_output = _fill_template(template, exec_context)
    output = _merge_output(template_output, workflow_results, decisions)

    # ── 所有级别都注入 context_variables ──
    for cv in experience.get("context_variables", []):
        if cv in exec_context and cv not in output:
            output[cv] = exec_context[cv]

    # ── 注入数据变化信息 ──
    if data_changes:
        output["_data_changes"] = data_changes

    # ── 按级别设置 decision/needs_llm ──
    if level == "automatic":
        logger.info(
            f"[EXECUTOR] bypass LLM: {experience['exp_id']} "
            f"({experience['name']}) conf={confidence:.3f} "
            f"wf={len(workflow_results)} dr={len(decisions)}"
        )
        return {
            "decision": "execute",
            "experience_id": experience["exp_id"],
            "output": output,
            "exec_context": exec_context,
            "needs_llm": False,
            "origin": origin,
            "workflow_results": workflow_results,
            "decisions_applied": decisions,
            "code_executed": False,
            "data_changes": data_changes,
        }

    elif level == "learning":
        logger.info(
            f"[EXECUTOR] assist: {experience['exp_id']} "
            f"({experience['name']}) conf={confidence:.3f}"
        )
        return {
            "decision": "assist",
            "experience_id": experience["exp_id"],
            "output": output,
            "exec_context": exec_context,
            "needs_llm": True,
            "origin": origin,
            "workflow_results": workflow_results,
            "decisions_applied": decisions,
            "assist_hint": _build_assist_hint(experience, decisions),
            "code_executed": False,
            "data_changes": data_changes,
        }

    else:
        # raw 级别也返回填充后的 output，不再返回空
        logger.info(
            f"[EXECUTOR] delegate with data: {experience['exp_id']} "
            f"({experience['name']}) conf={confidence:.3f} "
            f"(raw → output has {len(output)} keys)"
        )
        return {
            "decision": "delegate",
            "experience_id": experience["exp_id"],
            "output": output,
            "exec_context": exec_context,
            "needs_llm": True,
            "origin": origin,
            "reason": "经验处于 raw 阶段，已填充最新数据，建议 LLM 进一步分析",
            "workflow_results": workflow_results,
            "decisions_applied": decisions,
            "code_executed": False,
            "data_changes": data_changes,
        }


def execute_automatic_only(
    experience: Dict[str, Any], state: Dict[str, Any]
) -> Dict[str, Any]:
    """简化版: 仅执行 automatic 级别"""
    if experience.get("level") != "automatic":
        return {"decision": "delegate", "output": {}, "needs_llm": True}
    return execute(experience, state)


# ════════════════════════════════════════════════
# Context Builder: 从 State + DB 构建执行上下文
# ════════════════════════════════════════════════

def _build_context(state: Dict, experience: Dict) -> Dict:
    """
    构建模板填充上下文。合并:
      1. State Object 中的 entities + user_state
      2. State 顶层匹配 experience context_variables 的 key
      3. 数据库实时数据 (总资产/收入/支出/持仓)
      4. Experience 声明的 context_variables
    """
    ctx = {}

    # 从 state 提取
    ctx["intent"] = state.get("intent", "")
    raw = state.get("raw_input", "")
    ctx["user_input"] = raw
    ctx["user_input_short"] = raw[:60]

    user_state = state.get("user_state", {})
    for k, v in user_state.items():
        if k != "anomalies" and isinstance(v, (str, int, float, bool)):
            ctx[k] = v

    entities = state.get("entities", {})
    for k, v in entities.items():
        ctx[k] = v

    # ── 数据库实时数据 (先加载，让外部 State 值可覆盖) ──
    _load_realtime_financials(ctx)

    # ── 目标数据 ──
    _load_goals_data(ctx)

    # ── 生活规划数据（供 life_planner 经验使用）──
    _load_life_plan_data(ctx)

    # ── 派生变量（goals_summary / suggestion 等）──
    _build_derived_vars(ctx)

    # ── State 顶层直接值 (最后覆盖，优先级最高) ──
    cvars = experience.get("context_variables", [])
    for k, v in state.items():
        if k in cvars and isinstance(v, (str, int, float, bool)):
            ctx[k] = v

    # 只在变量完全缺失时才留占位符
    for var in cvars:
        if var not in ctx:
            ctx[var] = ''

    return ctx


def _load_realtime_financials(ctx: Dict):
    """从数据库加载实时财务数据到上下文（含月份回退 + YTD）"""
    try:
        from core.ve5_chatbot.financial_data import load_financial_summary
        fin = load_financial_summary()

        if fin.get("total_assets", 0) > 0:
            ctx["total_assets"] = fin["total_assets"]
            ctx["current_amount"] = fin["total_assets"]

        ctx["holdings_count"] = fin.get("holdings_count", 0)

        # 分类汇总
        for cls in ("aggressive", "stable", "liquid", "protection"):
            key = f"total_{cls}"
            if key in fin:
                ctx[key] = fin[key]

        # 月度收支（已含月份回退）
        ctx["monthly_income"] = fin.get("monthly_income", 0)
        ctx["monthly_expense"] = fin.get("monthly_expense", 0)
        ctx["monthly_savings"] = fin.get("monthly_savings", 0)
        ctx["savings_rate"] = fin.get("savings_rate", 0)
        ctx["monthly_data_month"] = fin.get("monthly_data_month", "")
        ctx["transaction_count"] = fin.get("transaction_count", 0)

        # YTD（年度累计）
        ctx["ytd_income"] = fin.get("ytd_income", 0)
        ctx["ytd_expense"] = fin.get("ytd_expense", 0)
        ctx["ytd_savings"] = fin.get("ytd_savings", 0)
        ctx["ytd_investment_return"] = fin.get("ytd_investment_return", 0)

    except Exception as e:
        logger.debug(f"[EXECUTOR] 加载财务数据失败: {e}")


def _load_goals_data(ctx: Dict):
    """加载目标数据到上下文（使用智能进度计算）"""
    try:
        from app_paths import DATA_DIR
        from core.ve5_chatbot.financial_data import calculate_goal_progress, load_financial_summary
        gf = DATA_DIR / "goals.json"
        if not gf.exists():
            return

        goals = json.loads(gf.read_text(encoding="utf-8")).get("goals", [])
        ctx["goals_count"] = len(goals)

        # 使用共享模块计算进度
        fin = load_financial_summary()
        progress_list = [calculate_goal_progress(g, fin) for g in goals]

        active = [g for g in progress_list if g.get("status") != "已达成"]
        if active:
            best = max(active, key=lambda g: g.get("target_amount", 0))
            ctx["goal_name"] = best.get("name", "")
            ctx["target_amount"] = best.get("target_amount", 0)
            ctx["goal_progress"] = best.get("progress_pct", 0)
            ctx["goal_status"] = best.get("status", "")
            ctx["months_needed"] = best.get("months_needed", 0)
            ctx["monthly_target"] = 0  # 如有 monthly_target 字段则从 goals.json 取
            ctx["gap"] = max(0, 100 - best.get("progress_pct", 0))
            ctx["goal_type"] = best.get("goal_type", "")
            ctx["remaining_amount"] = best.get("remaining_amount", 0)
            # 别名：兼容 legacy schema 用 progress/current_amount
            ctx["progress"] = ctx["goal_progress"]
            ctx["progress_pct"] = ctx["goal_progress"]  # 模板常用 {progress_pct}
            ctx["current_amount"] = best.get("current_amount", 0)
            if best.get("goal_type") == "accumulation":
                ctx["ytd_savings"] = best.get("ytd_savings", 0)

        # 生成 goals_summary（供模板 {goals_summary} 使用）
        summary_lines = []
        for g in progress_list:
            icon = g.get("icon", "")
            name = g.get("name", "")
            pct = g.get("progress_pct", 0)
            cur = g.get("current_amount", 0)
            tgt = g.get("target_amount", 0)
            gtype = "积累" if g.get("goal_type") == "accumulation" else "金额"
            summary_lines.append(
                f"- {icon} {name} [{gtype}]: {pct}% (¥{cur:,.0f}/¥{tgt:,.0f})"
            )
        if summary_lines:
            ctx["goals_summary"] = "\n".join(summary_lines)
    except Exception as e:
        logger.debug(f"[EXECUTOR] 加载目标数据失败: {e}")


def _load_life_plan_data(ctx: Dict):
    """从 report_store 加载最近一次生活规划（供 life_planner 经验使用）"""
    try:
        from core.ve5_chatbot.report_store import ve5_report_list, ve5_report_get

        reports = ve5_report_list("life_plan")
        if not reports:
            return

        latest = reports[0]
        report_id = latest.get("report_id", "")
        if not report_id:
            return

        report = ve5_report_get(report_id)
        if not report:
            return

        data = report.get("data", {})
        weekly_budget = data.get("weekly_budget", 0)

        # 元数据（索引里存了 title 等）
        metadata = report.get("metadata", {}) or latest

        recipes = data.get("recipes", [])
        shopping = data.get("shopping_list", [])

        ctx["weekly_budget"] = weekly_budget
        ctx["recipes_count"] = len(recipes)
        ctx["shopping_count"] = len(shopping)
        ctx["plan_type"] = metadata.get("title", "本周生活规划")
        ctx["content_summary"] = (
            f"{len(recipes)}道食谱、{len(shopping)}项购物清单，"
            f"周预算 ¥{weekly_budget:,.0f}"
        )
        ctx["_life_plan_report_id"] = report_id
        ctx["_life_plan_created_at"] = report.get("created_at", "")

        logger.debug(
            f"[EXECUTOR] 生活规划数据加载: {report_id} "
            f"(budget=¥{weekly_budget}, recipes={len(recipes)}, shopping={len(shopping)})"
        )
    except Exception as e:
        logger.debug(f"[EXECUTOR] 加载生活规划数据失败: {e}")


def _build_derived_vars(ctx: Dict):
    """构建派生变量：suggestion 等（基于已有 ctx 数据）"""
    # ── suggestion：基于目标进度和结余生成建议 ──
    if not ctx.get("suggestion"):
        goal_name = ctx.get("goal_name", "")
        progress = ctx.get("goal_progress", 0)
        months = ctx.get("months_needed", 0)
        savings = ctx.get("monthly_savings", 0)
        remaining = ctx.get("remaining_amount", 0)

        if goal_name and progress >= 100:
            ctx["suggestion"] = f"恭喜！「{goal_name}」已达成目标 🎉"
        elif goal_name and months > 0:
            ctx["suggestion"] = (
                f"按当前月结余 ¥{savings:,.0f}，"
                f"距离「{goal_name}」还需约 {months:.0f} 个月（缺口 ¥{remaining:,.0f}）。"
                f"建议保持储蓄节奏，或适当优化支出结构加速达成。"
            )
        elif goal_name:
            ctx["suggestion"] = (
                f"「{goal_name}」目前进度 {progress}%，"
                f"当前月结余 ¥{savings:,.0f}。"
            )
        else:
            ctx["suggestion"] = "当前暂无活跃目标，可前往目标规划页面设定新目标。"


# ════════════════════════════════════════════════
# Workflow Engine: 执行 workflow 步骤序列
# ════════════════════════════════════════════════

# workflow action → Python 函数映射
_WORKFLOW_ACTIONS: Dict[str, Callable] = {
    "load_goals": lambda ctx: {
        "action": "load_goals",
        "result": {
            "has_goals": ctx.get("goals_count", 0) > 0,
            "goal_name": ctx.get("goal_name", ""),
            "target_amount": ctx.get("target_amount", 0),
        },
    },
    "load_financial": lambda ctx: {
        "action": "load_financial",
        "result": {
            "total_assets": ctx.get("total_assets", 0),
            "monthly_income": ctx.get("monthly_income", 0),
            "monthly_expense": ctx.get("monthly_expense", 0),
            "savings_rate": ctx.get("savings_rate", 0),
        },
    },
    "calculate_progress": lambda ctx: {
        "action": "calculate_progress",
        "result": {
            "progress_pct": ctx.get("goal_progress", 0),
            "current_amount": ctx.get("current_amount", 0),
            "target_amount": ctx.get("target_amount", 0),
        },
    },
    "compare_budget": lambda ctx: {
        "action": "compare_budget",
        "result": {
            "monthly_savings": ctx.get("monthly_savings", 0),
            "monthly_target": ctx.get("monthly_target", 0),
            "on_track": ctx.get("monthly_savings", 0) >= ctx.get("monthly_target", 0),
        },
    },
    # ── LLM 编译器常用别名/扩展 action ──
    "load_finance": lambda ctx: {  # load_financial 的别名（LLM 常用）
        "action": "load_finance",
        "result": {
            "total_assets": ctx.get("total_assets", 0),
            "monthly_income": ctx.get("monthly_income", 0),
            "monthly_expense": ctx.get("monthly_expense", 0),
            "monthly_savings": ctx.get("monthly_savings", 0),
            "ytd_savings": ctx.get("ytd_savings", 0),
            "savings_rate": ctx.get("savings_rate", 0),
        },
    },
    "load_financial_data": lambda ctx: {  # 另一个别名
        "action": "load_financial_data",
        "result": {
            "total_assets": ctx.get("total_assets", 0),
            "monthly_income": ctx.get("monthly_income", 0),
            "monthly_expense": ctx.get("monthly_expense", 0),
            "monthly_savings": ctx.get("monthly_savings", 0),
            "ytd_savings": ctx.get("ytd_savings", 0),
        },
    },
    "compute_current_amount": lambda ctx: {
        "action": "compute_current_amount",
        "result": {
            "current_amount": ctx.get("current_amount", 0),
            "progress_pct": ctx.get("goal_progress", ctx.get("progress", 0)),
            "target_amount": ctx.get("target_amount", 0),
        },
    },
    "estimate_timeline": lambda ctx: {
        "action": "estimate_timeline",
        "result": {
            "months_needed": ctx.get("months_needed", 0),
            "monthly_savings": ctx.get("monthly_savings", 0),
            "remaining_amount": ctx.get("remaining_amount", 0),
        },
    },
    "generate_report": lambda ctx: {
        "action": "generate_report",
        "result": {
            "goals_summary": ctx.get("goals_summary", ""),
            "suggestion": ctx.get("suggestion", ""),
            "ready": bool(ctx.get("goals_summary")),
        },
    },
    "load_plan_result": lambda ctx: {
        "action": "load_plan_result",
        "result": {
            "plan_type": ctx.get("plan_type", "本周生活规划"),
            "content_summary": ctx.get("content_summary", ""),
            "weekly_budget": ctx.get("weekly_budget", 0),
            "recipes_count": ctx.get("recipes_count", 0),
            "shopping_count": ctx.get("shopping_count", 0),
            "ready": bool(ctx.get("content_summary") or ctx.get("weekly_budget")),
        },
    },
    "validate_result": lambda ctx: {
        "action": "validate_result",
        "result": {
            "valid": bool(ctx.get("content_summary") or ctx.get("goals_summary")),
        },
    },
    "compose_guide_message": lambda ctx: {
        "action": "compose_guide_message",
        "result": {
            "message_ready": True,
        },
    },
    "send_notification": lambda ctx: {
        "action": "send_notification",
        "result": {"sent": True},
    },
    "generate_message": lambda ctx: {
        "action": "generate_message",
        "result": {"ready": True},
    },
    "load_habits": lambda ctx: {
        "action": "load_habits",
        "result": {"habits_loaded": True},
    },
    "load_price_context": lambda ctx: {
        "action": "load_price_context",
        "result": {"price_context_loaded": True},
    },
    "generate_plan": lambda ctx: {
        "action": "generate_plan",
        "result": {"plan_generated": True},
    },
    "execute_skill": lambda ctx: {
        "action": "execute_skill",
        "result": {"executed": True},
    },
}


def _run_workflow(workflow: List[Dict], ctx: Dict) -> List[Dict]:
    """
    执行 workflow 步骤序列。
    跳过未知 action，记录执行日志。
    """
    results = []
    for step in workflow:
        action = step.get("action", "")
        description = step.get("description", "")

        handler = _WORKFLOW_ACTIONS.get(action)
        if handler:
            try:
                r = handler(ctx)
                r["step"] = step.get("step", len(results) + 1)
                r["description"] = description
                r["status"] = "ok"
                results.append(r)
            except Exception as e:
                results.append({
                    "step": step.get("step", len(results) + 1),
                    "action": action,
                    "description": description,
                    "status": "error",
                    "error": str(e),
                })
        else:
            results.append({
                "step": step.get("step", len(results) + 1),
                "action": action,
                "description": description,
                "status": "skipped",
                "reason": f"未知 action: {action}",
            })

    return results


# ════════════════════════════════════════════════
# Decision Evaluator: 求值 if/else 条件
# ════════════════════════════════════════════════

def _evaluate_decision_rules(rules: List[Dict], ctx: Dict) -> List[Dict]:
    """
    对每条 decision rule 求值条件。
    支持的条件表达式:
      - "progress < expected" → ctx["progress"] < ctx["expected"]
      - "收入变化>30%" → detected via anomaly check
      - "progress >= expected * 1.1" → 数值比较
    """
    applied = []
    for rule in rules:
        condition = rule.get("condition", "")
        action = rule.get("action", "")
        triggered = _eval_condition(condition, ctx)
        if triggered:
            applied.append({
                "condition": condition,
                "action": action,
                "triggered": True,
            })
    return applied


def _eval_condition(condition: str, ctx: Dict) -> bool:
    """求值单个条件表达式"""
    try:
        # "progress < expected"
        if "progress < expected" in condition:
            return ctx.get("goal_progress", 0) < (
                ctx.get("expected_progress", 50)
            )

        # "progress >= expected * 1.1"
        if "progress >= expected * 1.1" in condition:
            expected = ctx.get("expected_progress", 50)
            return ctx.get("goal_progress", 0) >= expected * 1.1

        # "收入变化>30%"
        if "收入变化>30%" in condition or "收入变化 > 30%" in condition:
            return _check_income_change(ctx)

        # "新增重大目标"
        if "新增" in condition and "目标" in condition:
            return ctx.get("goals_count", 0) > 0 and ctx.get("goal_status") == "进行中"

        # 通用数值比较: "key op value"
        m = _parse_comparison(condition, ctx)
        if m:
            return m

    except Exception:
        pass
    return False


def _check_income_change(ctx: Dict) -> bool:
    """检查收入是否有 ≥30% 的变化"""
    try:
        import sqlite3
        from datetime import datetime
        from app_paths import DB_PATH

        conn = sqlite3.connect(str(DB_PATH))
        this_month = datetime.now().strftime("%Y-%m")
        # 简易: 对比上月
        last_month_num = (datetime.now().month - 1) or 12
        last_month = f"{datetime.now().year}-{last_month_num:02d}"

        r = conn.execute(
            "SELECT SUM(amount) FROM transactions WHERE transaction_type='income' AND transaction_date LIKE ?",
            (f"{this_month}%",)
        ).fetchone()
        current = abs(float(r[0] or 0))

        r = conn.execute(
            "SELECT SUM(amount) FROM transactions WHERE transaction_type='income' AND transaction_date LIKE ?",
            (f"{last_month}%",)
        ).fetchone()
        previous = abs(float(r[0] or 0))

        conn.close()

        if previous > 0:
            change = abs(current - previous) / previous
            return change > 0.3
        return False
    except Exception:
        return False


def _parse_comparison(condition: str, ctx: Dict) -> Optional[bool]:
    """尝试解析 'key op value' 格式的比较式"""
    import re
    pattern = re.match(
        r'(\w+)\s*(>=|<=|!=|==|>|<)\s*([\d.]+)',
        condition.strip(),
    )
    if not pattern:
        return None

    key, op, val_str = pattern.groups()
    val = float(val_str)
    actual = ctx.get(key, 0)

    ops = {
        ">": lambda a, b: a > b,
        "<": lambda a, b: a < b,
        ">=": lambda a, b: a >= b,
        "<=": lambda a, b: a <= b,
        "==": lambda a, b: a == b,
        "!=": lambda a, b: a != b,
    }
    if op in ops:
        return ops[op](actual, val)

    return None


# ════════════════════════════════════════════════
# Template Filler
# ════════════════════════════════════════════════

def _fill_template(template: Dict, context: Dict) -> Dict:
    """模板字符串填充: {key} 或 {key:format} → value
    支持: {goal_progress}, {monthly_savings:,.0f}, {total_assets:,} 等
    从右到左替换避免位置偏移
    """
    import re

    result = {}
    for key, val in template.items():
        if isinstance(val, str):
            s = val
            matches = list(re.finditer(r'\{(\w+)(?::([^}]*))?\}', s))
            # 从右到左处理，避免位置漂移
            for match in reversed(matches):
                var_name = match.group(1)
                fmt_spec = match.group(2)
                full = match.group(0)

                if var_name in context:
                    cv = context[var_name]
                    if fmt_spec and isinstance(cv, (int, float)):
                        try:
                            formatted = f"{cv:{fmt_spec}}"
                        except (ValueError, TypeError):
                            formatted = f"¥{cv:,.0f}"
                        s = s[:match.start()] + formatted + s[match.end():]
                    elif isinstance(cv, (int, float)):
                        # 不自动加 ¥ 的字段
                        if var_name.endswith(('_pct','_progress','_rate','_count','_frequency',
                                              '_years','_months','_days','_age','_index'))\
                           or var_name in ('progress','goal_progress','months_needed','months_early','gap'):
                            s = s[:match.start()] + str(cv) + s[match.end():]
                        else:
                            # 若模板已有 ¥ 则不重复
                            prefix = '' if match.start() > 0 and val[match.start()-1] == '\u00a5' else '¥'
                            s = s[:match.start()] + prefix + f"{cv:,.0f}" + s[match.end():]
                    else:
                        s = s[:match.start()] + str(cv) + s[match.end():]
                else:
                    # 未找到变量 → 空字符串
                    s = s[:match.start()] + '' + s[match.end():]
            result[key] = s
        elif isinstance(val, (list, dict)):
            result[key] = val
        else:
            result[key] = val
    return result


# ════════════════════════════════════════════════
# 输出融合
# ════════════════════════════════════════════════

def _merge_output(
    template_output: Dict,
    workflow_results: List[Dict],
    decisions: List[Dict],
) -> Dict:
    """融合 template 输出 + workflow 结果 + decision 判定"""
    output = dict(template_output)

    # 附加 workflow 摘要
    if workflow_results:
        errors = [r for r in workflow_results if r.get("status") == "error"]
        skipped = [r for r in workflow_results if r.get("status") == "skipped"]
        ok = [r for r in workflow_results if r.get("status") == "ok"]
        output["_workflow_summary"] = {
            "total": len(workflow_results),
            "ok": len(ok),
            "errors": len(errors),
            "skipped": len(skipped),
        }
        if errors:
            output["_workflow_errors"] = errors[:3]

    # 附加 decision 结果
    if decisions:
        output["_decisions"] = decisions

    return output


def _build_assist_hint(experience: Dict, decisions: List[Dict]) -> str:
    """构建 LLM 辅助提示"""
    name = experience.get("name", "未知经验")
    origin = experience.get("origin", {})
    reason = origin.get("created_reason", "unknown")

    hint = f"基于历史经验「{name}」(来源: {reason})，已有模板输出，请 LLM 微调后返回。"

    if decisions:
        triggered = [d for d in decisions if d.get("triggered")]
        if triggered:
            conditions = ", ".join(d["condition"] for d in triggered[:2])
            hint += f" 触发的判定条件: {conditions}。"

    return hint


# ════════════════════════════════════════════════
# Life Planner History Replay — "生活惯性"模式
# ════════════════════════════════════════════════

def _replay_life_plan_history(experience: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    """
    回放历史生活规划 — 当 life_planner 经验被手动运行时，
    从 report_store 中取最近一次生活规划数据直接返回。

    设计理念（"生活惯性"）：
    - 高 confidence 的生活规划 = 用户多次采纳的习惯
    - 不需要重新调 LLM，直接复用上次的规划
    - 如果历史规划存在，回放它；否则返回空（交给下游模板填充）
    """
    try:
        from core.ve5_chatbot.report_store import ve5_report_list, ve5_report_get

        reports = ve5_report_list("life_plan")
        if not reports:
            logger.info("[EXECUTOR] life_planner replay: 无历史规划报告")
            return {}

        # 取最近一条报告
        latest = reports[0]
        report_id = latest.get("report_id", "")
        if not report_id:
            return {}

        report = ve5_report_get(report_id)
        if not report:
            return {}

        data = report.get("data", {})
        if not data or not data.get("weekly_budget"):
            logger.info(f"[EXECUTOR] life_planner replay: 报告 {report_id} 数据不完整")
            return {}

        # 标记来源
        data["_source_report_id"] = report_id
        data["_replay_mode"] = True
        data["_replay_message"] = "基于你的历史生活规划习惯，本周沿用上次规划"

        logger.info(
            f"[EXECUTOR] life_planner replay: 回放报告 {report_id} "
            f"(budget=¥{data.get('weekly_budget', 0)}, "
            f"recipes={len(data.get('recipes', []))}, "
            f"shopping={len(data.get('shopping_list', []))})"
        )
        return data

    except Exception as e:
        logger.warning(f"[EXECUTOR] life_planner replay 异常: {e}")
        return {}


# ════════════════════════════════════════════════
# Data Change Detection — 对比上次报告，标注变化
# ════════════════════════════════════════════════

# 需要跟踪变化的关键字段
_TRACKED_NUMERIC = [
    "total_assets", "monthly_income", "monthly_expense",
    "monthly_savings", "current_amount", "target_amount",
]
_TRACKED_PERCENT = [
    "progress", "goal_progress", "gap", "savings_rate",
]


def _detect_data_changes(experience: Dict, exec_context: Dict) -> List[Dict]:
    """
    对比当前执行数据与上次 LLM 报告中的数据，标注变化项。

    返回格式:
      [
        {
          "field": "total_assets",
          "label": "总资产",
          "previous": 1200000,
          "current": 1250000,
          "change": "+50000",
          "change_pct": 4.2,
          "direction": "up",
        },
        ...
      ]
    """
    changes = []
    try:
        exp_type = experience.get("type", "") or experience.get("exp_type", "")
        source_report_id = experience.get("source_report_id", "")
        if not source_report_id:
            return changes

        from core.ve5_chatbot.report_store import ve5_report_get
        report = ve5_report_get(source_report_id)
        if not report:
            return changes

        prev_data = report.get("data", {})
        if not prev_data:
            return changes

        # 比较数值字段
        for field in _TRACKED_NUMERIC:
            prev_val = prev_data.get(field, 0)
            curr_val = exec_context.get(field, 0)
            try:
                prev_f = float(prev_val) if prev_val else 0
                curr_f = float(curr_val) if curr_val else 0
            except (ValueError, TypeError):
                continue
            if abs(curr_f - prev_f) < 0.01:
                continue
            diff = curr_f - prev_f
            change_pct = round(abs(diff) / prev_f * 100, 1) if prev_f > 0 else 0
            changes.append({
                "field": field,
                "label": _FIELD_LABELS.get(field, field),
                "previous": prev_f,
                "current": curr_f,
                "change": f"+{diff:,.0f}" if diff > 0 else f"{diff:,.0f}",
                "change_pct": change_pct,
                "direction": "up" if diff > 0 else "down",
            })

        # 比较百分比字段
        for field in _TRACKED_PERCENT:
            prev_val = prev_data.get(field, 0)
            curr_val = exec_context.get(field, 0)
            try:
                prev_f = float(prev_val) if prev_val else 0
                curr_f = float(curr_val) if curr_val else 0
            except (ValueError, TypeError):
                continue
            if abs(curr_f - prev_f) < 0.1:
                continue
            diff = curr_f - prev_f
            changes.append({
                "field": field,
                "label": _FIELD_LABELS.get(field, field),
                "previous": prev_f,
                "current": curr_f,
                "change": f"+{diff:.1f}%" if diff > 0 else f"{diff:.1f}%",
                "direction": "up" if diff > 0 else "down",
            })

    except Exception as e:
        logger.debug(f"[EXECUTOR] 数据变化检测失败: {e}")

    return changes


_FIELD_LABELS = {
    "total_assets": "总资产",
    "monthly_income": "月收入",
    "monthly_expense": "月支出",
    "monthly_savings": "月结余",
    "current_amount": "当前金额",
    "target_amount": "目标金额",
    "progress": "进度",
    "goal_progress": "目标进度",
    "gap": "差距",
    "savings_rate": "储蓄率",
}
