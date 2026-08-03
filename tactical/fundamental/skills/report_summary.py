"""
VE4 研报摘要 Skill
===================
将研报内容存入 RAG 知识库，并提取关键观点。

命名规范：
    - 类名: VE4ReportSummarySkill
    - Skill 名: report_summary
"""

import logging
from typing import List

from tactical.quantitative.skills.skill_registry import VE4TacticalSkill
from tactical.shared.models.tactical_models import VE4SkillCategory, VE4SkillContext, VE4SkillResult

logger = logging.getLogger("ve4.tactical.skill.report_summary")


class VE4ReportSummarySkill(VE4TacticalSkill):
    """研报摘要 Skill"""

    name = "report_summary"
    description = "解析研报/新闻文本，提取关键投资观点和风险提示"
    category = VE4SkillCategory.REPORT
    required_data = []
    version = "1.0"

    async def execute(self, context: VE4SkillContext) -> VE4SkillResult:
        params = context.params or {}
        text = params.get("text", "")
        title = params.get("title", "")

        if not text:
            return self._make_result(success=False, error="未提供文本内容")

        # 关键词提取（简化版，未来接入 LLM）
        key_points = self._extract_points(text)
        risks = self._extract_risks(text)

        data = {
            "title": title,
            "key_points": key_points[:5],
            "risk_warnings": risks[:3],
            "text_length": len(text),
            "note": "简化版提取，未来接入 LLM task_type='tactical_report_parse'",
        }

        return self._make_result(success=True, data=data)

    def _extract_points(self, text: str) -> List[str]:
        indicators = ["看好", "买入", "增持", "减持", "卖出", "中性", "目标价", "超预期"]
        points = []
        for kw in indicators:
            if kw in text:
                idx = text.find(kw)
                points.append(text[max(0, idx-20):idx+40].strip())
        return points

    def _extract_risks(self, text: str) -> List[str]:
        idx = text.find("风险")
        if idx >= 0:
            return [text[idx:idx+100].strip()]
        return []
