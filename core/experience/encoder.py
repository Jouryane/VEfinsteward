"""
VE5 Experience Layer — State Encoder
=====================================
问题: "现在发生了什么？"

把用户输入 + 当前环境上下文转换成机器可比较的 State Object。
感知器/编码器，不是 LLM agent。

Phase 1 (冷启动): 规则匹配意图 + LLM 兜底
Phase 2 (有经验): 高频意图规则直通, 低频走 LLM
Phase 3 (成熟): 大部分程序化, LLM 只处理 novel state

State Object:
{
  "intent": str,              # spending_analysis | asset_review | goal_tracking | ...
  "entities": dict,           # { time_range, target_amount, ... }
  "user_state": {             # 自动探测的财务状态
    "total_assets": float,
    "monthly_income": float,
    "monthly_expense": float,
    "savings_rate": float,
    "has_transaction_data": bool,
    "has_holdings_data": bool,
    "has_goals": bool,
    "anomalies": [str],       # 自动检测的异常标记
  },
  "context": dict,
  "embedding": list[float],   # 语义向量, 供 Matcher 用 (V1)
  "raw_input": str,
  "encoded_at": str
}
"""

import json
import logging
import re
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta

logger = logging.getLogger("ve5.experience.encoder")


# ════════════════════════════════════════════════
# 意图关键词映射 (规则快速路径)
# ════════════════════════════════════════════════

_INTENT_PATTERNS: Dict[str, list] = {
    "spending_analysis": [
        "花销", "花了", "消费", "支出", "开销", "账单", "用钱",
        "花了多少", "买了什么", "钱花哪", "支出报告", "消费记录",
    ],
    "asset_review": [
        "账户", "资产", "持仓", "总资产", "看一下", "查一下",
        "还有多少钱", "盈亏", "收益", "涨了", "跌了", "市值",
    ],
    "goal_tracking": [
        "目标", "存钱", "买房", "买车", "首付", "储蓄",
        "攒钱", "进度", "还有多久", "还差多少",
    ],
    "budget_planning": [
        "预算", "省钱", "砍掉", "节省", "省下", "压缩",
        "计划", "规划", "分配", "减少",
    ],
    "investment_analysis": [
        "投资", "股票", "基金", "加仓", "减仓", "买入", "卖出",
        "止损", "止盈", "追涨", "抄底", "调仓",
    ],
    "life_planning": [
        "食谱", "买菜", "做饭", "吃什么", "购物清单",
        "周末", "娱乐", "生活规划", "生活开支", "食谱规划",
    ],
}

# 意图优先级 (当多个 pattern 命中时的 tie-breaking)
_INTENT_PRIORITY = [
    "investment_analysis", "goal_tracking", "budget_planning",
    "spending_analysis", "life_planning", "asset_review",
]


def _rule_extract_intent(user_input: str, context: Dict) -> Optional[str]:
    """
    Phase 1/2 规则快速路径: 关键词匹配 intent。
    命中的 intent 跳过 LLM 调用。
    多个命中时按优先级取第一个。
    """
    text = user_input.lower()
    hits = []
    for intent, keywords in _INTENT_PATTERNS.items():
        for kw in keywords:
            if kw in text:
                hits.append((_INTENT_PRIORITY.index(intent), intent))
                break

    if hits:
        hits.sort()
        return hits[0][1]

    # 从页面 context 推断
    module = context.get("module", "") if context else ""
    if module == "goal_tracker":
        return "goal_tracking"
    if module == "life_planner":
        return "life_planning"

    return None


def _llm_extract_state(user_input: str, context: Dict) -> Dict[str, Any]:
    """Phase 1 LLM 慢路径: 仅当规则未命中时调用"""
    try:
        from core.ai_gateway import ve4_ai_call

        system = """你是状态编码器。只输出JSON:
{"intent":"spending_analysis|asset_review|goal_tracking|budget_planning|investment_analysis|life_planning|unknown","entities":{"time_range":"recent|this_month|last_month|this_year|custom"},"summary":"一句话总结"}
只输出JSON。"""

        prompt = f"用户输入: {user_input}\n上下文: {json.dumps(context or {}, ensure_ascii=False)}"

        result = ve4_ai_call(
            task_type="state_encoding",
            system=system,
            prompt=prompt,
            format_type="json",
            complexity="low",
            max_tokens=256,
        )
        if result.success and result.text:
            return json.loads(result.text)
    except Exception as e:
        logger.warning(f"[ENCODER] LLM 抽取失败: {e}")

    return {"intent": "unknown", "entities": {}, "summary": user_input[:80]}


# ════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════

def encode(user_input: str, context: Dict = None) -> Dict[str, Any]:
    """
    编码当前状态为 State Object。
    """
    ctx = context or {}

    # ── 提取 intent ──
    intent = _rule_extract_intent(user_input, ctx)
    llm_used = False
    if intent is None:
        llm_state = _llm_extract_state(user_input, ctx)
        intent = llm_state.get("intent", "unknown")
        entities = llm_state.get("entities", {})
        llm_used = True
    else:
        entities = _rule_extract_entities(user_input)

    # ── 用户状态 (自动探测 + 调用方注入) ──
    user_state = ctx.get("user_state", {}) or {}
    if not user_state:
        user_state = _probe_user_state()

    # ── 语义 embedding (V1) ──
    embedding = _compute_state_embedding(user_input, intent)

    # ── 构建 ──
    state = {
        "intent": intent,
        "entities": entities,
        "user_state": user_state,
        "context": {
            "module": ctx.get("module", ""),
            "page": ctx.get("page", ""),
            "trigger_type": ctx.get("trigger_type", "user_message"),
        },
        "embedding": embedding,
        "raw_input": user_input,
        "encoding_method": "llm" if llm_used else "rule",
        "encoded_at": datetime.now().isoformat(),
    }

    logger.debug(f"[ENCODER] intent={intent} method={'llm' if llm_used else 'rule'}")
    return state


# ════════════════════════════════════════════════
# 实体提取 (规则)
# ════════════════════════════════════════════════

def _rule_extract_entities(user_input: str) -> Dict[str, str]:
    entities = {}
    if any(kw in user_input for kw in ["这个月", "本月", "当月"]):
        entities["time_range"] = "this_month"
    elif any(kw in user_input for kw in ["上个月", "上月"]):
        entities["time_range"] = "last_month"
    elif any(kw in user_input for kw in ["最近", "近期", "这几天"]):
        entities["time_range"] = "recent"
    elif any(kw in user_input for kw in ["今天", "今日"]):
        entities["time_range"] = "today"
    elif any(kw in user_input for kw in ["今年", "全年", "年度"]):
        entities["time_range"] = "this_year"
    else:
        entities["time_range"] = "unspecified"

    # 金额提取
    amount_match = re.search(r'(\d+\.?\d*)\s*(万|元|块)', user_input)
    if amount_match:
        num = float(amount_match.group(1))
        unit = amount_match.group(2)
        if unit == "万":
            num *= 10000
        entities["target_amount"] = str(int(num))

    return entities


# ════════════════════════════════════════════════
# 用户状态探测
# ════════════════════════════════════════════════

def _probe_user_state() -> Dict[str, Any]:
    """完整的用户财务状态探测 + 异常标记"""
    state = {
        "total_assets": 0.0,
        "monthly_income": 0.0,
        "monthly_expense": 0.0,
        "savings_rate": 0.0,
        "has_transaction_data": False,
        "has_holdings_data": False,
        "has_goals": False,
        "last_analysis_date": "",
        "anomalies": [],
    }

    month = datetime.now().strftime("%Y-%m")

    try:
        import sqlite3
        from app_paths import DB_PATH

        if not DB_PATH.exists():
            return state

        conn = sqlite3.connect(str(DB_PATH))

        # 总资产
        r = conn.execute("SELECT SUM(current_value) FROM asset_holdings WHERE is_superseded=0").fetchone()
        state["total_assets"] = float(r[0] or 0)

        # 持仓条数
        r = conn.execute("SELECT COUNT(*) FROM asset_holdings WHERE is_superseded=0").fetchone()
        state["has_holdings_data"] = (r[0] or 0) > 0

        # 本月收入/支出
        r = conn.execute(
            "SELECT SUM(amount) FROM transactions WHERE transaction_type='income' AND transaction_date LIKE ?",
            (f"{month}%",)
        ).fetchone()
        state["monthly_income"] = abs(float(r[0] or 0))

        r = conn.execute(
            "SELECT SUM(amount) FROM transactions WHERE transaction_type='expense' AND transaction_date LIKE ?",
            (f"{month}%",)
        ).fetchone()
        state["monthly_expense"] = abs(float(r[0] or 0))
        state["has_transaction_data"] = (state["monthly_income"] + state["monthly_expense"]) > 0

        # 储蓄率
        if state["monthly_income"] > 0:
            state["savings_rate"] = round(
                max(0, state["monthly_income"] - state["monthly_expense"]) / state["monthly_income"], 2
            )

        # ── 异常检测 ──
        anomalies = []

        # 本月有大额单笔交易 (> 总资产的 20%)
        if state["total_assets"] > 0:
            r = conn.execute(
                "SELECT MAX(abs(amount)) FROM transactions WHERE transaction_date LIKE ?",
                (f"{month}%",)
            ).fetchone()
            max_txn = abs(float(r[0] or 0))
            if max_txn > state["total_assets"] * 0.2:
                anomalies.append("本月有大额单笔交易")

        # 储蓄率暴跌 (< 10%)
        if state["monthly_income"] > 0 and state["savings_rate"] < 0.10:
            anomalies.append("储蓄率过低(<10%)")

        # 支出占收入比 > 95%
        if state["monthly_income"] > 0 and state["monthly_expense"] > state["monthly_income"] * 0.95:
            anomalies.append("支出接近或超过收入")

        # 无交易数据
        if not state["has_transaction_data"] and not state["has_holdings_data"]:
            anomalies.append("暂无财务数据")

        state["anomalies"] = anomalies

        conn.close()
    except Exception as e:
        logger.debug(f"[ENCODER] 用户状态探测出错: {e}")

    # goals
    try:
        from app_paths import DATA_DIR
        gf = DATA_DIR / "goals.json"
        if gf.exists():
            goals = json.loads(gf.read_text(encoding="utf-8")).get("goals", [])
            state["has_goals"] = len(goals) > 0
    except Exception:
        pass

    return state


# ════════════════════════════════════════════════
# Embedding (V1): 简单 TF-IDF 风格向量
# ════════════════════════════════════════════════

# 预定义的特征词表 (用于构建可比较的向量)
_FEATURE_WORDS = [
    # 意图类
    "花费", "消费", "支出", "账户", "资产", "持仓", "目标", "储蓄",
    "预算", "投资", "股票", "基金", "食谱", "购物", "规划",
    # 时间类
    "本月", "上月", "最近", "今年", "今天", "本周", "每月",
    # 状态类
    "收入", "结余", "余额", "盈亏", "进度", "金额", "总额",
    # 操作类
    "查看", "分析", "检查", "规划", "调整", "追踪", "监控",
]


def _compute_state_embedding(user_input: str, intent: str) -> List[float]:
    """
    V1 简易 embedding: TF 向量 (无需外部模型依赖)。

    特征: _FEATURE_WORDS 中每个词的 TF 频率 + intent one-hot。
    维度: len(_FEATURE_WORDS) + 7 (intent 数)
    """
    text = user_input.lower()

    # TF 向量
    tf_vector = []
    for word in _FEATURE_WORDS:
        count = text.count(word)
        # 归一化到 [0, 1]
        tf_vector.append(min(count / max(len(text.split()), 1), 1.0))

    # intent one-hot
    intent_list = [
        "spending_analysis", "asset_review", "goal_tracking",
        "budget_planning", "investment_analysis", "life_planning", "unknown",
    ]
    for i_name in intent_list:
        tf_vector.append(1.0 if intent == i_name else 0.0)

    return tf_vector


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """余弦相似度"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = (sum(x * x for x in a)) ** 0.5
    norm_b = (sum(y * y for y in b)) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
