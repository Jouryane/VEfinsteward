"""
VE5 Experience Layer — Activation Controller
=============================================
问题: "虽然有经验，但现在应该用它吗？"

最重要的一层——"找到经验" ≠ "执行经验"。

判定公式:
  Activation Score = Confidence × Similarity × Safety Gate

Safety Gate 因子 (0.0-1.0, 默认 1.0):
  0.0  → 用户状态剧变 → 强制走 LLM
  0.3  → 财务异常标记 → 大幅降权
  0.5  → 元数据不匹配 → 降低激活
  0.4  → 经验超期未用 → 降低激活
  1.0  → 完全匹配 → 正常通过

输出:
  {
    "activate": bool,
    "assist": bool,
    "reason": str,
    "experience": {...},
    "activation_score": float,
    "safety_factors": [...],
  }
"""

import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta

logger = logging.getLogger("ve5.experience.controller")

_AUTOMATIC_THRESHOLD = 0.75
_ASSIST_THRESHOLD = 0.35


def decide(
    candidates: List[Dict[str, Any]],
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """激活判定。"""
    intent = state.get("intent", "unknown")
    user_state = state.get("user_state", {})

    if not candidates:
        return {
            "activate": False,
            "assist": False,
            "reason": f"没有匹配的经验 (intent={intent})",
            "experience": None,
            "activation_score": 0.0,
            "safety_factors": [],
        }

    best = candidates[0]
    exp = best["experience"]
    similarity = best["similarity"]
    confidence = exp.get("confidence", 0.25)
    origin = exp.get("origin", {})

    # Safety Gate: 多重因子乘积
    safety, factors = _compute_safety_gate(user_state, exp, state)

    # Activation Score
    activation_score = confidence * similarity * safety

    logger.debug(
        f"[CONTROLLER] {exp['name']}: conf={confidence:.3f} "
        f"sim={similarity:.3f} safety={safety:.2f} → score={activation_score:.3f}"
    )

    if activation_score >= _AUTOMATIC_THRESHOLD:
        return {
            "activate": True,
            "assist": False,
            "reason": (
                f"经验 {exp['name']} 高度可靠 (conf={confidence:.2f}, "
                f"sim={similarity:.2f}) → 绕过 LLM 直接执行"
            ),
            "experience": exp,
            "activation_score": round(activation_score, 4),
            "safety_factors": factors,
        }
    elif activation_score >= _ASSIST_THRESHOLD:
        return {
            "activate": False,
            "assist": True,
            "reason": (
                f"经验 {exp['name']} 可用但需 LLM 辅助 (conf={confidence:.2f}, "
                f"sim={similarity:.2f})。Origin: {origin.get('created_reason', 'unknown')}"
            ),
            "experience": exp,
            "activation_score": round(activation_score, 4),
            "safety_factors": factors,
        }
    else:
        return {
            "activate": False,
            "assist": False,
            "reason": (
                f"经验 {exp['name']} 置信度不足 (conf={confidence:.2f}, "
                f"sim={similarity:.2f}) → LLM 完整推理"
            ),
            "experience": exp,
            "activation_score": round(activation_score, 4),
            "safety_factors": factors,
        }


# ════════════════════════════════════════════════
# Safety Gate — 多重因子检测
# ════════════════════════════════════════════════

def _compute_safety_gate(
    user_state: Dict, experience: Dict, state: Dict
) -> tuple:
    """
    计算 Safety Gate。

    返回 (safety_value, factors_list)

    检测维度:
      F1: 财务异常 (anomalies 标记)      → ×0.3
      F2: 元数据不匹配 (type ↔ 数据)     → ×0.5
      F3: 经验过期 (90天+ 未使用)        → ×0.4
      F4: 触发频率不匹配                 → ×0.7
      F5: 上下文模块不兼容               → ×0.6
    """
    factors = []
    safety = 1.0

    # ── F1: 财务异常标记 ──
    anomalies = user_state.get("anomalies", [])
    if anomalies:
        safety *= 0.3
        factors.append({
            "factor": "financial_anomaly",
            "multiplier": 0.3,
            "detail": f"检测到异常: {', '.join(anomalies[:2])}",
        })

    # ── F2: 元数据不匹配 ──
    exp_type = experience.get("type", "")
    data_check = _check_data_prerequisite(exp_type, user_state)
    if data_check:
        safety *= 0.5
        factors.append(data_check)

    # ── F3: 经验过期 ──
    last_used = experience.get("last_used", "")
    if last_used:
        try:
            last = datetime.fromisoformat(last_used)
            days = (datetime.now() - last).days
            if days > 90:
                safety *= 0.4
                factors.append({
                    "factor": "stale_experience",
                    "multiplier": 0.4,
                    "detail": f"经验 {days} 天未使用",
                })
            elif days > 30:
                safety *= 0.7
                factors.append({
                    "factor": "aging_experience",
                    "multiplier": 0.7,
                    "detail": f"经验 {days} 天未使用",
                })
        except Exception:
            pass

    # ── F4: 触发频率不匹配 ──
    trigger_freq = experience.get("trigger_frequency", "recurring")
    context = state.get("context", {})
    if trigger_freq == "recurring" and context.get("trigger_type") == "page_load":
        # 页面加载不应触发 recurring 经验 (应等用户主动输入)
        safety *= 0.5
        factors.append({
            "factor": "frequency_mismatch",
            "multiplier": 0.5,
            "detail": "recurring 经验不应由 page_load 触发",
        })

    # ── F5: 上下文模块不兼容 ──
    module = context.get("module", "")
    type_module_map = {
        "goal_tracking": {"goal_tracker"},
        "life_planner": {"life_planner"},
    }
    expected_modules = type_module_map.get(exp_type, set())
    if module and expected_modules and module not in expected_modules:
        safety *= 0.6
        factors.append({
            "factor": "module_mismatch",
            "multiplier": 0.6,
            "detail": f"经验 type={exp_type} 不适配 module={module}",
        })

    return round(safety, 4), factors


def _check_data_prerequisite(
    exp_type: str, user_state: Dict
) -> Optional[Dict]:
    """
    检查经验的必要数据是否存在。

    返回 None 表示通过，返回 dict 表示不通过的原因。
    """
    if exp_type == "goal_tracking" and not user_state.get("has_goals"):
        return {
            "factor": "missing_goals",
            "multiplier": 0.5,
            "detail": "用户没有目标数据",
        }

    if exp_type in ("life_planner",) and not user_state.get("has_transaction_data"):
        return {
            "factor": "missing_transactions",
            "multiplier": 0.5,
            "detail": "用户没有交易数据",
        }

    savings_rate = user_state.get("savings_rate", 0)
    if exp_type == "goal_tracking" and savings_rate < 0.05 and savings_rate > 0:
        return {
            "factor": "near_zero_savings",
            "multiplier": 0.6,
            "detail": f"储蓄率极低 ({savings_rate:.1%})，经验可能不适用",
        }

    return None
