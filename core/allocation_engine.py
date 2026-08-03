"""
VE4 资产配置推荐引擎
===================
纯代码实现，完全可审计、可复现。核心设计参考 ve4_allocation_biz_logic.md。

职责：
    1. 用户画像推断（年龄→阶段、风险偏好、月均支出）
    2. 标准框架目标（二维矩阵 × 活钱反推修正）
    3. 实际配置分析（持仓聚合、应急覆盖率、偏差检测）
    4. 四维度评分与可执行建议生成

输出：
    VE4AllocationReport — 完整的推荐配置对比报告

命名规范：
    - 类: VE4Allocation{模块名}
    - 函数: ve4_alloc_{功能}
    - 常量: VE4_ALLOC_{名称}
"""

import json
import sqlite3
import logging
from pathlib import Path
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from enum import Enum

logger = logging.getLogger("ve4.allocation_engine")

# ─── 数据库路径 ───
from app_paths import DB_PATH, DATA_DIR


# ════════════════════════════════════════════════════════════════
# 数据模型
# ════════════════════════════════════════════════════════════════

class VE4CareerStage(Enum):
    EARLY = "起步期"
    GROWTH = "成长期"
    PRESERVATION = "保值期"


class VE4RiskPreference(Enum):
    LOW = "保守型"
    MEDIUM = "稳健型"
    HIGH = "进取型"


class VE4WarningLevel(Enum):
    NORMAL = "normal"
    CAUTION = "caution"
    WARNING = "warning"


@dataclass
class VE4UserProfile:
    """用户画像"""
    age: int = 30
    career_stage: VE4CareerStage = VE4CareerStage.GROWTH
    risk_preference: VE4RiskPreference = VE4RiskPreference.MEDIUM
    monthly_expense: float = 0.0          # 月均总支出（含弹性）
    monthly_essential_expense: float = 0.0  # 月均必需消费（不含弹性）
    emergency_months: int = 3  # 用户可调，默认3
    # 风险偏好历史（供自适应问卷）
    risk_history: List[Dict] = field(default_factory=list)


@dataclass
class VE4FrameworkTarget:
    """标准框架目标"""
    liquid_pct: float = 0.0   # 活钱目标比例
    stable_pct: float = 0.0   # 防御目标比例
    aggressive_pct: float = 0.0  # 进取目标比例
    liquid_amount: float = 0.0   # 活钱目标金额（反推修正后）
    stable_amount: float = 0.0
    aggressive_amount: float = 0.0
    total_assets: float = 0.0
    # 推导链（用于前端展示透明度）
    derivation: Dict = field(default_factory=dict)


@dataclass
class VE4ActualAllocation:
    """实际配置"""
    liquid_pct: float = 0.0
    stable_pct: float = 0.0
    aggressive_pct: float = 0.0
    protection_pct: float = 0.0
    unclassified_pct: float = 0.0
    liquid_value: float = 0.0
    stable_value: float = 0.0
    aggressive_value: float = 0.0
    protection_value: float = 0.0
    unclassified_value: float = 0.0
    total_classified: float = 0.0
    total_all: float = 0.0


@dataclass
class VE4LiquidityCoverage:
    """应急覆盖率（独立于分类的预算型指标）"""
    emergency_target: float = 0.0     # 应急金目标金额
    liquidable_assets: float = 0.0   # 可流动资产合计
    coverage_ratio: float = 0.0      # 覆盖率
    status: str = "unknown"           # full / partial / insufficient
    gap: float = 0.0                  # 缺口


@dataclass
class VE4DimensionScore:
    """单维度评分"""
    name: str = ""
    score: float = 0.0       # 100 / 70 / 40
    status: VE4WarningLevel = VE4WarningLevel.NORMAL
    actual: float = 0.0
    target: float = 0.0
    deviation: float = 0.0   # 偏差（百分点或金额）
    suggestion: str = ""
    weight: float = 0.0      # 动态权重


@dataclass
class VE4OverallHealth:
    """综合健康度"""
    total_score: float = 0.0
    status: str = "unknown"  # 优秀 / 良好 / 一般 / 需改善 / 紧急
    summary: str = ""
    protection_bonus: float = 0.0   # 保障附加分（0-10）
    protection_pct: float = 0.0     # 保障类资产占总资产比例


@dataclass
class VE4ActionableAdvice:
    """可执行建议"""
    priority: int = 0           # 优先级（1=最高）
    level: str = "normal"       # warning / caution / normal
    dimension: str = ""
    message: str = ""           # 纯代码计算的核心信息
    ai_smoothed: str = ""       # AI润色后的表述（可选）
    months_to_goal: int = 0     # 达成月数估算
    monthly_action: float = 0.0 # 建议每月划拨金额


@dataclass
class VE4AllocationReport:
    """完整的推荐配置对比报告"""
    profile: VE4UserProfile = field(default_factory=VE4UserProfile)
    framework: VE4FrameworkTarget = field(default_factory=VE4FrameworkTarget)
    actual: VE4ActualAllocation = field(default_factory=VE4ActualAllocation)
    liquidity: VE4LiquidityCoverage = field(default_factory=VE4LiquidityCoverage)
    dimensions: List[VE4DimensionScore] = field(default_factory=list)
    overall: VE4OverallHealth = field(default_factory=VE4OverallHealth)
    advices: List[VE4ActionableAdvice] = field(default_factory=list)
    protection_management: Dict = field(default_factory=dict)  # 保障管理说明（不参与评分）
    generated_at: str = ""

    def to_dict(self) -> dict:
        """序列化为字典（供 API 返回）"""
        d = asdict(self)
        # 处理枚举类型
        if isinstance(self.profile.career_stage, VE4CareerStage):
            d["profile"]["career_stage"] = self.profile.career_stage.value
        if isinstance(self.profile.risk_preference, VE4RiskPreference):
            d["profile"]["risk_preference"] = self.profile.risk_preference.value
        for dim in d.get("dimensions", []):
            if isinstance(dim.get("status"), VE4WarningLevel):
                dim["status"] = dim["status"].value
        return d


# ════════════════════════════════════════════════════════════════
# 常量：二维矩阵
# ════════════════════════════════════════════════════════════════

VE4_ALLOC_MATRIX = {
    (VE4CareerStage.EARLY, VE4RiskPreference.LOW): {
        "stable": 0.85, "aggressive": 0.15,
    },
    (VE4CareerStage.EARLY, VE4RiskPreference.MEDIUM): {
        "stable": 0.70, "aggressive": 0.30,
    },
    (VE4CareerStage.EARLY, VE4RiskPreference.HIGH): {
        "stable": 0.30, "aggressive": 0.70,
    },
    (VE4CareerStage.GROWTH, VE4RiskPreference.LOW): {
        "stable": 0.80, "aggressive": 0.20,
    },
    (VE4CareerStage.GROWTH, VE4RiskPreference.MEDIUM): {
        "stable": 0.60, "aggressive": 0.40,
    },
    (VE4CareerStage.GROWTH, VE4RiskPreference.HIGH): {
        "stable": 0.18, "aggressive": 0.82,
    },
    (VE4CareerStage.PRESERVATION, VE4RiskPreference.LOW): {
        "stable": 0.93, "aggressive": 0.07,
    },
    (VE4CareerStage.PRESERVATION, VE4RiskPreference.MEDIUM): {
        "stable": 0.80, "aggressive": 0.20,
    },
    (VE4CareerStage.PRESERVATION, VE4RiskPreference.HIGH): {
        "stable": 0.33, "aggressive": 0.67,
    },
}
# 注：以上为投资资产内部的稳健/进取相对比例（活钱由必需消费独立计算，保障为加分项）

# 动态权重：起步期重收益，保值期重流动
VE4_ALLOC_DYNAMIC_WEIGHTS = {
    VE4CareerStage.EARLY:       {"risk": 0.29, "liquidity": 0.29, "return": 0.42},
    VE4CareerStage.GROWTH:      {"risk": 0.35, "liquidity": 0.29, "return": 0.36},
    VE4CareerStage.PRESERVATION:{"risk": 0.29, "liquidity": 0.42, "return": 0.29},
}

# 偏差阈值基础值（按总资产分层），实际使用时乘以风险偏好因子
VE4_ALLOC_DEVIATION_THRESHOLDS = [
    (50_000, 0.30),     # < 5万：±30%
    (500_000, 0.15),    # 5万~50万：±15%
    (float("inf"), 0.10),  # > 50万：±10%
]

# 风险偏好对偏差阈值的调节因子
# 保守型更严格（允许偏离更少），进取型更宽松
VE4_ALLOC_RISK_DEVIATION_FACTOR = {
    VE4RiskPreference.LOW: 0.70,      # 保守型：阈值 × 0.70（更严格）
    VE4RiskPreference.MEDIUM: 1.00,   # 稳健型：标准阈值
    VE4RiskPreference.HIGH: 1.30,     # 进取型：阈值 × 1.30（更宽松）
}

# 无风险利率（硬编码，不再从网络获取）
# 原因：网络获取的十年期国债收益率数据不准确，且影响页面加载速度
# 如需更新，直接修改此值
VE4_DEFAULT_RISK_FREE_RATE = 0.0175  # 1.75%

# 月均支出计算使用的消费分类（与消费提取标准分类对齐）
# 注意：这里包含所有消费分类，用于计算月均总支出
# 必需消费子集见 VE4_ALLOC_ESSENTIAL_CATEGORIES
VE4_ALLOC_EXPENSE_CATEGORIES = {
    "餐饮", "交通", "购物", "日用", "娱乐", "居住", "月供", "医疗", "教育", "通讯", "旅行", "其他",
}

# 生活必需消费分类（用于流动资金需求计算）
# 必需消费 = 维持基本生活的支出，不含弹性消费
VE4_ALLOC_ESSENTIAL_CATEGORIES = {
    "餐饮", "交通", "日用", "居住", "月供", "医疗", "通讯", "教育",
}
VE4_ALLOC_EXCLUDE_CATEGORIES = {
    "转账", "投资", "理财", "还款", "购买基金", "股票入金", "保险", "储蓄",
}


# ════════════════════════════════════════════════════════════════
# 工具函数
# ════════════════════════════════════════════════════════════════

def ve4_alloc_get_risk_free_rate() -> float:
    """获取无风险利率（硬编码默认值）。"""
    return VE4_DEFAULT_RISK_FREE_RATE


# ════════════════════════════════════════════════════════════════
# 核心引擎
# ════════════════════════════════════════════════════════════════

class VE4AllocationEngine:
    """资产配置推荐引擎"""

    def __init__(self):
        self.db_path = DB_PATH
        self._monthly_essential_expense = 0.0

    # ──────────────────────────────────────────
    # 第1层：用户画像
    # ──────────────────────────────────────────

    def ve4_alloc_load_profile(self, age: int = None,
                                risk_preference: VE4RiskPreference = None,
                                emergency_months: int = 3) -> VE4UserProfile:
        """加载/推断用户画像"""
        # 从 user_profile 表读取（如果存在）
        profile_data = self._load_user_profile_from_db()

        resolved_age = age or profile_data.get("age", 30)
        stage = self._infer_career_stage(resolved_age)

        rp = risk_preference or self._parse_risk_preference(profile_data.get("risk_preference", "medium"))
        rp_history = json.loads(profile_data.get("risk_preference_history", "[]") or "[]")

        # 月均支出：优先自动计算，其次使用存储值
        monthly_expense = self._compute_monthly_expense()
        if monthly_expense == 0:
            monthly_expense = profile_data.get("monthly_expense", 0)

        # 月均必需消费（从 _compute_monthly_expense 的实例属性获取）
        monthly_essential = getattr(self, '_monthly_essential_expense', 0)
        if monthly_essential == 0:
            monthly_essential = monthly_expense  # 回退到总消费

        return VE4UserProfile(
            age=resolved_age,
            career_stage=stage,
            risk_preference=rp,
            monthly_expense=monthly_expense,
            monthly_essential_expense=monthly_essential,
            emergency_months=emergency_months,
            risk_history=rp_history,
        )

    @staticmethod
    def _infer_career_stage(age: int) -> VE4CareerStage:
        if age < 30:
            return VE4CareerStage.EARLY
        elif age <= 45:
            return VE4CareerStage.GROWTH
        return VE4CareerStage.PRESERVATION

    @staticmethod
    def _parse_risk_preference(val) -> VE4RiskPreference:
        if isinstance(val, VE4RiskPreference):
            return val
        mapping = {"low": VE4RiskPreference.LOW, "medium": VE4RiskPreference.MEDIUM,
                   "high": VE4RiskPreference.HIGH, "conservative": VE4RiskPreference.LOW,
                   "moderate": VE4RiskPreference.MEDIUM, "aggressive": VE4RiskPreference.HIGH}
        return mapping.get(str(val).lower().strip(), VE4RiskPreference.MEDIUM)

    def _load_user_profile_from_db(self) -> dict:
        """从 user_profile 表读取用户画像"""
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM user_profile LIMIT 1")
            row = cursor.fetchone()
            conn.close()
            return dict(row) if row else {}
        except Exception:
            return {}

    def _compute_monthly_expense(self) -> float:
        """从 transactions 表自动计算月均支出（全部数据，兼容历史截图）"""
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row

            # 全部数据（不限制时间范围，兼容历史截图）
            row = conn.execute("""
                SELECT SUM(amount) as total,
                       COUNT(DISTINCT strftime('%Y-%m', transaction_date)) as months
                FROM transactions
                WHERE transaction_type = 'expense'
            """).fetchone()

            total = row["total"] or 0
            months = max(1, row["months"] or 1)

            # 月均必需消费（优先 is_essential 字段，回退到分类映射）
            ess_row = conn.execute("""
                SELECT SUM(amount) as total
                FROM transactions
                WHERE transaction_type = 'expense'
                  AND is_essential = 1
            """).fetchone()
            essential_total = ess_row["total"] or 0

            # 回退：如果 is_essential 全为0（旧数据），用分类映射兜底
            if essential_total == 0:
                essential_cats = list(VE4_ALLOC_ESSENTIAL_CATEGORIES)
                ess_placeholders = ",".join("?" for _ in essential_cats)
                ess_row2 = conn.execute(f"""
                    SELECT SUM(amount) as total
                    FROM transactions
                    WHERE transaction_type = 'expense'
                      AND category_primary IN ({ess_placeholders})
                """, (*essential_cats,)).fetchone()
                essential_total = ess_row2["total"] or 0

            conn.close()
            # 将必需消费月均值存入实例，供后续使用
            self._monthly_essential_expense = round(essential_total / months, 2)
            return round(total / months, 2)
        except Exception as e:
            logger.debug(f"[ALLOC] 月均支出计算失败：{e}")
            return 0.0

    def compute_essential_liquid(self) -> float:
        """计算生活必需流动资金（统一入口，三级优先级）。

        优先级：
          1. 用户手动设定（essential_liquid_config.json）
          2. 近3个月必需消费均值 × 3
          3. 单月必需消费 × 3（兜底）

        与 asset_allocation_detail._compute_liquid_breakdown 逻辑一致。
        """
        # 优先级1：用户手动设定
        try:
            config_path = DATA_DIR / "essential_liquid_config.json"
            if config_path.exists():
                data = json.loads(config_path.read_text(encoding="utf-8"))
                val = float(data.get("essential_liquid", 0))
                if val > 0:
                    return val
        except Exception:
            pass

        # 自动计算
        essential_expense = getattr(self, '_monthly_essential_expense', 0)
        if essential_expense <= 0:
            return 0.0
        return essential_expense * 3

    # ──────────────────────────────────────────
    # 第2层：标准框架目标
    # ──────────────────────────────────────────

    def ve4_alloc_compute_framework(self, profile: VE4UserProfile,
                                     total_assets: float,
                                     emergency_target: float = 0) -> VE4FrameworkTarget:
        """计算标准框架目标。

        活钱比例由应急需求决定：
            活钱目标 = (应急场景金额 + 月消费 × 3) / 总资产
        剩余比例按用户画像的进取/稳健设置分配，未设置时回退到二维矩阵。
        """
        # Step 1: 计算应急需求金额
        # 基础生活费储备 = 生活必需流动资金（三级优先级：用户设定 > 3月均值×3 > 单月×3）
        base_living_reserve = self.compute_essential_liquid()
        total_emergency_need = base_living_reserve + emergency_target

        # Step 2: 活钱比例 = 应急需求 / 总资产
        if total_assets > 0:
            final_liquid = min(total_emergency_need / total_assets, 1.0)
        else:
            final_liquid = 1.0

        # Step 3: 剩余比例分配（优先用户画像，其次矩阵）
        remaining = 1.0 - final_liquid

        # 尝试读取用户画像中的投资比例设置
        user_profile = self._load_user_allocation_profile()
        profile_aggressive = user_profile.get("aggressive_pct")
        profile_stable = user_profile.get("stable_pct")

        if (profile_aggressive is not None and profile_stable is not None
                and (profile_aggressive + profile_stable) > 0):
            # 用户设置了进取/稳健比例
            total_user = profile_aggressive + profile_stable
            final_stable = remaining * (profile_stable / total_user)
            final_aggressive = remaining * (profile_aggressive / total_user)
            allocation_source = "用户画像设置"
        else:
            # 回退到二维矩阵
            matrix = VE4_ALLOC_MATRIX.get(
                (profile.career_stage, profile.risk_preference),
                {"stable": 0.60, "aggressive": 0.40}
            )
            matrix_stable = matrix["stable"]
            matrix_aggressive = matrix["aggressive"]
            total_matrix_non_liquid = matrix_stable + matrix_aggressive
            if total_matrix_non_liquid > 0:
                final_stable = remaining * (matrix_stable / total_matrix_non_liquid)
                final_aggressive = remaining * (matrix_aggressive / total_matrix_non_liquid)
            else:
                final_stable = remaining / 2
                final_aggressive = remaining / 2
            allocation_source = "二维矩阵"

        # Step 4: 计算金额
        liquid_amount = round(final_liquid * total_assets, 2)
        stable_amount = round(final_stable * total_assets, 2)
        aggressive_amount = round(final_aggressive * total_assets, 2)

        # 推导链（公开透明）
        derivation = {
            "monthly_expense": profile.monthly_expense,
            "monthly_essential_expense": profile.monthly_essential_expense,
            "emergency_months": 3,  # 生活必需流动资金固定按3个月计算
            "emergency_target_amount": round(emergency_target, 2),
            "base_living_reserve": round(base_living_reserve, 2),
            "total_emergency_need": round(total_emergency_need, 2),
            "final_liquid_pct": round(final_liquid * 100, 1),
            "remaining_pct": round(remaining * 100, 1),
            "allocation_source": allocation_source,
            "reason": (f"应急需求 = 月消费×3(¥{round(base_living_reserve, 0)}) + "
                       f"应急场景(¥{round(emergency_target, 0)}) = ¥{round(total_emergency_need, 0)}；"
                       f"活钱目标 = {round(final_liquid*100,1)}% (占总资产)；"
                       f"剩余 {round(remaining*100,1)}% 按{allocation_source}分配"),
        }

        return VE4FrameworkTarget(
            liquid_pct=round(final_liquid, 4),
            stable_pct=round(final_stable, 4),
            aggressive_pct=round(final_aggressive, 4),
            liquid_amount=liquid_amount,
            stable_amount=stable_amount,
            aggressive_amount=aggressive_amount,
            total_assets=total_assets,
            derivation=derivation,
        )

    # ──────────────────────────────────────────
    # 第3层：实际配置分析
    # ──────────────────────────────────────────

    def ve4_alloc_compute_actual(self) -> VE4ActualAllocation:
        """从 asset_holdings 表计算实际配置。

        使用与 asset_allocation_detail 一致的分类逻辑（产品名称关键词优先），
        确保黄金等保障类资产无论 asset_class 标记是否正确都能被正确归类。
        """
        from core.asset_classification_rules import ve4_alloc_rules_classify

        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row

            rows = conn.execute("""
                SELECT product_name, asset_class, current_value
                FROM asset_holdings
                WHERE current_value > 0
                AND product_name NOT IN ('ETF', '证券市值', '总资产')
            """).fetchall()

            conn.close()

            liquid_value = 0.0
            stable_value = 0.0
            aggressive_value = 0.0
            protection_value = 0.0

            for row in rows:
                name = row["product_name"] or ""
                ac = row["asset_class"] or ""
                val = row["current_value"] or 0

                # 优先级1：asset_class 明确为保障类的直接归入保障
                if ac in ("alternative", "protection"):
                    cat = "protection"
                else:
                    # 优先级2：使用与 detail 模块一致的名称分类规则
                    cat = ve4_alloc_rules_classify(name)
                    # 若名称分类为 protection 但 asset_class 是 equity 等，
                    # 仍以名称分类为准（修复黄金被误标为 equity 的问题）

                if cat == "liquid":
                    liquid_value += val
                elif cat == "stable":
                    stable_value += val
                elif cat == "aggressive":
                    aggressive_value += val
                elif cat == "protection":
                    protection_value += val

            total_all = liquid_value + stable_value + aggressive_value + protection_value
            total_classified = total_all

            if total_all > 0:
                liquid_pct = liquid_value / total_all
                stable_pct = stable_value / total_all
                aggressive_pct = aggressive_value / total_all
                protection_pct = protection_value / total_all
            else:
                liquid_pct = stable_pct = aggressive_pct = protection_pct = 0.0

            return VE4ActualAllocation(
                liquid_pct=round(liquid_pct, 4),
                stable_pct=round(stable_pct, 4),
                aggressive_pct=round(aggressive_pct, 4),
                protection_pct=round(protection_pct, 4),
                unclassified_pct=0.0,
                liquid_value=round(liquid_value, 2),
                stable_value=round(stable_value, 2),
                aggressive_value=round(aggressive_value, 2),
                protection_value=round(protection_value, 2),
                unclassified_value=0.0,
                total_classified=round(total_classified, 2),
                total_all=round(total_all, 2),
            )

        except Exception as e:
            logger.warning(f"[ALLOC] 实际配置计算失败：{e}")
            return VE4ActualAllocation()

    def ve4_alloc_compute_liquidity_coverage(self, profile: VE4UserProfile,
                                              actual: VE4ActualAllocation,
                                              emergency_target: float = 0) -> VE4LiquidityCoverage:
        """计算应急覆盖率"""
        # 应急目标 = 生活必需流动资金（三级优先级） + 用户应急场景金额
        base_emergency_target = self.compute_essential_liquid()
        emergency_target = base_emergency_target + emergency_target

        # 可流动资产 = 实际流动类资产总额（仅活钱，不含稳健类）
        liquidable = actual.liquid_value

        if emergency_target > 0:
            coverage = liquidable / emergency_target
        else:
            coverage = 1.0 if liquidable > 0 else 0.0

        if coverage >= 1.0:
            status = "full"
        elif coverage >= 0.5:
            status = "partial"
        else:
            status = "insufficient"

        gap = max(0, emergency_target - liquidable)

        return VE4LiquidityCoverage(
            emergency_target=round(emergency_target, 2),
            liquidable_assets=round(liquidable, 2),
            coverage_ratio=round(coverage, 4),
            status=status,
            gap=round(gap, 2),
        )

    # ──────────────────────────────────────────
    # 偏差检测与评分
    # ──────────────────────────────────────────

    def ve4_alloc_get_deviation_threshold(self, total_assets: float) -> float:
        """根据总资产获取偏差阈值"""
        for threshold, pct in VE4_ALLOC_DEVIATION_THRESHOLDS:
            if total_assets < threshold:
                return pct
        return 0.10

    def ve4_alloc_score_dimensions(self, profile: VE4UserProfile,
                                    framework: VE4FrameworkTarget,
                                    actual: VE4ActualAllocation,
                                    liquidity: VE4LiquidityCoverage,
                                    emergency_target: float = 0) -> tuple:
        """
        三维度评分 + 保障管理说明。

        评分维度：
        - 风险管理：实际进取占比 vs 画像进取比例上限
        - 流动性管理：应急覆盖率（月均支出=0 时默认100分）
        - 收益率管理：条件性评分（数据不足时中性）

        保障管理：不参与评分，仅展示"可分配盈余空间"说明，
                  由用户自行决定是否以及如何配置保障类资产。
        """
        dimensions = []
        weights = VE4_ALLOC_DYNAMIC_WEIGHTS.get(profile.career_stage,
                                                 VE4_ALLOC_DYNAMIC_WEIGHTS[VE4CareerStage.GROWTH])
        # 基础阈值按总资产分层，再乘以风险偏好调节因子
        base_threshold = self.ve4_alloc_get_deviation_threshold(framework.total_assets)
        risk_factor = VE4_ALLOC_RISK_DEVIATION_FACTOR.get(profile.risk_preference, 1.00)
        threshold = base_threshold * risk_factor

        # ── 读取用户画像（allocation_profile.json）──
        user_profile = self._load_user_allocation_profile()
        profile_aggressive = user_profile.get("aggressive_pct")  # 用户设置的进取比例上限（百分比数值）
        profile_stable = user_profile.get("stable_pct")          # 用户设置的稳健比例

        # ── 维度1：风险管理（进取类占比 vs 画像上限）──
        if profile_aggressive is not None:
            risk_target = profile_aggressive / 100.0
            risk_target_display = profile_aggressive
        else:
            risk_target = framework.aggressive_pct
            risk_target_display = round(framework.aggressive_pct * 100, 1)

        risk_deviation = abs(actual.aggressive_pct - risk_target)
        risk_status, risk_score, risk_suggestion = self._judge_deviation(
            actual.aggressive_pct, risk_target, threshold,
            high_msg=f"进取类占比({actual.aggressive_pct*100:.1f}%)超过画像目标({risk_target_display}%)，建议适当降低风险敞口",
            low_msg=f"进取类占比({actual.aggressive_pct*100:.1f}%)低于画像目标({risk_target_display}%)，可根据风险承受能力适当增加",
            normal_msg=f"风险管理指标正常，进取类占比({actual.aggressive_pct*100:.1f}%)符合画像目标({risk_target_display}%)",
        )
        dimensions.append(VE4DimensionScore(
            name="风险管理", score=risk_score, status=risk_status,
            actual=round(actual.aggressive_pct * 100, 1),
            target=round(risk_target * 100, 1),
            deviation=round(risk_deviation * 100, 1),
            suggestion=risk_suggestion, weight=weights["risk"],
        ))

        # ── 维度2：流动性管理（应急覆盖率）──
        if liquidity.emergency_target <= 0:
            liq_status, liq_score = VE4WarningLevel.NORMAL, 100.0
            liq_suggestion = "未设置生活必需流动性目标，此项不参与评分（默认满分）"
            liq_actual_display = 100.0
            liq_target_display = 100.0
            liq_deviation_display = 0.0
        elif liquidity.coverage_ratio >= 1.0:
            liq_status, liq_score = VE4WarningLevel.NORMAL, 100.0
            liq_suggestion = f"应急资金充足，可覆盖 {profile.emergency_months} 个月支出"
            liq_actual_display = round(liquidity.coverage_ratio * 100, 1)
            liq_target_display = 100.0
            liq_deviation_display = round((1 - liquidity.coverage_ratio) * 100, 1)
        elif liquidity.coverage_ratio >= 0.5:
            liq_status, liq_score = VE4WarningLevel.CAUTION, 70.0
            liq_suggestion = f"应急资金部分覆盖（{round(liquidity.coverage_ratio*100,0)}%），建议补充"
            liq_actual_display = round(liquidity.coverage_ratio * 100, 1)
            liq_target_display = 100.0
            liq_deviation_display = round((1 - liquidity.coverage_ratio) * 100, 1)
        else:
            liq_status, liq_score = VE4WarningLevel.WARNING, 40.0
            liq_suggestion = f"应急资金严重不足（仅覆盖 {round(liquidity.coverage_ratio*100,0)}%），建议优先补充"
            liq_actual_display = round(liquidity.coverage_ratio * 100, 1)
            liq_target_display = 100.0
            liq_deviation_display = round((1 - liquidity.coverage_ratio) * 100, 1)

        dimensions.append(VE4DimensionScore(
            name="流动性管理", score=liq_score, status=liq_status,
            actual=liq_actual_display,
            target=liq_target_display,
            deviation=liq_deviation_display,
            suggestion=liq_suggestion, weight=weights["liquidity"],
        ))

        # ── 维度3：收益率管理（条件性评分）──
        avg_return, return_data_ratio = self._compute_avg_return()
        risk_free = ve4_alloc_get_risk_free_rate()

        if return_data_ratio < 0.5:
            ret_status, ret_score = VE4WarningLevel.NORMAL, 60.0
            ret_suggestion = (
                f"收益率数据不足（仅 {round(return_data_ratio*100,0)}% 的持仓有记录），"
                f"无法可靠评估。建议：\n"
                f"① 在持仓管理中补录各资产的最新年化收益率\n"
                f"② 或使用财务隐私 RAG 系统自动提取收益记录\n"
                f"当前参考无风险利率：{risk_free*100:.2f}%"
            )
        elif avg_return < 0:
            ret_status, ret_score = VE4WarningLevel.WARNING, 40.0
            ret_suggestion = "投资组合当前处于亏损状态，建议关注风险敞口"
        elif avg_return < risk_free:
            ret_status, ret_score = VE4WarningLevel.CAUTION, 70.0
            ret_suggestion = f"收益率({avg_return*100:.2f}%)低于当前无风险利率({risk_free*100:.2f}%)，建议优化配置"
        else:
            ret_status, ret_score = VE4WarningLevel.NORMAL, 100.0
            ret_suggestion = f"收益率({avg_return*100:.2f}%)优于无风险利率({risk_free*100:.2f}%)，表现正常"

        dimensions.append(VE4DimensionScore(
            name="收益率管理", score=ret_score, status=ret_status,
            actual=round(avg_return * 100, 2),
            target=round(risk_free * 100, 2),
            deviation=round((avg_return - risk_free) * 100, 2),
            suggestion=ret_suggestion, weight=weights["return"],
        ))

        # ── 保障管理（不参与评分，仅展示说明）──
        # 生活必需流动资金 = 三级优先级计算（用户设定 > 3月均值×3 > 单月×3）
        # 可分配盈余 = 实际流动类资金 - 生活必需流动资金
        essential_liquid = self.compute_essential_liquid()
        surplus = max(0, actual.liquid_value - essential_liquid)
        surplus_pct = (surplus / actual.total_all * 100) if actual.total_all > 0 else 0

        # 计算已配置保障的产品类型分布（用于LLM解读）
        # 同时按 asset_class 和产品名称关键词搜索，确保黄金等保障产品被正确识别
        protection_products = []
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT product_name, current_value, asset_class, account_key
                FROM asset_holdings
                WHERE (asset_class IN ('protection', 'alternative')
                       OR product_name LIKE '%黄金%'
                       OR product_name LIKE '%保险%'
                       OR product_name LIKE '%年金%'
                       OR product_name LIKE '%重疾%'
                       OR product_name LIKE '%意外%')
                  AND current_value > 0
                ORDER BY current_value DESC
            """).fetchall()
            conn.close()
            protection_products = [
                {"name": r["product_name"], "value": r["current_value"], "account": r["account_key"]}
                for r in rows
            ]
        except Exception:
            pass

        # 识别保障类型（黄金、重疾险、意外险、年金险、终身寿等）
        def _detect_protection_type(name: str) -> str:
            n = name.lower()
            if any(k in name for k in ["黄金", "金", "Gold", "gold", "AU", "au"]):
                return "黄金"
            if any(k in name for k in ["重疾", "大病", "医疗", "健康"]):
                return "健康险/重疾险"
            if any(k in name for k in ["意外", "意外险"]):
                return "意外险"
            if any(k in name for k in ["年金", "养老", "终身寿", "寿险", "增额"]):
                return "年金/寿险"
            if any(k in name for k in ["保险", "保单"]):
                return "保险类"
            return "其他保障"

        protection_types = {}
        for p in protection_products:
            t = _detect_protection_type(p["name"])
            if t not in protection_types:
                protection_types[t] = {"amount": 0, "count": 0, "products": []}
            protection_types[t]["amount"] += p["value"]
            protection_types[t]["count"] += 1
            protection_types[t]["products"].append(p["name"])

        protection_management = {
            "total_assets": round(actual.total_all, 2),
            "allocated_aggressive": round(actual.aggressive_value, 2),
            "allocated_stable": round(actual.stable_value, 2),
            "allocated_protection": round(actual.protection_value, 2),
            "essential_liquid": round(essential_liquid, 2),
            "surplus_amount": round(surplus, 2),
            "surplus_pct": round(surplus_pct, 1),
            "protection_types": protection_types,
            "protection_products": protection_products,
            "note": (
                "保障配置非固定比例项，不纳入战略配置框架。\n"
                "保障类资产是对投资和流动性之外的风险管理工具。\n"
                "当前可分配盈余为流动类资金超出生活必需的部分，可灵活支配。"
            ),
        }

        return dimensions, protection_management

    def _load_user_allocation_profile(self) -> dict:
        """从 allocation_profile.json 读取用户画像"""
        try:
            profile_path = DB_PATH.parent / "allocation_profile.json"
            if profile_path.exists():
                with open(profile_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.debug(f"[ALLOC] 画像文件读取失败: {e}")
        return {}

    @staticmethod
    def _judge_deviation(actual: float, target: float, threshold: float,
                          high_msg: str, low_msg: str, normal_msg: str) -> tuple:
        """判断偏差并返回状态和分数"""
        diff = abs(actual - target)
        half = threshold * 0.5

        if diff <= half:
            return VE4WarningLevel.NORMAL, 100.0, normal_msg
        elif diff <= threshold:
            if actual > target:
                return VE4WarningLevel.CAUTION, 70.0, high_msg
            else:
                return VE4WarningLevel.CAUTION, 70.0, low_msg
        else:
            if actual > target:
                return VE4WarningLevel.WARNING, 40.0, high_msg
            else:
                return VE4WarningLevel.WARNING, 40.0, low_msg

    def _compute_avg_return(self) -> tuple:
        """
        计算平均年化收益率，返回 (avg_return, data_ratio)
        data_ratio = 有收益率记录的持仓数 / 总持仓数
        """
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            # 总持仓数（已分类）
            total_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM asset_holdings WHERE is_classified = 1"
            ).fetchone()
            total = total_row["cnt"] or 0

            # 有收益率数据的持仓
            valid_row = conn.execute("""
                SELECT COUNT(*) as cnt, AVG(annualized_return_pct) as avg_return
                FROM asset_holdings
                WHERE is_classified = 1 AND annualized_return_pct IS NOT NULL AND annualized_return_pct != 0
            """).fetchone()
            valid = valid_row["cnt"] or 0
            avg = valid_row["avg_return"]

            conn.close()

            data_ratio = valid / total if total > 0 else 0
            avg_return = (avg or 0) / 100.0  # 百分比→小数
            return avg_return, data_ratio
        except Exception as e:
            logger.warning(f"[ALLOC] 收益率计算异常: {e}")
            return 0.0, 0.0

    # ──────────────────────────────────────────
    # 综合评分
    # ──────────────────────────────────────────

    @staticmethod
    def ve4_alloc_compute_overall(dimensions: List[VE4DimensionScore],
                                   protection_pct: float = 0.0) -> VE4OverallHealth:
        """计算综合健康度（基础分 + 保障加分）"""
        if not dimensions:
            return VE4OverallHealth()

        total_weight = sum(d.weight for d in dimensions)
        if total_weight == 0:
            return VE4OverallHealth()

        # 基础分（三维度加权）
        base_score = sum(d.score * d.weight for d in dimensions) / total_weight

        # 保障加分：保障占比 × 系数，上限10分
        # 保障类资产（黄金、保险等）是财务安全的加分项，不是必要条件
        protection_bonus = min(protection_pct * 100, 10.0)  # 占比10%时加满分10分
        total_score = min(base_score + protection_bonus, 100.0)

        bonus_note = f"（含保障加分 +{protection_bonus:.1f}）" if protection_bonus > 0.5 else ""

        if total_score >= 90:
            status = "优秀"
            summary = f"资产配置健康，三维度指标均表现优异{bonus_note}"
        elif total_score >= 75:
            status = "良好"
            summary = f"资产配置良好，大部分指标处于正常范围{bonus_note}"
        elif total_score >= 60:
            status = "一般"
            summary = f"资产配置一般，建议关注部分指标{bonus_note}"
        elif total_score >= 40:
            status = "需改善"
            summary = f"资产配置需要改善，存在多个待优化项{bonus_note}"
        else:
            status = "紧急"
            summary = f"⚠️ 资产配置存在重大风险，需要立即关注{bonus_note}"

        return VE4OverallHealth(
            total_score=round(total_score, 1),
            status=status,
            summary=summary,
            protection_bonus=round(protection_bonus, 1),
            protection_pct=round(protection_pct * 100, 1),
        )

    # ──────────────────────────────────────────
    # 可执行建议
    # ──────────────────────────────────────────

    def ve4_alloc_generate_advices(self, profile: VE4UserProfile,
                                    framework: VE4FrameworkTarget,
                                    actual: VE4ActualAllocation,
                                    liquidity: VE4LiquidityCoverage) -> List[VE4ActionableAdvice]:
        """生成带可执行度的优先级建议（不再依赖评分系统）"""
        advices = []

        # 1. 应急资金缺口（最优先）
        if liquidity.gap > 0:
            monthly_surplus = self._compute_monthly_surplus()
            if monthly_surplus > 0:
                months_to = int(liquidity.gap / monthly_surplus) + 1
                advices.append(VE4ActionableAdvice(
                    priority=1, level="warning",
                    dimension="流动性管理",
                    message=(f"应急资金缺口 ¥{liquidity.gap:,.0f}，"
                             f"建议每月划拨 ¥{monthly_surplus:,.0f}，约 {months_to} 个月可覆盖"
                             f"{profile.emergency_months}个月支出"),
                    months_to_goal=months_to,
                    monthly_action=monthly_surplus,
                ))
            else:
                advices.append(VE4ActionableAdvice(
                    priority=1, level="warning",
                    dimension="流动性管理",
                    message=f"应急资金缺口 ¥{liquidity.gap:,.0f}，当前无结余，建议审视支出结构",
                ))

        # 2. 进取类偏离（直接从实际与框架对比）
        aggressive_diff = actual.aggressive_pct - framework.aggressive_pct
        if abs(aggressive_diff) > 0.05:
            direction = "偏高" if aggressive_diff > 0 else "偏低"
            level = "warning" if abs(aggressive_diff) > 0.15 else "caution"
            advices.append(VE4ActionableAdvice(
                priority=2 if level == "warning" else 3,
                level=level,
                dimension="风险管理",
                message=f"进取类占比{direction}（实际{(actual.aggressive_pct*100):.0f}% vs 目标{(framework.aggressive_pct*100):.0f}%），{'建议逐步降低权益敞口以匹配风险承受力' if aggressive_diff > 0 else '适当增加权益配置可提升长期收益潜力'}",
            ))

        # 3. 流动性偏离
        liquid_diff = actual.liquid_pct - framework.liquid_pct
        if liquid_diff < -0.05:
            advices.append(VE4ActionableAdvice(
                priority=2, level="warning",
                dimension="流动性管理",
                message=f"流动类资金偏低（实际{(actual.liquid_pct*100):.0f}% vs 目标{(framework.liquid_pct*100):.0f}%），建议增加应急储备",
            ))

        if not advices:
            advices.append(VE4ActionableAdvice(
                priority=9, level="normal",
                dimension="整体",
                message="当前资产配置符合战略框架，建议定期检视并根据人生阶段调整",
            ))

        # 排序：优先级小的在前
        advices.sort(key=lambda a: a.priority)
        return advices

    def _compute_monthly_surplus(self) -> float:
        """计算近3月月均结余"""
        try:
            cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row

            income = conn.execute("""
                SELECT SUM(amount) as total FROM transactions
                WHERE transaction_date >= ? AND transaction_type = 'income'
            """, (cutoff,)).fetchone()["total"] or 0

            expense = conn.execute("""
                SELECT SUM(amount) as total FROM transactions
                WHERE transaction_date >= ? AND transaction_type = 'expense'
                  AND category_primary IN ({})
            """.format(",".join("?" for _ in VE4_ALLOC_EXPENSE_CATEGORIES)),
                (cutoff, *VE4_ALLOC_EXPENSE_CATEGORIES)).fetchone()["total"] or 0

            conn.close()
            months = max(1, (datetime.now() - datetime.strptime(cutoff, "%Y-%m-%d")).days / 30)
            return round((income - expense) / months, 2)
        except Exception:
            return 0.0

    # ──────────────────────────────────────────
    # 主入口：生成完整报告
    # ──────────────────────────────────────────

    def ve4_alloc_generate_report(self, age: int = None,
                                   risk_preference: VE4RiskPreference = None,
                                   emergency_months: int = 3,
                                   emergency_target: float = 0) -> VE4AllocationReport:
        """
        生成完整的推荐配置对比报告。

        Args:
            age: 年龄（空=从数据库读取）
            risk_preference: 风险偏好（空=从数据库读取）
            emergency_months: 应急月数（默认3）
            emergency_target: 前端应急场景总金额（默认0）

        Returns:
            VE4AllocationReport
        """
        logger.info("[ALLOC] 开始生成资产配置报告")
        start = datetime.now()

        # Step 1: 加载用户画像
        profile = self.ve4_alloc_load_profile(age, risk_preference, emergency_months)

        # Step 2: 计算实际配置
        actual = self.ve4_alloc_compute_actual()
        total_assets = actual.total_all

        # Step 3: 计算标准框架目标（传入前端应急场景金额）
        framework = self.ve4_alloc_compute_framework(profile, total_assets,
                                                      emergency_target=emergency_target)

        # Step 4: 应急覆盖率（传入前端应急场景金额）
        liquidity = self.ve4_alloc_compute_liquidity_coverage(profile, actual,
                                                               emergency_target=emergency_target)

        # Step 5: 三维度评分 + 保障管理说明
        dimensions, protection_management = self.ve4_alloc_score_dimensions(profile, framework, actual, liquidity,
                                                                    emergency_target=emergency_target)

        # 计算保障类资产占比（用于加分）
        protection_value = actual.protection_value if hasattr(actual, 'protection_value') else 0
        protection_pct = protection_value / actual.total_all if actual.total_all > 0 else 0
        overall = self.ve4_alloc_compute_overall(dimensions, protection_pct=protection_pct)

        # Step 6: 可执行建议（不再依赖评分维度，直接从配置偏离计算）
        advices = self.ve4_alloc_generate_advices(profile, framework, actual, liquidity)

        elapsed = (datetime.now() - start).total_seconds()
        logger.info(f"[ALLOC] 报告生成完成（耗时 {elapsed:.2f}s）")

        return VE4AllocationReport(
            profile=profile,
            framework=framework,
            actual=actual,
            liquidity=liquidity,
            dimensions=dimensions,
            overall=overall,
            advices=advices,
            protection_management=protection_management,
            generated_at=datetime.now().isoformat(),
        )


# ─── 便捷函数 ───

def ve4_alloc_generate_report(age: int = None, risk_preference=None,
                               emergency_months: int = 3,
                               emergency_target: float = 0) -> VE4AllocationReport:
    """便捷入口：生成资产配置报告"""
    engine = VE4AllocationEngine()
    rp = None
    if risk_preference:
        rp = VE4AllocationEngine._parse_risk_preference(risk_preference)
    return engine.ve4_alloc_generate_report(age=age, risk_preference=rp,
                                             emergency_months=emergency_months,
                                             emergency_target=emergency_target)


def ve4_alloc_get_career_stage_name(stage: VE4CareerStage) -> str:
    return stage.value


def ve4_alloc_get_risk_name(rp: VE4RiskPreference) -> str:
    return rp.value
