"""
VE4 投资标的识别 Agent
=======================
从用户持仓中识别投资标的（股票、基金、ETF、债券等），
提取标的名称、类型、行业、市值信息。

数据来源：userdata/ve4.db → asset_holdings 表

命名规范：
    - 类名: VE4InvestmentIdentifierAgent
    - 函数名: ve4_tactical_identify_{action}
"""

import sqlite3
import logging
from pathlib import Path
from typing import List, Dict, Any
from datetime import datetime

from tactical.shared.base_agent import VE4TacticalAgent
from tactical.shared.models.tactical_models import (
    VE4AgentTask,
    VE4AgentResult,
    VE4AgentStatus,
    VE4InvestmentIdentifier,
    VE4HoldingType,
)

logger = logging.getLogger("ve5.tactical.identifier_agent")

# 数据库路径
from app_paths import DB_PATH


# ════════════════════════════════════════════════════════════════
# 识别规则
# ════════════════════════════════════════════════════════════════

# 投资类型关键词映射
VE4_IDENTIFIER_TYPE_RULES = {
    VE4HoldingType.ETF: {
        "keywords": ["ETF", "etf", "指数", "沪深300", "中证500", "上证50", "创业板", "科创板", "纳指", "标普", "道琼斯"],
        "codes": ["510", "512", "513", "515", "516", "518", "560", "563", "159"],
    },
    VE4HoldingType.STOCK: {
        "keywords": ["股票", "个股", "A股", "港股", "美股", "蓝筹", "白马", "茅台", "腾讯", "阿里"],
    },
    VE4HoldingType.FUND: {
        "keywords": ["基金", "混合", "偏股", "偏债", "灵活配置", "FOF", "QDII", "联接"],
    },
    VE4HoldingType.BOND: {
        "keywords": ["债券", "债基", "纯债", "短债", "中短债", "长债", "国债", "企业债", "可转债", "转债"],
    },
    VE4HoldingType.REIT: {
        "keywords": ["REIT", "reit", "基础设施", "仓储物流", "产业园", "保障房"],
    },
    VE4HoldingType.COMMODITY: {
        "keywords": ["黄金", "白银", "贵金属", "商品", "原油", "有色金属"],
    },
}

# 行业关键词映射
VE4_IDENTIFIER_SECTOR_RULES = {
    "金融": ["银行", "保险", "券商", "证券", "信托", "金融", "地产", "房地产"],
    "科技": ["科技", "半导体", "芯片", "人工智能", "AI", "计算机", "软件", "互联网", "通信", "5G"],
    "医药": ["医药", "医疗", "生物", "疫苗", "器械", " healthcare", "健康"],
    "消费": ["消费", "食品饮料", "白酒", "家电", "汽车", "零售", "电商", "旅游", "酒店"],
    "新能源": ["新能源", "光伏", "风电", "储能", "电池", "锂电", "电动车", "新能源汽车", "碳中和"],
    "能源": ["能源", "煤炭", "石油", "天然气", "电力", "公用事业"],
    "基建": ["基建", "建筑", "建材", "钢铁", "水泥", "机械", "工程"],
    "军工": ["军工", "国防", "航天", "航空", "船舶"],
    "传媒": ["传媒", "游戏", "影视", "广告", "出版", "文化"],
    "材料": ["材料", "化工", "有色", "稀土", "新材料"],
}

# 市场映射
VE4_IDENTIFIER_MARKET_RULES = {
    "A股": ["沪深300", "中证500", "上证50", "创业板", "科创板", "A股"],
    "港股": ["港股", "恒生", "H股", "香港"],
    "美股": ["纳指", "标普", "道琼斯", "美股", "纳斯达克", "NYSE"],
}


# ════════════════════════════════════════════════════════════════
# Agent 实现
# ════════════════════════════════════════════════════════════════

class VE4InvestmentIdentifierAgent(VE4TacticalAgent):
    """投资标的识别 Agent"""

    @property
    def agent_type(self) -> str:
        return "identifier"

    async def execute(self, task: VE4AgentTask) -> VE4AgentResult:
        """
        执行标的识别任务。

        流程：
            1. 从数据库加载进取类 + 稳健类持仓
            2. 逐条识别标的类型、行业、市场
            3. 计算各标的占比
            4. 返回结构化标的清单
        """
        start_time = __import__('time').time()
        self.set_status(VE4AgentStatus.EXECUTING)
        self.emit_progress("正在读取持仓数据...", 10)

        try:
            # Step 1: 加载持仓
            holdings = self._load_investment_holdings()
            if not holdings:
                self.emit_progress("暂无投资持仓数据", 100)
                return self.make_result(
                    task, success=True,
                    data={"holdings_summary": {"count": 0, "total_value": 0, "identifiers": []}},
                    duration_ms=int((__import__('time').time() - start_time) * 1000)
                )

            self.emit_progress(f"已读取 {len(holdings)} 条持仓，正在识别标的类型...", 30)

            # Step 2: 识别每条持仓
            identifiers = []
            for i, h in enumerate(holdings):
                ident = self._identify_holding(h)
                identifiers.append(ident)
                pct = 30 + int((i + 1) / len(holdings) * 50)
                self.emit_progress(f"正在识别: {ident.raw_name} → {ident.holding_type.value}", pct)

            # Step 3: 汇总统计
            total_value = sum(h.get("current_value", 0) for h in holdings)
            type_distribution = {}
            sector_distribution = {}
            for ident in identifiers:
                # 类型分布
                t = ident.holding_type.value
                type_distribution[t] = type_distribution.get(t, 0) + 1
                # 行业分布
                if ident.sector:
                    sector_distribution[ident.sector] = sector_distribution.get(ident.sector, 0) + 1

            self.emit_progress("标的识别完成", 100)
            self.set_status(VE4AgentStatus.COMPLETED)

            data = {
                "holdings_summary": {
                    "count": len(holdings),
                    "total_value": round(total_value, 2),
                    "type_distribution": type_distribution,
                    "sector_distribution": sector_distribution,
                },
                "identifiers": [self._identifier_to_dict(ident) for ident in identifiers],
            }

            elapsed_ms = int((__import__('time').time() - start_time) * 1000)
            return self.make_result(task, success=True, data=data, duration_ms=elapsed_ms)

        except Exception as e:
            logger.error(f"[IDENTIFIER] 执行异常: {e}")
            self.emit_error(str(e))
            return self.make_result(task, success=False, error=str(e))

    # ── 数据加载 ──

    def _load_investment_holdings(self) -> List[Dict]:
        """从数据库加载进取类和稳健类持仓"""
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row

            # 进取类 + 稳健类持仓（排除流动类和保障类）
            rows = conn.execute("""
                SELECT id, account_key, product_name, product_code, current_value,
                       cost_basis, holding_return_pct, annualized_return_pct,
                       purchase_date, holding_days, asset_class, user_note
                FROM asset_holdings
                WHERE is_classified = 1
                  AND (
                      LOWER(product_name) LIKE '%股票%'
                      OR LOWER(product_name) LIKE '%基金%'
                      OR LOWER(product_name) LIKE '%ETF%'
                      OR LOWER(product_name) LIKE '%债券%'
                      OR LOWER(product_name) LIKE '%指数%'
                      OR LOWER(product_name) LIKE '%债基%'
                      OR LOWER(product_name) LIKE '%理财%'
                      OR LOWER(product_name) LIKE '%QDII%'
                      OR LOWER(product_name) LIKE '%REIT%'
                      OR LOWER(product_name) LIKE '%黄金%'
                      OR asset_class IN ('equity', 'fixed_income', 'alternative')
                  )
                  AND current_value > 0
                ORDER BY current_value DESC
            """).fetchall()
            conn.close()

            holdings = []
            for row in rows:
                # 排除明显是现金类的产品
                name = (row["product_name"] or "").lower()
                if any(kw in name for kw in ["活期", "现金", "余额宝", "零钱通", "朝朝宝", "存款", "货币"]):
                    continue
                holdings.append({
                    "id": row["id"],
                    "account_key": row["account_key"] or "",
                    "product_name": row["product_name"] or "",
                    "product_code": row["product_code"] or "",
                    "current_value": round(row["current_value"] or 0, 2),
                    "cost_basis": round(row["cost_basis"] or 0, 2),
                    "holding_return_pct": round(row["holding_return_pct"] or 0, 2),
                    "annualized_return_pct": round(row["annualized_return_pct"] or 0, 2),
                    "purchase_date": row["purchase_date"] or "",
                    "holding_days": row["holding_days"] or 0,
                    "asset_class": row["asset_class"] or "",
                })

            logger.info(f"[IDENTIFIER] 加载 {len(holdings)} 条投资持仓")
            return holdings

        except Exception as e:
            logger.error(f"[IDENTIFIER] 加载持仓失败: {e}")
            return []

    # ── 单条识别 ──

    def _identify_holding(self, holding: Dict) -> VE4InvestmentIdentifier:
        """识别单条持仓的标的类型"""
        raw_name = holding.get("product_name", "")
        name = raw_name.lower()
        code = holding.get("product_code", "")
        asset_class = holding.get("asset_class", "")

        # 1. 判断类型
        holding_type, confidence, evidence = self._determine_type(name, code, asset_class)

        # 2. 判断行业
        sector = self._determine_sector(name)

        # 3. 判断市场
        market = self._determine_market(name)

        return VE4InvestmentIdentifier(
            name=raw_name,
            raw_name=raw_name,
            holding_type=holding_type,
            confidence=confidence,
            evidence=evidence,
            sector=sector,
            market=market,
        )

    def _determine_type(self, name: str, code: str, asset_class: str) -> tuple:
        """判断投资标的类型，返回 (type, confidence, evidence)"""
        evidence = []

        # 检查 ETF（最高优先级）
        etf_keywords = VE4_IDENTIFIER_TYPE_RULES[VE4HoldingType.ETF]["keywords"]
        etf_codes = VE4_IDENTIFIER_TYPE_RULES[VE4HoldingType.ETF].get("codes", [])
        if any(kw in name for kw in etf_keywords):
            evidence.append(f"名称含ETF/指数关键词: {[kw for kw in etf_keywords if kw in name]}")
            return VE4HoldingType.ETF, 0.90, evidence
        if any(code.startswith(prefix) for prefix in etf_codes):
            evidence.append(f"代码前缀匹配ETF: {code}")
            return VE4HoldingType.ETF, 0.85, evidence

        # 检查 REIT
        reit_keywords = VE4_IDENTIFIER_TYPE_RULES[VE4HoldingType.REIT]["keywords"]
        if any(kw in name for kw in reit_keywords):
            evidence.append("名称含REIT关键词")
            return VE4HoldingType.REIT, 0.85, evidence

        # 检查债券
        bond_keywords = VE4_IDENTIFIER_TYPE_RULES[VE4HoldingType.BOND]["keywords"]
        if any(kw in name for kw in bond_keywords) or asset_class == "fixed_income":
            evidence.append(f"名称含债券关键词: {[kw for kw in bond_keywords if kw in name]}")
            return VE4HoldingType.BOND, 0.80, evidence

        # 检查商品
        commodity_keywords = VE4_IDENTIFIER_TYPE_RULES[VE4HoldingType.COMMODITY]["keywords"]
        if any(kw in name for kw in commodity_keywords):
            evidence.append("名称含商品/贵金属关键词")
            return VE4HoldingType.COMMODITY, 0.75, evidence

        # 检查基金（排除债券基金）
        fund_keywords = VE4_IDENTIFIER_TYPE_RULES[VE4HoldingType.FUND]["keywords"]
        if any(kw in name for kw in fund_keywords):
            # 进一步判断是股基还是债基
            if any(kw in name for kw in ["债券", "债基", "纯债", "短债", "中短债", "长债", "可转债"]):
                evidence.append("名称含债券基金关键词")
                return VE4HoldingType.BOND, 0.80, evidence
            evidence.append("名称含基金关键词")
            return VE4HoldingType.FUND, 0.75, evidence

        # 检查股票
        stock_keywords = VE4_IDENTIFIER_TYPE_RULES[VE4HoldingType.STOCK]["keywords"]
        if any(kw in name for kw in stock_keywords) or asset_class == "equity":
            evidence.append("名称含股票关键词或asset_class=equity")
            return VE4HoldingType.STOCK, 0.70, evidence

        # 默认回退
        if asset_class == "equity":
            evidence.append("asset_class=equity，默认归为股票")
            return VE4HoldingType.STOCK, 0.60, evidence
        if asset_class == "fixed_income":
            evidence.append("asset_class=fixed_income，默认归为债券")
            return VE4HoldingType.BOND, 0.60, evidence

        evidence.append("无法精确识别，默认归为基金")
        return VE4HoldingType.FUND, 0.50, evidence

    def _determine_sector(self, name: str) -> str:
        """判断行业"""
        for sector, keywords in VE4_IDENTIFIER_SECTOR_RULES.items():
            if any(kw in name for kw in keywords):
                return sector
        return "其他"

    def _determine_market(self, name: str) -> str:
        """判断市场"""
        for market, keywords in VE4_IDENTIFIER_MARKET_RULES.items():
            if any(kw in name for kw in keywords):
                return market
        return "A股"  # 默认A股

    # ── 序列化 ──

    @staticmethod
    def _identifier_to_dict(ident: VE4InvestmentIdentifier) -> dict:
        return {
            "name": ident.name,
            "type": ident.holding_type.value,
            "confidence": ident.confidence,
            "sector": ident.sector,
            "market": ident.market,
            "evidence": ident.evidence,
        }
