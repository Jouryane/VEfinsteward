"""
VE5 截图数据提取 Pydantic 模型
==============================
用于强制校验 LLM 输出，防止名称粘连、金额错位、类型混淆。

命名规范：ve5_extract_{功能}
"""
from typing import List, Optional, Literal
from pydantic import BaseModel, Field, field_validator
from core.asset_classification_rules import ve4_alloc_rules_classify


class StockHolding(BaseModel):
    """股票持仓"""
    name: str = Field(description="股票完整名称，如'大秦铁路'。若OCR截断需自动补全")
    asset_class: Optional[str] = Field(default=None, description="资产分类: aggressive/stable/liquid/protection")
    quantity: Optional[int] = Field(default=None, description="持仓数量（股）")
    current_price: Optional[float] = Field(default=None, description="现价")
    cost_price: Optional[float] = Field(default=None, description="成本价")
    profit: Optional[float] = Field(default=None, description="持仓盈亏金额（负值表示亏损）")
    market_value: Optional[float] = Field(default=None, description="证券市值")

    @field_validator('name')
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        # 名称补全规则
        corrections = {
            "XD大秦铁": "大秦铁路",
            "大秦铁": "大秦铁路",
            "纳指100": "纳指100ETF",
            "纳斯达克100": "纳指100ETF",
            "道琼斯": "纳指100ETF",  # OCR常见误识别
            "道琥斯": "纳指100ETF",
            "低波红利": "红利低波ETF",
            "红利低波": "红利低波ETF",
            "工银黄金": "工银黄金ETF",
            "黄金ETF": "黄金ETF",
            "春秋航空": "春秋航空",
        }
        for wrong, right in corrections.items():
            if wrong in v or v.startswith(wrong):
                return right
        return v

    @field_validator('market_value')
    @classmethod
    def validate_market_value(cls, v, info) -> float:
        if v is None:
            return None
        # 校验：市值 ≈ 数量 * 现价（允许±5%误差）
        data = info.data
        qty = data.get('quantity')
        price = data.get('current_price')
        if qty and price and qty > 0 and price > 0:
            expected = qty * price
            if abs(v - expected) / expected > 0.05:
                # 市值偏差过大，使用计算值
                return round(expected, 2)
        return v


class ETFHolding(BaseModel):
    """ETF持仓（场内基金）"""
    name: str = Field(description="ETF完整名称，如'酒ETF'、'纳指100ETF'")
    asset_class: Optional[str] = Field(default=None, description="资产分类: aggressive/stable/liquid/protection")
    quantity: Optional[int] = Field(default=None, description="持仓数量（份）")
    current_price: Optional[float] = Field(default=None, description="现价")
    cost_price: Optional[float] = Field(default=None, description="成本价")
    profit: Optional[float] = Field(default=None, description="持仓盈亏金额")
    market_value: Optional[float] = Field(default=None, description="证券市值")

    @field_validator('name')
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            return v
        # ETF名称补全
        corrections = {
            "纳指100": "纳指100ETF",
            "纳斯达克100": "纳指100ETF",
            "道琼斯": "道琼斯ETF",
            "低波红利": "红利低波ETF",
            "红利低波": "红利低波ETF",
            "工银黄金": "工银黄金ETF",
        }
        for wrong, right in corrections.items():
            if v == wrong:
                return right
        # 如果以ETF结尾且长度合理，保留原样
        if v.endswith("ETF") and len(v) > 3:
            return v
        return v

    @field_validator('market_value')
    @classmethod
    def validate_market_value(cls, v, info) -> float:
        if v is None:
            return None
        data = info.data
        qty = data.get('quantity')
        price = data.get('current_price')
        if qty and price and qty > 0 and price > 0:
            expected = qty * price
            if abs(v - expected) / expected > 0.05:
                return round(expected, 2)
        return v


class BankHolding(BaseModel):
    """银行理财/货币基金持仓"""
    name: str = Field(description="产品完整名称，如'朝朝宝'、'广发纳斯达克生物科技'")
    asset_class: Optional[str] = Field(default=None, description="资产分类: aggressive/stable/liquid/protection")
    market_value: float = Field(description="持仓金额/市值")

    @field_validator('name')
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        # 排除非持仓项
        invalid_names = ["总资产", "证券市值", "可用资金", "今日收益", "持仓收益",
                        "累计收益", "直接可用", "快速赎回", "上月支出", "看看钱花"]
        for invalid in invalid_names:
            if invalid in v:
                return ""  # 空名称会被过滤
        return v


class FundHolding(BaseModel):
    """公募基金持仓（场外）"""
    name: str = Field(description="基金完整名称，如'摩根全球多元配置人民币C'")
    asset_class: Optional[str] = Field(default=None, description="资产分类: aggressive/stable/liquid/protection")
    market_value: float = Field(description="持仓金额/市值")

    @field_validator('name')
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        invalid_names = ["总资产", "昨日收益", "累计收益", "基金持仓"]
        for invalid in invalid_names:
            if invalid in v:
                return ""
        return v


class PortfolioData(BaseModel):
    """完整持仓数据（统一输出格式）"""
    screenshot_type: Literal["broker", "bank", "fund", "unknown"] = Field(
        description="截图类型：broker=证券, bank=银行, fund=基金"
    )
    total_assets: Optional[float] = Field(None, description="总资产")
    position_ratio: Optional[float] = Field(None, description="仓位比例，如73.6")
    available_cash: float = Field(0.0, description="可用资金")
    stocks: List[StockHolding] = Field(default_factory=list, description="股票持仓")
    etfs: List[ETFHolding] = Field(default_factory=list, description="ETF持仓")
    bank_holdings: List[BankHolding] = Field(default_factory=list, description="银行理财/货币基金")
    fund_holdings: List[FundHolding] = Field(default_factory=list, description="公募基金")

    def to_legacy_holdings(self) -> list:
        """
        转换为 pipeline 使用的旧格式列表。
        保持向后兼容，不影响现有数据库写入流程。
        """
        result = []
        account_map = {
            "broker": "证券账户",
            "bank": "招商银行",
            "fund": "基金账户",
            "unknown": "未知账户",
        }
        account = account_map.get(self.screenshot_type, "未知账户")

        # 汇总项（仅证券截图生成"证券可用资金"）
        if self.screenshot_type == "broker" and self.available_cash > 0:
            result.append({
                "name": "证券可用资金", "value": self.available_cash,
                "type": "liquid", "account": account
            })

        # 股票（含盈亏数据）
        for h in self.stocks:
            if h.name:
                # 优先使用LLM输出的asset_class，如果没有则回退到规则分类
                if h.asset_class and h.asset_class in ["liquid", "stable", "aggressive", "protection"]:
                    four_level = h.asset_class
                else:
                    four_level = ve4_alloc_rules_classify(h.name)
                item = {
                    "name": h.name, "value": h.market_value,
                    "type": four_level, "account": account
                }
                if h.profit != 0:
                    item["unrealized_pnl"] = h.profit
                if h.cost_price and h.cost_price > 0 and h.quantity and h.quantity > 0:
                    item["cost_basis"] = round(h.cost_price * h.quantity, 2)
                if h.market_value > 0 and item.get("cost_basis", 0) > 0:
                    item["holding_return_pct"] = round((h.market_value - item["cost_basis"]) / item["cost_basis"] * 100, 2)
                result.append(item)

        # ETF（含盈亏数据）
        for h in self.etfs:
            if h.name and len(h.name) > 3:  # 过滤"ETF"这种空名称
                # 优先使用LLM输出的asset_class，如果没有则回退到规则分类
                if h.asset_class and h.asset_class in ["liquid", "stable", "aggressive", "protection"]:
                    four_level = h.asset_class
                else:
                    four_level = ve4_alloc_rules_classify(h.name)
                item = {
                    "name": h.name, "value": h.market_value,
                    "type": four_level, "account": account
                }
                if h.profit != 0:
                    item["unrealized_pnl"] = h.profit
                if h.cost_price and h.cost_price > 0 and h.quantity and h.quantity > 0:
                    item["cost_basis"] = round(h.cost_price * h.quantity, 2)
                if h.market_value > 0 and item.get("cost_basis", 0) > 0:
                    item["holding_return_pct"] = round((h.market_value - item["cost_basis"]) / item["cost_basis"] * 100, 2)
                result.append(item)

        # 银行理财
        for h in self.bank_holdings:
            if h.name:
                # 优先使用LLM输出的asset_class，如果没有则回退到规则分类
                if h.asset_class and h.asset_class in ["liquid", "stable", "aggressive", "protection"]:
                    four_level = h.asset_class
                else:
                    four_level = _classify_bank(h.name)
                result.append({
                    "name": h.name, "value": h.market_value,
                    "type": four_level, "account": account
                })

        # 基金
        for h in self.fund_holdings:
            if h.name:
                # 优先使用LLM输出的asset_class，如果没有则回退到规则分类
                if h.asset_class and h.asset_class in ["liquid", "stable", "aggressive", "protection"]:
                    four_level = h.asset_class
                else:
                    four_level = _classify_fund(h.name)
                result.append({
                    "name": h.name, "value": h.market_value,
                    "type": four_level, "account": account
                })

        return result


def _classify_bank(name: str) -> str:
    """银行产品分类"""
    name_l = name.lower()
    # 流动性资金（最高优先级）
    if any(kw in name_l for kw in ["活期", "存款", "现金", "余额宝", "朝朝宝", "零钱", "活钱", "货币"]):
        return "liquid"
    # 保障类
    if any(kw in name_l for kw in ["黄金", "保险", "年金"]):
        return "protection"
    # 进取类：R4/R5级别理财、权益类、混合类、股票型
    if any(kw in name_l for kw in ["R4", "R5", "PR4", "PR5", "权益类", "股票型", "混合类", "偏股", "高风险"]):
        return "aggressive"
    # 稳健类：R1/R2/R3债券类理财、固收类（需排除已被上述规则命中的情况）
    if any(kw in name_l for kw in ["债券", "理财", "固收", "稳健", "R1", "R2", "R3", "PR1", "PR2", "PR3", "中低风险", "低风险"]):
        return "stable"
    # 默认归为进取类（不明产品不应保守估计）
    return "aggressive"


def _classify_fund(name: str) -> str:
    """基金产品分类"""
    name_l = name.lower()
    # 流动性资金（最高优先级）
    if any(kw in name_l for kw in ["货币", "现金", "活钱"]):
        return "liquid"
    # 保障类
    if any(kw in name_l for kw in ["黄金", "保险"]):
        return "protection"
    # 进取类：股票型、混合型、指数型、QDII、R4/R5等
    if any(kw in name_l for kw in ["股票型", "混合型", "指数型", "QDII", "ETF", "LOF", "R4", "R5", "PR4", "PR5", "权益", "偏股", "量化", "增强", "成长", "价值", "蓝筹", "红利", "全球", "海外", "纳斯达克", "标普", "道琼斯"]):
        return "aggressive"
    # 稳健类：债券型、纯债、短债、理财型
    if any(kw in name_l for kw in ["债券", "纯债", "短债", "中短债", "理财型", "固收", "稳健", "R1", "R2", "R3", "PR1", "PR2", "PR3"]):
        return "stable"
    # 默认归为进取类（基金默认是权益类）
    return "aggressive"


# ════════════════════════════════════════════════════════
# 截图属性预分类（Step 0：在提取前判断截图属于什么类型）
# ════════════════════════════════════════════════════════

class ScreenshotCategory(BaseModel):
    """截图属性预分类结果"""
    category: Literal["asset", "expense", "income", "other"] = Field(
        description="截图类型：asset=资产持仓, expense=消费支出, income=收入, other=其它/无关"
    )
    confidence: float = Field(default=0.9, description="分类置信度 0-1")
    reason: str = Field(default="", description="分类理由（一句话）")


class ExpenseRecord(BaseModel):
    """单条消费记录"""
    date: str = Field(default="", description="消费日期，格式 YYYY-MM-DD 或 MM-DD")
    counterparty: str = Field(default="", description="商户名/交易对方")
    amount: float = Field(description="消费金额（正数）")
    category_primary: str = Field(default="其他", description="一级分类：餐饮/交通/购物/日用/娱乐/居住/月供/医疗/教育/通讯/旅行/其他")
    is_essential: bool = Field(default=False, description="是否为生活必需消费（餐饮/交通/日用/居住/医疗/通讯/教育=必需）")
    description: str = Field(default="", description="原始描述文本")


class ExpenseData(BaseModel):
    """消费截图提取结果"""
    total_expense: float = Field(default=0.0, description="汇总消费金额")
    total_expense_from_summary: float = Field(default=0.0, description="从截图汇总行直接提取的总支出（如微信/支付宝的'总支出'行）")
    records: List[ExpenseRecord] = Field(default_factory=list, description="消费记录列表")

    def to_legacy_expenses(self) -> list:
        """转换为 pipeline _write_expenses_tx 使用的旧格式"""
        class _ExpenseList(list):
            """list 子类，用于挂载 _total_from_summary 属性"""
            pass
        result = _ExpenseList()
        for r in self.records:
            if r.amount > 0:
                result.append({
                    "date": r.date,
                    "amount": r.amount,
                    "counterparty": r.counterparty,
                    "category": r.category_primary,
                    "is_essential": r.is_essential,
                    "description": r.description,
                })
        # 附加截图原始汇总金额，供 pipeline 消费路径使用
        result._total_from_summary = self.total_expense_from_summary
        return result


class IncomeRecord(BaseModel):
    """单条收入记录"""
    date: str = Field(default="", description="收入日期")
    source: str = Field(default="", description="收入来源")
    amount: float = Field(description="收入金额（正数）")
    category_primary: str = Field(default="工资", description="一级分类：工资/理财收益/转账收入/退款/其他")
    description: str = Field(default="", description="原始描述")


class IncomeData(BaseModel):
    """收入截图提取结果"""
    total_income: float = Field(default=0.0, description="汇总收入金额")
    records: List[IncomeRecord] = Field(default_factory=list, description="收入记录列表")
