"""
VE管家 母skill — 核心管家
=========================
人物设定：VEchatbot是用户在整个VE系统的核心管家，拥有访问本地文件、
管理用户财务隐私的职能。它以温暖、专业、高效的风格为用户服务。

核心职责：
1. 意图识别 — 判断用户请求属于哪个子skill
2. Skill路由 — 将请求分发给对应子skill处理
3. 会话管理 — 维护多轮对话上下文
4. 隐私保护 — 所有数据本地处理，不上传云端
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Dict, Any, List, Callable, Optional
from core.ai_gateway import ve4_ai_call
from .session_manager import ve5_chat_session_get, ve5_chat_session_append

logger = logging.getLogger("ve5.chatbot.mother")


@dataclass
class VE5ChatbotSkill:
    """子skill接口定义"""
    name: str           # skill英文标识
    display_name: str   # 中文显示名
    description: str    # 功能描述
    icon: str           # emoji图标
    trigger_keywords: List[str] = field(default_factory=list)  # 触发关键词
    handler: Callable = None  # 处理函数


# ════════════════════════════════════════════════════════════════
# 意图识别系统
# ════════════════════════════════════════════════════════════════

_INTENT_SYSTEM = """你是VE管家，负责判断用户的请求属于哪个skill类别。

可选类别：
1. life_planner — 生活规划（食谱、购物清单、娱乐休闲、生活开支计划）
2. asset_doctor — 资产诊断（持仓分析、风险扫描、资产配置建议）
3. investment_advisor — 投资顾问（研报解读、市场热点、持仓影响分析）
4. goal_tracker — 目标追踪（目标进度、资金规划、达成路径）
5. spending_analyst — 消费分析（支出结构、必需/弹性分析、节省建议）
6. general_chat — 通用对话（问候、闲聊、其他不属于上述类别的请求）

判断规则：
- 用户提到"食谱""买菜""做饭""吃什么""购物清单""周末安排""娱乐""生活规划" → life_planner
- 用户提到"持仓""资产配置""风险""诊断""我的资产"" portfolio" → asset_doctor
- 用户提到"研报""新闻""市场""热点""股票分析""这只股票" → investment_advisor
- 用户提到"目标""进度""规划路径""什么时候能""存钱计划" → goal_tracker
- 用户提到"支出""消费""省钱""预算""花了多少""必需消费" → spending_analyst
- 其他 → general_chat

只返回JSON格式：{"intent": "类别名", "confidence": 0-1, "extracted_params": {}}
不要返回其他文字。"""


def ve5_chatbot_intent_detect(user_message: str) -> Dict[str, Any]:
    """识别用户意图，返回 {intent, confidence, params}"""
    # 快速关键词匹配
    msg = user_message.lower()
    quick_map = {
        "life_planner": ["食谱", "买菜", "做饭", "吃什么", "购物清单", "周末", "娱乐", "生活规划", "习惯", "饮食", "生活开支"],
        "asset_doctor": ["持仓", "资产配置", "风险", "诊断", "我的资产", "portfolio", "投资组合", "配了多少", "财务情况", "财务状况", "财务概览", "看财务"],
        "investment_advisor": ["研报", "新闻", "市场", "热点", "股票分析", "这只股票", "行情分析", "怎么看"],
        "goal_tracker": ["目标", "进度", "规划路径", "存钱计划", "什么时候能", "达成", "还需多久"],
        "spending_analyst": ["支出", "消费", "省钱", "预算", "花了多少", "必需消费", "弹性消费", "开销"],
    }
    for intent, keywords in quick_map.items():
        for kw in keywords:
            if kw in msg:
                return {"intent": intent, "confidence": 0.85, "extracted_params": {}}

    # LLM意图识别
    try:
        result = ve4_ai_call(
            task_type="chatbot_intent",
            system=_INTENT_SYSTEM,
            prompt=f"用户消息：{user_message}\n\n判断意图类别，只返回JSON。",
            format_type="json",
            complexity="low",
            max_tokens=128,
        )
        if result.success and result.text:
            parsed = json.loads(result.text)
            return {
                "intent": parsed.get("intent", "general_chat"),
                "confidence": parsed.get("confidence", 0.5),
                "extracted_params": parsed.get("extracted_params", {}),
            }
    except Exception as e:
        logger.warning(f"[CHATBOT] 意图识别失败：{e}")

    return {"intent": "general_chat", "confidence": 1.0, "extracted_params": {}}


# ════════════════════════════════════════════════════════════════
# 母skill回复（通用对话）
# ════════════════════════════════════════════════════════════════

_MOTHER_SYSTEM = """你是VE管家，用户在整个VE财务系统的核心AI管家。

你的设定：
- 名字：VE管家
- 性格：温暖、专业、高效、幽默
- 能力：你可以访问用户的本地财务数据（持仓、收入、支出、目标等），但严格保护隐私，所有数据本地处理不上云
- 风格：简洁直接，优先给出 actionable 的建议，不啰嗦

当前可用的skill：
🍽 生活规划 — 根据财务情况和习惯，规划食谱、购物清单、娱乐安排
💰 资产诊断 — 分析持仓结构、风险扫描、配置优化建议
📈 投资顾问 — 解读研报新闻、分析市场热点对持仓的影响
🎯 目标追踪 — 追踪目标进度、规划达成路径
📊 消费分析 — 分析支出结构、识别节省空间

## 数据可用性
系统会在每次对话时注入「数据可用性清单」，列出所有已预加载的数据点。
- "✓已加载"的数据可直接引用数值，不要重新计算
- "📋可展开"的数据需引导用户查看对应页面，不要臆测
- 完整 API Surface 文档见 docs/api_surface.md

重要规则：
- 系统注入的"推荐配比"和"维度评分"是引擎权威计算结果，请引用，不要自行提出不同配比
- 风险分析应基于实际配比与推荐配比的偏差，不要擅自对配比提出异议
- 目标进度已动态计算，请直接引用百分比
- 积累型目标进度=YTD储蓄/目标额，金额型目标进度=总资产/目标额
- 月度数据可能来自最近有数据的月份（非当月），请引用数据月份

回复用户时：
1. 如果是通用问题，直接回答
2. 如果涉及具体skill，告知用户已调用对应skill，并展示结果
3. 可以引导用户使用某个skill（例如"需要我帮你分析一下最近的支出结构吗？"）
4. 所有金额用人民币，格式为 ¥X,XXX
"""


def ve5_chatbot_mother_reply(user_message: str, history: List[Dict]) -> Dict[str, str]:
    """母skill通用回复，注入实时财务数据上下文"""
    history_text = ""
    for msg in history[-6:]:  # 最近6条
        role = "用户" if msg["role"] == "user" else "VE管家"
        history_text += f"{role}：{msg['content']}\n"

    # ── 注入导航上下文（权威数据 + 导航指引）──
    from .context_navigator import build_navigation_context
    nav_ctx = build_navigation_context("general_chat", user_message)

    prompt = f"""{history_text}
{nav_ctx}
用户：{user_message}

请回复。如果用户的请求可以用某个skill更好地回答，请在回复末尾加上 [skill:skill_name] 标记。"""

    result = ve4_ai_call(
        task_type="chatbot_general",
        system=_MOTHER_SYSTEM,
        prompt=prompt,
        format_type="text",
        complexity="medium",
        max_tokens=512,
    )
    reply = result.text.strip() if result.success else "抱歉，我暂时无法回答这个问题。"
    reasoning = result.reasoning.strip() if result.success else ""
    return {"reply": reply, "reasoning": reasoning}


def _build_finance_context() -> str:
    """构建实时财务数据上下文，注入到 LLM prompt 中"""
    try:
        import sqlite3
        from datetime import datetime
        from app_paths import DB_PATH, DATA_DIR
        import json

        if not DB_PATH.exists():
            return ""

        conn = sqlite3.connect(str(DB_PATH))
        month = datetime.now().strftime("%Y-%m")

        parts = ["[当前财务数据 — 实时]"]

        # 总资产
        r = conn.execute(
            "SELECT SUM(current_value) FROM asset_holdings WHERE is_superseded=0"
        ).fetchone()
        total_assets = float(r[0] or 0)
        parts.append(f"总资产: ¥{total_assets:,.0f}")

        # 持仓数
        r = conn.execute(
            "SELECT COUNT(*) FROM asset_holdings WHERE is_superseded=0 AND current_value > 0"
        ).fetchone()
        parts.append(f"持仓条数: {r[0] if r else 0}")

        # 按分类汇总
        rows = conn.execute(
            "SELECT asset_class, SUM(current_value) FROM asset_holdings "
            "WHERE is_superseded=0 GROUP BY asset_class"
        ).fetchall()
        class_map = {"aggressive": "进取类", "stable": "稳健类", "liquid": "流动类", "protection": "保障类"}
        for row in rows:
            label = class_map.get(row[0], row[0] or "未知")
            parts.append(f"  {label}: ¥{float(row[1] or 0):,.0f}")

        # 本月收支
        r = conn.execute(
            "SELECT SUM(amount) FROM transactions WHERE transaction_type='income' AND transaction_date LIKE ?",
            (f"{month}%",)
        ).fetchone()
        income = abs(float(r[0] or 0))
        parts.append(f"本月收入: ¥{income:,.0f}")

        r = conn.execute(
            "SELECT SUM(amount) FROM transactions WHERE transaction_type='expense' AND transaction_date LIKE ?",
            (f"{month}%",)
        ).fetchone()
        expense = abs(float(r[0] or 0))
        parts.append(f"本月支出: ¥{expense:,.0f}")

        savings = max(0, income - expense)
        parts.append(f"本月结余: ¥{savings:,.0f}")

        conn.close()

        # 目标
        gf = DATA_DIR / "goals.json"
        if gf.exists():
            goals = json.loads(gf.read_text(encoding="utf-8")).get("goals", [])
            active = [g for g in goals if g.get("status") != "已达成"]
            if active:
                parts.append("当前目标:")
                for g in active[:3]:
                    parts.append(
                        f"  {g.get('name','?')}: {g.get('progress_pct',0)}% "
                        f"(¥{g.get('current_amount',0):,.0f}/¥{g.get('estimated_cost',0):,.0f})"
                    )

        return "\n".join(parts) + "\n"
    except Exception:
        return ""


# ════════════════════════════════════════════════════════════════
# 主处理流程
# ════════════════════════════════════════════════════════════════

def ve5_chatbot_process(session_id: str, user_message: str, force_skill: str = "") -> Dict[str, Any]:
    """
    VE管家主处理入口。

    Args:
        session_id: 会话ID
        user_message: 用户输入
        force_skill: 强制使用指定skill（空=自动识别）

    Returns:
        {
            "reply": str,           # AI回复文本
            "skill": str,           # 使用的skill名
            "skill_result": dict,   # skill结构化结果（如有）
            "cards": list,          # 前端可渲染的卡片数据
            "actions": list,        # 建议的后续操作
        }
    """
    from .skills import (
        ve5_skill_life_planner,
        ve5_skill_asset_doctor,
        ve5_skill_investment_advisor,
        ve5_skill_goal_tracker,
        ve5_skill_spending_analyst,
    )

    # 记录用户消息
    ve5_chat_session_append(session_id, "user", user_message)
    history = ve5_chat_session_get(session_id)

    # 确定skill
    skill_name = force_skill
    if not skill_name:
        intent = ve5_chatbot_intent_detect(user_message)
        skill_name = intent.get("intent", "general_chat")

    logger.info(f"[CHATBOT] session={session_id} skill={skill_name} msg={user_message[:30]}...")

    # ═══ Experience Runtime Layer ═══
    # 在调用 LLM Skill 之前，检查是否有可用的经验可以绕过 LLM
    exp_runtime_result = _exp_runtime_intercept(user_message, skill_name)
    # ════════════════════════════════

    # 路由到对应skill
    skill_map = {
        "life_planner": ve5_skill_life_planner,
        "asset_doctor": ve5_skill_asset_doctor,
        "investment_advisor": ve5_skill_investment_advisor,
        "goal_tracker": ve5_skill_goal_tracker,
        "spending_analyst": ve5_skill_spending_analyst,
    }

    result = {
        "reply": "",
        "reasoning": "",
        "skill": skill_name,
        "skill_result": {},
        "cards": [],
        "actions": [],
        "exp_used": False,  # 标记是否使用了经验
    }

    try:
        if skill_name in skill_map:
            skill_fn = skill_map[skill_name]

            # ── 经验直通：automatic 级别直接使用模板输出 ──
            if exp_runtime_result and exp_runtime_result.get("decision") == "execute":
                output = exp_runtime_result.get("output", {})
                # 从经验模板中提取可展示的回复
                reply_text = _exp_extract_reply(output)
                result["reply"] = reply_text if reply_text else "经验已自动执行"
                result["reasoning"] = "[Experience Runtime] bypass LLM — automatic"
                result["skill_result"] = output.get("_decisions", [])
                result["cards"] = _exp_build_cards(output, exp_runtime_result.get("experience", {}))
                result["actions"] = [{"label": "查看详情", "action": "rerun_skill", "skill": skill_name, "icon": "📊"}]
                result["exp_used"] = True
                logger.info(f"[CHATBOT] ⚡ 经验直通: {exp_runtime_result.get('experience_id','?')}")

            elif exp_runtime_result and exp_runtime_result.get("decision") == "assist":
                # ── 经验辅助：把 origin 信息 + prefill 注入 skill ──
                assist_hint = exp_runtime_result.get("output", {}).get("assist_hint", "")
                origin = exp_runtime_result.get("output", {}).get("origin", {})
                # 提取经验 prefill 中可展示的文本
                exp_prefill_text = _exp_extract_reply(exp_runtime_result.get("output", {}))
                augmented_message = _build_assist_message(user_message, assist_hint, origin, exp_prefill_text)
                skill_result = skill_fn(augmented_message, history)
                # 合并经验 output 到 skill 结果
                result["reply"] = skill_result.get("reply", "")
                result["reasoning"] = skill_result.get("reasoning", "")
                result["skill_result"] = skill_result.get("data", {})
                result["cards"] = skill_result.get("cards", [])
                if exp_runtime_result.get("output", {}).get("_decisions"):
                    result["skill_result"]["_exp_decisions"] = exp_runtime_result["output"]["_decisions"]
                result["actions"] = skill_result.get("actions", [])
                result["exp_used"] = True
                logger.info(f"[CHATBOT] 🧠 经验辅助: {exp_runtime_result.get('experience_id','?')}")

                # ── Episode 保存 (assist 也保存，用于后续 compiler) ──
                _episode_auto_save(skill_name, user_message, skill_result, result.get("reasoning", ""),
                                   history, skill_result.get("data", {}))

            else:
                # ── 无经验匹配 → 正常 LLM Skill ──
                skill_result = skill_fn(user_message, history)
                result["reply"] = skill_result.get("reply", "")
                result["reasoning"] = skill_result.get("reasoning", "")
                result["skill_result"] = skill_result.get("data", {})
                result["cards"] = skill_result.get("cards", [])
                result["actions"] = skill_result.get("actions", [])
                # 保存 Episode
                _episode_auto_save(skill_name, user_message, skill_result, result.get("reasoning", ""),
                                   history, skill_result.get("data", {}))
        else:
            # 通用对话 — 走 Experience Runtime 拦截，也尝试保存 Episode
            # 先检查经验是否命中（assist/execute）
            if exp_runtime_result and exp_runtime_result.get("decision") == "execute":
                output = exp_runtime_result.get("output", {})
                reply_text = _exp_extract_reply(output)
                result["reply"] = reply_text if reply_text else "经验已自动执行"
                result["reasoning"] = "[Experience Runtime] bypass LLM — automatic"
                result["exp_used"] = True
                logger.info(f"[CHATBOT] ⚡ 通用对话经验直通: {exp_runtime_result.get('experience_id','?')}")
            elif exp_runtime_result and exp_runtime_result.get("decision") == "assist":
                exp_prefill = _exp_extract_reply(exp_runtime_result.get("output", {}))
                assist_hint = exp_runtime_result.get("output", {}).get("assist_hint", "")
                augmented = f"[经验预填]: {exp_prefill[:300]}\n\n用户问题: {user_message}"
                reply_data = ve5_chatbot_mother_reply(augmented, history)
                if isinstance(reply_data, dict):
                    result["reply"] = reply_data.get("reply", "")
                    result["reasoning"] = reply_data.get("reasoning", "")
                else:
                    result["reply"] = str(reply_data)
                result["exp_used"] = True
                logger.info(f"[CHATBOT] 🧠 通用对话经验辅助: {exp_runtime_result.get('experience_id','?')}")
            else:
                # 正常 LLM 通用对话
                reply_data = ve5_chatbot_mother_reply(user_message, history)
                if isinstance(reply_data, dict):
                    result["reply"] = reply_data.get("reply", "")
                    result["reasoning"] = reply_data.get("reasoning", "")
                else:
                    result["reply"] = str(reply_data)
            result["skill"] = "general_chat"
            # 通用对话也保存 Episode（用于后续 auto formation）
            _episode_auto_save("general_chat", user_message, {"reply": result["reply"]},
                               result.get("reasoning", ""), history,
                               {"reply": result["reply"], "intent": skill_name})
    except Exception as e:
        logger.error(f"[CHATBOT] skill处理失败：{e}")
        result["reply"] = f"抱歉，处理时出现了问题：{str(e)[:100]}"

    # 如果 reply 为空但有 reasoning，给一个友好提示
    if not result["reply"] and result["reasoning"]:
        result["reply"] = "我正在思考中，请稍等片刻再试，或者换个方式提问。"

    # ═══ Confidence Feedback: 更新经验 confidence ═══
    # 核验流程第5步：对比 LLM 输出与经验输出，更新 confidence 函数值
    _record_exp_feedback(exp_runtime_result, result.get("reply", ""), result.get("skill_result"))

    # 记录AI回复
    ve5_chat_session_append(session_id, "assistant", result["reply"], skill_name=skill_name, metadata={
        "skill": skill_name,
        "cards": result.get("cards", []),
        "actions": result.get("actions", []),
        "reasoning": result.get("reasoning", ""),
        "exp_used": result.get("exp_used", False),
    })
    return result


# ════════════════════════════════════════════════════════════════
# Experience Runtime 钩子
# ════════════════════════════════════════════════════════════════

def _exp_runtime_intercept(user_message: str, skill_name: str) -> Optional[Dict]:
    """
    在 LLM 调用前，查询 Experience Runtime 是否有可用的经验。

    返回 None 表示不拦截（走正常 LLM），
    返回 dict 表示命中经验（按 decision 处理）。

    映射: chatbot intent → experience module
    """
    # intent → module 映射
    INTENT_MODULE_MAP = {
        "life_planner": "life_planner",
        "asset_doctor": "goal_tracker",
        "investment_advisor": "goal_tracker",
        "goal_tracker": "goal_tracker",
        "spending_analyst": "goal_tracker",
        "general_chat": "goal_tracker",  # 通用对话也可能命中经验
    }
    module = INTENT_MODULE_MAP.get(skill_name)
    if not module:
        return None

    try:
        from core.experience import exp_runtime_dispatch
        context = {
            "module": module,
            "trigger_type": "user_message",
        }
        result = exp_runtime_dispatch(user_message, context)

        # execute/assist: 命中经验
        if result["decision"] in ("execute", "assist"):
            return result

        # delegate: 如果有候选经验，也返回（用于 LLM 完成后对比反馈）
        if result.get("candidates"):
            return result  # 返回 delegate 结果，含 candidates

        return None
    except Exception as e:
        logger.debug(f"[CHATBOT] exp_runtime 跳过: {e}")
        return None


def _build_assist_message(user_message: str, assist_hint: str, origin: Dict, prefill_text: str = "") -> str:
    """构建增强的 user_message，注入经验上下文和 prefill"""
    parts = [user_message]
    if prefill_text:
        parts.append(f"\n\n[经验预填结果——请在此基础上微调，不要重新生成]:\n{prefill_text[:500]}")
    if assist_hint:
        parts.append(f"\n[系统提示: {assist_hint}]")
    if origin.get("created_reason"):
        parts.append(f"[经验来源: {origin['created_reason']}]")
    return "".join(parts)


def _exp_extract_reply(output: Dict) -> str:
    """从经验模板输出中提取适合展示的回复"""
    # 优先选择 progress_normal / monthly_brief / summary 等模板
    priority_keys = [
        "monthly_brief", "progress_normal", "summary",
        "normal", "progress_ahead", "progress_behind",
        "rebalance_notice", "spending_alert", "investment_check",
    ]
    for k in priority_keys:
        v = output.get(k)
        if v and isinstance(v, str) and len(v) > 10:
            return v
    # fallback: 任意文本值
    for k, v in output.items():
        if not k.startswith("_") and isinstance(v, str) and len(v) > 10:
            return v
    return ""


def _exp_build_cards(output: Dict, experience: Dict) -> List[Dict]:
    """从经验输出构建卡片"""
    cards = []
    if experience:
        cards.append({
            "type": "experience_label",
            "title": "经验自动执行",
            "data": {
                "name": experience.get("name", ""),
                "confidence": experience.get("confidence", 0),
                "origin": experience.get("origin", {}).get("type", ""),
            }
        })
    if output.get("_workflow_summary"):
        cards.append({
            "type": "workflow",
            "title": "执行摘要",
            "data": output["_workflow_summary"],
        })
    if output.get("_decisions"):
        cards.append({
            "type": "decisions",
            "title": "触发判定",
            "data": output["_decisions"],
        })
    return cards


def _episode_auto_save(
    skill_name: str,
    user_message: str,
    skill_result: Dict,
    llm_reasoning: str,
    history: List[Dict],
    result_data: Dict,
):
    """在 LLM Skill 执行后自动保存 Episode（非阻塞）。同时检查是否触发自动编译。"""
    try:
        from core.episode_store import ep_save
        # 提取上下文（最近一次历史中的关键数据）
        # 注意：llm_action_steps 必须传空列表（而非 [skill_name]），
        # 否则 V0 编译器会生成 workflow=[{"action": "life_planner"}] 这类无效单步。
        # 空列表会让 V0 编译器回退到按 skill 类型的默认 workflow。
        # source_report_id：优先取 skill 返回的报告 ID，保证 episode 可溯源。
        report_id = ""
        if isinstance(skill_result, dict):
            report_id = str(skill_result.get("report_id", "") or "")
        ep_save(
            source_skill=skill_name,
            source_report_id=report_id,
            user_input=user_message,
            context_json={
                "skill": skill_name,
                "history_length": len(history),
            },
            llm_reasoning=llm_reasoning[:2000],
            llm_action_steps=[],
            result_json=result_data,
        )

        # ── Auto Formation: 检查是否积累足够 Episode → 自动编译 Experience ──
        try:
            from core.experience_engine import exp_check_auto_compilation
            exp_id = exp_check_auto_compilation(skill_name)
            if exp_id:
                logger.info(f"[CHATBOT] 🤖 自动编译新经验: {exp_id}")
        except Exception:
            pass  # auto-compilation is best-effort
    except Exception as e:
        logger.debug(f"[CHATBOT] Episode 保存跳过: {e}")


def _record_exp_feedback(
    exp_runtime_result: Optional[Dict],
    reply: str,
    skill_result: Any,
) -> None:
    """
    在 chatbot 路径完成后，记录 confidence 反馈。

    根据 exp_runtime_result 中的 decision 字段自动路由:
      - execute: 经验直接执行 → 自动检测 → 更新 confidence
      - assist: LLM 辅助 → 对比 LLM 输出与经验 prefill → 更新 confidence
      - delegate: LLM 完整输出 → 运行候选经验 executor → 对比 → 更新 confidence

    这是用户描述的核验流程第 5 步：
    "LLM 对比输出和 experience 程序的差异，更新 confidence 函数值"
    """
    if not exp_runtime_result:
        return

    try:
        from core.experience.confidence_feedback import record_feedback, record_activation_only
    except Exception as e:
        logger.debug(f"[CHATBOT] confidence_feedback 不可用: {e}")
        return

    decision = exp_runtime_result.get("decision", "delegate")
    exp_id = exp_runtime_result.get("experience_id")
    exp_output = exp_runtime_result.get("output")

    # ── execute/assist: 经验已执行，直接记录反馈 ──
    if decision in ("execute", "assist") and exp_id:
        try:
            fb = record_feedback(
                exp_id=exp_id,
                decision=decision,
                exp_output=exp_output,
                llm_output=reply if decision == "assist" else None,
                llm_data=skill_result if isinstance(skill_result, dict) else None,
                context={"triggered_by": "chatbot"},
            )
            logger.debug(f"[CHATBOT] confidence反馈({decision}): {fb}")
        except Exception as e:
            logger.debug(f"[CHATBOT] confidence反馈异常({decision}): {e}")

    # ── delegate: 候选经验存在但未执行，运行 executor 后对比 ──
    elif decision == "delegate":
        candidates = exp_runtime_result.get("candidates", [])
        if not candidates:
            return

        best = candidates[0]
        best_exp_id = best.get("exp_id")
        best_exp = best.get("experience", {})

        if not best_exp_id or not best_exp:
            return

        try:
            from core.experience.executor import execute as _exec
            exp_state = exp_runtime_result.get("state", {})
            exec_result = _exec(best_exp, exp_state)
            exp_out = exec_result.get("output", {})

            fb = record_feedback(
                exp_id=best_exp_id,
                decision="delegate",
                exp_output=exp_out,
                llm_output=reply,
                llm_data=skill_result if isinstance(skill_result, dict) else None,
                context={"triggered_by": "chatbot"},
            )
            logger.debug(f"[CHATBOT] confidence反馈(delegate): {fb}")
        except Exception as e:
            logger.debug(f"[CHATBOT] delegate对比失败: {e}")
            try:
                record_activation_only(
                    best_exp_id, "delegate",
                    triggered_by="chatbot",
                    output_summary=reply[:500],
                )
            except Exception:
                pass
