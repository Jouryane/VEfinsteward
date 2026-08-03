"""
VE4 资产配置当前分步模块
===========================
四级分类引擎：流动类 / 进取类 / 稳健类 / 保障类

职责：
    1. 从 asset_holdings 读取持仓，按产品名称和类别映射到四级
    2. 流动类展开：日常备用金（从 transactions 消费支出反推）+ 其它备用金
    3. 不确定的产品通过 ai_gateway 辅助判断（可选）
    4. 返回结构化数据供前端展示

命名规范：
    - 函数: ve4_alloc_detail_{功能}
    - 类: VE4AllocationDetail{模块名}
"""

import sqlite3
import json
import logging
from pathlib import Path
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from core.asset_classification_rules import (
    VE4_ALLOC_RULES, VE4_LEGACY_TO_FOUR_LEVEL,
    ve4_alloc_rules_classify, ve4_alloc_rules_legacy_convert,
)

logger = logging.getLogger("ve5.alloc_detail")

from app_paths import DB_PATH, DATA_DIR

# 用户设定的生活必需流动资金存储路径
_ESSENTIAL_LIQUID_CONFIG_PATH = DATA_DIR / "essential_liquid_config.json"


# ════════════════════════════════════════════════════════════════
# 数据模型
# ════════════════════════════════════════════════════════════════

@dataclass
class VE4HoldingItem:
    """单条持仓明细"""
    id: int = 0
    account_key: str = ""
    product_name: str = ""
    product_code: str = ""
    current_value: float = 0.0
    cost_basis: float = 0.0
    holding_return_pct: float = 0.0
    annualized_return_pct: float = 0.0
    purchase_date: str = ""
    holding_days: int = 0
    source_file: str = ""
    asset_class: str = ""  # 原始分类（cash/equity/fixed_income/alternative）
    user_note: str = ""


@dataclass
class VE4CategoryGroup:
    """单一大类分组"""
    key: str = ""               # liquid / aggressive / stable / protection
    label: str = ""             # 中文名
    total_value: float = 0.0
    total_pct: float = 0.0
    count: int = 0
    color: str = ""
    items: List[VE4HoldingItem] = field(default_factory=list)
    # 流动类展开子项
    sub_breakdown: Dict = field(default_factory=dict)


@dataclass
class VE4LiquidBreakdown:
    """流动类展开"""
    total_liquid: float = 0.0          # 流动类总额
    daily_reserve: float = 0.0         # 生活必需流动资金（必需消费×N 或用户设定）
    daily_reserve_pct: float = 0.0     # 占流动类比例
    other_reserve: float = 0.0         # 其它流动资金（流动类总额 - 生活必需流动资金）
    other_reserve_pct: float = 0.0     # 占流动类比例
    daily_reserve_source: str = ""     # 数据来源说明
    emergency_months: int = 3
    monthly_expense: float = 0.0       # 月均总消费（参考）
    monthly_essential_expense: float = 0.0  # 月均必需消费（核心）
    essential_liquid_method: str = ""  # 计算方式：auto_3month / auto_single / user_manual
    user_essential_liquid: float = 0.0 # 用户手动设定的生活必需流动资金


@dataclass
class VE4AllocationDetailReport:
    """四级分类报告"""
    total_assets: float = 0.0
    total_classified: float = 0.0
    unclassified_value: float = 0.0
    unclassified_pct: float = 0.0
    groups: List[VE4CategoryGroup] = field(default_factory=list)
    liquid_breakdown: VE4LiquidBreakdown = field(default_factory=VE4LiquidBreakdown)
    generated_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ════════════════════════════════════════════════════════════════
# 分类规则（从统一规则导入）
# ════════════════════════════════════════════════════════════════

VE4_ALLOC_DETAIL_RULES = {
    "liquid": {
        "label": VE4_ALLOC_RULES["liquid"]["label"],
        "color": VE4_ALLOC_RULES["liquid"]["color"],
        "keywords": VE4_ALLOC_RULES["liquid"]["keywords"],
        "asset_classes": ["cash", "liquid"],
        "exclude_keywords": VE4_ALLOC_RULES["liquid"]["exclude_keywords"],
    },
    "aggressive": {
        "label": VE4_ALLOC_RULES["aggressive"]["label"],
        "color": VE4_ALLOC_RULES["aggressive"]["color"],
        "keywords": VE4_ALLOC_RULES["aggressive"]["keywords"],
        "asset_classes": ["equity", "aggressive"],
        "exclude_keywords": VE4_ALLOC_RULES["aggressive"]["exclude_keywords"],
    },
    "stable": {
        "label": VE4_ALLOC_RULES["stable"]["label"],
        "color": VE4_ALLOC_RULES["stable"]["color"],
        "keywords": VE4_ALLOC_RULES["stable"]["keywords"],
        "asset_classes": ["fixed_income", "stable"],
        "exclude_keywords": VE4_ALLOC_RULES["stable"]["exclude_keywords"],
    },
    "protection": {
        "label": VE4_ALLOC_RULES["protection"]["label"],
        "color": VE4_ALLOC_RULES["protection"]["color"],
        "keywords": VE4_ALLOC_RULES["protection"]["keywords"],
        "asset_classes": ["alternative", "protection"],
        "exclude_keywords": VE4_ALLOC_RULES["protection"]["exclude_keywords"],
    },
}


class VE4AllocationDetailEngine:
    """资产配置四级分类引擎"""

    def __init__(self):
        self.db_path = DB_PATH

    # ──────────────────────────────────────────
    # 主入口
    # ──────────────────────────────────────────

    def ve4_alloc_detail_generate(self, emergency_months: int = 3,
                                   use_ai: bool = False) -> VE4AllocationDetailReport:
        """生成四级分类报告"""
        logger.info("[ALLOC-DETAIL] 开始生成四级分类报告")
        start = datetime.now()

        # Step 1: 加载所有持仓
        holdings = self._load_all_holdings()

        # Step 2: 四级分类
        classified = self._classify_holdings(holdings, use_ai=use_ai)

        # Step 3: 聚合分组
        groups = self._aggregate_groups(classified)

        # Step 4: 流动类展开
        liquid_breakdown = self._compute_liquid_breakdown(
            groups.get("liquid", VE4CategoryGroup()), emergency_months
        )

        # Step 5: 汇总
        total_assets = sum(h.current_value for h in holdings)
        total_classified = sum(g.total_value for g in groups.values())

        # 排序 groups（按价值降序）
        group_list = sorted(groups.values(), key=lambda g: g.total_value, reverse=True)

        for g in group_list:
            g.total_pct = round(g.total_value / total_classified * 100, 1) if total_classified > 0 else 0

        elapsed = (datetime.now() - start).total_seconds()
        logger.info(f"[ALLOC-DETAIL] 报告生成完成（耗时 {elapsed:.2f}s）")

        return VE4AllocationDetailReport(
            total_assets=round(total_assets, 2),
            total_classified=round(total_classified, 2),
            unclassified_value=0.0,
            unclassified_pct=0.0,
            groups=group_list,
            liquid_breakdown=liquid_breakdown,
            generated_at=datetime.now().isoformat(),
        )

    # ──────────────────────────────────────────
    # 数据加载
    # ──────────────────────────────────────────

    def _load_all_holdings(self) -> List[VE4HoldingItem]:
        """从数据库加载所有已分类持仓"""
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            # 列存在性检查，避免旧表结构报错
            cols = [r[1] for r in conn.execute("PRAGMA table_info(asset_holdings)").fetchall()]
            has_holding_days = "holding_days" in cols
            has_purchase_date = "purchase_date" in cols
            has_annualized = "annualized_return_pct" in cols
            has_user_note = "user_note" in cols
            has_superseded = "is_superseded" in cols

            select_cols = [
                "id", "account_key", "product_name", "product_code", "current_value",
                "cost_basis", "holding_return_pct", "source_file", "asset_class",
            ]
            if has_annualized:
                select_cols.append("annualized_return_pct")
            if has_purchase_date:
                select_cols.append("purchase_date")
            if has_holding_days:
                select_cols.append("holding_days")
            if has_user_note:
                select_cols.append("user_note")

            # 动态构建 WHERE 条件：兼容有无 is_superseded 列
            where_clause = "current_value > 0 AND product_name NOT IN ('ETF', '证券市值', '总资产')"
            if has_superseded:
                where_clause += " AND is_superseded=0"

            rows = conn.execute(
                f"""SELECT {', '.join(select_cols)} FROM asset_holdings
                    WHERE {where_clause}
                    ORDER BY current_value DESC"""
            ).fetchall()
            conn.close()

            items = []
            for row in rows:
                items.append(VE4HoldingItem(
                    id=row["id"],
                    account_key=row["account_key"] or "",
                    product_name=row["product_name"] or "",
                    product_code=row["product_code"] or "",
                    current_value=round(row["current_value"] or 0, 2),
                    cost_basis=round(row["cost_basis"] or 0, 2),
                    holding_return_pct=round(row["holding_return_pct"] or 0, 2),
                    annualized_return_pct=round(row["annualized_return_pct"] or 0, 2) if has_annualized else 0.0,
                    purchase_date=row["purchase_date"] or "" if has_purchase_date else "",
                    holding_days=row["holding_days"] or 0 if has_holding_days else 0,
                    source_file=row["source_file"] or "",
                    asset_class=row["asset_class"] or "",
                    user_note=row["user_note"] or "" if has_user_note else "",
                ))
            return items
        except Exception as e:
            logger.warning(f"[ALLOC-DETAIL] 加载持仓失败：{e}")
            return []

    # ──────────────────────────────────────────
    # 四级分类逻辑
    # ──────────────────────────────────────────

    def _classify_holdings(self, holdings: List[VE4HoldingItem],
                            use_ai: bool = False) -> Dict[str, List[VE4HoldingItem]]:
        """
        将持仓分配到四级分类。
        规则：纯代码关键词优先，不确定时可选 AI 辅助。
        """
        result = {"liquid": [], "aggressive": [], "stable": [], "protection": []}

        for item in holdings:
            cat = self._determine_category(item, use_ai)
            result[cat].append(item)

        return result

    def _determine_category(self, item: VE4HoldingItem, use_ai: bool = False) -> str:
        """单条持仓分类判断"""
        name = (item.product_name or "").lower()
        original_class = item.asset_class or ""

        # 优先级1：保障类关键词（黄金/保险等非常确定）
        prot_kw = VE4_ALLOC_DETAIL_RULES["protection"]["keywords"]
        if any(kw in name for kw in prot_kw):
            return "protection"

        # 优先级2：流动类关键词（现金类产品非常确定）
        liq_kw = VE4_ALLOC_DETAIL_RULES["liquid"]["keywords"]
        if any(kw in name for kw in liq_kw) or original_class == "cash":
            return "liquid"

        # 优先级3：稳健类关键词（债券等）
        stab_kw = VE4_ALLOC_DETAIL_RULES["stable"]["keywords"]
        if any(kw in name for kw in stab_kw) or original_class == "fixed_income":
            return "stable"

        # 优先级4：进取类排除稳健类
        agg_kw = VE4_ALLOC_DETAIL_RULES["aggressive"]["keywords"]
        exc_kw = VE4_ALLOC_DETAIL_RULES["aggressive"].get("exclude_keywords", [])

        # 检查进取类关键词
        is_aggressive = any(kw in name for kw in agg_kw) or original_class == "equity"
        # 排除稳健类
        is_stable = any(kw in name for kw in exc_kw)

        if is_aggressive and not is_stable:
            return "aggressive"

        # 默认回退：equity 默认进取类，alternative 默认保障类
        if original_class == "equity":
            return "aggressive"
        if original_class == "alternative":
            return "protection"

        # 最后回退：如果仍不确定，用 AI 辅助（如果启用）
        if use_ai:
            ai_cat = self._ai_assist_category(item)
            if ai_cat:
                return ai_cat

        return "aggressive"  # 最不坏的默认

    def _ai_assist_category(self, item: VE4HoldingItem) -> Optional[str]:
        """AI 辅助判断产品类型"""
        try:
            from core.ai_gateway import ve4_ai_ask_choice
            question = f"产品名称：'{item.product_name}'，原始分类：{item.asset_class}。请问这个产品属于哪一类投资？"
            choices = ["流动类", "进取类", "稳健类", "保障类"]
            result = ve4_ai_ask_choice(
                question=question,
                choices=choices,
                task_type="asset_classification",
                contains_privacy=True,
            )
            mapping = {"流动类": "liquid", "进取类": "aggressive",
                       "稳健类": "stable", "保障类": "protection"}
            return mapping.get(result)
        except Exception as e:
            logger.debug(f"[ALLOC-DETAIL] AI 辅助分类失败：{e}")
            return None

    # ──────────────────────────────────────────
    # 聚合分组
    # ──────────────────────────────────────────

    def _aggregate_groups(self, classified: Dict[str, List[VE4HoldingItem]]) -> Dict[str, VE4CategoryGroup]:
        """按四级聚合"""
        groups = {}
        for key, items in classified.items():
            rule = VE4_ALLOC_DETAIL_RULES[key]
            total = sum(i.current_value for i in items)
            groups[key] = VE4CategoryGroup(
                key=key,
                label=rule["label"],
                total_value=round(total, 2),
                total_pct=0.0,
                count=len(items),
                color=rule["color"],
                items=items,
            )
        return groups

    # ──────────────────────────────────────────
    # 流动类展开
    # ──────────────────────────────────────────

    def _compute_liquid_breakdown(self, liquid_group: VE4CategoryGroup,
                                   emergency_months: int) -> VE4LiquidBreakdown:
        """
        流动类展开 — 生活必需流动资金计算。

        优先级：
          1. 用户手动设定（user_manual）
          2. 近N个月必需消费均值 × 3（auto_3month，N=有效月数，至少取3倍）
          3. 单月必需消费 × 3（auto_single，兜底）

        生活必需流动资金 = 必需消费相关计算结果（不是总消费）
        """
        total_liquid = liquid_group.total_value
        monthly_expense, monthly_essential = self._compute_monthly_expense()

        # ── 三级优先级计算 ──
        user_essential_liquid = self._load_user_essential_liquid()
        essential_liquid = 0.0
        method = ""
        source = ""

        if user_essential_liquid > 0:
            # 优先级1：用户手动设定
            essential_liquid = user_essential_liquid
            method = "user_manual"
            source = f"用户手动设定 ¥{essential_liquid:,.0f}"
        elif monthly_essential > 0:
            # 优先级2 & 3：自动计算
            # 尝试取近3个月的必需消费均值
            try:
                cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
                conn = sqlite3.connect(str(self.db_path))
                conn.row_factory = sqlite3.Row
                row3m = conn.execute("""
                    SELECT SUM(amount) as total,
                           COUNT(DISTINCT strftime('%Y-%m', transaction_date)) as months
                    FROM transactions
                    WHERE transaction_type = 'expense'
                      AND is_essential = 1
                      AND transaction_date >= ?
                """, (cutoff,)).fetchone()
                conn.close()

                total_3m = row3m["total"] or 0
                months_3m = max(1, row3m["months"] or 1)
                avg_3m = total_3m / months_3m

                if months_3m >= 3:
                    # 优先级2：3个月均值 × 3
                    essential_liquid = avg_3m * 3
                    method = "auto_3month"
                    source = (f"近{months_3m}个月必需消费均值 ¥{avg_3m:,.0f}/月 × 3 = ¥{essential_liquid:,.0f}")
                else:
                    # 优先级3：单月必需消费 × 3
                    essential_liquid = monthly_essential * 3
                    method = "auto_single"
                    source = f"必需消费 ¥{monthly_essential:,.0f}/月 × 3 = ¥{essential_liquid:,.0f}"
            except Exception:
                essential_liquid = monthly_essential * 3
                method = "auto_single"
                source = f"必需消费 ¥{monthly_essential:,.0f}/月 × 3 = ¥{essential_liquid:,.0f}"
        else:
            source = "暂无消费记录，无法推算生活必需流动资金"

        # 生活必需流动资金不能超过流动类总额
        if essential_liquid > total_liquid:
            essential_liquid = total_liquid

        other_reserve = max(0, total_liquid - essential_liquid)

        return VE4LiquidBreakdown(
            total_liquid=round(total_liquid, 2),
            daily_reserve=round(essential_liquid, 2),
            daily_reserve_pct=round(essential_liquid / total_liquid * 100, 1) if total_liquid > 0 else 0,
            other_reserve=round(other_reserve, 2),
            other_reserve_pct=round(other_reserve / total_liquid * 100, 1) if total_liquid > 0 else 0,
            daily_reserve_source=source,
            emergency_months=emergency_months,
            monthly_expense=monthly_expense,
            monthly_essential_expense=monthly_essential,
            essential_liquid_method=method,
            user_essential_liquid=user_essential_liquid,
        )

    def _compute_monthly_expense(self) -> Tuple[float, float]:
        """从 transactions 表计算月均支出和月均必需消费。

        返回: (月均总消费, 月均必需消费)
        查询全部数据（不限制时间范围），兼容历史截图。
        """
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row

            # 月均总消费
            row = conn.execute("""
                SELECT SUM(amount) as total,
                       COUNT(DISTINCT strftime('%Y-%m', transaction_date)) as months
                FROM transactions
                WHERE transaction_type = 'expense'
            """).fetchone()
            total = row["total"] or 0
            months = max(1, row["months"] or 1)
            monthly_total = round(total / months, 2)

            # 月均必需消费（优先 is_essential 字段，回退到分类映射）
            ess_row = conn.execute("""
                SELECT SUM(amount) as total
                FROM transactions
                WHERE transaction_type = 'expense'
                  AND is_essential = 1
            """).fetchone()
            essential_total = ess_row["total"] or 0

            if essential_total == 0:
                # 回退：用必需分类映射
                essential_cats = {"餐饮", "交通", "日用", "居住", "月供", "医疗", "通讯", "教育"}
                placeholders = ",".join("?" for _ in essential_cats)
                ess_row2 = conn.execute(f"""
                    SELECT SUM(amount) as total
                    FROM transactions
                    WHERE transaction_type = 'expense'
                      AND category_primary IN ({placeholders})
                """, (*essential_cats,)).fetchone()
                essential_total = ess_row2["total"] or 0

            monthly_essential = round(essential_total / months, 2)
            conn.close()
            return monthly_total, monthly_essential
        except Exception as e:
            logger.debug(f"[ALLOC-DETAIL] 月均支出计算失败：{e}")
            return 0.0, 0.0

    def _load_user_essential_liquid(self) -> float:
        """读取用户手动设定的生活必需流动资金"""
        try:
            if _ESSENTIAL_LIQUID_CONFIG_PATH.exists():
                data = json.loads(_ESSENTIAL_LIQUID_CONFIG_PATH.read_text(encoding="utf-8"))
                val = float(data.get("essential_liquid", 0))
                return val if val > 0 else 0.0
        except Exception:
            pass
        return 0.0


# ─── 便捷入口 ───

def ve4_alloc_detail_generate(emergency_months: int = 3,
                               use_ai: bool = False) -> VE4AllocationDetailReport:
    engine = VE4AllocationDetailEngine()
    return engine.ve4_alloc_detail_generate(emergency_months=emergency_months, use_ai=use_ai)
