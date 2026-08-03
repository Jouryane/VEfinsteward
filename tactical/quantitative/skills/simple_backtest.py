"""
VE4 简单回测 Skill
===================
基于本地持仓数据和用户策略代码执行回测。

回测逻辑：
    - 接收策略代码（Python）
    - 在 CodeSandbox 中安全执行
    - 返回收益、波动、回撤等指标

命名规范：
    - 类名: VE4SimpleBacktestSkill
    - Skill 名: simple_backtest
"""

import logging
from typing import Dict, Any

from tactical.quantitative.skills.skill_registry import VE4TacticalSkill
from tactical.shared.models.tactical_models import VE4SkillCategory, VE4SkillContext, VE4SkillResult
from tactical.quantitative.tools.code_sandbox import VE4CodeSandbox

logger = logging.getLogger("ve4.tactical.skill.simple_backtest")


class VE4SimpleBacktestSkill(VE4TacticalSkill):
    """简单策略回测 Skill"""

    name = "simple_backtest"
    description = "执行用户策略代码的回测，计算收益、波动、回撤等指标"
    category = VE4SkillCategory.BACKTEST
    required_data = []
    version = "1.0"

    async def execute(self, context: VE4SkillContext) -> VE4SkillResult:
        """执行回测"""
        params = context.params or {}
        strategy_code = params.get("strategy_code", "")
        strategy_description = params.get("strategy_description", "")

        if not strategy_code and not strategy_description:
            return self._make_result(success=False, error="未提供策略代码或策略描述")

        # 如果没有代码但有描述，生成简单代码框架
        if not strategy_code and strategy_description:
            strategy_code = self._generate_stub_code(strategy_description)

        # 准备回测数据（简化版：使用持仓的收益率数据模拟）
        holdings = context.holdings or []
        backtest_data = self._prepare_backtest_data(holdings)

        # 在沙盒中执行策略代码
        sandbox = VE4CodeSandbox()
        sandbox_params = {
            "holdings": holdings,
            "backtest_data": backtest_data,
            "strategy_description": strategy_description,
        }

        sandbox_result = await sandbox.execute(strategy_code, sandbox_params, timeout=30)

        if not sandbox_result["success"]:
            return self._make_result(
                success=False,
                error=f"回测执行失败: {sandbox_result['error']}",
                data={"sandbox_output": sandbox_result.get("output", "")},
            )

        # 解析结果
        result_data = sandbox_result.get("result", {})
        backtest_metrics = self._extract_metrics(result_data, holdings)

        data = {
            "backtest": backtest_metrics,
            "sandbox_output": sandbox_result.get("output", "")[:1000],
        }

        return self._make_result(success=True, data=data)

    def _generate_stub_code(self, description: str) -> str:
        """根据描述生成策略代码框架"""
        return f'''# 策略: {description}
# 自动生成代码框架

def ve4_strategy_run(params):
    holdings = params.get("holdings", [])
    # TODO: 实现策略逻辑
    # 示例：计算简单平均收益率
    returns = [h.get("holding_return_pct", 0) for h in holdings if h.get("holding_return_pct", 0) != 0]
    avg_return = sum(returns) / len(returns) if returns else 0
    ve4_report_result("strategy_return", avg_return)
    ve4_report_result("holding_count", len(holdings))

ve4_strategy_run(PARAMS)
'''

    def _prepare_backtest_data(self, holdings: list) -> list:
        """准备回测数据（简化版）"""
        data = []
        for h in holdings:
            data.append({
                "name": h.get("product_name", ""),
                "value": h.get("current_value", 0),
                "cost": h.get("cost_basis", 0),
                "return_pct": h.get("holding_return_pct", 0),
                "annualized_pct": h.get("annualized_return_pct", 0),
            })
        return data

    def _extract_metrics(self, result: dict, holdings: list) -> dict:
        """从沙盒结果提取回测指标"""
        total_value = sum(h.get("current_value", 0) for h in holdings)
        total_cost = sum(h.get("cost_basis", 0) for h in holdings)
        total_return = ((total_value - total_cost) / total_cost * 100) if total_cost > 0 else 0

        return {
            "strategy_return": result.get("strategy_return", total_return),
            "holding_count": result.get("holding_count", len(holdings)),
            "total_value": round(total_value, 2),
            "period": "当前持仓",
            "note": "基于现有持仓数据的简化回测",
        }
