"""
VE4 资产分类规则（统一数据源）
==============================

本文件定义资产四级分类的完整规则，作为：
1. LLM prompt 的注入文本（确保分类一致性）
2. 代码端分类逻辑的统一数据源
3. 文档化的分类标准

分类体系：
    流动类 (liquid)   → 日常备用金、高流动性资产
    稳健类 (stable)   → 债券、银行理财、固收产品
    进取类 (aggressive) → 股票、偏股基金、指数基金、QDII等权益类
    保障类 (protection) → 黄金、保险、年金等保障/避险资产

重要规则：
    - 黄金被视为真实货币，属于保障类资产（而非另类/商品类）
    - 黄金ETF、纸黄金、黄金活期、黄金理财等均归入保障类
    - 保险、年金、寿险、健康险等均归入保障类

命名规范：
    - 常量: VE4_ALLOC_RULES_*
    - 函数: ve4_alloc_rules_*
"""

from typing import Dict, List


# ════════════════════════════════════════════════════════════════
# 四级分类规则（核心数据结构）
# ════════════════════════════════════════════════════════════════

VE4_ALLOC_RULES = {
    "liquid": {
        "label": "流动类",
        "description": "高流动性资产，可随时用于日常支出或应急",
        "color": "#22d3ee",
        "keywords": [
            "活期", "可用资金", "现金", "存款", "货币基金", "货币", "余额宝",
            "零钱通", "朝朝宝", "天天宝", "零钱", "余额", "T+0", "通知存款",
            "活期存款", "活期宝", "现金宝", "现金管理", "国库现金", "活钱",
        ],
        "exclude_keywords": [],
    },
    "stable": {
        "label": "稳健类",
        "description": "中低风险固定收益资产，追求本金安全和稳健收益",
        "color": "#34d399",
        "keywords": [
            "债券", "债", "理财", "固收", "纯债", "短债", "中短债", "国债",
            "企业债", "同业存单", "结构性存款", "协议存款", "定期", "大额存单",
            "银行理财", "稳健", "R1", "R2", "PR1", "PR2", "中低风险", "低风险",
            "可转债", "转债", "信用债", "利率债", "政策性金融债",
        ],
        "exclude_keywords": [
            "股票", "偏股", "指数", "ETF", "QDII", "黄金", "商品",
            "R4", "R5", "PR4", "PR5", "权益类", "股票型", "混合类", "高风险",
        ],
    },
    "aggressive": {
        "label": "进取类",
        "description": "中高风险权益类资产，追求长期增值",
        "color": "#6366f1",
        "keywords": [
            "股票", "A股", "港股", "美股", "基金", "指数", "混合", "偏股",
            "ETF", "LOF", "QDII", "FOF", "量化", "增强", "蓝筹", "成长",
            "价值", "红利", "纳斯达克", "标普", "道琼斯", "场内", "海外",
            "全球", "日本", "亚洲", "新兴市场", "高风险", "R4", "R5",
            "PR4", "PR5", "中风险", "权益类", "股票型", "混合类",
        ],
        "exclude_keywords": [
            "债券", "债基", "货币", "稳健", "固收", "纯债", "短债", "中短债",
            "黄金", "保险", "年金",
        ],
    },
    "protection": {
        "label": "保障类",
        "description": "避险/保障资产，包括黄金（真实货币）和保险产品",
        "color": "#f59e0b",
        "keywords": [
            "黄金", "保险", "年金", "寿险", "健康险", "重疾险", "意外险",
            "医疗险", "保障", "贵金属", "白银", "铂金", "黄金ETF", "黄金etf",
            "纸黄金", "黄金活期", "黄金理财", "黄金积存", "积存金", "黄金定投",
            "黄金账户", "黄金钱包", "实物金", "金条", "金币",
        ],
        "exclude_keywords": [],
    },
}


# ════════════════════════════════════════════════════════════════
# 黄金特殊规则（重要：黄金属于保障类）
# ════════════════════════════════════════════════════════════════

VE4_GOLD_RULES = {
    "classification": "protection",
    "description": "黄金被视为真实货币，属于保障类资产，而非商品类或另类资产",
    "include_products": [
        "黄金ETF", "黄金etf", "纸黄金", "黄金活期", "黄金理财",
        "黄金积存", "积存金", "黄金定投", "黄金账户", "黄金钱包",
        "实物金", "金条", "金币", "AU9999", "AU999", "AU100g",
        "上海金", "伦敦金", "COMEX黄金", "黄金期货", "黄金期权",
    ],
}


# ════════════════════════════════════════════════════════════════
# 旧分类体系到四级分类的映射（用于兼容历史数据）
# ════════════════════════════════════════════════════════════════

VE4_LEGACY_TO_FOUR_LEVEL = {
    "cash": "liquid",
    "fixed_income": "stable",
    "equity": "aggressive",
    "alternative": "protection",
    "commodity": "protection",
}


# ════════════════════════════════════════════════════════════════
# 生成 LLM Prompt 注入文本
# ════════════════════════════════════════════════════════════════

def ve4_alloc_rules_to_prompt_text() -> str:
    """生成分类规则的文本描述，用于注入LLM prompt"""
    lines = []
    lines.append("【资产分类规则】")
    lines.append("")
    lines.append("以下是资产四级分类标准，请严格按照此标准对金融产品进行分类：")
    lines.append("")

    for key, rule in VE4_ALLOC_RULES.items():
        lines.append(f"1. {rule['label']} ({key})")
        lines.append(f"   - 说明：{rule['description']}")
        lines.append(f"   - 关键词：{', '.join(rule['keywords'])}")
        if rule["exclude_keywords"]:
            lines.append(f"   - 排除：{', '.join(rule['exclude_keywords'])}")
        lines.append("")

    lines.append("【黄金特殊规则】")
    lines.append("")
    lines.append("黄金被视为真实货币，属于保障类资产，而非商品类或另类资产。")
    lines.append("以下产品均归入保障类：")
    lines.append(", ".join(VE4_GOLD_RULES["include_products"]))
    lines.append("")

    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# 代码端分类函数
# ════════════════════════════════════════════════════════════════

def ve4_alloc_rules_classify(product_name: str) -> str:
    """根据产品名称进行四级分类"""
    name_lower = product_name.lower()
    name = product_name

    protection_kw = VE4_ALLOC_RULES["protection"]["keywords"]
    if any(kw in name or kw.lower() in name_lower for kw in protection_kw):
        return "protection"

    liquid_kw = VE4_ALLOC_RULES["liquid"]["keywords"]
    if any(kw in name or kw.lower() in name_lower for kw in liquid_kw):
        return "liquid"

    stable_kw = VE4_ALLOC_RULES["stable"]["keywords"]
    stable_exclude = VE4_ALLOC_RULES["stable"]["exclude_keywords"]
    if (any(kw in name or kw.lower() in name_lower for kw in stable_kw) and
        not any(kw in name or kw.lower() in name_lower for kw in stable_exclude)):
        return "stable"

    aggressive_kw = VE4_ALLOC_RULES["aggressive"]["keywords"]
    aggressive_exclude = VE4_ALLOC_RULES["aggressive"]["exclude_keywords"]
    if (any(kw in name or kw.lower() in name_lower for kw in aggressive_kw) and
        not any(kw in name or kw.lower() in name_lower for kw in aggressive_exclude)):
        return "aggressive"

    return "aggressive"


def ve4_alloc_rules_legacy_convert(legacy_class: str) -> str:
    """将旧分类转换为四级分类"""
    return VE4_LEGACY_TO_FOUR_LEVEL.get(legacy_class, "aggressive")


# ════════════════════════════════════════════════════════════════
# 去重规则
# ════════════════════════════════════════════════════════════════

VE4_DUPLICATE_RULES = {
    "name_similarity_threshold": 0.85,
    "amount_tolerance_pct": 0.05,
    "description": (
        "当两个产品满足以下条件时，视为同一产品：\n"
        "1. 名称高度相似（编辑距离相似度≥85%，或仅后缀/括号内容不同）\n"
        "2. 金额差异在5%以内\n"
        "\n"
        "名称相似度判断规则：\n"
        "- 去除括号内容后比较（如'摩根日本精选A' vs '摩根日本精选股票(QDII)A'）\n"
        "- 去除常见后缀后比较（A/B/C类、ETF、联接等）\n"
        "- 去除空格和标点后比较\n"
        "\n"
        "跨平台同产品识别：\n"
        "- 基金直销平台和代销平台的同一产品名称可能略有不同\n"
        "- 同一基金代码对应不同名称的视为同一产品\n"
        "- 金额完全相同或接近的同名产品视为同一持仓"
    ),
}


def ve4_alloc_rules_is_duplicate(name1: str, name2: str, amount1: float, amount2: float) -> bool:
    """判断两个产品是否为重复项"""
    if not name1 or not name2:
        return False

    normalized1 = _normalize_name(name1)
    normalized2 = _normalize_name(name2)

    if normalized1 == normalized2:
        amount_diff = abs(amount1 - amount2)
        max_amount = max(abs(amount1), abs(amount2), 1)
        if amount_diff / max_amount <= VE4_DUPLICATE_RULES["amount_tolerance_pct"]:
            return True

    similarity = _string_similarity(normalized1, normalized2)
    if similarity >= VE4_DUPLICATE_RULES["name_similarity_threshold"]:
        amount_diff = abs(amount1 - amount2)
        max_amount = max(abs(amount1), abs(amount2), 1)
        if amount_diff / max_amount <= VE4_DUPLICATE_RULES["amount_tolerance_pct"]:
            return True

    return False


def _normalize_name(name: str) -> str:
    """标准化产品名称（用于去重比较）"""
    import re
    s = name.strip().lower()
    s = re.sub(r'\s+', '', s)
    s = re.sub(r'[（\(].*?[）\)]', '', s)
    s = re.sub(r'[（\(].*', '', s)
    suffixes = ['a', 'b', 'c', 'etf', '联接', '联接基金', 'qdii', 'lof', 'fof']
    for suf in suffixes:
        if s.endswith(suf):
            s = s[:-len(suf)]
    return s.strip()


def _string_similarity(s1: str, s2: str) -> float:
    """计算字符串相似度（Levenshtein距离）"""
    if not s1 or not s2:
        return 0.0
    if s1 == s2:
        return 1.0

    len1, len2 = len(s1), len(s2)
    max_len = max(len1, len2)

    if max_len == 0:
        return 1.0

    import difflib
    return difflib.SequenceMatcher(None, s1, s2).ratio()


# ════════════════════════════════════════════════════════════════
# 摘要提取规则（用于LLM）
# ════════════════════════════════════════════════════════════════

VE4_SUMMARY_RULES = {
    "fund_summary": (
        "对于公募基金，摘要应包含：\n"
        "- 基金类型（股票型/混合型/债券型/指数型/QDII等）\n"
        "- 投资方向或策略\n"
        "- 主要投资市场（A股/港股/美股/全球等）\n"
        "- 一句话描述其核心特点\n"
        "\n"
        "示例：\n"
        "- '易方达蓝筹精选混合：偏股混合型基金，主要投资A股和港股的蓝筹股票'\n"
        "- '招商中证白酒指数：指数型基金，跟踪中证白酒指数，投资白酒行业'\n"
        "- '华夏纳斯达克100ETF(QDII)：QDII指数基金，跟踪纳斯达克100指数'\n"
    ),
    "finance_summary": (
        "对于银行理财产品，摘要应包含：\n"
        "- 产品类型（固收类/混合类/结构性等）\n"
        "- 风险等级（R1-R5）\n"
        "- 期限（如30天、90天、180天等）\n"
        "- 预期收益范围（如2.5%-3.2%）\n"
        "- 一句话描述其核心特点\n"
        "\n"
        "示例：\n"
        "- '招商银行月月宝：固收类理财，R2级，每月开放，预期收益2.8%-3.1%'\n"
        "- '工商银行安享利：混合类理财，R3级，180天封闭，预期收益3.0%-3.5%'\n"
    ),
    "stock_summary": (
        "对于股票，摘要应包含：\n"
        "- 所属行业或板块\n"
        "- 一句话描述其业务特点\n"
        "\n"
        "示例：\n"
        "- '贵州茅台：白酒行业龙头，中国高端白酒代表'\n"
        "- '腾讯控股：互联网科技巨头，社交和游戏领域领导者'\n"
    ),
    "default_summary": (
        "对于其他类型产品，简要描述其本质特征即可。\n"
        "示例：\n"
        "- '余额宝：货币基金，高流动性，低风险'\n"
        "- '黄金ETF：追踪黄金价格的交易所交易基金'\n"
    ),
}


def ve4_alloc_rules_summary_prompt() -> str:
    """生成摘要提取规则的prompt文本"""
    lines = []
    lines.append("【金融产品摘要提取规则】")
    lines.append("")
    lines.append("请为每个金融产品生成简短的一句话摘要（50字以内）：")
    lines.append("")

    for key, rule in VE4_SUMMARY_RULES.items():
        lines.append(rule)
        lines.append("")

    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# LLM 提取字段定义
# ════════════════════════════════════════════════════════════════

VE4_EXTRACT_FIELDS = {
    "name": "产品名称（如基金名、股票名、理财产品名）",
    "value": "持仓金额（数字）",
    "type": "资产类别（四级分类：liquid/aggressive/stable/protection）",
    "account": "基金账户/证券账户/银行账户",
    "summary": "产品摘要（一句话描述产品类型和特点）",
}


def ve4_alloc_rules_extract_fields_prompt() -> str:
    """生成提取字段定义的prompt文本"""
    lines = []
    lines.append("【提取字段定义】")
    lines.append("")
    lines.append("请提取以下字段：")
    for field, desc in VE4_EXTRACT_FIELDS.items():
        lines.append(f"- {field}: {desc}")
    lines.append("")
    return "\n".join(lines)