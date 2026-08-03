"""
VE管家 子skill集合
==================
所有子skill必须实现 VE5ChatbotSkill 接口。
"""

from .life_planner import ve5_skill_life_planner
from .asset_doctor import ve5_skill_asset_doctor
from .investment_advisor import ve5_skill_investment_advisor
from .goal_tracker import ve5_skill_goal_tracker
from .spending_analyst import ve5_skill_spending_analyst

__all__ = [
    "ve5_skill_life_planner",
    "ve5_skill_asset_doctor",
    "ve5_skill_investment_advisor",
    "ve5_skill_goal_tracker",
    "ve5_skill_spending_analyst",
]
