"""
VE4 研报分析 Agent
===================
处理研报和新闻：
    - 解析 URL 或 PDF 文档
    - 使用 LLM 提取关键观点
    - 关联用户持仓，评估影响

命名规范：
    - 类名: VE4ReportAnalysisAgent
    - 函数名: ve4_tactical_report_{action}
"""

import logging
from typing import Dict, Any, List

from tactical.shared.base_agent import VE4TacticalAgent
from tactical.shared.models.tactical_models import VE4AgentTask, VE4AgentResult, VE4AgentStatus, VE4ReportSummary
from tactical.fundamental.tools.document_parser import VE4DocumentParser
# 注意：持仓数据直接从 SQL 读取，不通过 identifier_agent（identifier 属于战略模块）

logger = logging.getLogger("ve4.tactical.report_agent")


class VE4ReportAnalysisAgent(VE4TacticalAgent):
    """研报分析 Agent"""

    @property
    def agent_type(self) -> str:
        return "report"

    # ── 便捷方法（供 API 直接调用） ──

    async def parse_url(self, url: str) -> Dict[str, Any]:
        """解析 URL 并返回结构化结果（供 API 层直接调用）"""
        from tactical.shared.models.tactical_models import VE4AgentTask, VE4AgentTaskType
        import time
        task = VE4AgentTask(
            task_id=f"parse_{time.time():.0f}",
            task_type=VE4AgentTaskType.ANALYZE_REPORT,
            goal="解析研报URL",
            params={"url": url},
        )
        result = await self.execute(task)
        if result.success:
            return result.data
        else:
            raise RuntimeError(result.error or "解析失败")

    async def parse_text(self, text: str, title: str = "手动粘贴文本") -> Dict[str, Any]:
        """解析粘贴的文本并返回结构化结果"""
        from tactical.shared.models.tactical_models import VE4AgentTask, VE4AgentTaskType
        import time
        task = VE4AgentTask(
            task_id=f"parse_{time.time():.0f}",
            task_type=VE4AgentTaskType.ANALYZE_REPORT,
            goal="解析文本",
            params={"text": text, "title": title},
        )
        result = await self.execute(task)
        if result.success:
            return result.data
        else:
            raise RuntimeError(result.error or "解析失败")

    async def execute(self, task: VE4AgentTask) -> VE4AgentResult:
        """
        执行研报分析任务。

        流程：
            1. 解析文档（URL 抓取 / PDF 提取）
            2. 使用 LLM 提取关键观点
            3. 关联用户持仓
            4. 生成影响评估
        """
        self.set_status(VE4AgentStatus.EXECUTING)
        self.emit_progress("正在解析文档...", 10)

        try:
            params = task.params or {}
            url = params.get("url", "")
            pdf_path = params.get("pdf_path", "")
            raw_text = params.get("text", "")
            raw_title = params.get("title", "")

            # 支持三种输入：URL、PDF、纯文本
            if raw_text:
                text = raw_text
                title = raw_title or "手动粘贴文本"
                source_url = ""
            elif url:
                pass  # 下面走 URL 解析
            elif pdf_path:
                pass  # 下面走 PDF 解析
            else:
                self.set_status(VE4AgentStatus.ERROR)
                return self.make_result(task, success=False, error="未提供 URL、PDF 或文本")

            if not raw_text:
                source = url or pdf_path

                # Step 1: 解析文档
                parser = VE4DocumentParser()
                parse_result = await parser.parse(source)

                if not parse_result["success"]:
                    self.set_status(VE4AgentStatus.ERROR)
                    return self.make_result(
                        task, success=False,
                        error=f"文档解析失败: {parse_result['error']}"
                    )

                title = parse_result["title"]
                text = parse_result["text"]
                source_url = url

            self.emit_progress(f"文档解析完成: {title[:30]}...", 30)

            # Step 2: LLM 深度分析
            self.emit_progress("正在使用 AI 分析研报...", 50)
            llm_result = self._llm_analyze_report(text, title)

            # Step 3: 关联用户持仓
            affected_holdings = self._find_affected_holdings(text)
            self.emit_progress("正在关联持仓...", 80)

            # Step 4: 生成摘要
            summary = VE4ReportSummary(
                report_id=f"report_{__import__('time').time():.0f}",
                title=title,
                source_url=url,
                key_points=llm_result.get("key_points", []),
                investment_thesis=llm_result.get("investment_thesis", ""),
                risk_warnings=llm_result.get("risk_warnings", []),
                target_price=llm_result.get("target_price", ""),
                rating=llm_result.get("rating", ""),
                sentiment=llm_result.get("sentiment", ""),  # 新增
                affected_holdings=affected_holdings,
                summary_text=llm_result.get("summary", text[:500] + "..." if len(text) > 500 else text),
                parsed_at=__import__('datetime').datetime.now().isoformat(),
            )

            # RAG: 研报入库
            report_dict = summary.to_dict()
            report_dict["full_text"] = text[:20000]  # 限制全文长度
            try:
                from tactical.fundamental.knowledge.vector_store import ve4_kb_upsert_report
                ve4_kb_upsert_report(report_dict)
                logger.info(f"[RAG] 研报已入库: {report_dict['report_id']}")
            except Exception as e:
                logger.warning(f"[RAG] 研报入库失败（非阻塞）: {e}")

            self.emit_progress("研报分析完成", 100)
            self.set_status(VE4AgentStatus.COMPLETED)

            return self.make_result(task, success=True, data=report_dict)

        except Exception as e:
            logger.error(f"[REPORT] 执行异常: {e}")
            self.emit_error(str(e))
            return self.make_result(task, success=False, error=str(e))

    def _extract_key_points(self, text: str) -> List[str]:
        """从文本提取关键观点（简化版关键词匹配）"""
        points = []
        indicators = [
            ("看好", "看好/推荐"), ("买入", "买入评级"), ("增持", "增持建议"),
            ("减持", "减持建议"), ("卖出", "卖出评级"), ("中性", "中性评级"),
            ("目标价", "目标价调整"), ("风险提示", "风险提示"),
            ("业绩超预期", "业绩表现"), ("盈利预测", "盈利预测"),
        ]
        for keyword, label in indicators:
            if keyword in text:
                # 找到关键词所在句子
                idx = text.find(keyword)
                start = max(0, idx - 30)
                end = min(len(text), idx + 60)
                sentence = text[start:end].strip()
                points.append(f"[{label}] {sentence}")
        return points[:5]

    def _extract_thesis(self, text: str) -> str:
        """提取投资逻辑"""
        thesis_keywords = ["核心逻辑", "投资逻辑", "核心观点", "主要观点", "我们认为"]
        for kw in thesis_keywords:
            if kw in text:
                idx = text.find(kw)
                return text[idx:idx + 200].strip()
        return "未明确提取投资逻辑"

    def _extract_risks(self, text: str) -> List[str]:
        """提取风险提示"""
        risks = []
        risk_section = text.find("风险")
        if risk_section >= 0:
            section = text[risk_section:risk_section + 300]
            # 简单分句
            sentences = section.replace("。", "|").replace("；", "|").split("|")
            for s in sentences[:3]:
                if len(s.strip()) > 5:
                    risks.append(s.strip())
        return risks

    def _llm_analyze_report(self, text: str, title: str) -> Dict[str, Any]:
        """使用 LLM 分析研报，生成摘要、投资逻辑、关键观点等。"""
        try:
            from core.ai_gateway import ve4_ai_call
        except ImportError as e:
            logger.warning(f"[REPORT] ai_gateway 不可用: {e}")
            return {}

        system = """你是一位资深金融分析师，擅长阅读研报和财经文章并提取核心信息。

你的任务：阅读以下研报/文章，提取并总结核心信息。

**输出要求（严格按以下JSON格式输出）：**
{
  "summary": "用2-3句话概括这篇研报的核心观点和结论",
  "investment_thesis": "详细阐述投资逻辑，包括：1)核心观点 2)支撑论据 3)对市场的判断",
  "key_points": ["关键观点1", "关键观点2", "关键观点3"],
  "risk_warnings": ["风险1", "风险2"],
  "sentiment": "看多/看空/中性",
  "rating": "买入/增持/中性/减持/卖出/无评级"
}

**规则：**
- summary 必须简洁有力，不超过100字
- investment_thesis 必须包含具体的投资逻辑，不要泛泛而谈
- key_points 提取3-5个最关键的观点
- risk_warnings 提取1-3个主要风险
- sentiment 必须是"看多"/"看空"/"中性"之一
- 只输出JSON，不要任何解释"""

        prompt = f"标题：{title}\n\n正文：\n{text[:8000]}"

        result = ve4_ai_call(
            task_type="json",
            system=system,
            prompt=prompt,
            format_type="json",
            contains_privacy_data=False,
            complexity="high",
            max_tokens=4096,
            temperature=0.1,
        )

        if not result.success or not result.text:
            logger.warning("[REPORT] LLM 分析无返回")
            return {}

        try:
            import json
            parsed = json.loads(result.text)
            logger.info(f"[REPORT] LLM 分析完成: {len(parsed.get('summary', ''))}字摘要")
            return parsed
        except json.JSONDecodeError as e:
            logger.warning(f"[REPORT] LLM 返回非JSON: {e}")
            return {}

    def _find_affected_holdings(self, text: str) -> List[str]:
        """关联用户持仓（简化版：检查持仓名称是否出现在文本中）"""
        try:
            identifier = VE4InvestmentIdentifierAgent()
            holdings = identifier._load_investment_holdings()
            affected = []
            text_lower = text.lower()
            for h in holdings:
                name = h.get("product_name", "")
                # 提取产品名称中的关键词（去除通用词）
                keywords = self._extract_name_keywords(name)
                if any(kw in text_lower for kw in keywords):
                    affected.append(name)
            return affected[:5]
        except Exception:
            return []

    @staticmethod
    def _extract_name_keywords(name: str) -> List[str]:
        """从产品名称提取关键词"""
        # 去除常见停用词
        stopwords = {"基金", "股票", "债券", "指数", "ETF", "混合", "A类", "C类",
                     "联接", "增强", "QDII", "LOF", "FOF", "分级"}
        parts = name.replace("-", "").replace("(", "").replace(")", "").split()
        keywords = []
        for p in parts:
            if len(p) >= 2 and p not in stopwords:
                keywords.append(p.lower())
        return keywords
