"""
VE4 量化分析 Agent
===================
执行预设 Skill 进行量化分析，包括：
    - 模式识别 (pattern_detection)
    - 风险指标 (risk_metrics)
    - 分散度评分 (diversification_score)

数据来源：由 IdentifierAgent 识别的持仓数据，或直接从数据库读取。

命名规范：
    - 类名: VE4QuantitativeAnalysisAgent
    - 函数名: ve4_tactical_quant_{action}
"""

import logging
from typing import List, Dict, Any

from tactical.shared.base_agent import VE4TacticalAgent
from tactical.shared.models.tactical_models import (
    VE4AgentTask,
    VE4AgentResult,
    VE4AgentStatus,
    VE4SkillContext,
)
from tactical.quantitative.skills.skill_registry import VE4SkillRegistry
from tactical.quantitative.agents.identifier_agent import VE4InvestmentIdentifierAgent

logger = logging.getLogger("ve4.tactical.quant_agent")


class VE4QuantitativeAnalysisAgent(VE4TacticalAgent):
    """量化分析 Agent —— 调用预设 Skill 执行分析"""

    @property
    def agent_type(self) -> str:
        return "quant"

    async def execute(self, task: VE4AgentTask) -> VE4AgentResult:
        """
        执行量化分析任务。

        流程：
            1. 获取持仓数据（从 task.params 或现场读取）
            2. 根据 task.params["skills"] 列表调用对应 Skill
            3. 聚合所有 Skill 结果
        """
        start_time = __import__('time').time()
        self.set_status(VE4AgentStatus.EXECUTING)

        try:
            # Step 1: 获取持仓数据
            holdings = task.params.get("holdings", [])
            transactions = task.params.get("transactions", [])

            if not holdings:
                self.emit_progress("正在读取持仓数据...", 10)
                # 现场读取：复用 IdentifierAgent 的数据加载逻辑
                identifier = VE4InvestmentIdentifierAgent()
                raw_holdings = identifier._load_investment_holdings()
                if raw_holdings:
                    # 同时识别类型和行业
                    holdings = []
                    for h in raw_holdings:
                        ident = identifier._identify_holding(h)
                        h["type"] = ident.holding_type.value
                        h["sector"] = ident.sector
                        h["market"] = ident.market
                        holdings.append(h)
                self.emit_progress(f"已加载 {len(holdings)} 条持仓", 20)

            if not holdings:
                self.set_status(VE4AgentStatus.COMPLETED)
                return self.make_result(
                    task, success=True,
                    data={"patterns": [], "risk_metrics": {}, "diversification": {},
                         "recommendations": ["暂无投资持仓数据"]}
                )

            # Step 2: 确定要执行的 Skills
            skill_names = task.params.get("skills", [])
            if not skill_names:
                # 默认执行全部核心分析 Skill
                skill_names = ["pattern_detection", "risk_metrics", "diversification_score"]

            # Step 3: 逐个执行 Skill
            registry = VE4SkillRegistry()
            all_patterns = []
            risk_metrics = {}
            diversification = {}
            recommendations = []

            for i, skill_name in enumerate(skill_names):
                pct = 20 + int((i / len(skill_names)) * 70)
                self.emit_progress(f"正在执行: {skill_name}...", pct)

                context = VE4SkillContext(
                    holdings=holdings,
                    transactions=transactions,
                    params=task.params,
                )

                result = await registry.execute(skill_name, context)

                if not result.success:
                    logger.warning(f"[QUANT] Skill '{skill_name}' 执行失败: {result.error}")
                    recommendations.append(f"{skill_name} 分析失败: {result.error}")
                    continue

                # 聚合结果
                data = result.data
                if "patterns" in data:
                    all_patterns.extend(data.get("patterns", []))
                if "risk_metrics" in data:
                    risk_metrics = data.get("risk_metrics", {})
                if "diversification" in data:
                    diversification = data.get("diversification", {})
                if "summary" in data:
                    recommendations.append(data["summary"])
                if "recommendations" in data:
                    recommendations.extend(data.get("recommendations", []))

            # 去重排序 patterns
            seen_types = set()
            unique_patterns = []
            for p in all_patterns:
                if p["type"] not in seen_types:
                    seen_types.add(p["type"])
                    unique_patterns.append(p)
            unique_patterns.sort(key=lambda x: x["confidence"], reverse=True)

            # 生成战术建议
            if not recommendations:
                recommendations = self._generate_recommendations(
                    unique_patterns, risk_metrics, diversification
                )

            self.emit_progress("量化分析完成", 100)
            self.set_status(VE4AgentStatus.COMPLETED)

            data = {
                "patterns": unique_patterns,
                "risk_metrics": risk_metrics,
                "diversification": diversification,
                "recommendations": recommendations,
            }

            elapsed_ms = int((__import__('time').time() - start_time) * 1000)
            return self.make_result(task, success=True, data=data, duration_ms=elapsed_ms)

        except Exception as e:
            logger.error(f"[QUANT] 执行异常: {e}")
            self.emit_error(str(e))
            return self.make_result(task, success=False, error=str(e))

    def _generate_recommendations(self, patterns: List[Dict],
                                   risk_metrics: Dict,
                                   diversification: Dict) -> List[str]:
        """基于分析结果生成战术建议"""
        recs = []

        # 模式建议
        if patterns:
            primary = patterns[0]
            recs.append(f"主投资模式：{primary['label']}（置信度 {primary['confidence']*100:.0f}%）")
            recs.extend(primary.get("recommendations", [])[:2])

        # 风险建议
        sharpe = risk_metrics.get("sharpe_ratio", 0)
        if sharpe < 0.5:
            recs.append("当前夏普比率偏低，可考虑增加低风险资产配置")
        elif sharpe > 1.5:
            recs.append("风险调整后收益良好，当前策略有效")

        # 分散度建议
        div_score = diversification.get("score", 0)
        if div_score < 40:
            recs.append("组合分散度不足，建议增加不同行业/市场的配置")
        elif div_score > 80:
            recs.append("组合分散度优秀")

        return recs
