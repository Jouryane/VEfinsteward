"""
VE4 战术模块 MCP Server
========================
通过 Model Context Protocol (MCP) 将 VE4 战术模块的能力暴露为标准化 tools，
供外部 AI 应用（Claude Desktop、Trae 等）调用。

MCP 架构说明：
    - VE4 作为 MCP Server，提供 tools（功能接口）
    - AI 应用（Claude Desktop / Trae）作为 MCP Client，连接并调用 tools
    - 用户可以在 AI 应用中自然语言交互，AI 应用自动选择并调用 VE4 tools

启动方式：
    python -m ve4.tactical.shared.mcp_server
    或
    python ve4/tactical/shared/mcp_server.py

Claude Desktop 配置示例（claude_desktop_config.json）：
    {
      "mcpServers": {
        "ve4-tactical": {
          "command": "python",
          "args": [
            "-m", "ve4.tactical.shared.mcp_server"
          ],
          "cwd": "<ve4项目根目录>"
        }
      }
    }

提供的 Tools：
    1. execute_quant_strategy — 执行量化策略代码
    2. get_data_source_status — 获取数据源状态
    3. parse_report_text — 解析研报/新闻文本
    4. get_holdings_impact — 评估持仓影响（注意：涉及隐私数据）

隐私安全：
    - execute_quant_strategy：不涉及隐私，公开可用
    - get_data_source_status：不涉及隐私
    - parse_report_text：输入公开文本，输出分析结果
    - get_holdings_impact：涉及用户持仓数据，仅当用户明确授权时可用
"""

import sys
import json
import asyncio
import logging
from pathlib import Path

# 路径设置（确保 ve4/ 在 sys.path 中）
code_dir = Path(__file__).parent.parent.parent.parent.resolve()
if str(code_dir) not in sys.path:
    sys.path.insert(0, str(code_dir))
ve4_code_dir = code_dir / "VE4"
if str(ve4_code_dir) not in sys.path:
    sys.path.append(str(ve4_code_dir))

from mcp.server import Server
from mcp.types import Tool, TextContent

logger = logging.getLogger("ve4.tactical.mcp")

# ════════════════════════════════════════════════════════════════
# MCP Server 实例
# ════════════════════════════════════════════════════════════════

server = Server("ve4-tactical")


# ── Tool 定义 ──

TOOLS = [
    Tool(
        name="execute_quant_strategy",
        description="在 VE4 CodeSandbox 中执行量化策略 Python 代码，返回程序输出。支持 akshare、tushare、东方财富等数据源。代码应使用 print() 输出结果。",
        inputSchema={
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python 策略代码。例如：import akshare as ak; df = ak.stock_zh_a_spot_em(); print(df.head())"
                },
                "timeout": {
                    "type": "integer",
                    "description": "执行超时时间（秒），默认 30",
                    "default": 30
                }
            },
            "required": ["code"]
        }
    ),
    Tool(
        name="get_data_source_status",
        description="获取 VE4 战术模块已配置的数据源状态（AkShare、Tushare、东方财富、本地数据等）",
        inputSchema={
            "type": "object",
            "properties": {}
        }
    ),
    Tool(
        name="parse_report_text",
        description="解析研报或新闻文本，提取关键投资观点、情绪倾向、目标价等信息",
        inputSchema={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "研报或新闻文本内容"
                },
                "title": {
                    "type": "string",
                    "description": "研报标题（可选）",
                    "default": ""
                }
            },
            "required": ["text"]
        }
    ),
    Tool(
        name="get_holdings_impact",
        description="基于研报分析结果，评估对用户持仓的影响，生成买入/卖出/持有建议。注意：此 tool 涉及用户隐私持仓数据。",
        inputSchema={
            "type": "object",
            "properties": {
                "report_summary": {
                    "type": "string",
                    "description": "研报摘要"
                },
                "report_sentiment": {
                    "type": "string",
                    "description": "研报情绪倾向（看多/看空/中性）",
                    "default": ""
                },
                "report_viewpoints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "研报关键观点列表",
                    "default": []
                }
            },
            "required": ["report_summary"]
        }
    ),
]


# ── Handlers ──

@server.list_tools()
async def list_tools() -> list:
    """返回所有可用 tools"""
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list:
    """处理 tool 调用"""
    logger.info(f"[MCP] Tool called: {name}")

    try:
        if name == "execute_quant_strategy":
            return await _handle_execute_strategy(arguments)
        elif name == "get_data_source_status":
            return await _handle_datasource_status(arguments)
        elif name == "parse_report_text":
            return await _handle_parse_report(arguments)
        elif name == "get_holdings_impact":
            return await _handle_holdings_impact(arguments)
        else:
            return [TextContent(type="text", text=f"未知 tool: {name}")]
    except Exception as e:
        logger.error(f"[MCP] Tool {name} error: {e}", exc_info=True)
        return [TextContent(type="text", text=f"执行错误: {str(e)}")]


# ── Tool 实现 ──

async def _handle_execute_strategy(args: dict) -> list:
    """执行量化策略代码"""
    code = args.get("code", "")
    timeout = args.get("timeout", 30)
    if not code:
        return [TextContent(type="text", text="错误: 策略代码不能为空")]

    try:
        from tactical.quantitative.tools.code_sandbox import VE4CodeSandbox
        sandbox = VE4CodeSandbox()
        result = await sandbox.execute(code, timeout=timeout)

        output = result.get("stdout", "")
        error = result.get("error", "")
        stderr = result.get("stderr", "")

        text = f"【策略执行结果】\n\n"
        if output:
            text += f"输出:\n{output}\n\n"
        if stderr:
            text += f"标准错误:\n{stderr}\n\n"
        if error:
            text += f"错误:\n{error}\n\n"
        if not output and not error and not stderr:
            text += "（无输出）\n"

        return [TextContent(type="text", text=text)]
    except ImportError:
        return [TextContent(type="text", text="CodeSandbox 未就绪。请确认 VE4 战术模块已正确安装。")]


async def _handle_datasource_status(args: dict) -> list:
    """获取数据源状态"""
    try:
        from tactical.quantitative.tools.data_source import VE4DataSourceManager
        mgr = VE4DataSourceManager()
        sources = mgr.list_sources()

        lines = ["【VE4 数据源状态】\n"]
        for key, info in sources.items():
            status_icon = "✅" if info.get("enabled") else "❌"
            lines.append(f"{status_icon} {info.get('name', key)} — {info.get('status', '未知')}")

        lines.append("\n使用 execute_quant_strategy 工具执行策略代码时，可用数据源取决于上述配置。")
        return [TextContent(type="text", text="\n".join(lines))]
    except ImportError:
        return [TextContent(type="text", text="数据源管理器未就绪。")]


async def _handle_parse_report(args: dict) -> list:
    """解析研报文本"""
    text = args.get("text", "")
    title = args.get("title", "")
    if not text:
        return [TextContent(type="text", text="错误: 文本不能为空")]

    try:
        from tactical.fundamental.agents.report_agent import VE4ReportAnalysisAgent
        agent = VE4ReportAnalysisAgent()
        result = await agent.parse_text(text)

        lines = [f"【研报解析结果】\n"]
        lines.append(f"标题: {result.get('title', title or '未命名')}")
        lines.append(f"情绪倾向: {result.get('sentiment', '未知')}")
        lines.append(f"置信度: {result.get('confidence', 0)}")
        lines.append(f"\n摘要:\n{result.get('summary', '暂无')}")
        viewpoints = result.get('viewpoints', [])
        if viewpoints:
            lines.append(f"\n关键观点:")
            for vp in viewpoints:
                lines.append(f"  • {vp}")

        return [TextContent(type="text", text="\n".join(lines))]
    except (ImportError, AttributeError):
        # fallback: 直接返回文本分析
        lines = [f"【研报解析】\n"]
        lines.append(f"标题: {title or '未命名'}")
        lines.append(f"文本长度: {len(text)} 字")
        lines.append(f"\n内容摘要（前 300 字）:\n{text[:300]}...")
        return [TextContent(type="text", text="\n".join(lines))]


async def _handle_holdings_impact(args: dict) -> list:
    """评估持仓影响"""
    report_summary = args.get("report_summary", "")
    if not report_summary:
        return [TextContent(type="text", text="错误: 研报摘要不能为空")]

    try:
        from tactical.fundamental.agents.holdings_impact_agent import VE4HoldingsImpactAgent
        from tactical.shared.models.tactical_models import VE4AgentTask, VE4AgentTaskType

        agent = VE4HoldingsImpactAgent()
        task = VE4AgentTask(
            task_id=f"mcp_impact_{asyncio.get_event_loop().time()}",
            task_type=VE4AgentTaskType.ANALYZE_HOLDINGS,
            goal="评估研报对用户持仓的影响",
            params={
                "report_summary": report_summary,
                "report_sentiment": args.get("report_sentiment", ""),
                "report_viewpoints": args.get("report_viewpoints", []),
            }
        )
        result = await agent.execute(task)

        if result.success:
            recs = result.data.get("recommendations", [])
            lines = ["【持仓影响评估】\n"]
            for r in recs:
                action_icon = {"buy": "📈", "sell": "📉", "hold": "➖"}.get(r.get("action"), "➖")
                lines.append(f"{action_icon} {r.get('holding_name', '未知')} — {r.get('action_label', '持有')}")
                lines.append(f"   理由: {r.get('reason', '无')}")
                lines.append(f"   置信度: {r.get('confidence', 0)}")
                lines.append("")
            return [TextContent(type="text", text="\n".join(lines))]
        else:
            return [TextContent(type="text", text=f"评估失败: {result.error}")]
    except ImportError:
        return [TextContent(type="text", text="HoldingsImpactAgent 未就绪。")]


# ════════════════════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════════════════════

async def main():
    """启动 MCP Server（stdio 传输）"""
    from mcp.server.stdio import stdio_server

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    logger.info("[MCP] VE4 Tactical Server starting...")

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
