"""
VE4 战术编排器 (TacticalOrchestrator)
======================================
战术模块的中央调度器，负责：
    1. 接收用户高阶目标，分解为子任务
    2. 选择合适的 Agent 执行每个子任务
    3. 管理任务依赖关系（DAG 执行）
    4. 聚合多 Agent 结果，生成 TacticalReport
    5. 事件总线：Agent 间通信

命名规范：
    - 类名: VE4TacticalOrchestrator
    - 函数名: ve4_tactical_{action}
"""

import uuid
import time
import asyncio
import logging
from typing import Dict, List, Any, Optional, Callable
from datetime import datetime

from tactical.shared.models.tactical_models import (
    VE4AgentStatus,
    VE4AgentTask,
    VE4AgentTaskType,
    VE4AgentResult,
    VE4AgentEvent,
    VE4TacticalReport,
)
from tactical.shared.base_agent import VE4TacticalAgent

logger = logging.getLogger("ve4.tactical.orchestrator")


# ════════════════════════════════════════════════════════════════
# 编排器
# ════════════════════════════════════════════════════════════════

class VE4TacticalOrchestrator:
    """
    战术模块中央编排器。

    使用方式：
        orchestrator = VE4TacticalOrchestrator()
        orchestrator.register_agent("identifier", MyIdentifierAgent)
        report = await orchestrator.execute_goal("分析我的持仓风险")
    """

    def __init__(self):
        self._agents: Dict[str, VE4TacticalAgent] = {}
        self._agent_classes: Dict[str, type] = {}
        self._task_registry: Dict[str, VE4AgentTask] = {}
        self._result_registry: Dict[str, VE4AgentResult] = {}
        self._event_listeners: List[Callable] = []
        self._status = "idle"
        self._current_goal = ""
        self._report_id = ""

    # ── Agent 注册 ──

    def register_agent(self, agent_type: str, agent_class: type):
        """
        注册 Agent 类型。

        Args:
            agent_type: Agent 类型标识（如 "identifier", "quant"）
            agent_class: VE4TacticalAgent 的子类
        """
        if not issubclass(agent_class, VE4TacticalAgent):
            raise ValueError(f"Agent 类必须继承 VE4TacticalAgent: {agent_class}")
        self._agent_classes[agent_type] = agent_class
        logger.info(f"[ORCHESTRATOR] 注册 Agent 类型: {agent_type} -> {agent_class.__name__}")

    def get_agent(self, agent_type: str) -> Optional[VE4TacticalAgent]:
        """获取或创建 Agent 实例（每个类型复用同一实例）"""
        if agent_type in self._agents:
            # 重置状态后复用
            self._agents[agent_type].reset()
            return self._agents[agent_type]

        agent_class = self._agent_classes.get(agent_type)
        if not agent_class:
            logger.error(f"[ORCHESTRATOR] 未注册的 Agent 类型: {agent_type}")
            return None

        agent = agent_class(orchestrator=self)
        self._agents[agent_type] = agent
        return agent

    # ── 事件监听 ──

    def add_event_listener(self, handler: Callable):
        """添加全局事件监听者（用于前端 SSE 推送等）"""
        self._event_listeners.append(handler)

    def on_agent_event(self, event: VE4AgentEvent):
        """接收 Agent 事件并分发给监听者"""
        logger.debug(f"[ORCHESTRATOR] 事件来自 {event.agent_id}: {event.event_type}")
        for handler in self._event_listeners:
            try:
                handler(event)
            except Exception as e:
                logger.warning(f"[ORCHESTRATOR] 事件监听者异常: {e}")

    # ── 核心：执行目标 ──

    async def execute_goal(self, goal: str, params: dict = None) -> VE4TacticalReport:
        """
        执行用户高阶目标。

        Args:
            goal: 用户目标描述（如 "分析我的持仓风险"）
            params: 额外参数

        Returns:
            VE4TacticalReport 聚合报告
        """
        start_time = time.time()
        self._status = "planning"
        self._current_goal = goal
        self._report_id = f"report_{uuid.uuid4().hex[:12]}"
        self._task_registry.clear()
        self._result_registry.clear()

        logger.info(f"[ORCHESTRATOR] 开始执行目标: {goal}")

        # Step 1: 目标分解
        tasks = self._decompose_goal(goal, params or {})
        for t in tasks:
            self._task_registry[t.task_id] = t

        # Step 2: 构建 DAG 并执行
        self._status = "executing"
        await self._execute_dag(tasks)

        # Step 3: 聚合结果
        self._status = "aggregating"
        report = self._aggregate_results(goal, tasks)

        elapsed = int((time.time() - start_time) * 1000)
        logger.info(f"[ORCHESTRATOR] 目标完成 [{elapsed}ms]: {goal}")
        self._status = "completed"

        return report

    # ── 目标分解 ──

    def _decompose_goal(self, goal: str, params: dict) -> List[VE4AgentTask]:
        """
        将用户目标分解为子任务序列。

        当前使用基于关键词的规则分解，未来可接入 LLM 进行智能分解。
        """
        tasks = []
        goal_lower = goal.lower()

        # 模式 1: 分析持仓战术
        if any(k in goal_lower for k in ["持仓", "风险", "分析", "战术", "overview"]):
            t1 = VE4AgentTask(
                task_id=f"task_{uuid.uuid4().hex[:8]}",
                task_type=VE4AgentTaskType.IDENTIFY_HOLDINGS,
                goal="识别用户持仓中的投资标的",
                params=params,
            )
            t2 = VE4AgentTask(
                task_id=f"task_{uuid.uuid4().hex[:8]}",
                task_type=VE4AgentTaskType.QUANT_ANALYSIS,
                goal="计算风险指标、检测投资模式、评估分散度",
                params={**params, "skills": ["pattern_detection", "risk_metrics", "diversification_score"]},
                depends_on=[t1.task_id],
            )
            t3 = VE4AgentTask(
                task_id=f"task_{uuid.uuid4().hex[:8]}",
                task_type=VE4AgentTaskType.QUANT_ANALYSIS,
                goal="生成再平衡建议",
                params={**params, "skills": ["rebalance_suggestion"]},
                depends_on=[t2.task_id],
            )
            tasks = [t1, t2, t3]

        # 模式 2: 回测
        elif any(k in goal_lower for k in ["回测", "backtest", "策略"]):
            t1 = VE4AgentTask(
                task_id=f"task_{uuid.uuid4().hex[:8]}",
                task_type=VE4AgentTaskType.GENERATE_CODE,
                goal="生成策略代码",
                params=params,
            )
            t2 = VE4AgentTask(
                task_id=f"task_{uuid.uuid4().hex[:8]}",
                task_type=VE4AgentTaskType.BACKTEST,
                goal="执行策略回测",
                params=params,
                depends_on=[t1.task_id] if params.get("strategy_description") else [],
            )
            tasks = [t1, t2]

        # 模式 3: 研报分析
        elif any(k in goal_lower for k in ["研报", "报告", "report", "新闻", "news"]):
            t1 = VE4AgentTask(
                task_id=f"task_{uuid.uuid4().hex[:8]}",
                task_type=VE4AgentTaskType.ANALYZE_REPORT,
                goal="解析研报/新闻内容",
                params=params,
            )
            tasks = [t1]

        # 默认：通用分析
        else:
            t1 = VE4AgentTask(
                task_id=f"task_{uuid.uuid4().hex[:8]}",
                task_type=VE4AgentTaskType.IDENTIFY_HOLDINGS,
                goal="识别投资标的",
                params=params,
            )
            t2 = VE4AgentTask(
                task_id=f"task_{uuid.uuid4().hex[:8]}",
                task_type=VE4AgentTaskType.QUANT_ANALYSIS,
                goal="执行量化分析",
                params=params,
                depends_on=[t1.task_id],
            )
            tasks = [t1, t2]

        logger.info(f"[ORCHESTRATOR] 目标分解为 {len(tasks)} 个子任务")
        return tasks

    # ── DAG 执行 ──

    async def _execute_dag(self, tasks: List[VE4AgentTask]):
        """按依赖关系执行 DAG"""
        completed_tasks = set()
        pending_tasks = {t.task_id: t for t in tasks}

        while pending_tasks:
            # 找出当前可执行的任务（无依赖或依赖已完成）
            ready = [
                t for t in pending_tasks.values()
                if all(dep in completed_tasks for dep in t.depends_on)
            ]

            if not ready:
                # 死锁检测
                remaining = list(pending_tasks.keys())
                logger.error(f"[ORCHESTRATOR] 任务死锁，剩余: {remaining}")
                break

            # 并行执行就绪任务
            coros = [self._execute_single_task(t) for t in ready]
            results = await asyncio.gather(*coros, return_exceptions=True)

            for task, result in zip(ready, results):
                if isinstance(result, Exception):
                    self._result_registry[task.task_id] = VE4AgentResult(
                        task_id=task.task_id,
                        success=False,
                        agent_id="orchestrator",
                        agent_type="orchestrator",
                        error=str(result),
                    )
                    logger.error(f"[ORCHESTRATOR] 任务异常: {task.task_id} - {result}")
                else:
                    self._result_registry[task.task_id] = result
                completed_tasks.add(task.task_id)
                del pending_tasks[task.task_id]

    async def _execute_single_task(self, task: VE4AgentTask) -> VE4AgentResult:
        """执行单个任务"""
        # 映射 task_type -> agent_type
        agent_type_map = {
            VE4AgentTaskType.IDENTIFY_HOLDINGS: "identifier",
            VE4AgentTaskType.QUANT_ANALYSIS: "quant",
            VE4AgentTaskType.BACKTEST: "backtest",
            VE4AgentTaskType.ANALYZE_REPORT: "report",
            VE4AgentTaskType.GENERATE_CODE: "code",
            VE4AgentTaskType.COLLECT_DATA: "collector",
        }

        agent_type = agent_type_map.get(task.task_type)
        if not agent_type:
            return VE4AgentResult(
                task_id=task.task_id,
                success=False,
                agent_id="orchestrator",
                agent_type="orchestrator",
                error=f"未知任务类型: {task.task_type.value}",
            )

        agent = self.get_agent(agent_type)
        if not agent:
            return VE4AgentResult(
                task_id=task.task_id,
                success=False,
                agent_id="orchestrator",
                agent_type=agent_type,
                error=f"Agent 类型未注册: {agent_type}",
            )

        logger.info(f"[ORCHESTRATOR] 调度任务 {task.task_id} -> {agent_type} Agent")
        return await agent.execute(task)

    # ── 结果聚合 ──

    def _aggregate_results(self, goal: str, tasks: List[VE4AgentTask]) -> VE4TacticalReport:
        """聚合所有子任务结果为 TacticalReport"""
        report = VE4TacticalReport(
            report_id=self._report_id,
            goal=goal,
            status="completed",
        )

        for task in tasks:
            result = self._result_registry.get(task.task_id)
            if not result or not result.success:
                report.status = "partial"
                continue

            data = result.data
            agent_type = result.agent_type

            # 根据 Agent 类型聚合数据
            if agent_type == "identifier":
                report.holdings_summary = data.get("holdings_summary", {})
            elif agent_type == "quant":
                if "patterns" in data:
                    report.patterns.extend(data.get("patterns", []))
                if "risk_metrics" in data:
                    report.risk_metrics.update(data.get("risk_metrics", {}))
                if "diversification" in data:
                    report.diversification = data.get("diversification", {})
                if "recommendations" in data:
                    report.recommendations.extend(data.get("recommendations", []))
            elif agent_type == "backtest":
                report.backtest_results.append(data)
            elif agent_type == "report":
                report.report_summaries.append(data)

        return report

    # ── 状态查询 ──

    def get_status(self) -> dict:
        """获取当前执行状态"""
        return {
            "status": self._status,
            "goal": self._current_goal,
            "report_id": self._report_id,
            "agents": {k: v.status.value for k, v in self._agents.items()},
            "tasks": {
                tid: {"type": t.task_type.value, "depends_on": t.depends_on}
                for tid, t in self._task_registry.items()
            },
        }
