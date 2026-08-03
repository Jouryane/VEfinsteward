"""
VE4 策略回测 Agent
===================
执行投资策略回测：
    - 接收策略代码或策略描述
    - 在 CodeSandbox 中安全执行
    - 返回回测结果和指标

命名规范：
    - 类名: VE4BacktestAgent
    - 函数名: ve4_tactical_backtest_{action}
"""

import logging
from typing import Dict, Any

from tactical.shared.base_agent import VE4TacticalAgent
from tactical.shared.models.tactical_models import VE4AgentTask, VE4AgentResult, VE4AgentStatus
from tactical.quantitative.skills.skill_registry import VE4SkillRegistry

logger = logging.getLogger("ve4.tactical.backtest_agent")


class VE4BacktestAgent(VE4TacticalAgent):
    """策略回测 Agent"""

    @property
    def agent_type(self) -> str:
        return "backtest"

    async def execute(self, task: VE4AgentTask) -> VE4AgentResult:
        """
        执行回测任务。

        支持两种模式：
            1. 自定义策略：提供 strategy_code（Python 代码）
            2. 预设策略：提供 strategy_name（调用预设 Skill）
        """
        self.set_status(VE4AgentStatus.EXECUTING)
        self.emit_progress("正在准备回测环境...", 10)

        try:
            params = task.params or {}
            strategy_code = params.get("strategy_code", "")
            strategy_description = params.get("strategy_description", "")
            strategy_name = params.get("strategy_name", "")

            if not any([strategy_code, strategy_description, strategy_name]):
                self.set_status(VE4AgentStatus.ERROR)
                return self.make_result(
                    task, success=False,
                    error="未提供策略代码、策略描述或策略名称"
                )

            self.emit_progress("正在执行回测...", 40)

            # 调用 simple_backtest Skill
            registry = VE4SkillRegistry()
            from tactical.shared.models.tactical_models import VE4SkillContext

            context = VE4SkillContext(params=params)
            skill_result = await registry.execute("simple_backtest", context)

            if not skill_result.success:
                self.set_status(VE4AgentStatus.ERROR)
                return self.make_result(
                    task, success=False,
                    error=skill_result.error,
                    data={"sandbox_output": skill_result.data.get("sandbox_output", "")}
                )

            self.emit_progress("回测完成，正在整理结果...", 90)

            data = skill_result.data.get("backtest", {})
            full_data = {
                "backtest": data,
                "sandbox_output": skill_result.data.get("sandbox_output", ""),
                "strategy_name": strategy_name or "自定义策略",
            }

            self.emit_progress("回测完成", 100)
            self.set_status(VE4AgentStatus.COMPLETED)
            return self.make_result(task, success=True, data=full_data)

        except Exception as e:
            logger.error(f"[BACKTEST] 执行异常: {e}")
            self.emit_error(str(e))
            return self.make_result(task, success=False, error=str(e))
