"""
VE4 战术模块 Agent 基类
=======================
所有战术模块 Agent 的抽象基类，定义生命周期、状态机和事件通信。

命名规范：
    - 类名: VE4Tactical{Function}Agent
    - 函数名: ve4_tactical_{action}
"""

import uuid
import time
import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, Callable
from datetime import datetime

from tactical.shared.models.tactical_models import (
    VE4AgentStatus,
    VE4AgentTask,
    VE4AgentResult,
    VE4AgentEvent,
)

logger = logging.getLogger("ve4.tactical.agent")


# ════════════════════════════════════════════════════════════════
# Agent 基类
# ════════════════════════════════════════════════════════════════

class VE4TacticalAgent(ABC):
    """战术模块 Agent 抽象基类"""

    def __init__(self, agent_id: str = None, orchestrator=None):
        self.agent_id = agent_id or f"{self.__class__.__name__.lower()}_{uuid.uuid4().hex[:8]}"
        self.status = VE4AgentStatus.IDLE
        self.orchestrator = orchestrator
        self.context: Dict[str, Any] = {}
        self._event_handlers: Dict[str, list] = {}
        self._created_at = datetime.now().isoformat()

    # ── 抽象接口 ──

    @property
    @abstractmethod
    def agent_type(self) -> str:
        """Agent 类型标识（如 "identifier", "quant", "backtest"）"""
        pass

    @abstractmethod
    async def execute(self, task: VE4AgentTask) -> VE4AgentResult:
        """执行具体任务（子类必须实现）"""
        pass

    # ── 生命周期管理 ──

    def set_status(self, status: VE4AgentStatus):
        """设置状态并通知 Orchestrator"""
        old_status = self.status
        self.status = status
        logger.debug(f"[AGENT:{self.agent_id}] {old_status.value} → {status.value}")
        self.emit("status_change", {
            "old": old_status.value,
            "new": status.value,
        })

    def reset(self):
        """重置 Agent 状态（任务完成后调用）"""
        self.status = VE4AgentStatus.IDLE
        self.context.clear()
        logger.debug(f"[AGENT:{self.agent_id}] 已重置")

    # ── 事件通信 ──

    def emit(self, event_type: str, payload: dict):
        """向 Orchestrator 发送事件"""
        event = VE4AgentEvent(
            agent_id=self.agent_id,
            event_type=event_type,
            payload=payload,
        )
        # 调用本地监听者
        for handler in self._event_handlers.get(event_type, []):
            try:
                handler(event)
            except Exception as e:
                logger.warning(f"[AGENT:{self.agent_id}] 事件处理器异常: {e}")
        # 通知 Orchestrator
        if self.orchestrator:
            try:
                self.orchestrator.on_agent_event(event)
            except Exception as e:
                logger.warning(f"[AGENT:{self.agent_id}] Orchestrator 通知失败: {e}")

    def on(self, event_type: str, handler: Callable):
        """注册本地事件监听者"""
        if event_type not in self._event_handlers:
            self._event_handlers[event_type] = []
        self._event_handlers[event_type].append(handler)

    # ── 便捷方法 ──

    def emit_progress(self, message: str, percent: int = 0):
        """发送进度事件"""
        self.emit("progress", {"message": message, "percent": min(100, max(0, percent))})

    def emit_completed(self, result_summary: dict):
        """发送完成事件"""
        self.emit("completed", result_summary)

    def emit_error(self, error: str):
        """发送错误事件"""
        self.emit("error", {"error": error})
        self.set_status(VE4AgentStatus.ERROR)

    # ── 结果构造 ──

    def make_result(self, task: VE4AgentTask, success: bool, data: dict = None,
                    error: str = "", duration_ms: int = 0) -> VE4AgentResult:
        """便捷构造 AgentResult"""
        return VE4AgentResult(
            task_id=task.task_id,
            success=success,
            agent_id=self.agent_id,
            agent_type=self.agent_type,
            data=data or {},
            error=error,
            duration_ms=duration_ms,
        )

    def __repr__(self):
        return f"<{self.__class__.__name__} id={self.agent_id} status={self.status.value}>"
