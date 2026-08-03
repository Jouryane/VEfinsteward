"""
VE5 资产分类引擎（模块2核心）
=============================
从模块1产出的 OCR 文本和交易记录中，提取资产持仓信息并分类。

职责：
    1. 关键词匹配 → 资产大类 / 流动性 / 风险等级
    2. OCR 字段提取 → 持有收益率 / 成本 / 市值 / 买入日期
    3. 可行度评分 → confidence < 0.5 归入 unclassified
    4. 数据持久化 → asset_holdings 表
    5. 轻量模型辅助 → 未知产品名称调本地模型判断

命名规范：
    - 类: VE4AssetClassifier
    - 函数: ve4_asset_{功能}
    - 常量: VE4_ASSET_{名称}

用法：
    from asset_classifier import VE4AssetClassifier
    classifier = VE4AssetClassifier()
    classifier.classify_and_save(account_key, product_name, raw_text, source_file)
"""

import os
import re
import json
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

from .config import ensure_dirs
from core.asset_classification_rules import (
    VE4_ALLOC_RULES, VE4_GOLD_RULES, VE4_LEGACY_TO_FOUR_LEVEL,
    ve4_alloc_rules_classify, ve4_alloc_rules_legacy_convert,
)

logger = logging.getLogger("ve4.asset_classifier")

# ─── 数据库路径（与 api_server 共用）───
from app_paths import DB_PATH


# ─── 资产分类关键词字典（从统一规则导入）───

VE4_ASSET_KEYWORDS = {
    "cash": {
        "keywords": VE4_ALLOC_RULES["liquid"]["keywords"],
        "icon": "现",
        "color": "emerald",
        "default_liquidity": "high",
        "default_risk": "low",
    },
    "fixed_income": {
        "keywords": VE4_ALLOC_RULES["stable"]["keywords"],
        "icon": "债",
        "color": "cyan",
        "default_liquidity": "medium",
        "default_risk": "medium_low",
    },
    "equity": {
        "keywords": VE4_ALLOC_RULES["aggressive"]["keywords"],
        "icon": "股",
        "color": "indigo",
        "default_liquidity": "high",
        "default_risk": "medium_high",
    },
    "alternative": {
        "keywords": VE4_ALLOC_RULES["protection"]["keywords"],
        "icon": "另",
        "color": "amber",
        "default_liquidity": "low",
        "default_risk": "high",
    },
}

# 流动性关键词覆盖（覆盖默认设定）
VE4_LIQUIDITY_KEYWORDS = {
    "high":   ["活期", "T+0", "货币基金", "余额宝", "上市", "股票", "ETF", "场内"],
    "medium": ["开放式", "T+1", "短债", "7天", "14天", "月度", "季度开放"],
    "low":    ["封闭", "定期", "私募", "信托", "3年", "5年", "锁定", "持有期", "不可赎回"],
}

# 风险关键词覆盖（覆盖默认设定）
VE4_RISK_KEYWORDS = {
    "low":        ["存款", "国债", "货币基金", "保本", "R1", "PR1", "低风险"],
    "medium_low": ["纯债", "短债", "R2", "PR2", "中低风险", "固收"],
    "medium_high":["混合", "指数", "可转债", "R3", "PR3", "中风险"],
    "high":       ["股票", "偏股", "QDII", "私募", "期货", "期权", "R4", "R5", "PR4", "PR5", "高风险"],
}


# ─── 数据模型 ───

@dataclass
class VE4AssetClassification:
    """单次分类结果"""
    asset_class: str = "unclassified"
    liquidity_level: str = "unknown"
    risk_level: str = "unknown"
    confidence: float = 0.0
    inference_source: str = ""
    reason: str = ""


@dataclass
class VE4ExtractedHolding:
    """从 OCR 提取的持仓字段"""
    product_name: str = ""
    product_code: str = ""
    holding_quantity: float = 0.0
    cost_basis: float = 0.0
    current_value: float = 0.0
    unrealized_pnl: float = 0.0
    holding_return_pct: float = 0.0
    annualized_return_pct: float = 0.0
    purchase_date: str = ""
    holding_days: int = 0


# ─── 分类引擎 ───

class VE4AssetClassifier:
    """资产分类引擎"""

    def __init__(self):
        self._ensure_db()

    # ── 数据库 ──

    def _ensure_db(self):
        """确保 asset_holdings 表存在"""
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS asset_holdings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_key TEXT,
                source_file TEXT,
                product_name TEXT,
                product_code TEXT,
                asset_class TEXT DEFAULT 'unclassified',
                liquidity_level TEXT DEFAULT 'unknown',
                risk_level TEXT DEFAULT 'unknown',
                is_classified BOOLEAN DEFAULT 0,
                classification_confidence REAL DEFAULT 0,
                holding_quantity REAL,
                cost_basis REAL,
                current_value REAL,
                unrealized_pnl REAL,
                holding_return_pct REAL,
                annualized_return_pct REAL,
                purchase_date TEXT,
                holding_days INTEGER,
                inference_source TEXT,
                user_overridden BOOLEAN DEFAULT 0,
                user_note TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_asset_account ON asset_holdings(account_key)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_asset_class ON asset_holdings(asset_class)
        """)
        conn.commit()
        conn.close()

    # ── 主入口 ──

    def ve4_asset_classify_and_save(self, account_key: str, product_name: str,
                                    raw_text: str, source_file: str,
                                    account_type: str = "") -> Dict:
        """
        主入口：对产品名称和 OCR 文本做分类 + 字段提取，保存到数据库。

        Returns:
            {"classification": VE4AssetClassification, "holding": VE4ExtractedHolding, "saved": bool}
        """
        # Step 1: 关键词分类
        classification = self.ve4_asset_classify_by_keywords(product_name, raw_text)

        # Step 2: 如果关键词无法判断，调轻量模型辅助
        if classification.confidence < 0.5:
            model_result = self.ve4_asset_classify_by_model(product_name, raw_text)
            if model_result.confidence > classification.confidence:
                classification = model_result

        # Step 3: 从 OCR 提取持仓字段
        holding = self.ve4_asset_extract_fields(raw_text, product_name)
        holding.source_file = source_file  # 额外绑定

        # Step 4: 保存
        saved = self.ve4_asset_save_to_db(account_key, source_file, holding, classification)

        logger.info(f"[ASSET] 分类完成：{product_name} → {classification.asset_class} "
                    f"(置信度:{classification.confidence:.0%})")
        return {
            "classification": classification,
            "holding": holding,
            "saved": saved,
        }

    # ── L1: 关键词分类 ──

    def ve4_asset_classify_by_keywords(self, product_name: str, raw_text: str) -> VE4AssetClassification:
        """基于关键词字典分类，返回可行度评分"""
        text = f"{product_name} {raw_text}"
        scores = {}

        # 资产大类打分
        for asset_class, config in VE4_ASSET_KEYWORDS.items():
            score = 0.0
            for kw in config["keywords"]:
                if kw in text:
                    score += 0.3  # 每个关键词 +0.3
                    if kw in product_name:
                        score += 0.2  # 在名称中额外 +0.2
            scores[asset_class] = min(score, 1.0)

        # 取最高分
        if not scores or max(scores.values()) < 0.3:
            return VE4AssetClassification(
                asset_class="unclassified",
                confidence=0.0,
                inference_source="keyword",
                reason="无匹配关键词",
            )

        best_class = max(scores, key=scores.get)
        best_score = scores[best_class]
        config = VE4_ASSET_KEYWORDS[best_class]

        # 流动性覆盖
        liquidity = config["default_liquidity"]
        for level, kws in VE4_LIQUIDITY_KEYWORDS.items():
            if any(kw in text for kw in kws):
                liquidity = level
                break

        # 风险覆盖
        risk = config["default_risk"]
        for level, kws in VE4_RISK_KEYWORDS.items():
            if any(kw in text for kw in kws):
                risk = level
                break

        return VE4AssetClassification(
            asset_class=best_class,
            liquidity_level=liquidity,
            risk_level=risk,
            confidence=best_score,
            inference_source="keyword",
            reason=f"关键词匹配：{best_class}，得分{best_score:.1f}",
        )

    # ── L1+：模型辅助分类 ──

    def ve4_asset_classify_by_model(self, product_name: str, raw_text: str) -> VE4AssetClassification:
        """
        调本地轻量模型判断未知产品。
        限制：只问分类，不问详情，token < 15。
        """
        try:
            from .model_client import ve4_model_get_client
            client = ve4_model_get_client()

            question = (f"产品名称：{product_name[:30]}。"
                        "这是哪类资产？只选一个：现金、债券理财、股票基金、另类、未知")
            choice = client.ask_choice(question, ["现金", "债券理财", "股票基金", "另类", "未知"])

            mapping = {
                "现金": ("cash", "high", "low"),
                "债券理财": ("fixed_income", "medium", "medium_low"),
                "股票基金": ("equity", "high", "medium_high"),
                "另类": ("alternative", "low", "high"),
            }

            if choice and choice in mapping:
                asset_class, liq, risk = mapping[choice]
                return VE4AssetClassification(
                    asset_class=asset_class,
                    liquidity_level=liq,
                    risk_level=risk,
                    confidence=0.6,  # 模型结果给中等置信度
                    inference_source="model",
                    reason=f"本地模型判断：{choice}",
                )
        except Exception as e:
            logger.debug(f"[ASSET] 模型辅助分类失败：{e}")

        return VE4AssetClassification(confidence=0.0, inference_source="model")

    # ── L2: OCR 字段提取 ──

    def ve4_asset_extract_fields(self, raw_text: str, product_name: str = "") -> VE4ExtractedHolding:
        """
        从 OCR 文本中提取金融字段。
        策略：正则匹配常见的中文金融报表字段。
        """
        holding = VE4ExtractedHolding(product_name=product_name)

        # 产品代码：6位数字（股票/基金）或字母数字组合
        code_match = re.search(r'[（(](\d{6})[)）]|代码[：:]\s*(\w+)', raw_text)
        if code_match:
            holding.product_code = code_match.group(1) or code_match.group(2)

        # 持有数量/份额
        qty_patterns = [
            r'持有数量[：:]\s*([\d,]+\.?\d*)',
            r'持仓[：:]\s*([\d,]+\.?\d*)',
            r'持有份额[：:]\s*([\d,]+\.?\d*)',
            r'([\d,]+\.?\d*)\s*股',
            r'([\d,]+\.?\d*)\s*份',
        ]
        for pat in qty_patterns:
            m = re.search(pat, raw_text)
            if m:
                holding.holding_quantity = self._parse_float(m.group(1))
                break

        # 成本 / 成本价 / 成本净值
        cost_patterns = [
            r'成本[价净值]*[：:]\s*¥?\s*([\d,]+\.?\d*)',
            r'持仓成本[：:]\s*¥?\s*([\d,]+\.?\d*)',
            r'买入均价[：:]\s*¥?\s*([\d,]+\.?\d*)',
        ]
        for pat in cost_patterns:
            m = re.search(pat, raw_text)
            if m:
                holding.cost_basis = self._parse_float(m.group(1))
                break

        # 当前市值 / 当前净值 / 最新市值
        value_patterns = [
            r'当前市值[：:]\s*¥?\s*([\d,]+\.?\d*)',
            r'最新市值[：:]\s*¥?\s*([\d,]+\.?\d*)',
            r'资产[：:]\s*¥?\s*([\d,]+\.?\d*)',
            r'最新净值[：:]\s*([\d.]+)',
        ]
        for pat in value_patterns:
            m = re.search(pat, raw_text)
            if m:
                holding.current_value = self._parse_float(m.group(1))
                break

        # 持有收益率 / 盈亏比例
        return_patterns = [
            r'持有收益率[：:]\s*([+-]?\d+\.?\d*)%?',
            r'盈亏比例[：:]\s*([+-]?\d+\.?\d*)%?',
            r'收益率[：:]\s*([+-]?\d+\.?\d*)%?',
            r'盈亏[：:]\s*([+-]?\d+\.?\d*)%?',
        ]
        for pat in return_patterns:
            m = re.search(pat, raw_text)
            if m:
                holding.holding_return_pct = self._parse_float(m.group(1))
                break

        # 年化收益率
        annual_match = re.search(r'年化[收益]*[：:]\s*([+-]?\d+\.?\d*)%?', raw_text)
        if annual_match:
            holding.annualized_return_pct = self._parse_float(annual_match.group(1))

        # 买入日期
        date_patterns = [
            r'买入日期[：:]\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2})',
            r'购入日期[：:]\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2})',
            r'成交日期[：:]\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2})',
        ]
        for pat in date_patterns:
            m = re.search(pat, raw_text)
            if m:
                holding.purchase_date = self._normalize_date(m.group(1))
                break

        # 持有天数
        days_match = re.search(r'持有[天数]*[：:]\s*(\d+)\s*天', raw_text)
        if days_match:
            holding.holding_days = int(days_match.group(1))
        elif holding.purchase_date:
            try:
                pd = datetime.strptime(holding.purchase_date, "%Y-%m-%d")
                holding.holding_days = (datetime.now() - pd).days
            except Exception:
                pass

        # 盈亏金额
        pnl_patterns = [
            r'盈亏[金额]*[：:]\s*¥?\s*([+-]?[\d,]+\.?\d*)',
            r'收益[：:]\s*¥?\s*([+-]?[\d,]+\.?\d*)',
        ]
        for pat in pnl_patterns:
            m = re.search(pat, raw_text)
            if m:
                holding.unrealized_pnl = self._parse_float(m.group(1))
                break

        # 如果从成本+市值推算收益率
        if holding.holding_return_pct == 0 and holding.cost_basis > 0 and holding.current_value > 0:
            if holding.holding_quantity > 0:
                total_cost = holding.cost_basis * holding.holding_quantity
                holding.holding_return_pct = round((holding.current_value - total_cost) / total_cost * 100, 2)
            else:
                holding.holding_return_pct = round((holding.current_value - holding.cost_basis) / holding.cost_basis * 100, 2)

        return holding

    # ── 数据库写入 ──

    def ve4_asset_save_to_db(self, account_key: str, source_file: str,
                             holding: VE4ExtractedHolding,
                             classification: VE4AssetClassification) -> bool:
        """保存分类结果到 asset_holdings"""
        try:
            conn = sqlite3.connect(str(DB_PATH))
            now = datetime.now().isoformat()
            is_classified = 1 if classification.confidence >= 0.5 else 0

            conn.execute("""
                INSERT INTO asset_holdings (
                    account_key, source_file, product_name, product_code,
                    asset_class, liquidity_level, risk_level,
                    is_classified, classification_confidence,
                    holding_quantity, cost_basis, current_value,
                    unrealized_pnl, holding_return_pct, annualized_return_pct,
                    purchase_date, holding_days,
                    inference_source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
            """, (
                account_key, source_file, holding.product_name, holding.product_code,
                classification.asset_class, classification.liquidity_level, classification.risk_level,
                is_classified, classification.confidence,
                holding.holding_quantity, holding.cost_basis, holding.current_value,
                holding.unrealized_pnl, holding.holding_return_pct, holding.annualized_return_pct,
                holding.purchase_date, holding.holding_days,
                classification.inference_source, now,
            ))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.warning(f"[ASSET] 保存失败：{e}")
            return False

    # ── 聚合查询（供 API 层调用）──

    def ve4_asset_get_classified_summary(self) -> Dict:
        """获取已分类资产的聚合摘要
        
        修复：1. 添加 current_value > 0 过滤（排除负值脏数据）
              2. 应用 VE4_LEGACY_TO_FOUR_LEVEL 映射（alternative→protection）
        """
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row

        # 按资产大类聚合（过滤负值，避免"另类 -840"类 bug）
        rows = conn.execute("""
            SELECT asset_class,
                   SUM(current_value) as total_value,
                   COUNT(*) as count,
                   AVG(holding_return_pct) as avg_return
            FROM asset_holdings
            WHERE is_classified = 1 AND current_value > 0
            GROUP BY asset_class
        """).fetchall()

        classified = {}
        for r in rows:
            ac = r["asset_class"] or ""
            # 应用 legacy → 四级映射（alternative→protection, equity→aggressive 等）
            mapped_ac = VE4_LEGACY_TO_FOUR_LEVEL.get(ac, ac)
            if mapped_ac in classified:
                classified[mapped_ac]["value"] += r["total_value"] or 0
                classified[mapped_ac]["count"] += r["count"]
            else:
                classified[mapped_ac] = {
                    "value": r["total_value"] or 0,
                    "count": r["count"],
                    "avg_return": round(r["avg_return"] or 0, 2),
                }

        # 未分类资产（同样过滤负值）
        unclassified_rows = conn.execute("""
            SELECT SUM(current_value) as total_value, COUNT(*) as count
            FROM asset_holdings
            WHERE is_classified = 0 AND current_value > 0
        """).fetchone()

        unclassified = {
            "value": unclassified_rows["total_value"] or 0,
            "count": unclassified_rows["count"],
        }

        conn.close()
        return {"classified": classified, "unclassified": unclassified}

    def ve4_asset_get_unclassified_list(self, limit: int = 20) -> List[Dict]:
        """获取未分类资产清单（供用户补全）"""
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT id, account_key, product_name, current_value, source_file
            FROM asset_holdings
            WHERE is_classified = 0
            ORDER BY current_value DESC
            LIMIT ?
        """, (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ── 工具 ──

    @staticmethod
    def _parse_float(s: str) -> float:
        if not s:
            return 0.0
        try:
            return float(str(s).replace(",", "").replace("¥", "").replace("￥", "").strip())
        except ValueError:
            return 0.0

    @staticmethod
    def _normalize_date(s: str) -> str:
        """统一日期格式为 YYYY-MM-DD"""
        try:
            s = s.replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-")
            parts = s.split("-")
            if len(parts) == 3:
                y, m, d = parts
                return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        except Exception:
            pass
        return ""


# ─── 便捷函数（供 pipeline 调用）──

def ve4_asset_classify_from_pipeline(account_key: str, product_name: str,
                                      raw_text: str, source_file: str) -> Dict:
    """pipeline 后置钩子调用入口"""
    classifier = VE4AssetClassifier()
    return classifier.ve4_asset_classify_and_save(account_key, product_name, raw_text, source_file)
