"""
VE4 风险指标计算 Skill
=======================
计算投资组合的核心风险指标：
    - 波动率 (Volatility)
    - 最大回撤 (Max Drawdown)
    - 夏普比率 (Sharpe Ratio)
    - 收益率相关指标

命名规范：
    - 类名: VE4RiskMetricsSkill
    - Skill 名: risk_metrics
"""

import math
import logging
from typing import List, Dict, Any

from tactical.quantitative.skills.skill_registry import VE4TacticalSkill
from tactical.shared.models.tactical_models import VE4SkillCategory, VE4SkillContext, VE4SkillResult

logger = logging.getLogger("ve4.tactical.skill.risk_metrics")


class VE4RiskMetricsSkill(VE4TacticalSkill):
    """风险指标计算 Skill"""

    name = "risk_metrics"
    description = "计算投资组合的波动率、最大回撤、夏普比率等核心风险指标"
    category = VE4SkillCategory.ANALYSIS
    required_data = ["holdings"]
    version = "1.0"

    async def execute(self, context: VE4SkillContext) -> VE4SkillResult:
        """执行风险指标计算"""
        holdings = context.holdings or []

        if not holdings:
            return self._make_result(success=False, error="无持仓数据")

        # 从持仓数据计算指标
        total_value = sum(h.get("current_value", 0) for h in holdings)
        total_cost = sum(h.get("cost_basis", 0) for h in holdings)

        # 收益率列表
        returns = []
        for h in holdings:
            ret = h.get("holding_return_pct", 0)
            if ret != 0:
                returns.append(ret)

        # 年化收益率列表
        annualized = []
        for h in holdings:
            ar = h.get("annualized_return_pct", 0)
            if ar != 0:
                annualized.append(ar)

        # 计算指标
        total_return_pct = ((total_value - total_cost) / total_cost * 100) if total_cost > 0 else 0
        volatility = self._calc_volatility(returns)
        max_drawdown = self._estimate_max_drawdown(returns)
        sharpe_ratio = self._calc_sharpe(annualized, 2.5)  # 无风险利率 2.5%
        avg_annualized = sum(annualized) / len(annualized) if annualized else 0

        data = {
            "risk_metrics": {
                "total_value": round(total_value, 2),
                "total_cost": round(total_cost, 2),
                "total_return_pct": round(total_return_pct, 2),
                "avg_annualized_return": round(avg_annualized, 2),
                "volatility": round(volatility, 2),
                "max_drawdown": round(max_drawdown, 2),
                "sharpe_ratio": round(sharpe_ratio, 2),
                "holding_count": len(holdings),
            },
            "summary": self._generate_summary(total_return_pct, volatility, max_drawdown, sharpe_ratio),
        }

        metrics = {
            "sharpe_ratio": sharpe_ratio,
            "volatility": volatility,
            "max_drawdown": max_drawdown,
        }

        return self._make_result(success=True, data=data, metrics=metrics)

    def _calc_volatility(self, returns: List[float]) -> float:
        """计算收益率波动率（标准差）"""
        if len(returns) < 2:
            return 0.0
        avg = sum(returns) / len(returns)
        variance = sum((r - avg) ** 2 for r in returns) / len(returns)
        return math.sqrt(variance)

    def _estimate_max_drawdown(self, returns: List[float]) -> float:
        """估算最大回撤（基于收益率列表的简化估算）"""
        if not returns:
            return 0.0
        # 简化：取最小收益率作为回撤估算
        min_return = min(returns)
        return abs(min_return)

    def _calc_sharpe(self, annualized_returns: List[float], risk_free_rate: float = 2.5) -> float:
        """计算夏普比率"""
        if not annualized_returns:
            return 0.0
        avg = sum(annualized_returns) / len(annualized_returns)
        variance = sum((r - avg) ** 2 for r in annualized_returns) / len(annualized_returns)
        std = math.sqrt(variance)
        if std == 0:
            return 0.0
        return (avg - risk_free_rate) / std

    def _generate_summary(self, total_return: float, volatility: float,
                          max_dd: float, sharpe: float) -> str:
        """生成风险指标摘要"""
        parts = []
        if sharpe > 1.5:
            parts.append("夏普比率优秀，风险调整后收益良好")
        elif sharpe > 0.5:
            parts.append("夏普比率尚可")
        else:
            parts.append("夏普比率偏低，需关注风险收益比")

        if volatility > 30:
            parts.append("组合波动率较高")
        elif volatility > 15:
            parts.append("组合波动率中等")
        else:
            parts.append("组合波动率较低")

        if max_dd > 20:
            parts.append(f"最大回撤达 {max_dd:.1f}%，风险敞口较大")

        return "；".join(parts)
