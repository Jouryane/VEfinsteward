"""
VE4 战术模块 Skill 注册中心
============================
管理所有预设量化分析 Skill 的注册、发现和执行。

借鉴 TRAE MCP 协议设计：
    - 每个 Skill 有 name + description（给 Orchestrator/LLM 看）
    - 标准化的 inputSchema（通过 VE4SkillContext）
    - 统一的 handler 接口（execute 方法）

命名规范：
    - 类名: VE4{SkillName}Skill
    - Skill 名: {domain}_{action}（如 pattern_detection）
"""

import logging
from abc import ABC, abstractmethod
from typing import Dict, List, Type, Optional

from tactical.shared.models.tactical_models import (
    VE4SkillCategory,
    VE4SkillInfo,
    VE4SkillContext,
    VE4SkillResult,
)

logger = logging.getLogger("ve4.tactical.skill_registry")


# ════════════════════════════════════════════════════════════════
# Skill 基类
# ════════════════════════════════════════════════════════════════

class VE4TacticalSkill(ABC):
    """战术 Skill 抽象基类"""

    name: str = ""
    description: str = ""
    category: VE4SkillCategory = VE4SkillCategory.ANALYSIS
    required_data: List[str] = []
    version: str = "1.0"

    @classmethod
    def get_info(cls) -> VE4SkillInfo:
        """获取 Skill 元信息"""
        return VE4SkillInfo(
            name=cls.name,
            description=cls.description,
            category=cls.category,
            required_data=cls.required_data,
            version=cls.version,
        )

    @abstractmethod
    async def execute(self, context: VE4SkillContext) -> VE4SkillResult:
        """执行 Skill（子类必须实现）"""
        pass

    def _validate_context(self, context: VE4SkillContext) -> Optional[str]:
        """验证上下文是否包含 required_data"""
        context_keys = []
        if context.holdings:
            context_keys.append("holdings")
        if context.transactions:
            context_keys.append("transactions")
        if context.profile:
            context_keys.append("profile")

        missing = [req for req in self.required_data if req not in context_keys]
        if missing:
            return f"缺少必需数据: {', '.join(missing)}"
        return None

    def _make_result(self, success: bool, data: dict = None,
                     error: str = "", metrics: dict = None) -> VE4SkillResult:
        """便捷构造 SkillResult"""
        return VE4SkillResult(
            success=success,
            skill_name=self.name,
            data=data or {},
            error=error,
            metrics=metrics or {},
        )


# ════════════════════════════════════════════════════════════════
# Skill 注册中心
# ════════════════════════════════════════════════════════════════

class VE4SkillRegistry:
    """
    Skill 注册中心（单例模式）。

    使用方式：
        registry = VE4SkillRegistry()
        registry.register(MyPatternDetectionSkill)
        result = await registry.execute("pattern_detection", context=ctx)
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._skills: Dict[str, Type[VE4TacticalSkill]] = {}
            cls._instance._instances: Dict[str, VE4TacticalSkill] = {}
        return cls._instance

    def register(self, skill_class: Type[VE4TacticalSkill]):
        """注册 Skill 类"""
        if not skill_class.name:
            raise ValueError(f"Skill 类必须定义 name 属性: {skill_class}")
        self._skills[skill_class.name] = skill_class
        logger.info(f"[SKILL-REGISTRY] 注册 Skill: {skill_class.name} ({skill_class.category.value})")

    def unregister(self, skill_name: str):
        """注销 Skill"""
        self._skills.pop(skill_name, None)
        self._instances.pop(skill_name, None)

    def get(self, skill_name: str) -> Optional[VE4TacticalSkill]:
        """获取 Skill 实例（懒加载）"""
        if skill_name in self._instances:
            return self._instances[skill_name]

        skill_class = self._skills.get(skill_name)
        if not skill_class:
            return None

        instance = skill_class()
        self._instances[skill_name] = instance
        return instance

    def list_skills(self, category: VE4SkillCategory = None) -> List[VE4SkillInfo]:
        """列出所有已注册的 Skill"""
        results = []
        for name, skill_class in self._skills.items():
            if category and skill_class.category != category:
                continue
            results.append(skill_class.get_info())
        return results

    async def execute(self, skill_name: str, context: VE4SkillContext) -> VE4SkillResult:
        """执行指定 Skill"""
        skill = self.get(skill_name)
        if not skill:
            return VE4SkillResult(
                success=False,
                skill_name=skill_name,
                error=f"Skill '{skill_name}' 未注册",
            )

        # 验证上下文
        validation_error = skill._validate_context(context)
        if validation_error:
            return VE4SkillResult(
                success=False,
                skill_name=skill_name,
                error=validation_error,
            )

        # 执行
        try:
            logger.info(f"[SKILL-REGISTRY] 执行 Skill: {skill_name}")
            return await skill.execute(context)
        except Exception as e:
            logger.error(f"[SKILL-REGISTRY] Skill 执行异常: {skill_name} - {e}")
            return VE4SkillResult(
                success=False,
                skill_name=skill_name,
                error=str(e),
            )

    def __repr__(self):
        return f"<VE4SkillRegistry skills={list(self._skills.keys())}>"
