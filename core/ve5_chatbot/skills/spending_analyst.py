"""
VE管家 子skill — 消费分析师 📊
================================
分析用户支出结构、必需/弹性分析、节省建议。
读取transactions表，按分类统计，
识别消费模式和节省空间。
"""

import json
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List
from core.ai_gateway import ve4_ai_call
from app_paths import DATA_DIR, DB_PATH
from core.ve5_chatbot.report_store import ve5_report_save

logger = logging.getLogger("ve5.chatbot.spending_analyst")

_OUTPUT_DIR = DATA_DIR / "chatbot" / "skills"
_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
_SPENDING_FILE = _OUTPUT_DIR / "spending_analysis.json"


def _load_monthly_spending(months: int = 3) -> Dict[str, Any]:
    """加载最近N个月的消费数据"""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row

        results = {}
        for i in range(months):
            dt = datetime.now() - timedelta(days=30 * i)
            month_str = dt.strftime("%Y-%m")

            # 总支出
            total = conn.execute(
                "SELECT SUM(amount) FROM transactions WHERE transaction_type='expense' AND transaction_date LIKE ?",
                (f"{month_str}%",)
            ).fetchone()[0] or 0

            # 必需支出
            essential = conn.execute(
                "SELECT SUM(amount) FROM transactions WHERE transaction_type='expense' AND transaction_date LIKE ? AND (is_essential=1 OR category_primary IN ('餐饮','交通','日用','居住','医疗','教育','通讯'))",
                (f"{month_str}%",)
            ).fetchone()[0] or 0

            # 分类明细
            cats = conn.execute(
                "SELECT category_primary, SUM(amount) as total, COUNT(*) as cnt FROM transactions WHERE transaction_type='expense' AND transaction_date LIKE ? GROUP BY category_primary ORDER BY total DESC",
                (f"{month_str}%",)
            ).fetchall()

            results[month_str] = {
                "total": total,
                "essential": essential,
                "non_essential": total - essential,
                "categories": [{"name": r["category_primary"], "amount": r["total"], "count": r["cnt"]} for r in cats],
            }

        conn.close()
        return results
    except Exception as e:
        logger.warning(f"[SPENDING_ANALYST] 消费数据加载失败：{e}")
        return {}


def _load_top_merchants(limit: int = 10) -> List[Dict]:
    """加载消费最多的商户"""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        month = datetime.now().strftime("%Y-%m")
        rows = conn.execute(
            "SELECT counterparty, SUM(amount) as total, COUNT(*) as cnt FROM transactions WHERE transaction_type='expense' AND transaction_date LIKE ? AND counterparty != '' GROUP BY counterparty ORDER BY total DESC LIMIT ?",
            (f"{month}%", limit)
        ).fetchall()
        conn.close()
        return [{"name": r["counterparty"], "amount": r["total"], "count": r["cnt"]} for r in rows]
    except Exception:
        return []


def ve5_skill_spending_analyst(user_message: str, history: List[Dict]) -> Dict[str, Any]:
    """消费分析skill主入口"""
    spending = _load_monthly_spending(3)
    merchants = _load_top_merchants(10)

    # 检查是否有实际消费数据
    has_data = False
    for month_data in spending.values():
        if month_data.get("total", 0) > 0:
            has_data = True
            break

    if not has_data:
        return {
            "reply": "📭 当前还没有消费记录数据。需要先通过「数据同步」上传消费截图，系统会自动提取支出信息。",
            "data": {},
            "cards": [],
            "actions": [
                {"label": "查看数据同步状态", "action": "navigate", "url": "dataflow-status.html", "icon": "📤"},
            ],
        }

    # ── 使用共享模块获取财务概览（含月份回退）──
    from core.ve5_chatbot.financial_data import load_financial_summary
    fin = load_financial_summary()
    data_month = fin.get("monthly_data_month", "")

    # 找到有数据的月份（优先使用共享模块的月份）
    current_month = data_month if data_month else datetime.now().strftime("%Y-%m")
    current = spending.get(current_month, {"total": 0, "essential": 0, "non_essential": 0, "categories": []})

    # 如果当月无数据但其它月份有，使用最近的月份
    if current["total"] == 0:
        for m, d in spending.items():
            if d.get("total", 0) > 0:
                current_month = m
                current = d
                break

    if current["total"] == 0:
        return {
            "reply": f"📭 暂无消费记录。你可能有支出数据但尚未同步截图。请通过「数据同步」上传消费截图。",
            "data": {},
            "cards": [],
            "actions": [
                {"label": "查看数据同步状态", "action": "navigate", "url": "dataflow-status.html", "icon": "📤"},
            ],
        }

    context = f"""用户消费概况（{current_month}）：
- 月总支出：¥{current['total']:,.0f}
- 必需支出：¥{current['essential']:,.0f}
- 弹性支出：¥{current['non_essential']:,.0f}

财务概览：
- 总资产：¥{fin.get('total_assets', 0):,.0f}
- 月收入：¥{fin.get('monthly_income', 0):,.0f}
- 月结余：¥{fin.get('monthly_savings', 0):,.0f}
- 年度累计储蓄(YTD)：¥{fin.get('ytd_savings', 0):,.0f}

分类明细：
"""
    for c in current.get("categories", [])[:10]:
        context += f"- {c['name']}: ¥{c['amount']:,.0f}（{c['count']}笔）\n"

    if merchants:
        context += "\n消费最多的商户：\n"
        for m in merchants[:5]:
            context += f"- {m['name']}: ¥{m['amount']:,.0f}（{m['count']}笔）\n"

    # 环比变化
    prev_month = (datetime.now() - timedelta(days=30)).strftime("%Y-%m")
    prev = spending.get(prev_month, {"total": 0})
    if prev["total"] > 0:
        change = (current["total"] - prev["total"]) / prev["total"] * 100
        context += f"\n环比上月：{change:+.1f}%\n"

    # ── 注入导航上下文（含数据可用性清单）──
    from core.ve5_chatbot.context_navigator import build_navigation_context
    nav_ctx = build_navigation_context("spending_analyst", user_message)

    system = f"""你是VE管家的消费分析师。根据用户的消费数据，进行深度消费分析。

{nav_ctx}

分析维度：
1. 支出结构 — 各类别占比和趋势
2. 必需 vs 弹性 — 分析弹性支出空间
3. 消费集中度 — 是否过度集中于某些商户/类别
4. 节省建议 — 具体的、可执行的节省方案
5. 预算建议 — 下个月各类别的合理预算
6. 收支平衡 — 结合月收入分析储蓄率

风格：数据驱动、不judge、给出建设性建议。"""

    prompt = f"""{context}

用户需求：{user_message}

请进行消费分析。"""

    result = ve4_ai_call(
        task_type="spending_analysis",
        system=system,
        prompt=prompt,
        format_type="text",
        complexity="high",
        max_tokens=2048,
        contains_privacy_data=True,
    )

    reply = result.text if result.success else "抱歉，消费分析失败。"

    data = {"current_month": current, "merchants": merchants, "history": spending}
    _SPENDING_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 多版本存储 ──
    top_cat = current.get("categories", [{}])[0].get("name", "")
    metadata = {
        "title": f"{current_month} 消费分析",
        "total": current.get("total", 0),
        "essential": current.get("essential", 0),
        "non_essential": current.get("non_essential", 0),
        "transaction_count": sum(c.get("count", 0) for c in current.get("categories", [])),
        "top_category": top_cat,
    }
    report_id = ve5_report_save("spending", data, metadata)

    cards = []
    if current.get("categories"):
        cards.append({"type": "spending_chart", "title": f"{current_month} 支出结构", "data": current["categories"]})

    actions = [
        {"label": "查看支出详情", "action": "navigate", "url": "pwa/account-overview.html", "icon": "📊"},
        {"label": "重新分析", "action": "rerun_skill", "skill": "spending_analyst", "icon": "🔄"},
    ]

    return {"reply": reply, "reasoning": result.reasoning.strip() if result.success else "", "data": data, "cards": cards, "actions": actions, "report_id": report_id}
