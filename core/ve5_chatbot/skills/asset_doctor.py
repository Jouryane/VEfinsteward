"""
VE管家 子skill — 资产诊断师 💰
================================
分析用户持仓结构、风险扫描、资产配置建议。
读取asset_holdings表，计算各类资产占比，
结合用户画像给出配置优化建议。
"""

import json
import sqlite3
import logging
from pathlib import Path
from typing import Dict, Any, List
from core.ai_gateway import ve4_ai_call
from app_paths import DATA_DIR, DB_PATH

logger = logging.getLogger("ve5.chatbot.asset_doctor")

_OUTPUT_DIR = DATA_DIR / "chatbot" / "skills"
_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
_DIAGNOSIS_FILE = _OUTPUT_DIR / "asset_diagnosis.json"


def _load_holdings() -> List[Dict]:
    """加载全部持仓"""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM asset_holdings ORDER BY current_value DESC").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"[ASSET_DOCTOR] 持仓加载失败：{e}")
        return []


def _load_profile() -> Dict:
    """加载用户画像"""
    profile_file = DATA_DIR / "allocation_profile.json"
    if not profile_file.exists():
        return {}
    try:
        return json.loads(profile_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def ve5_skill_asset_doctor(user_message: str, history: List[Dict]) -> Dict[str, Any]:
    """资产诊断skill主入口"""
    holdings = _load_holdings()
    profile = _load_profile()

    if not holdings:
        return {
            "reply": "📭 当前还没有持仓数据。你可以通过「数据同步」上传截图自动识别，或者手动在账户总览中添加持仓。",
            "data": {},
            "cards": [],
            "actions": [{"label": "去添加持仓", "action": "navigate", "url": "account-overview.html", "icon": "➕"}],
        }

    # 计算分类统计（使用四级分类，alternative 归入 protection）
    total = sum(h.get("current_value", 0) for h in holdings if h.get("current_value", 0) > 0)
    categories = {"aggressive": 0, "stable": 0, "liquid": 0, "protection": 0}
    for h in holdings:
        ac = h.get("asset_class", "")
        val = h.get("current_value", 0)
        if val <= 0:
            continue
        if ac in categories:
            categories[ac] += val
        elif ac in ("equity", "fixed_income", "cash"):
            mapping = {"equity": "aggressive", "fixed_income": "stable", "cash": "liquid"}
            categories[mapping.get(ac, "protection")] += val
        elif ac in ("alternative", "commodity"):
            categories["protection"] += val
        else:
            categories["protection"] += val

    # ── 注入导航上下文（推荐配比 + 评分 + 建议等权威数据）──
    from core.ve5_chatbot.context_navigator import build_navigation_context
    nav_ctx = build_navigation_context("asset_doctor", user_message)

    # 构建上下文
    context = f"""用户持仓概况：
总资产：¥{total:,.0f}
持仓数量：{len(holdings)} 条

分类占比：
"""
    for cat, val in categories.items():
        pct = (val / total * 100) if total > 0 else 0
        context += f"- {cat}: ¥{val:,.0f} ({pct:.1f}%)\n"

    context += "\n前10大持仓：\n"
    for h in holdings[:10]:
        if h.get("current_value", 0) <= 0:
            continue
        context += f"- {h.get('product_name','未知')}: ¥{h.get('current_value',0):,.0f}（{h.get('asset_class','')}）\n"

    if profile:
        context += f"\n用户画像：{json.dumps(profile, ensure_ascii=False)[:300]}\n"

    context += f"\n{nav_ctx}\n"

    system = """你是VE管家的资产诊断师。根据用户的持仓数据，进行专业的资产诊断分析。

诊断维度：
1. 资产配置结构 — 对比"推荐配比"与"实际配比"的偏差，引用系统计算结果
2. 集中度风险 — 是否过度集中于某几只产品
3. 流动性评估 — 流动资产是否充足
4. 与画像匹配度 — 实际配置与用户风险偏好的偏差
5. 优化建议 — 具体的调仓建议

重要规则：
- "推荐配比"和"维度评分"由系统引擎计算，是权威结果，请引用而非自行推算
- 风险分析应基于实际配比与推荐配比的偏差，不要自行提出不同的配比建议
- 不要对系统的进取/稳健/流动/保障配比提出异议，这些是用户画像和矩阵计算的结果

输出要求：
- 给出温暖但专业的诊断报告
- 包含关键数据点和百分比
- 建议要具体、可执行
- 风险提示要明确但不过分 alarmist
- 在末尾给出结构化的JSON摘要"""

    prompt = f"""{context}

用户需求：{user_message}

请进行资产诊断。"""

    result = ve4_ai_call(
        task_type="asset_diagnosis",
        system=system,
        prompt=prompt,
        format_type="text",
        complexity="high",
        max_tokens=2048,
        contains_privacy_data=True,
    )

    reply = result.text if result.success else "抱歉，资产诊断失败。"

    data = {"total": total, "categories": categories, "holding_count": len(holdings)}
    try:
        if "```json" in reply:
            json_str = reply.split("```json")[1].split("```")[0].strip()
            data.update(json.loads(json_str))
    except Exception:
        pass

    # 如果 gateway 标记截断，记录日志（text 格式下一般已从 reasoning 提取）
    if result.is_truncated:
        import logging
        logging.getLogger("ve5.chatbot.asset_doctor").info(
            f"[ASSET_DOCTOR] gateway标记截断，reply可能不完整 (长度={len(reply)})")

    _DIAGNOSIS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    cards = []
    if total > 0:
        cards.append({"type": "asset_pie", "title": "资产配置结构", "data": categories})

    actions = [
        {"label": "查看账户总览", "action": "navigate", "url": "account-overview.html", "icon": "📊"},
        {"label": "资产配置详情", "action": "navigate", "url": "asset-allocation.html", "icon": "📈"},
        {"label": "重新诊断", "action": "rerun_skill", "skill": "asset_doctor", "icon": "🔄"},
    ]

    return {"reply": reply, "data": data, "cards": cards, "actions": actions}
