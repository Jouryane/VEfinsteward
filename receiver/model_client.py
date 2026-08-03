"""
VE4 本地模型客户端（已接入 AI 配置中心）
=========================================
封装对本地 Ollama 模型的轻量调用。

**重要变更：** 自 2026-07-01 起，所有模型调用统一走 `core.ai_gateway` 网关，
本文件仅保留兼容接口。新代码请直接使用 `core.ai_gateway.ve4_ai_call()`。

使用规范：
1. 长上下文 → 模型仅做分类/判断，不做长文本生成
2. 结构化输入 → 结构化输出（JSON）
3. 超时控制（5秒内），避免阻塞管道
4. 调用级别记录，可用于调试和审计
"""

import os
import sys
import json
import time
import logging
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger("ve4.model_client")

# 路径调整：确保从 receiver 导入 core 时路径正确
_RECEIVER_DIR = Path(__file__).parent.resolve()
_PROJECT_ROOT = _RECEIVER_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# 导入 AI 统一网关
from core.ai_gateway import (
    ve4_ai_call,
    ve4_ai_ask_yesno,
    ve4_ai_ask_choice,
    VE4AiResult,
)

# 模型配置（保留，但仅作为默认值向后兼容）
MODEL_NAME = "qwen2:1.5b"
OLLAMA_BASE_URL = "http://localhost:11434"
REQUEST_TIMEOUT = 5  # 单次调用超时（秒）
CACHE_ENABLED = True


@dataclass
class ModelResponse:
    text: str
    duration_ms: int
    cached: bool = False


class LocalModelClient:
    """本地轻量模型客户端（已适配 AI 配置中心）"""

    def __init__(self, model: str = MODEL_NAME):
        self.model = model
        self.base_url = OLLAMA_BASE_URL
        self._local_cache = {}  # 额外一层本地缓存（已废弃，缓存由网关统一处理）

    def ask(self,
            system: str,
            prompt: str,
            max_tokens: int = 30,
            temperature: float = 0.1,
            format_type: str = "text",
            task_type: str = "") -> ModelResponse:
        """
        轻量模型调用（路由到 AI 配置中心）。

        Args:
            system: 系统指令（简短，< 100 字）
            prompt: 用户输入（简短，< 200 字）
            max_tokens: 最大输出 token
            temperature: 温度
            format_type: "text" | "json"
            task_type: 显式指定任务类型（空=自动推断）

        Returns:
            ModelResponse（兼容旧接口）
        """
        # 自动推断 task_type（如果未显式指定）
        resolved_task = task_type or self._infer_task_type(system, prompt)

        result: VE4AiResult = ve4_ai_call(
            task_type=resolved_task,
            system=system,
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            format_type=format_type,
            contains_privacy_data=True,  # receiver 处理的数据含隐私
            complexity="low",
        )

        if not result.success:
            logger.warning(f"[MODEL] 调用失败 [{result.duration_ms}ms]: {result.error}")
            return ModelResponse(text="", duration_ms=result.duration_ms, cached=False)

        return ModelResponse(
            text=result.text,
            duration_ms=result.duration_ms,
            cached=result.cached,
        )

    @staticmethod
    def _infer_task_type(system: str, prompt: str) -> str:
        """根据 system 和 prompt 推断任务类型（用于路由）"""
        s = system.lower()
        p = prompt.lower()

        if "判断助手" in system or "yes 或 no" in system:
            return "yes_no_question"
        if "分类助手" in system:
            if any(kw in p for kw in ["流动性", "活钱", "赎回", "t+"]):
                return "liquidity_inference"
            return "classification_helper"
        if "分类" in system or "选择" in system:
            return "classification_helper"

        return "text_summary"

    def ask_yesno(self, question: str) -> Optional[bool]:
        """
        二分类：是/否（路由到 AI 配置中心）。

        示例：
            question = "这份 CSV 看起来是银行账单吗？"
            return True / False / None(不确定)
        """
        return ve4_ai_ask_yesno(question, contains_privacy=True)

    def ask_choice(self, question: str, choices: list[str]) -> Optional[str]:
        """
        多选一：从选项中选择（路由到 AI 配置中心）。

        示例：
            question = "这份文件是？"
            choices = ["银行账单", "券商交割单", "消费账单", "其他"]
            return "银行账单" / None
        """
        # 自动推断任务类型
        task_type = "classification_helper"
        if any(kw in question for kw in ["流动性", "活钱", "赎回"]):
            task_type = "liquidity_inference"

        return ve4_ai_ask_choice(
            question=question,
            choices=choices,
            task_type=task_type,
            contains_privacy=True,
        )

    def clear_cache(self):
        """清除缓存（文件类型变化时才需要）—— 已由网关统一管理"""
        pass


class ModelCallStats:
    """模型调用统计（用于调试和优化）"""
    def __init__(self):
        self.total_calls = 0
        self.cache_hits = 0
        self.total_duration_ms = 0

    def record(self, response: ModelResponse):
        self.total_calls += 1
        if response.cached:
            self.cache_hits += 1
        self.total_duration_ms += response.duration_ms

    @property
    def avg_duration_ms(self) -> float:
        if self.total_calls == 0:
            return 0
        return self.total_duration_ms / self.total_calls

    @property
    def cache_rate(self) -> float:
        if self.total_calls == 0:
            return 0
        return self.cache_hits / self.total_calls


# 全局单例
_client = LocalModelClient()
_stats = ModelCallStats()


def ve4_model_get_client() -> LocalModelClient:
    """获取 VE4 本地模型客户端单例（模块安全命名）"""
    return _client


def ve4_model_get_stats() -> ModelCallStats:
    """获取 VE4 模型调用统计单例（模块安全命名）"""
    return _stats


# 保留旧别名以便向后兼容（仅内部使用，不鼓励）
get_client = ve4_model_get_client
get_stats = ve4_model_get_stats