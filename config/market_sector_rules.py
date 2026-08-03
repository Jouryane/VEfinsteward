"""
市场×板块分类规则
================
定义持仓产品的资本市场和行业板块分类。

资本市场分类：
  A股 / 美股 / 港股 / 债券 / 黄金 / QDII混合 / 未分类

行业板块分类（仅股票类持仓有意义）：
  科技/半导体 / 消费 / 医药 / 金融 / 新能源 / 红利/价值 / 基建/地产 / 交运/公用 / 其他

匹配优先级（先匹配到即停止）：
  1. 黄金/保障类 → market=黄金, sector=null
  2. 纯债券/纯固收 → market=债券, sector=null
  3. 流动类（现金/货币） → market=null, sector=null（不参与旭日图）
  4. 特定市场关键词 → 确定市场
  5. 特定板块关键词 → 确定板块
  6. 无法匹配 → market=未分类, sector=其他
"""

from typing import Dict, List, Optional, Tuple

# ──────────────────────────────────────
# 市场关键词（优先匹配）
# ──────────────────────────────────────
MARKET_KEYWORDS: Dict[str, List[str]] = {
    "A股": [
        # 指数/ETF
        "沪深300", "中证500", "上证50", "中证1000", "中证A50",
        "科创50", "创业板", "红利低波", "红利ETF", "中证红利",
        "国证2000", "中证全指",
        # 个股（A股特有）
        "春秋航空", "大秦铁路", "招商银行", "中国平安", "贵州茅台",
        "宁德时代", "比亚迪", "中国中免", "海天味业", "恒瑞医药",
        "美的集团", "格力电器", "紫金矿业", "中国神华",
    ],
    "美股": [
        "纳斯达克", "纳指", "标普500", "标普", "SP500", "S&P",
        "QQQ", "SPY", "VOO", "VTI", "AAPL",
        "道琼斯", "DOW", "罗素",
    ],
    "港股": [
        "恒生", "恒指", "港股通", "H股",
    ],
    "黄金": [
        "黄金", "GOLD", "金ETF",
    ],
    "债券": [
        "国债", "国开", "农发", "进出口", "政金债",
        "信用债", "短融", "存单", "同业存单",
        "纯债", "利率债", "中短债",
        "亚洲总收益债券",  # 摩根亚洲总收益债券人民币对冲
    ],
}

# ──────────────────────────────────────
# 板块关键词（在市场确定后匹配）
# ──────────────────────────────────────
SECTOR_KEYWORDS: Dict[str, List[str]] = {
    "科技/半导体": [
        "科技", "芯片", "半导体", "AI", "人工智能",
        "纳斯达克", "纳指", "信息技术", "计算机",
        "软件", "互联网", "云计算", "数据中心",
        "生物科技",  # 广发纳斯达克生物科技
    ],
    "消费": [
        "消费", "白酒", "食品", "饮料", "零售",
        "可选消费", "必选消费", "主要消费",
    ],
    "医药": [
        "医药", "医疗", "创新药", "医疗器械", "CRO",
        "健康", "生物", "基因",
    ],
    "金融": [
        "金融", "银行", "保险", "证券", "券商",
        "非银金融", "信托",
    ],
    "新能源": [
        "新能源", "光伏", "锂电", "电车", "储能",
        "碳中和", "绿色能源", "风电",
    ],
    "红利/价值": [
        "红利", "价值", "高股息", "低波", "低估值",
        "红利低波", "中证红利", "高股息策略",
    ],
    "基建/地产": [
        "基建", "地产", "建筑", "建材", "工程机械",
        "城投", "REITs",
    ],
    "交运/公用": [
        "航空", "铁路", "交运", "物流", "高速",
        "电力", "公用事业", "水务", "燃气",
    ],
}

# ──────────────────────────────────────
# QDII混合规则（跨市场，无法单归）
# ──────────────────────────────────────
QDII_MIXED_KEYWORDS = [
    "全球多元", "全球配置", "全球精选", "全球平衡",
    "全球机遇", "全球增长",
    "优势企业混合",  # 招商优势企业混合A（主要A股但也可能含港股）
]

# ──────────────────────────────────────
# 股票名称 → 硬编码分类（最高优先级）
# 针对现有用户持仓的特殊处理
# ──────────────────────────────────────
HARDCODED_CLASSIFICATIONS: Dict[str, Tuple[str, Optional[str]]] = {
    "摩根全球多元配置人民币C": ("QDII混合", "其他"),
    "摩根亚洲总收益债券人民币对冲": ("债券", None),
    "摩根日本精选股票(QDII)A": ("美股", "其他"),  # 日本股票，归类到美股市场（非A股非港股）
    "招商优势企业混合A": ("A股", "消费"),
    "广发纳斯达克生物科技(QDII)C": ("美股", "科技/半导体"),
    "天弘标普500A": ("美股", "科技/半导体"),
    "南方纳斯达克100指数发起(QDII)A": ("美股", "科技/半导体"),
    "纳指100ETF": ("美股", "科技/半导体"),
    "工银黄金ETF": ("黄金", None),
    "招行黄金账户": ("黄金", None),
    "红利低波ETF": ("A股", "红利/价值"),
    "春秋航空": ("A股", "交运/公用"),
    "大秦铁路": ("A股", "红利/价值"),
    "证券可用资金": (None, None),  # 流动类不参与
    "活期存款": (None, None),
    "朝朝宝": (None, None),
}


def classify_holding(name: str, asset_class: str) -> Dict[str, object]:
    """
    对单个持仓进行市场+板块分类。

    Args:
        name: 持仓名称
        asset_class: 四级分类（liquid/aggressive/stable/protection）

    Returns:
        {
            "market": str | None,      # 资本市场
            "sector": str | None,      # 行业板块
            "source": str,             # "hardcoded" / "rule" / "excluded"
        }
    """
    # 流动类不参与市场分类
    if asset_class == "liquid":
        return {"market": None, "sector": None, "source": "excluded"}

    # 保障类（黄金）直接确定
    if asset_class == "protection":
        return {"market": "黄金", "sector": None, "source": "rule"}

    # 1. 硬编码匹配（最高优先级）
    if name in HARDCODED_CLASSIFICATIONS:
        m, s = HARDCODED_CLASSIFICATIONS[name]
        return {"market": m, "sector": s, "source": "hardcoded"}

    # 2. 市场关键词匹配
    matched_market = None
    for market, keywords in MARKET_KEYWORDS.items():
        for kw in keywords:
            if kw in name:
                matched_market = market
                break
        if matched_market:
            break

    # 3. QDII混合检测
    if matched_market is None:
        for kw in QDII_MIXED_KEYWORDS:
            if kw in name:
                matched_market = "QDII混合"
                break

    # 4. 未匹配到市场的进取类 → 尝试推断
    if matched_market is None and asset_class == "aggressive":
        # 含 QDII → 美股或QDII混合
        if "QDII" in name.upper():
            matched_market = "QDII混合"
        # 含"混合" → A股
        elif "混合" in name:
            matched_market = "A股"
        # 含"ETF"或"指数"但无市场关键词 → A股
        elif "ETF" in name or "指数" in name:
            matched_market = "A股"
        else:
            matched_market = "未分类"

    # 5. 未匹配到市场的稳健类 → 债券
    if matched_market is None and asset_class == "stable":
        matched_market = "债券"

    # 6. 板块匹配（仅在确定了股票类市场时）
    matched_sector = None
    if matched_market in ("A股", "美股", "港股", "QDII混合"):
        for sector, keywords in SECTOR_KEYWORDS.items():
            for kw in keywords:
                if kw in name:
                    matched_sector = sector
                    break
            if matched_sector:
                break
        if matched_sector is None:
            matched_sector = "其他"

    return {"market": matched_market, "sector": matched_sector, "source": "rule"}


def classify_all_holdings(holdings: List[Dict]) -> Dict[str, object]:
    """
    批量分类持仓。

    Args:
        holdings: [{"name": str, "asset_class": str, "current_value": float}, ...]

    Returns:
        {
            "markets": [...],  # 按市场分组的结构
            "holdings": [...],  # 每个持仓的分类结果
            "concentration": {...},  # 集中度指标
        }
    """
    from pathlib import Path
    import json
    from app_paths import DATA_DIR

    # 加载手动分类覆盖
    manual = {}
    cls_path = DATA_DIR / "holding_classifications.json"
    if cls_path.exists():
        try:
            data = json.loads(cls_path.read_text(encoding="utf-8"))
            manual = data.get("classifications", {})
        except Exception:
            pass

    results = []
    market_map = {}  # market -> [{name, amount, sector, holdings_list}]

    for h in holdings:
        name = h.get("name", "")
        asset_class = h.get("asset_class", "")
        amount = h.get("current_value", 0)

        # 手动分类优先
        if name in manual:
            mc = manual[name]
            market = mc.get("market")
            sector = mc.get("sector")
            source = mc.get("source", "manual")
        else:
            cls = classify_holding(name, asset_class)
            market, sector, source = cls["market"], cls["sector"], cls["source"]

        results.append({
            "name": name,
            "asset_class": asset_class,
            "current_value": amount,
            "market": market,
            "sector": sector,
            "source": source,
        })

        # 跳过不参与旭日图的（流动类）
        if market is None:
            continue

        if market not in market_map:
            market_map[market] = {"market": market, "amount": 0, "sectors": {}}
        market_map[market]["amount"] += amount

        sector_key = sector or market  # 债券/黄金用市场名作为sector
        if sector_key not in market_map[market]["sectors"]:
            market_map[market]["sectors"][sector_key] = {
                "sector": sector if sector != market else None,
                "amount": 0,
                "holdings": [],
            }
        market_map[market]["sectors"][sector_key]["amount"] += amount
        market_map[market]["sectors"][sector_key]["holdings"].append(name)

    # 转换为数组格式
    markets_list = []
    for m in market_map.values():
        sectors_list = []
        for s in m["sectors"].values():
            sectors_list.append({
                "sector": s["sector"],
                "amount": round(s["amount"], 2),
                "holdings": s["holdings"],
            })
        markets_list.append({
            "market": m["market"],
            "amount": round(m["amount"], 2),
            "sectors": sectors_list,
        })

    # 集中度指标（HHI）
    total_investable = sum(m["amount"] for m in markets_list)
    hhi = 0
    max_sector = ""
    max_sector_pct = 0
    all_sectors = []
    sector_set = set()
    for m in markets_list:
        for s in m["sectors"]:
            sk = s["sector"] or m["market"]
            if sk not in sector_set:
                sector_set.add(sk)
                all_sectors.append({"sector": sk, "amount": s["amount"]})
    for s in all_sectors:
        pct = s["amount"] / total_investable if total_investable > 0 else 0
        hhi += pct * pct
        if pct > max_sector_pct:
            max_sector_pct = pct
            max_sector = s["sector"]

    if hhi < 0.15:
        hhi_label = "分散"
    elif hhi < 0.25:
        hhi_label = "适度集中"
    else:
        hhi_label = "过度集中"

    return {
        "markets": markets_list,
        "holdings": results,
        "concentration": {
            "hhi": round(hhi, 3),
            "hhi_label": hhi_label,
            "max_sector": max_sector,
            "max_sector_pct": round(max_sector_pct * 100, 1),
            "market_count": len(markets_list),
            "sector_count": len(sector_set),
        },
    }