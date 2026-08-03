"""
VE5 Experience Layer — Experience Matcher
==========================================
问题: "有没有类似的经验？"

不是 LLM——是搜索引擎。
输入 State Object，输出候选经验列表。

算法:
  1. 规则匹配 (intent == exp.trigger_event)
  2. 标签相似度 (Jaccard)
  3. Embedding 余弦相似度 (V1: TF向量)
  4. Metadata 过滤 (intent→type 跨表, 触发频率检查)
  5. 排序 (0.5×rule + 0.3×tag + 0.2×embedding) × confidence

相似度综合公式:
  similarity = 0.5 × rule_score + 0.3 × tag_score + 0.2 × embedding_score
"""

import json
import logging
import math
from typing import Dict, Any, List, Optional

logger = logging.getLogger("ve5.experience.matcher")


# ════════════════════════════════════════════════
# Metadata: intent → 允许的 experience type
# ════════════════════════════════════════════════

_INTENT_TYPE_MAP = {
    "spending_analysis": {"goal_tracking", "life_planner"},
    "asset_review": {"goal_tracking"},
    "goal_tracking": {"goal_tracking"},
    "budget_planning": {"life_planner", "goal_tracking"},
    "investment_analysis": {"goal_tracking"},
    "life_planning": {"life_planner"},
    "unknown": set(),
}


# ════════════════════════════════════════════════
# Trigger → Intent 语义映射 (monthly_check → goal_tracking)
# ════════════════════════════════════════════════
_TRIGGER_INTENT_MAP = {
    "monthly_check": {"goal_tracking", "spending_analysis", "budget_planning"},
    "weekly_check": {"life_planner", "life_planning"},
    "on_income": {"goal_tracking", "spending_analysis"},
    "on_new_expense": {"spending_analysis"},
}


def match(state: Dict[str, Any], limit: int = 5) -> List[Dict[str, Any]]:
    """
    输入: State Object (来自 encoder.encode())
    输出: 候选经验列表 [{exp_id, similarity, rule_score, tag_score, emb_score, experience}, ...]

    三元组匹配:
      similarity = 0.5 × rule_score + 0.3 × tag_score + 0.2 × embedding_score
    """
    intent = state.get("intent", "unknown")
    state_embedding = state.get("embedding", [])
    state_tags = _state_to_tags(state)

    # Metadata filter: 只查相关 type
    allowed_types = _INTENT_TYPE_MAP.get(intent, set())

    # 从 store 加载经验
    try:
        from core.experience_store import exp_list as _store_list
        all_exps = _store_list()
    except Exception as e:
        logger.warning(f"[MATCHER] 加载经验失败: {e}")
        return []

    candidates = []

    for exp in all_exps:
        exp_type = exp.get("type", "")
        exp_tags = exp.get("tags", [])
        exp_trigger = exp.get("trigger_event", "")

        # ── M1: Metadata filter ──
        if allowed_types and exp_type not in allowed_types:
            continue

        # ── M2: 规则匹配 ──
        rule_score = _compute_rule_score(intent, exp_trigger, exp_type)

        # ── M3: 标签 Jaccard ──
        tag_score = _jaccard(state_tags, exp_tags)

        # ── M4: Embedding 余弦 ──
        emb_score = _compute_embedding_score(state_embedding, exp)
        if emb_score is None:
            emb_score = 0.0

        # ── 综合相似度 ──
        similarity = 0.5 * rule_score + 0.3 * tag_score + 0.2 * emb_score

        # 最低阈值
        if similarity < 0.10:
            continue

        candidates.append({
            "exp_id": exp["exp_id"],
            "similarity": round(similarity, 4),
            "rule_score": round(rule_score, 4),
            "tag_score": round(tag_score, 4),
            "emb_score": round(emb_score, 4),
            "experience": exp,
        })

    # 排序: similarity × confidence 降序, 去重
    seen = set()
    unique = []
    for c in candidates:
        if c["exp_id"] not in seen:
            seen.add(c["exp_id"])
            unique.append(c)
    unique.sort(
        key=lambda c: c["similarity"] * c["experience"].get("confidence", 0),
        reverse=True,
    )

    result = unique[:limit]
    logger.debug(
        f"[MATCHER] intent={intent} candidates={len(candidates)} returned={len(result)}"
    )
    return result


# ════════════════════════════════════════════════
# 匹配子算法
# ════════════════════════════════════════════════

def _compute_rule_score(intent: str, exp_trigger: str, exp_type: str) -> float:
    """
    规则匹配分:
      1.0 → intent 完全等于 exp_trigger
      0.8 → exp_trigger 在 Trigger→Intent 语义映射中匹配当前 intent
      0.7 → intent 是 exp_trigger 的子串
      0.5 → intent 部分匹配 exp_trigger (拆词)
      0.3 → exp 无特定 trigger (通用经验)
      0.1 → 其他
    """
    if not exp_trigger:
        return 0.3

    if intent == exp_trigger:
        return 1.0

    # Trigger→Intent 语义匹配 (monthly_check → goal_tracking)
    if exp_trigger in _TRIGGER_INTENT_MAP:
        if intent in _TRIGGER_INTENT_MAP[exp_trigger]:
            return 0.8

    if intent in exp_trigger or exp_trigger in intent:
        return 0.7

    # 拆词匹配
    intent_parts = set(intent.split("_"))
    trigger_parts = set(exp_trigger.split("_"))
    overlap = intent_parts & trigger_parts
    if overlap:
        return 0.3 + 0.4 * (len(overlap) / max(len(trigger_parts), 1))

    return 0.1


def _compute_embedding_score(
    state_emb: List[float], exp: Dict
) -> Optional[float]:
    """Embedding 余弦相似度 (V1)"""
    if not state_emb:
        return None

    exp_emb = exp.get("embedding")
    if not exp_emb:
        # 经验没有预存向量 → 从 tags + name 即时生成
        exp_emb = _compute_experience_embedding(exp)
        # 不在这里写回 store, 避免导入循环; 由调用方按需缓存

    if not exp_emb or len(state_emb) != len(exp_emb):
        return None

    return _cosine(state_emb, exp_emb)


def _compute_experience_embedding(exp: Dict) -> List[float]:
    """从经验的 tags + name + description 即时计算 embedding"""
    from core.experience.encoder import _FEATURE_WORDS

    text = " ".join([
        exp.get("name", ""),
        exp.get("description", ""),
        " ".join(exp.get("tags", [])),
    ]).lower()

    tf_vector = []
    for word in _FEATURE_WORDS:
        count = text.count(word)
        tf_vector.append(min(count / max(len(text.split()), 1), 1.0))

    intent_list = [
        "spending_analysis", "asset_review", "goal_tracking",
        "budget_planning", "investment_analysis", "life_planning", "unknown",
    ]
    trigger = exp.get("trigger_event", "")
    for i_name in intent_list:
        tf_vector.append(1.0 if i_name in trigger or i_name in exp.get("type", "") else 0.0)

    return tf_vector


def _jaccard(a: List[str], b: List[str]) -> float:
    """Jaccard 相似度"""
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.5
    if not sa or not sb:
        return 0.1
    return len(sa & sb) / len(sa | sb)


def _cosine(a: List[float], b: List[float]) -> float:
    """余弦相似度"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _state_to_tags(state: Dict) -> List[str]:
    """State Object → 标签列表"""
    tags = []
    intent = state.get("intent", "")
    if intent:
        tags.append(intent)

    entities = state.get("entities", {})
    if entities.get("time_range"):
        tags.append(entities["time_range"])

    context = state.get("context", {})
    if context.get("module"):
        tags.append(context["module"])
    if context.get("trigger_type"):
        tags.append(context["trigger_type"])

    user_state = state.get("user_state", {})
    if user_state.get("has_transaction_data"):
        tags.append("has_transactions")
    if user_state.get("has_holdings_data"):
        tags.append("has_holdings")
    if user_state.get("has_goals"):
        tags.append("has_goals")

    return tags
