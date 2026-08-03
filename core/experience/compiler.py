"""
VE5 Episode Compiler — 经历编译器
===================================
从 Episode（一次或多次 LLM 交互记录）中蒸馏出可复用的 Experience。

设计原则：
    V0: 基于规则的提取（Pattern Mining、变量识别、Workflow 生成）
    V1: LLM 辅助归纳（传入多个 Episode，让 LLM 总结通用经验）

核心算法：
    1. 提取目标变量   — 从 user_input + result_json 中抽取关键数值
    2. 提取触发条件   — 识别什么事件/状态应触发此经验
    3. 提取重复步骤   — 从 llm_action_steps + workflow 中提取可程序化的步骤
    4. 提取决策逻辑   — 从 llm_reasoning 中提取 if/else 判断
    5. 生成执行模板   — 将 result_json 参数化为 {variable} 模板
    6. 识别例外情况   — 哪些情况下需要回退 LLM

用法：
    from core.episode_compiler import ep_compile

    # 从单个 Episode 编译
    experience = ep_compile(episode_id="ep_goal_tracker_xxx")

    # 从多个 Episode 归纳（V1）
    experience = ep_compile_from_batch(episode_ids=["...", "..."])
"""

import json
import logging
import re
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime

logger = logging.getLogger("ve5.episode_compiler")


# ════════════════════════════════════════════════
# V0: 基于规则的编译器
# ════════════════════════════════════════════════

def ep_compile(episode_id: str) -> Optional[Dict[str, Any]]:
    """
    V0 Compiler: 从单个 Episode 蒸馏 Experience Schema。

    返回标准的 Experience Dict，可直接传给 experience_store.exp_create()。
    """
    from core.episode_store import ep_get

    episode = ep_get(episode_id)
    if not episode:
        logger.warning(f"[COMPILER] Episode 不存在: {episode_id}")
        return None

    skill = episode.get("source_skill", "unknown")
    result = episode.get("result_json", {})
    context = episode.get("context_json", {})
    reasoning = episode.get("llm_reasoning", "")
    action_steps = episode.get("llm_action_steps", [])
    user_input = episode.get("user_input", "")

    # ── Step 1: 提取目标变量 ──
    target_variables = _extract_target_variables(user_input, result, context)

    # ── Step 2: 提取触发条件 ──
    trigger, trigger_condition = _extract_trigger(skill, context, user_input)

    # ── Step 3: 提取 Workflow ──
    workflow = _extract_workflow(skill, action_steps, result)

    # ── Step 4: 提取决策逻辑 ──
    decision_rules = _extract_decision_rules(reasoning, result)

    # ── Step 5: 生成模板 ──
    template = _generate_template(result, target_variables)

    # ── Step 6: 识别 LLM 介入条件 ──
    exception_rules = _extract_exceptions(reasoning, context)

    # ── Step 7: 生成标签 ──
    tags = _generate_tags(skill, user_input, context, target_variables)

    # ── Step 8: 命名 ──
    name = _generate_name(skill, target_variables)

    # ── 构建 Experience Object ──
    experience = {
        "exp_type": skill,
        "name": name,
        "description": f"从 Episode {episode_id} 蒸馏而来。触发条件: {trigger}",
        "source_episode_id": episode_id,
        "source_report_id": episode.get("source_report_id", ""),

        # Trigger
        "trigger_event": trigger,
        "trigger_condition": trigger_condition,

        # Context
        "context_variables": target_variables,

        # Workflow
        "workflow": workflow,

        # Decision
        "decision_rules": decision_rules,

        # Template
        "template_json": template,

        # Exception
        "exception_rules": exception_rules,

        # Tags
        "tags": tags,

        # LLM required (默认 raw 阶段需要)
        "llm_required": True,
    }

    logger.info(
        f"[COMPILER] 编译完成: {episode_id} → {name} "
        f"(variables={len(target_variables)}, workflow={len(workflow)} steps)"
    )
    return experience


# ════════════════════════════════════════════════
# 提取子函数
# ════════════════════════════════════════════════

def _extract_target_variables(
    user_input: str, result: Dict, context: Dict
) -> List[str]:
    """
    从用户输入和结果中提取关键数值变量名。
    例如: "我想存12万" → ["target_amount", "goal_type"]
    """
    variables = set()

    # 从 result 顶层 key 提取
    numeric_keys = [
        "total_assets", "monthly_income", "monthly_expense",
        "weekly_budget", "target_amount", "estimated_cost",
        "progress_pct", "current_amount", "monthly_savings",
        "savings_rate",
    ]
    for k in numeric_keys:
        if k in result or k in context:
            variables.add(k)

    # 从 skill 类型推断
    if "goal" in str(result.get("goals", "")):
        variables.add("goal_type")
        variables.add("goal_name")

    # 特殊字段
    if "screenshot_type" in result:
        variables.add("account_name")

    # 从 user_input 中的数字模式提取语义
    amount_match = re.search(r'(\d+)\s*(万|元|块)', user_input)
    if amount_match:
        variables.add("target_amount")

    return sorted(list(variables))


def _extract_trigger(
    skill: str, context: Dict, user_input: str
) -> Tuple[str, str]:
    """
    提取触发条件。
    返回 (trigger_event, trigger_condition)
    """
    # 按 skill 类型的默认触发
    skill_triggers = {
        "goal_tracker": ("monthly_check", ""),
        "life_planner": ("weekly_check", ""),
        "budget_planner": ("month_start", ""),
    }

    if skill in skill_triggers:
        trigger, condition = skill_triggers[skill]
    else:
        trigger = "on_demand"
        condition = ""

    # 从上下文推断附加条件
    if "savings_rate" in context:
        rate = context.get("savings_rate", 0)
        if isinstance(rate, (int, float)) and rate < 0.2:
            condition = "savings_rate < 0.2"

    return trigger, condition


def _extract_workflow(
    skill: str, action_steps: List[str], result: Dict
) -> List[Dict]:
    """
    从 LLM 推理步骤中提取 Workflow。
    如果 action_steps 为空，则按 skill 类型使用默认 workflow。
    """
    if action_steps:
        return [
            {"step": i + 1, "action": step.lower().replace(" ", "_")[:40], "description": step}
            for i, step in enumerate(action_steps)
        ]

    # 默认 Workflow（按 skill 类型）
    default_workflows = {
        "goal_tracker": [
            {"step": 1, "action": "load_goals", "description": "加载用户目标列表"},
            {"step": 2, "action": "load_financial", "description": "加载资产/收入/支出数据"},
            {"step": 3, "action": "calculate_progress", "description": "计算目标完成进度"},
            {"step": 4, "action": "compare_budget", "description": "对比月度预算与实际结余"},
            {"step": 5, "action": "generate_message", "description": "生成进度播报文本"},
        ],
        "life_planner": [
            {"step": 1, "action": "load_financial", "description": "加载收入/支出/资产"},
            {"step": 2, "action": "load_goals", "description": "加载目标"},
            {"step": 3, "action": "load_habits", "description": "加载用户习惯"},
            {"step": 4, "action": "load_price_context", "description": "加载区域物价"},
            {"step": 5, "action": "generate_plan", "description": "生成生活规划"},
        ],
    }

    return default_workflows.get(skill, [
        {"step": 1, "action": "execute_skill", "description": f"执行 {skill}"},
    ])


def _extract_decision_rules(reasoning: str, result: Dict) -> List[Dict]:
    """
    从 LLM reasoning 中提取 if/else 决策规则。
    简单实现：扫描关键词模式。
    """
    rules = []

    # 模式1: "如果...则..."
    pattern = re.findall(r'如果([^，。；则]+)则([^，。；]+)', reasoning)
    for cond, act in pattern[:3]:
        rules.append({
            "condition": cond.strip(),
            "action": act.strip(),
        })

    # 模式2: "when X then Y"
    pattern_en = re.findall(r'when\s+([^,]+),\s*(.+)', reasoning, re.IGNORECASE)
    for cond, act in pattern_en[:2]:
        rules.append({
            "condition": cond.strip(),
            "action": act.strip(),
        })

    # 模式3: 从结果中推断（progress vs expected）
    if "progress" in str(result) and "target" in str(result):
        rules.append({
            "condition": "progress < expected",
            "action": "warning",
        })

    return rules


def _generate_template(result: Dict, variables: List[str]) -> Dict:
    """
    生成模板：将 result 中的具体值替换为 {variable} 占位符。
    保留原始结构，仅数值字段参数化。
    """
    template = {}

    # 常见消息模板
    if "goals" in result:
        template["normal"] = (
            "🎯 {goal_name}：已完成 {progress}%，"
            "当前 ¥{current_amount}/¥{target_amount}，"
            "月度计划 ¥{monthly_target}"
        )
        template["ahead"] = (
            "🚀 {goal_name}：进度超前！已完成 {progress}%，"
            "预计提前 {months_early} 个月达成"
        )
        template["behind"] = (
            "⚠️ {goal_name}：进度落后，已完成 {progress}%，"
            "需要加强储蓄。月度目标 ¥{monthly_target}"
        )
        template["achieved"] = (
            "🎉 {goal_name}：目标已达成！¥{current_amount}/¥{target_amount}"
        )
    elif "weekly_budget" in result:
        template["summary"] = (
            "📋 本周生活规划：预算 ¥{weekly_budget}，"
            "{recipes_count} 道食谱，{shopping_count} 项购物清单"
        )
    else:
        # 通用模板：保留顶层结构，数值参数化
        for k, v in result.items():
            if k in variables and isinstance(v, (int, float)):
                template[k] = f"{{{k}}}"
            elif isinstance(v, str) and len(v) < 500:
                template[k] = v
            else:
                template[k] = f"{{{k}}}"

    return template


def _extract_exceptions(reasoning: str, context: Dict) -> List[Dict]:
    """
    识别 LLM 介入的例外情况。
    """
    exceptions = []

    # 规则：重大财务变化 → LLM
    exceptions.append({
        "condition": "收入变化 > 30%",
        "action": "call_llm",
    })
    exceptions.append({
        "condition": "新增重大目标",
        "action": "call_llm",
    })

    # 从 reasoning 中扫描 LLM 不自信的部分
    unsure_indicators = ["不确定", "可能", "建议核实", "需要确认", "暂不"]
    for indicator in unsure_indicators:
        if indicator in reasoning:
            exceptions.append({
                "condition": f"LLM reasoning 中出现「{indicator}」",
                "action": "call_llm",
            })
            break

    return exceptions


def _generate_tags(skill: str, user_input: str, context: Dict, variables: List[str]) -> List[str]:
    """生成场景标签"""
    tags = {skill}

    # 从变量推断主题标签
    topic_map = {
        "goal_type": "goal_tracking",
        "target_amount": "saving",
        "weekly_budget": "budget",
        "savings_rate": "saving",
        "estimated_cost": "saving",
        "recipes_count": "food",
        "shopping_count": "shopping",
    }
    for var in variables:
        if var in topic_map:
            tags.add(topic_map[var])

    # 时间标签
    now = datetime.now()
    tags.add(f"month_{now.month}")
    tags.add(f"year_{now.year}")

    # 金额级别标签（大额/小额）
    for var in variables:
        val = context.get(var) or 0
        if isinstance(val, (int, float)):
            if val > 100000:
                tags.add(f"large_amount")
            elif val < 1000:
                tags.add(f"small_amount")

    return sorted(list(tags))


def _generate_name(skill: str, variables: List[str]) -> str:
    """生成人类可读的经验名称"""
    # 按 skill 预设名称
    name_map = {
        "goal_tracker": "储蓄目标追踪",
        "life_planner": "生活规划",
        "budget_planner": "预算规划",
    }
    base = name_map.get(skill, skill.replace("_", " ").title())

    # 附加关键变量
    if "target_amount" in variables:
        base += " (自动)"
    elif "weekly_budget" in variables:
        base += " (每周)"

    return base


# ════════════════════════════════════════════════
# V1 (预览): LLM 辅助编译
# ════════════════════════════════════════════════

def ep_compile_batch(episode_ids: List[str], use_llm: bool = False) -> Optional[Dict]:
    """
    从多个 Episode 归纳 Experience。
    V1 预览：use_llm=True 时调用 LLM 做跨 Episode 归纳。
    """
    from core.episode_store import ep_get, ep_mark_compiled

    episodes = []
    for eid in episode_ids:
        ep = ep_get(eid)
        if ep:
            episodes.append(ep)

    if not episodes:
        logger.warning("[COMPILER] 没有有效的 Episode 可编译")
        return None

    if not use_llm or len(episodes) == 1:
        # 回退到 V0 单条编译
        return ep_compile(episode_ids[0])

    # V1: LLM 归纳
    # TODO: 实现 LLM 辅助的多 Episode 归纳
    # 将多个 Episode 的 result_json + reasoning 汇总，让 LLM 归纳出通用经验
    logger.info("[COMPILER] V1 LLM 编译暂未实现，回退到 V0")
    return ep_compile(episode_ids[0])


# 模块初始化
logger.info("[COMPILER] Episode Compiler 已就绪 (V0)")
