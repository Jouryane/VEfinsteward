"""
VE4 战术模块数据模型
====================
定义 Agent、Skill、Task、Report 等核心数据结构。

命名规范：类名 VE4Tactical{ModelName}
"""

from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Any
from enum import Enum
from datetime import datetime


# ════════════════════════════════════════════════════════════════
# Agent 状态与任务
# ════════════════════════════════════════════════════════════════

class VE4AgentStatus(Enum):
    """Agent 执行状态"""
    IDLE = "idle"
    PLANNING = "planning"
    DISPATCHING = "dispatching"
    EXECUTING = "executing"
    AGGREGATING = "aggregating"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"
    PAUSED = "paused"


class VE4AgentTaskType(Enum):
    """任务类型"""
    IDENTIFY_HOLDINGS = "identify_holdings"
    QUANT_ANALYSIS = "quant_analysis"
    BACKTEST = "backtest"
    ANALYZE_REPORT = "analyze_report"
    GENERATE_CODE = "generate_code"
    COLLECT_DATA = "collect_data"
    ANALYZE_HOLDINGS = "analyze_holdings"


@dataclass
class VE4AgentTask:
    """Agent 任务定义"""
    task_id: str
    task_type: VE4AgentTaskType
    goal: str
    params: Dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    depends_on: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class VE4AgentResult:
    """Agent 执行结果"""
    task_id: str
    success: bool
    agent_id: str
    agent_type: str
    data: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    duration_ms: int = 0
    completed_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VE4AgentEvent:
    """Agent 事件（用于 Orchestrator 通信）"""
    agent_id: str
    event_type: str  # "started" | "progress" | "completed" | "error"
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


# ════════════════════════════════════════════════════════════════
# Skill 相关模型
# ════════════════════════════════════════════════════════════════

class VE4SkillCategory(Enum):
    """Skill 类别"""
    ANALYSIS = "analysis"
    BACKTEST = "backtest"
    REPORT = "report"


@dataclass
class VE4SkillInfo:
    """Skill 元信息"""
    name: str
    description: str
    category: VE4SkillCategory
    required_data: List[str] = field(default_factory=list)
    version: str = "1.0"


@dataclass
class VE4SkillContext:
    """Skill 执行上下文"""
    holdings: List[Dict] = field(default_factory=list)
    transactions: List[Dict] = field(default_factory=list)
    profile: Dict[str, Any] = field(default_factory=dict)
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VE4SkillResult:
    """Skill 执行结果"""
    success: bool
    skill_name: str
    data: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    metrics: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ════════════════════════════════════════════════════════════════
# 投资标的模型
# ════════════════════════════════════════════════════════════════

class VE4HoldingType(Enum):
    """投资标的类型"""
    STOCK = "stock"
    FUND = "fund"
    ETF = "etf"
    BOND = "bond"
    REIT = "reit"
    COMMODITY = "commodity"
    CASH = "cash"
    UNKNOWN = "unknown"


@dataclass
class VE4InvestmentIdentifier:
    """识别的投资标的"""
    name: str
    raw_name: str
    holding_type: VE4HoldingType
    confidence: float
    evidence: List[str] = field(default_factory=list)
    sector: str = ""  # 行业
    market: str = ""  # 市场（A股/港股/美股）


# ════════════════════════════════════════════════════════════════
# 回测模型
# ════════════════════════════════════════════════════════════════

@dataclass
class VE4BacktestResult:
    """回测结果"""
    period: str
    total_return: float
    annualized_return: Optional[float] = None
    volatility: Optional[float] = None
    max_drawdown: Optional[float] = None
    sharpe_ratio: Optional[float] = None
    win_rate: Optional[float] = None
    is_positive: bool = False
    comparison_with_benchmark: Optional[float] = None
    trade_count: int = 0
    equity_curve: List[Dict[str, float]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ════════════════════════════════════════════════════════════════
# 研报分析模型
# ════════════════════════════════════════════════════════════════

@dataclass
class VE4ReportSummary:
    """研报摘要"""
    report_id: str
    title: str
    source_url: str = ""
    key_points: List[str] = field(default_factory=list)
    investment_thesis: str = ""
    risk_warnings: List[str] = field(default_factory=list)
    target_price: str = ""
    rating: str = ""
    sentiment: str = ""  # 新增：看多/看空/中性
    affected_holdings: List[str] = field(default_factory=list)
    summary_text: str = ""
    parsed_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return asdict(self)


# ════════════════════════════════════════════════════════════════
# 战术报告模型
# ════════════════════════════════════════════════════════════════

@dataclass
class VE4TacticalReport:
    """战术分析总报告（Orchestrator 聚合输出）"""
    report_id: str
    goal: str
    status: str  # "completed" | "partial" | "failed"
    holdings_summary: Dict[str, Any] = field(default_factory=dict)
    patterns: List[Dict] = field(default_factory=list)
    risk_metrics: Dict[str, float] = field(default_factory=dict)
    diversification: Dict[str, Any] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)
    backtest_results: List[Dict] = field(default_factory=list)
    report_summaries: List[Dict] = field(default_factory=list)
    generated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return asdict(self)
