"""
VE5 Experience Runtime — 统一调度入口
=======================================
每个 Skill 在入口处调用 exp_runtime_dispatch()，
Runtime 自动完成 Encoder → Matcher → Controller → Executor 四层决策。

用法:
    from core.experience import exp_runtime_dispatch

    result = exp_runtime_dispatch(
        user_input="帮我看看最近花销",
        context={"module": "goal_tracker", "trigger_type": "user_message"},
    )

    if result["decision"] == "execute":
        return result["output"]  # 直接返回，不调 LLM
    elif result["decision"] == "assist":
        call_llm_with_origin(result)  # LLM 辅助，注入 origin
    else:
        call_llm_full()  # 完整 LLM 探索
"""

from .encoder import encode
from .matcher import match
from .controller import decide
from .executor import execute


def exp_runtime_dispatch(
    user_input: str,
    context: dict = None,
) -> dict:
    """
    统一调度入口。

    参数:
        user_input: 用户原始输入
        context: {
            "module": str,
            "page": str,
            "trigger_type": str,
            "user_state": dict (optional)
        }

    返回:
        {
            "decision": "execute" | "assist" | "delegate",
            "experience": {...} or None,      # 命中的经验（delegate 路径也有候选经验）
            "experience_id": str or None,      # 经验 ID（方便 feedback 调用）
            "state": {...},
            "output": {...} or None,           # execute/assist 有输出，delegate 为 None
            "activation_score": float or None,
            "candidates": [...] or [],          # 候选经验列表（供 delegate 后对比使用）
        }
    """
    # Layer 1: 编码当前状态
    state = encode(user_input, context)

    # Layer 2: 检索候选经验
    candidates = match(state)

    # Layer 3: 决定是否激活
    decision = decide(candidates, state)

    # Layer 4: 执行或委托
    if decision["activate"] and decision["experience"]:
        result = execute(decision["experience"], state)
        result["state"] = state
        result["activation_score"] = decision.get("activation_score")
        return result

    elif decision["assist"] and decision["experience"]:
        result = execute(decision["experience"], state)
        result["state"] = state
        result["activation_score"] = decision.get("activation_score")
        return result

    else:
        # delegate: 保留候选经验信息，供 LLM 完成后做对比反馈
        candidate_exps = [
            {
                "exp_id": c["exp_id"],
                "similarity": c.get("similarity", 0),
                "experience": c.get("experience", {}),
            }
            for c in candidates[:3]  # 最多保留 3 个候选
        ]
        return {
            "decision": "delegate",
            "experience": decision.get("experience"),
            "experience_id": decision.get("experience", {}).get("exp_id") if decision.get("experience") else None,
            "state": state,
            "output": None,
            "reason": decision.get("reason", "无可用经验"),
            "activation_score": decision.get("activation_score"),
            "safety_factors": decision.get("safety_factors", []),
            "candidates": candidate_exps,
        }


# ── 便捷导出 ──
__all__ = [
    "exp_runtime_dispatch",
    "encode",
    "match",
    "decide",
    "execute",
]
