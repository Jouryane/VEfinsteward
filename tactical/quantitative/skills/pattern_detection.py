"""
VE4 投资模式识别 Skill
=======================
从 VE3 TacticalInvestmentService 迁移的投资模式检测逻辑。

检测 8 种核心投资模式：
    - 定投 (Dollar Cost Averaging)
    - 指数化投资 (Index Tracking)
    - 股息投资 (Dividend Investing)
    - 成长投资 (Growth Investing)
    - 价值投资 (Value Investing)
    - 行业集中 (Sector Specific)
    - 动量交易 (Momentum Trading)
    - 平衡配置 (Balanced Allocation)

命名规范：
    - 类名: VE4PatternDetectionSkill
    - Skill 名: pattern_detection
"""

import math
import logging
from typing import List, Dict, Any

from tactical.quantitative.skills.skill_registry import VE4TacticalSkill
from tactical.shared.models.tactical_models import VE4SkillCategory, VE4SkillContext, VE4SkillResult

logger = logging.getLogger("ve4.tactical.skill.pattern_detection")


class VE4PatternDetectionSkill(VE4TacticalSkill):
    """投资模式识别 Skill（从 VE3 迁移）"""

    name = "pattern_detection"
    description = "识别用户的投资风格模式，如定投、价值投资、指数化投资等"
    category = VE4SkillCategory.ANALYSIS
    required_data = ["holdings"]
    version = "1.0"

    async def execute(self, context: VE4SkillContext) -> VE4SkillResult:
        """执行模式识别"""
        holdings = context.holdings or []
        transactions = context.transactions or []

        if not holdings:
            return self._make_result(success=False, error="无持仓数据，无法识别投资模式")

        patterns = []

        # 模式 1: 定投
        if self._is_dollar_cost_averaging(holdings, transactions):
            patterns.append({
                "type": "dollar_cost_averaging",
                "label": "定期定额投资",
                "confidence": 0.85,
                "description": "每月固定金额投资，忽略市场短期波动",
                "evidence": ["存在定期投资记录", "投资金额相对稳定", "跨多个月份"],
                "recommendations": ["继续坚持定投策略", "可考虑在低估时适度加码"],
            })

        # 模式 2: 指数化投资
        if self._is_index_tracking(holdings):
            patterns.append({
                "type": "index_tracking",
                "label": "指数化投资",
                "confidence": 0.80,
                "description": "追踪宽基指数，追求市场平均收益",
                "evidence": ["主要持有ETF/指数基金", "持仓分散度高"],
                "recommendations": ["指数化投资是有效的被动策略", "可加入债券指数平滑波动"],
            })

        # 模式 3: 股息投资
        if self._is_dividend_investing(holdings):
            patterns.append({
                "type": "dividend_investing",
                "label": "股息投资",
                "confidence": 0.75,
                "description": "偏好高分红股票，追求稳定现金流",
                "evidence": ["持有高股息率相关标的", "关注分红记录"],
                "recommendations": ["关注股息可持续性", "注意股价波动风险"],
            })

        # 模式 4: 成长投资
        if self._is_growth_investing(holdings):
            patterns.append({
                "type": "growth_investing",
                "label": "成长投资",
                "confidence": 0.70,
                "description": "偏好高增长行业和公司，追求资本增值",
                "evidence": ["持有科技/新能源等成长板块"],
                "recommendations": ["关注估值合理性", "保持适当的风险敞口"],
            })

        # 模式 5: 价值投资
        if self._is_value_investing(holdings):
            patterns.append({
                "type": "value_investing",
                "label": "价值投资",
                "confidence": 0.70,
                "description": "寻找被低估的优质公司，长期持有",
                "evidence": ["持仓偏向低估值蓝筹", "换手率较低"],
                "recommendations": ["坚持价值投资理念", "耐心等待价值回归"],
            })

        # 模式 6: 行业集中
        if self._is_sector_specific(holdings):
            patterns.append({
                "type": "sector_specific",
                "label": "行业集中",
                "confidence": 0.65,
                "description": "专注于特定行业或主题",
                "evidence": ["行业集中度高", "主题投资特征明显"],
                "recommendations": ["注意行业集中风险", "可适度分散配置"],
            })

        # 模式 7: 动量交易
        if self._is_momentum_trading(transactions):
            patterns.append({
                "type": "momentum_trading",
                "label": "动量交易",
                "confidence": 0.60,
                "description": "追涨杀跌，顺应市场趋势",
                "evidence": ["交易频率较高", "存在明显的买入卖出时机"],
                "recommendations": ["注意交易成本控制", "建议设置止损纪律"],
            })

        # 模式 8: 平衡配置
        if self._is_balanced_allocation(holdings):
            patterns.append({
                "type": "balanced",
                "label": "平衡配置",
                "confidence": 0.75,
                "description": "股债平衡或跨资产配置",
                "evidence": ["股债配置相对均衡", "多资产类别分布"],
                "recommendations": ["保持动态平衡", "定期再平衡操作"],
            })

        # 排序：按置信度降序
        patterns.sort(key=lambda p: p["confidence"], reverse=True)

        # 生成主模式
        primary_pattern = patterns[0] if patterns else None

        data = {
            "patterns": patterns,
            "primary_pattern": primary_pattern,
            "pattern_count": len(patterns),
            "has_explicit_strategy": len(patterns) > 0,
        }

        metrics = {
            "primary_confidence": primary_pattern["confidence"] if primary_pattern else 0.0,
            "pattern_diversity": len(patterns),
        }

        return self._make_result(success=True, data=data, metrics=metrics)

    # ════════════════════════════════════════════════════════════════
    # 模式检测逻辑（从 VE3 迁移）
    # ════════════════════════════════════════════════════════════════

    def _is_dollar_cost_averaging(self, holdings: List[Dict],
                                   transactions: List[Dict]) -> bool:
        """检测定投模式：定期定额，变异系数 CV < 0.5"""
        if len(transactions) < 3:
            # 无交易记录时，检查持仓名称是否含定投关键词
            names = " ".join(h.get("product_name", "").lower() for h in holdings)
            return "定投" in names

        # 按月份分组统计投资金额
        monthly = {}
        for t in transactions:
            date = t.get("date", "")[:7]  # YYYY-MM
            if date:
                monthly[date] = monthly.get(date, 0) + abs(t.get("amount", 0))

        if len(monthly) < 3:
            return False

        amounts = list(monthly.values())
        avg = sum(amounts) / len(amounts)
        if avg <= 0:
            return False

        variance = sum((a - avg) ** 2 for a in amounts) / len(amounts)
        std = math.sqrt(variance)
        cv = std / avg

        return cv < 0.5

    def _is_index_tracking(self, holdings: List[Dict]) -> bool:
        """检测指数化投资：ETF/指数基金占比 > 50%"""
        etf_keywords = ["etf", "指数", "沪深300", "中证500", "上证50", "创业板", "科创板", "纳指", "标普"]
        etf_count = 0
        for h in holdings:
            name = h.get("product_name", "").lower()
            if any(kw in name for kw in etf_keywords):
                etf_count += 1

        return etf_count > 0 and etf_count / len(holdings) > 0.5

    def _is_dividend_investing(self, holdings: List[Dict]) -> bool:
        """检测股息投资：持有高分红相关标的"""
        dividend_keywords = ["分红", "股息", "银行", "公用事业", "能源", "红利", "高股息"]
        return any(
            any(kw in h.get("product_name", "").lower() for kw in dividend_keywords)
            for h in holdings
        ) and len(holdings) >= 2

    def _is_growth_investing(self, holdings: List[Dict]) -> bool:
        """检测成长投资：科技/新能源/医药/消费占比 > 40%"""
        growth_keywords = ["科技", "半导体", "芯片", "新能源", "光伏", "医药", "医疗", "消费", "互联网", "AI", "人工智能"]
        growth_count = 0
        for h in holdings:
            name = h.get("product_name", "").lower()
            if any(kw in name for kw in growth_keywords):
                growth_count += 1
        return growth_count > 0 and growth_count / len(holdings) > 0.4

    def _is_value_investing(self, holdings: List[Dict]) -> bool:
        """检测价值投资：银行/保险/基建/能源/蓝筹占比 > 30%"""
        value_keywords = ["银行", "保险", "基建", "能源", "蓝筹", "地产", "建筑", "钢铁", "煤炭"]
        value_count = 0
        for h in holdings:
            name = h.get("product_name", "").lower()
            if any(kw in name for kw in value_keywords):
                value_count += 1
        return value_count > 0 and value_count / len(holdings) > 0.3

    def _is_sector_specific(self, holdings: List[Dict]) -> bool:
        """检测行业集中：单一行业集中度 > 60%"""
        from tactical.quantitative.agents.identifier_agent import VE4_IDENTIFIER_SECTOR_RULES
        sector_counts = {}
        for h in holdings:
            name = h.get("product_name", "").lower()
            matched = False
            for sector, keywords in VE4_IDENTIFIER_SECTOR_RULES.items():
                if any(kw in name for kw in keywords):
                    sector_counts[sector] = sector_counts.get(sector, 0) + 1
                    matched = True
                    break
            if not matched:
                sector_counts["其他"] = sector_counts.get("其他", 0) + 1

        if not sector_counts:
            return False

        max_sector_count = max(sector_counts.values())
        return max_sector_count / len(holdings) > 0.6 and len(holdings) >= 3

    def _is_momentum_trading(self, transactions: List[Dict]) -> bool:
        """检测动量交易：月均交易 > 2 笔"""
        if len(transactions) < 10:
            return False

        monthly = {}
        for t in transactions:
            date = t.get("date", "")[:7]
            if date:
                monthly[date] = monthly.get(date, 0) + 1

        if not monthly:
            return False

        avg_monthly = len(transactions) / len(monthly)
        return avg_monthly > 2.0

    def _is_balanced_allocation(self, holdings: List[Dict]) -> bool:
        """检测平衡配置：同时持有股票类和债券类产品"""
        has_equity = False
        has_bond = False
        for h in holdings:
            name = h.get("product_name", "").lower()
            asset_class = h.get("asset_class", "")
            if any(kw in name for kw in ["股票", "etf", "指数"]) or asset_class == "equity":
                has_equity = True
            if any(kw in name for kw in ["债券", "债基", "纯债", "短债"]) or asset_class == "fixed_income":
                has_bond = True
        return has_equity and has_bond
