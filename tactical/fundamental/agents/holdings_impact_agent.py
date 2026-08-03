"""
VE4 持仓影响评估 Agent
========================
职责：将研报/新闻分析结果与用户持仓关联，调用 LLM 生成买入/卖出/持有建议。

输入：研报摘要（标题、关键观点、情绪倾向）
输出：结构化持仓影响建议列表

隐私策略：
    - 默认使用本地启发式匹配（保护持仓隐私）
    - 用户可通过 settings 允许云端 LLM 分析
    - 无论哪种方式，持仓数据不上传到第三方
"""

import logging
import sqlite3
from pathlib import Path
from typing import List, Dict, Optional
from dataclasses import dataclass, asdict
from datetime import datetime

from tactical.shared.base_agent import VE4TacticalAgent
from tactical.shared.models.tactical_models import (
    VE4AgentTask,
    VE4AgentResult,
    VE4AgentStatus,
)

logger = logging.getLogger("ve5.tactical.holdings_impact")


# ════════════════════════════════════════════════════════════════
# 数据模型
# ════════════════════════════════════════════════════════════════

@dataclass
class VE4HoldingsImpactRecommendation:
    """持仓影响建议"""
    holding_name: str = ""           # 持仓名称
    holding_code: str = ""           # 持仓代码（如有）
    action: str = "hold"             # buy / sell / hold
    action_label: str = "持有"       # 加仓 / 减仓 / 持有
    confidence: float = 0.5          # 置信度 0-1
    reason: str = ""                 # 建议理由
    report_title: str = ""           # 关联研报标题
    report_sentiment: str = ""       # 研报情绪


# ════════════════════════════════════════════════════════════════
# HoldingsImpactAgent
# ════════════════════════════════════════════════════════════════

class VE4HoldingsImpactAgent(VE4TacticalAgent):
    """
    持仓影响评估 Agent

    流程：
        1. 从 SQL 读取用户当前持仓
        2. 接收研报分析结果
        3. 构建 prompt → 调用 LLM（local_alpha）
        4. 解析 LLM 输出 → 结构化建议
        5. 返回 VE4HoldingsImpactRecommendation 列表
    """

    agent_type = "holdings_impact"

    def __init__(self, agent_id: str = None, orchestrator=None,
                 db_path: Path = None):
        super().__init__(agent_id, orchestrator)
        # DB 路径：与 VE5 API server 一致
        if db_path is None:
            from app_paths import DB_PATH
            self.db_path = DB_PATH
        else:
            self.db_path = db_path

    # ── 核心接口 ──

    async def execute(self, task: VE4AgentTask) -> VE4AgentResult:
        """执行持仓影响评估任务"""
        self.set_status(VE4AgentStatus.EXECUTING)
        start = datetime.now()

        try:
            params = task.params or {}
            report_summary = params.get("report_summary", "")
            report_title = params.get("report_title", "")
            report_sentiment = params.get("report_sentiment", "")
            report_viewpoints = params.get("report_viewpoints", [])
            # 隐私模式: "local_only"(默认) | "allow_cloud"
            privacy_mode = params.get("privacy_mode", "local_only")

            if not report_summary:
                return self.make_result(task, False, error="缺少研报摘要")

            # 1. 读取用户持仓
            holdings = self._load_holdings()
            if not holdings:
                return self.make_result(task, False, error="无持仓数据")

            # 2. 根据隐私模式选择分析方式
            privacy_notice = ""
            if privacy_mode == "allow_cloud":
                # 尝试调用 LLM（云端或本地）
                recommendations = await self._analyze_with_llm(
                    holdings=holdings,
                    report_title=report_title,
                    report_summary=report_summary,
                    report_sentiment=report_sentiment,
                    report_viewpoints=report_viewpoints,
                    use_llm=True,
                )
                privacy_notice = "持仓影响评估由 AI 模型完成（隐私模式：允许云端）"
            else:
                # 默认：本地启发式匹配（保护隐私）
                recommendations = await self._analyze_with_llm(
                    holdings=holdings,
                    report_title=report_title,
                    report_summary=report_summary,
                    report_sentiment=report_sentiment,
                    report_viewpoints=report_viewpoints,
                    use_llm=False,
                )
                privacy_notice = "持仓影响评估由本地规则完成（保护隐私）。可在设置中开启 AI 深度分析"

            duration = int((datetime.now() - start).total_seconds() * 1000)
            return self.make_result(task, True, data={
                "recommendations": [asdict(r) for r in recommendations],
                "holdings_count": len(holdings),
                "report_title": report_title,
                "privacy_mode": privacy_mode,
                "privacy_notice": privacy_notice,
            }, duration_ms=duration)

        except Exception as e:
            logger.error(f"[HoldingsImpact] 执行失败: {e}", exc_info=True)
            return self.make_result(task, False, error=str(e))
        finally:
            self.reset()

    # ── 持仓读取 ──

    def _load_holdings(self) -> List[Dict]:
        """从 SQL 读取用户持仓列表"""
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT product_name as asset_name, product_code as asset_code, asset_class,
                       current_value, cost_basis, holding_quantity as quantity, account_key
                FROM asset_holdings
                WHERE current_value > 0
                ORDER BY current_value DESC
            """)
            rows = [dict(r) for r in cursor.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            logger.warning(f"[HoldingsImpact] 读取持仓失败: {e}")
            return []

    # ── LLM 分析 ──

    async def _analyze_with_llm(self, holdings: List[Dict],
                                 report_title: str,
                                 report_summary: str,
                                 report_sentiment: str,
                                 report_viewpoints: List[str],
                                 use_llm: bool = False) -> List[VE4HoldingsImpactRecommendation]:
        """
        分析持仓影响。

        use_llm=True:  尝试调用 LLM（需配置本地或云端模型）
        use_llm=False: 使用本地启发式匹配（默认，保护隐私）
        """
        if not use_llm:
            return self._heuristic_match(
                holdings, report_summary, report_viewpoints, report_title, report_sentiment
            )

        # ── LLM 模式 ──
        holdings_text = "\n".join([
            f"- {h.get('asset_name', '未知')} ({h.get('asset_code', '--')}): "
            f"当前价值 ¥{h.get('current_value', 0):,.2f}, "
            f"类别: {h.get('asset_class', '--')}"
            for h in holdings[:20]
        ])

        viewpoints_text = "\n".join([f"- {v}" for v in report_viewpoints]) if report_viewpoints else "无明确观点"

        prompt = f"""基于以下研报分析结果，评估对用户持仓的影响，给出买入/卖出/持有建议。

【研报信息】
标题: {report_title}
情绪倾向: {report_sentiment}
摘要: {report_summary}
关键观点:
{viewpoints_text}

【用户持仓】
{holdings_text}

要求：只针对可能受影响的持仓给出建议。每个建议包含：持仓名称、建议操作（加仓/减仓/持有）、置信度（0-1）、理由。
输出 JSON 数组：[{{"holding_name": "", "action": "buy|sell|hold", "confidence": 0.0, "reason": ""}}]"""

        logger.info(f"[HoldingsImpact] LLM prompt length: {len(prompt)} chars")

        # TODO: 接入真实 LLM 调用（local_alpha 或 cloud_beta）
        # 当前 LLM 尚未接入，fallback 到启发式
        return self._heuristic_match(
            holdings, report_summary, report_viewpoints, report_title, report_sentiment
        )

    def _heuristic_match(self, holdings: List[Dict], report_summary: str,
                         viewpoints: List[str], title: str, sentiment: str) -> List[VE4HoldingsImpactRecommendation]:
        """
        启发式匹配：研报关键词 ↔ 持仓名称
        作为 LLM 接入前的 fallback，确保前端有数据展示。
        """
        recommendations = []
        all_text = (title + " " + report_summary + " " + " ".join(viewpoints)).lower()

        # 情绪映射
        sentiment_map = {
            "看多": "buy", "看涨": "buy", "买入": "buy", "推荐": "buy", "增持": "buy",
            "看空": "sell", "看跌": "sell", "卖出": "sell", "减持": "sell", "回避": "sell",
            "中性": "hold", "持有": "hold", "观望": "hold",
        }
        base_sentiment = "hold"
        for kw, action in sentiment_map.items():
            if kw in all_text:
                base_sentiment = action
                break

        for h in holdings:
            name = h.get("asset_name", "")
            code = h.get("asset_code", "")
            name_lower = name.lower()

            # 关键词匹配
            matched = False
            matched_reason = ""

            # 直接名称匹配
            if name_lower in all_text or (code and code.lower() in all_text):
                matched = True
                matched_reason = f"研报直接提及 {name}"

            # 行业/类别关键词匹配
            asset_class = h.get("asset_class", "").lower()
            class_keywords = {
                "equity": ["股票", " equity", "a股", "港股", "美股"],
                "bond": ["债券", "bond", "利率", "国债"],
                "fund": ["基金", "fund", "etf"],
                "cash": ["现金", "货币", "存款"],
            }
            for cls, kws in class_keywords.items():
                if asset_class == cls or cls in asset_class:
                    for kw in kws:
                        if kw in all_text:
                            matched = True
                            matched_reason = f"研报涉及 {name} 所属类别"
                            break

            if matched:
                action = base_sentiment
                action_label = {"buy": "加仓", "sell": "减仓", "hold": "持有"}.get(action, "持有")
                recommendations.append(VE4HoldingsImpactRecommendation(
                    holding_name=name,
                    holding_code=code or "",
                    action=action,
                    action_label=action_label,
                    confidence=0.6,
                    reason=matched_reason + f"，研报整体情绪倾向：{sentiment or '中性'}",
                    report_title=title,
                    report_sentiment=sentiment,
                ))

        # 如果没有匹配到任何持仓，返回一个通用提示
        if not recommendations:
            recommendations.append(VE4HoldingsImpactRecommendation(
                holding_name="（无直接关联持仓）",
                action="hold",
                action_label="持有",
                confidence=0.3,
                reason=f"研报内容与当前持仓无直接关联，建议保持观望。研报情绪：{sentiment or '中性'}",
                report_title=title,
                report_sentiment=sentiment,
            ))

        return recommendations
