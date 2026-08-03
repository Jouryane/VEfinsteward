"""
VE4 分散度评分 Skill
=====================
评估投资组合的分散程度：
    - 行业分散度
    - 标的类型分散度
    - 市场分散度
    - 综合分散度评分 (0-100)

命名规范：
    - 类名: VE4DiversificationSkill
    - Skill 名: diversification_score
"""

import math
import logging
from typing import List, Dict, Any
from collections import Counter

from tactical.quantitative.skills.skill_registry import VE4TacticalSkill
from tactical.shared.models.tactical_models import VE4SkillCategory, VE4SkillContext, VE4SkillResult

logger = logging.getLogger("ve4.tactical.skill.diversification")


class VE4DiversificationSkill(VE4TacticalSkill):
    """分散度评分 Skill"""

    name = "diversification_score"
    description = "评估投资组合的行业、类型、市场分散程度，给出综合评分"
    category = VE4SkillCategory.ANALYSIS
    required_data = ["holdings"]
    version = "1.0"

    async def execute(self, context: VE4SkillContext) -> VE4SkillResult:
        """执行分散度评估"""
        holdings = context.holdings or []

        if not holdings:
            return self._make_result(success=False, error="无持仓数据")

        if len(holdings) < 2:
            return self._make_result(
                success=True,
                data={"diversification": {"score": 0, "level": "不足", "reason": "持仓数量太少"}},
            )

        # 使用持仓的 sector 和 type 信息（由 IdentifierAgent 提供）
        # 如果 context 中没有这些字段，从 product_name 推断
        sectors = []
        types = []
        markets = []
        values = []

        for h in holdings:
            name = h.get("product_name", "")
            value = h.get("current_value", 0)

            # 尝试从已识别数据读取
            sector = h.get("sector", "")
            holding_type = h.get("type", "")
            market = h.get("market", "")

            # 回退：从名称推断
            if not sector:
                sector = self._infer_sector(name)
            if not holding_type:
                holding_type = self._infer_type(name)
            if not market:
                market = self._infer_market(name)

            sectors.append(sector)
            types.append(holding_type)
            markets.append(market)
            values.append(value)

        # 计算各维度分散度
        sector_score = self._calc_concentration_score(sectors, values)
        type_score = self._calc_concentration_score(types, values)
        market_score = self._calc_concentration_score(markets, values)

        # 综合评分（加权平均）
        # 行业分散权重 40%，类型分散 35%，市场分散 25%
        composite = round(
            sector_score * 0.40 + type_score * 0.35 + market_score * 0.25,
            1
        )

        # 评级
        level, advice = self._rate_diversification(composite, len(holdings))

        data = {
            "diversification": {
                "score": composite,
                "level": level,
                "advice": advice,
                "breakdown": {
                    "sector_score": round(sector_score, 1),
                    "type_score": round(type_score, 1),
                    "market_score": round(market_score, 1),
                },
                "sector_distribution": dict(Counter(sectors)),
                "type_distribution": dict(Counter(types)),
                "market_distribution": dict(Counter(markets)),
                "holding_count": len(holdings),
            },
        }

        metrics = {"diversification_score": composite}

        return self._make_result(success=True, data=data, metrics=metrics)

    def _calc_concentration_score(self, categories: List[str],
                                   values: List[float]) -> float:
        """
        计算集中度得分（反向）。
        使用赫芬达尔指数（HHI）的变体：
            - 完全分散（每个类别占比相等）→ 100 分
            - 完全集中（单一类别）→ 0 分
        """
        if not categories or not values:
            return 0.0

        total = sum(values)
        if total <= 0:
            return 0.0

        # 按类别聚合价值
        cat_values = {}
        for cat, val in zip(categories, values):
            cat_values[cat] = cat_values.get(cat, 0) + val

        n = len(cat_values)
        if n <= 1:
            return 0.0

        # 计算 HHI
        hhi = sum((v / total) ** 2 for v in cat_values.values())

        # 转换为分数：HHI 范围 [1/n, 1]，映射到 [100, 0]
        # 理想分散时 HHI = 1/n，得 100 分
        # 完全集中时 HHI = 1，得 0 分
        min_hhi = 1.0 / n
        if hhi >= 1.0:
            return 0.0

        score = (1.0 - hhi) / (1.0 - min_hhi) * 100
        return max(0.0, min(100.0, score))

    def _infer_sector(self, name: str) -> str:
        """从名称推断行业"""
        from tactical.quantitative.agents.identifier_agent import VE4_IDENTIFIER_SECTOR_RULES
        name_lower = name.lower()
        for sector, keywords in VE4_IDENTIFIER_SECTOR_RULES.items():
            if any(kw in name_lower for kw in keywords):
                return sector
        return "其他"

    def _infer_type(self, name: str) -> str:
        """从名称推断类型"""
        name_lower = name.lower()
        if any(kw in name_lower for kw in ["etf", "指数", "沪深300", "中证500"]):
            return "etf"
        if any(kw in name_lower for kw in ["债券", "债基", "纯债", "短债"]):
            return "bond"
        if any(kw in name_lower for kw in ["基金", "混合", "偏股", "qdii"]):
            return "fund"
        if any(kw in name_lower for kw in ["股票", "个股", "a股"]):
            return "stock"
        return "other"

    def _infer_market(self, name: str) -> str:
        """从名称推断市场"""
        name_lower = name.lower()
        if any(kw in name_lower for kw in ["纳指", "标普", "道琼斯", "美股", "纳斯达克"]):
            return "美股"
        if any(kw in name_lower for kw in ["港股", "恒生", "h股"]):
            return "港股"
        return "A股"

    def _rate_diversification(self, score: float, count: int) -> tuple:
        """评级和建议"""
        if score >= 80:
            return "优秀", "组合分散度良好，风险暴露均衡"
        elif score >= 60:
            return "良好", "组合有一定分散度，可考虑进一步平衡"
        elif score >= 40:
            return "一般", "组合集中度偏高，建议增加不同行业/类型的配置"
        else:
            return "不足", "组合高度集中，风险敞口过大，强烈建议分散投资"
