"""
VE5 消费档次模型
================
根据用户收入、支出和所在城市，自动推导消费档次，并生成价格系数。
用户可手动调整档次。

四档模型：
    - 经济型 (economy):  系数 0.7  — 精打细算，追求性价比
    - 标准型 (standard): 系数 1.0  — 正常消费，不铺张不拮据
    - 优质型 (premium):  系数 1.3  — 注重品质，愿意为好东西付费
    - 轻奢型 (luxury):   系数 1.8  — 消费自由，追求体验

推导逻辑：
    1. 读取用户月收入、月必需支出、所在城市
    2. 计算可支配收入 = 月收入 - 月必需支出
    3. 根据可支配收入 vs 城市人均消费水平，判定档次
    4. 用户可手动覆盖

使用方式：
    from core.consumption_tier import get_tier_config, get_tier_prompt_snippet
    config = get_tier_config()
    snippet = get_tier_prompt_snippet()
"""

import json
import sqlite3
import logging
import time as _time
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional, Literal

logger = logging.getLogger("ve5.consumption_tier")

from app_paths import DATA_DIR, DB_PATH

# ── 城市人均月消费参考（2026年估算，单位：元）──
_CITY_CONSUMPTION_LEVELS: Dict[str, float] = {
    # 一线城市
    "北京": 5500, "上海": 5800, "广州": 4800, "深圳": 5200,
    # 新一线
    "杭州": 4500, "成都": 3800, "武汉": 3500, "南京": 4200,
    "重庆": 3200, "天津": 3800, "苏州": 4000, "西安": 3200,
    "长沙": 3300, "青岛": 3500, "郑州": 3000, "东莞": 3500,
    "宁波": 3800, "合肥": 3000, "佛山": 3200, "沈阳": 3000,
    # 二线代表
    "厦门": 4000, "福州": 3200, "昆明": 2800, "大连": 3300,
    "济南": 3200, "无锡": 3500, "温州": 3000, "石家庄": 2600,
    "哈尔滨": 2500, "长春": 2400, "南昌": 2700, "贵阳": 2500,
    "南宁": 2500, "太原": 2400, "乌鲁木齐": 2800, "兰州": 2500,
    "海口": 3000, "银川": 2300, "西宁": 2400, "呼和浩特": 2600,
    "拉萨": 3000,
}

# 默认值（未知城市）
_DEFAULT_CONSUMPTION = 3000.0

# ── 档次定义 ──
TierName = Literal["economy", "standard", "premium", "luxury"]

_TIER_CONFIG: Dict[str, Dict[str, Any]] = {
    "economy": {
        "name": "经济型",
        "label": "精打细算",
        "multiplier": 0.7,
        "description": "以性价比为首要考虑，关注折扣和优惠，适合预算紧张或储蓄优先的用户",
        "daily_food_budget_percent": 0.08,  # 日伙食费占月可支配收入比例
        "entertainment_budget_percent": 0.05,
    },
    "standard": {
        "name": "标准型",
        "label": "均衡消费",
        "multiplier": 1.0,
        "description": "日常消费不铺张不拮据，该花的花，该省的省，适合大多数城市白领",
        "daily_food_budget_percent": 0.10,
        "entertainment_budget_percent": 0.08,
    },
    "premium": {
        "name": "优质型",
        "label": "品质生活",
        "multiplier": 1.3,
        "description": "注重食材品质和购物体验，愿意为优质商品支付溢价，常去精品超市和品牌店",
        "daily_food_budget_percent": 0.12,
        "entertainment_budget_percent": 0.12,
    },
    "luxury": {
        "name": "轻奢型",
        "label": "消费自由",
        "multiplier": 1.8,
        "description": "消费自由度高，追求顶级食材和高端体验，不设硬性预算上限",
        "daily_food_budget_percent": 0.15,
        "entertainment_budget_percent": 0.15,
    },
}

# ── 存储路径 ──
_TIER_FILE = DATA_DIR / "consumption_tier.json"


def _load_user_tier() -> Optional[str]:
    """读取用户手动设置的档次"""
    if not _TIER_FILE.exists():
        return None
    try:
        data = json.loads(_TIER_FILE.read_text(encoding="utf-8"))
        return data.get("tier")
    except Exception:
        return None


def _save_user_tier(tier: str):
    """保存用户手动设置的档次"""
    _TIER_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TIER_FILE.write_text(
        json.dumps({"tier": tier, "updated_at": datetime.now().isoformat()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _get_user_financial_data() -> Dict[str, float]:
    """从数据库读取用户财务数据"""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row

        # 月收入（取最近3个月平均）
        income = conn.execute(
            """SELECT AVG(amount) as avg_income FROM (
                SELECT SUM(amount) as amount FROM transactions 
                WHERE transaction_type='income' 
                GROUP BY SUBSTR(transaction_date, 1, 7)
                ORDER BY SUBSTR(transaction_date, 1, 7) DESC LIMIT 3
            )"""
        ).fetchone()
        monthly_income = income["avg_income"] if income and income["avg_income"] else 0

        # 月必需支出（取最近3个月平均）
        expense = conn.execute(
            """SELECT AVG(amount) as avg_expense FROM (
                SELECT SUM(amount) as amount FROM transactions 
                WHERE transaction_type='expense' AND (is_essential=1 OR is_essential='1')
                GROUP BY SUBSTR(transaction_date, 1, 7)
                ORDER BY SUBSTR(transaction_date, 1, 7) DESC LIMIT 3
            )"""
        ).fetchone()
        monthly_essential = expense["avg_expense"] if expense and expense["avg_expense"] else 0

        # 如果必需支出为0，回退到总支出
        if monthly_essential == 0:
            total_expense = conn.execute(
                """SELECT AVG(amount) as avg_expense FROM (
                    SELECT SUM(amount) as amount FROM transactions 
                    WHERE transaction_type='expense'
                    GROUP BY SUBSTR(transaction_date, 1, 7)
                    ORDER BY SUBSTR(transaction_date, 1, 7) DESC LIMIT 3
                )"""
            ).fetchone()
            monthly_essential = total_expense["avg_expense"] if total_expense and total_expense["avg_expense"] else 0

        conn.close()
        return {
            "monthly_income": round(monthly_income, 2),
            "monthly_essential": round(monthly_essential, 2),
        }
    except Exception as e:
        logger.warning(f"[TIER] 财务数据读取失败: {e}")
        return {"monthly_income": 0, "monthly_essential": 0}


def _get_city_consumption_level(city: str) -> float:
    """获取城市人均月消费水平"""
    if not city:
        return _DEFAULT_CONSUMPTION
    # 精确匹配
    if city in _CITY_CONSUMPTION_LEVELS:
        return _CITY_CONSUMPTION_LEVELS[city]
    # 模糊匹配（去掉"市"后缀）
    city_clean = city.rstrip("市")
    for c, level in _CITY_CONSUMPTION_LEVELS.items():
        if c.rstrip("市") == city_clean:
            return level
    # 模糊匹配（子串）
    for c, level in _CITY_CONSUMPTION_LEVELS.items():
        if city_clean in c or c in city_clean:
            return level
    return _DEFAULT_CONSUMPTION


# ── 缓存（避免单次请求内重复检测）──
_tier_cache: Dict[str, Any] = {}
_TIER_CACHE_TTL = 30  # 秒

def detect_tier(city: str = "") -> str:
    """
    自动检测用户消费档次。

    推导逻辑：
        可支配收入 = 月收入 - 月必需支出
        档次判定：
            - 可支配收入 < 城市人均消费 × 0.5  → 经济型
            - 可支配收入 < 城市人均消费 × 1.0  → 标准型
            - 可支配收入 < 城市人均消费 × 2.0  → 优质型
            - 可支配收入 >= 城市人均消费 × 2.0 → 轻奢型
        如果无收入数据，默认标准型
    """
    cache_key = f"detect:{city}"
    cached = _tier_cache.get(cache_key)
    if cached and (_time.time() - cached["ts"] < _TIER_CACHE_TTL):
        return cached["value"]

    finance = _get_user_financial_data()
    income = finance["monthly_income"]
    essential = finance["monthly_essential"]
    disposable = income - essential

    city_level = _get_city_consumption_level(city)

    if income == 0:
        logger.info("[TIER] 无收入数据，默认标准型")
        _tier_cache[cache_key] = {"ts": _time.time(), "value": "standard"}
        return "standard"

    ratio = disposable / city_level if city_level > 0 else 0

    if ratio < 0.5:
        tier = "economy"
    elif ratio < 1.0:
        tier = "standard"
    elif ratio < 2.0:
        tier = "premium"
    else:
        tier = "luxury"

    logger.info(
        f"[TIER] 自动检测: 收入={income:.0f} 必需={essential:.0f} "
        f"可支配={disposable:.0f} 城市={city}({city_level:.0f}) "
        f"比率={ratio:.2f} → {tier}"
    )
    _tier_cache[cache_key] = {"ts": _time.time(), "value": tier}
    return tier


def get_effective_tier(city: str = "") -> str:
    """
    获取当前生效的消费档次。
    优先使用用户手动设置的档次，否则自动检测。
    """
    manual = _load_user_tier()
    if manual and manual in _TIER_CONFIG:
        logger.info(f"[TIER] 使用手动设置: {manual}")
        return manual
    return detect_tier(city)


def get_tier_config(tier: str = None) -> Dict[str, Any]:
    """
    获取指定档次的完整配置。

    Args:
        tier: 档次名，默认取当前生效档次

    Returns:
        {name, label, multiplier, description, ...}
    """
    if tier is None:
        tier = get_effective_tier()
    return _TIER_CONFIG.get(tier, _TIER_CONFIG["standard"])


def set_tier(tier: str) -> bool:
    """
    手动设置消费档次。

    Args:
        tier: economy | standard | premium | luxury
    """
    if tier not in _TIER_CONFIG:
        logger.warning(f"[TIER] 无效档次: {tier}")
        return False
    _save_user_tier(tier)
    logger.info(f"[TIER] 手动设置为: {tier}")
    return True


def reset_tier():
    """清除手动设置，恢复自动检测"""
    if _TIER_FILE.exists():
        _TIER_FILE.unlink()
        logger.info("[TIER] 已清除手动设置，恢复自动检测")


def get_all_tiers() -> list:
    """获取所有档次列表（供前端下拉选择）"""
    return [
        {
            "id": tid,
            "name": cfg["name"],
            "label": cfg["label"],
            "multiplier": cfg["multiplier"],
            "description": cfg["description"],
        }
        for tid, cfg in _TIER_CONFIG.items()
    ]


def get_tier_prompt_snippet(city: str = "") -> str:
    """
    获取用于 LLM prompt 的消费档次提示片段。
    包含档次信息、价格系数、预算建议。
    """
    tier = get_effective_tier(city)
    config = get_tier_config(tier)
    finance = _get_user_financial_data()
    city_level = _get_city_consumption_level(city)

    # 基于档次计算建议日预算
    disposable = finance["monthly_income"] - finance["monthly_essential"]
    if disposable <= 0:
        disposable = city_level  # 无收入数据时用城市均值

    daily_food = disposable * config["daily_food_budget_percent"]
    daily_entertainment = disposable * config["entertainment_budget_percent"]

    lines = [
        f"## 消费档次",
        f"用户消费档次：{config['name']}（{config['label']}）",
        f"价格系数：{config['multiplier']}（标准型=1.0，经济型=0.7，优质型=1.3，轻奢型=1.8）",
        f"消费特征：{config['description']}",
        f"建议日伙食预算：约 ¥{daily_food:.0f}",
        f"建议周娱乐预算：约 ¥{daily_entertainment * 7:.0f}",
        "",
        "价格映射规则：",
        f"- 以标准型价格（1.0系数）为基准",
        f"- 本档次系数 {config['multiplier']}，即标准价格的 {config['multiplier']:.0%}",
        f"- 经济型用户选择社区菜市场、平价超市（如钱大妈、永辉）",
        f"- 标准型用户选择综合超市（如永辉、盒马奥莱、大润发）",
        f"- 优质型用户选择精品超市（如盒马鲜生、Ole'、山姆）",
        f"- 轻奢型用户选择高端超市和进口食材（如city'super、久光）",
        "",
        f"注意：档次由系统根据用户收入和支出自动判定，用户可手动调整。",
        f"当前档次判定依据：月收入约 ¥{finance['monthly_income']:.0f}，",
        f"月必需支出约 ¥{finance['monthly_essential']:.0f}，",
        f"所在城市人均消费约 ¥{city_level:.0f}。",
    ]
    return "\n".join(lines)


def get_tier_status(city: str = "") -> Dict[str, Any]:
    """
    获取当前档次状态（供前端展示）。
    当用户已手动设置档次时，不再调用 detect_tier 避免重复查库和噪音日志。
    """
    manual_tier = _load_user_tier()

    if manual_tier and manual_tier in _TIER_CONFIG:
        # 手动设置已生效，不需要调用 detect_tier
        logger.info(f"[TIER] 使用手动设置: {manual_tier}")
        return {
            "effective_tier": manual_tier,
            "auto_detected_tier": None,
            "manual_tier": manual_tier,
            "is_manual": True,
            "config": get_tier_config(manual_tier),
            "all_tiers": get_all_tiers(),
        }

    auto_tier = detect_tier(city)
    return {
        "effective_tier": auto_tier,
        "auto_detected_tier": auto_tier,
        "manual_tier": None,
        "is_manual": False,
        "config": get_tier_config(auto_tier),
        "all_tiers": get_all_tiers(),
    }