"""
VE管家 子skill — 投资顾问 📈
================================
解读研报新闻、分析市场热点对持仓的影响。
读取RAG知识库中的研报，结合用户持仓，
给出投资建议和风险提示。
"""

import json
import sqlite3
import logging
from typing import Dict, Any, List
from core.ai_gateway import ve4_ai_call
from app_paths import DATA_DIR, DB_PATH

logger = logging.getLogger("ve5.chatbot.investment_advisor")

_OUTPUT_DIR = DATA_DIR / "chatbot" / "skills"
_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
_ADVICE_FILE = _OUTPUT_DIR / "investment_advice.json"


def _load_holdings() -> List[Dict]:
    """加载股票/ETF类持仓"""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT product_name, product_code, current_value, asset_class, account_key FROM asset_holdings WHERE asset_class IN ('aggressive','equity','stable','fixed_income') ORDER BY current_value DESC"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"[INVESTMENT_ADVISOR] 持仓加载失败：{e}")
        return []


def _load_rag_reports() -> List[Dict]:
    """加载RAG知识库中的研报"""
    try:
        from tactical.fundamental.knowledge.vector_store import ve4_kb_list
        reports = ve4_kb_list(limit=10)
        return [
            {"title": r.get("title",""), "summary": r.get("summary",""), "sentiment": r.get("sentiment","")}
            for r in reports
        ]
    except Exception as e:
        logger.warning(f"[INVESTMENT_ADVISOR] RAG加载失败：{e}")
        return []


def ve5_skill_investment_advisor(user_message: str, history: List[Dict]) -> Dict[str, Any]:
    """投资顾问skill主入口"""
    holdings = _load_holdings()
    reports = _load_rag_reports()

    if not holdings:
        return {
            "reply": "📭 当前还没有可分析的投资持仓。你可以通过「数据同步」上传证券截图，或手动添加持仓。",
            "data": {},
            "cards": [],
            "actions": [{"label": "去添加持仓", "action": "navigate", "url": "account-overview.html", "icon": "➕"}],
        }

    # 构建上下文
    context = "用户持仓：\n"
    for h in holdings[:15]:
        context += f"- {h.get('product_name','')}（{h.get('product_code','')}）: ¥{h.get('current_value',0):,.0f}\n"

    if reports:
        context += "\n近期研报摘要：\n"
        for r in reports[:5]:
            context += f"- {r['title']} [{r.get('sentiment','')}]: {r['summary'][:100]}...\n"

    system = """你是VE管家的投资顾问。根据用户的持仓和最新研报，提供投资分析和建议。

分析维度：
1. 持仓集中度 — 是否过度集中
2. 行业分布 — 行业暴露是否均衡
3. 研报关联 — 近期研报对用户持仓的影响分析
4. 风险提示 — 当前持仓面临的主要风险
5. 调仓建议 — 具体可执行的建议

风格：专业、理性、不制造焦虑。所有分析基于已有数据，不做预测。"""

    prompt = f"""{context}

用户需求：{user_message}

请进行投资分析。"""

    result = ve4_ai_call(
        task_type="investment_advice",
        system=system,
        prompt=prompt,
        format_type="text",
        complexity="high",
        max_tokens=2048,
        contains_privacy_data=True,
    )

    reply = result.text if result.success else "抱歉，投资分析失败。"

    data = {"holding_count": len(holdings), "report_count": len(reports), "holdings": holdings[:10]}
    _ADVICE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    cards = []
    if reports:
        cards.append({"type": "report_list", "title": "相关研报", "data": reports[:5]})

    actions = [
        {"label": "研报知识库", "action": "navigate", "url": "tactical-sandbox.html", "icon": "📚"},
        {"label": "持仓影响分析", "action": "navigate", "url": "tactical-planning.html", "icon": "🔍"},
        {"label": "重新分析", "action": "rerun_skill", "skill": "investment_advisor", "icon": "🔄"},
    ]

    return {"reply": reply, "data": data, "cards": cards, "actions": actions}
