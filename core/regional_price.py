"""
VE5 区域物价上下文生成器
==========================
为 life_planner 提供用户所在区域的物价参考信息。

四重数据源：
    1. 高德地图 API — 周边商铺列表（超市/菜市场/便利店）
    2. 消费价格 RAG — 用户历史购物价格记录
    3. 政府公开物价 — WebSearch 实时搜索（非爬虫）
    4. 季节波动 + 消费档次 — 本地模型辅助价格区间

使用方式：
    from core.regional_price import build_regional_context
    context = build_regional_context()
    # 返回字符串，直接拼接到 LLM prompt 中
"""

import json
import logging
import time as _time
import urllib.request
import urllib.parse
from pathlib import Path
from typing import List, Dict, Any
from datetime import datetime

logger = logging.getLogger("ve5.regional_price")

from app_paths import DATA_DIR

# ── 用户位置配置 ──
_USER_LOC_FILE = DATA_DIR / "user_location.json"


def _get_user_location() -> Dict[str, str]:
    """读取用户设置的位置信息"""
    default = {"province": "", "city": "", "district": "", "county": "", "address": "", "amap_key": ""}
    if not _USER_LOC_FILE.exists():
        return default
    try:
        data = json.loads(_USER_LOC_FILE.read_text(encoding="utf-8"))
        return {**default, **data}
    except Exception:
        return default


def _save_user_location(
    province: str = "", city: str = "", district: str = "", county: str = "",
    address: str = "", amap_key: str = ""
):
    """保存用户位置信息"""
    data = {
        "province": province, "city": city, "district": district,
        "county": county, "address": address, "amap_key": amap_key,
    }
    _USER_LOC_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ════════════════════════════════════════════════════════
# 数据源 1：高德地图 API（周边商铺）
# ════════════════════════════════════════════════════════

def _get_nearby_shops(city: str, amap_key: str) -> List[Dict[str, Any]]:
    """
    通过高德地图 API 搜索城市内的超市/菜市场/便利店。
    由于需要经纬度，这里使用城市级搜索（text 接口）而非 around 接口。
    """
    if not amap_key or not city:
        return []

    keywords = "超市|菜市场|生鲜|便利店|水果店"
    url = (
        f"https://restapi.amap.com/v3/place/text?"
        f"key={amap_key}&city={urllib.parse.quote(city)}&"
        f"keywords={urllib.parse.quote(keywords)}&types=060000|060100|060400|060600&"
        f"offset=10&page=1&extensions=all&children=0"
    )

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "VE5-Client/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if data.get("status") != "1":
            logger.warning(f"[REGIONAL] 高德 API 错误: {data.get('info', 'unknown')}")
            return []

        shops = []
        for p in data.get("pois", [])[:8]:
            shops.append({
                "name": p.get("name", ""),
                "type": p.get("type", ""),
                "address": p.get("address", ""),
                "distance": "",
            })
        return shops
    except Exception as e:
        logger.warning(f"[REGIONAL] 高德 API 请求失败: {e}")
        return []


# ════════════════════════════════════════════════════════
# 数据源 2：消费价格 RAG（用户历史价格）
# ════════════════════════════════════════════════════════

def _get_user_price_history(city: str) -> List[Dict[str, Any]]:
    """从消费价格 RAG 查询用户历史购物记录"""
    try:
        from core.consumer_price_rag import ve5_price_rag_recent
        # 查询最近 8 条记录（不限分类，让用户看到全面的价格）
        records = ve5_price_rag_recent(n=8, location=city)
        return records
    except Exception as e:
        logger.warning(f"[REGIONAL] 消费价格 RAG 查询失败: {e}")
        return []


# ════════════════════════════════════════════════════════
# 数据源 3：政府公开物价（WebSearch）
# ════════════════════════════════════════════════════════

_gov_price_cache: Dict[str, Any] = {}
_GOV_CACHE_TTL = 300  # 5 分钟缓存

def _search_government_prices(city: str, items: List[str]) -> Dict[str, float]:
    """
    通过搜索引擎获取政府公开物价数据。
    使用 DuckDuckGo HTML 接口，无需 API Key。

    Args:
        city: 城市名（如"上海"）
        items: 商品列表（如["鸡蛋", "猪肉", "青菜"]）

    Returns:
        {商品名: 价格, ...}
    """
    prices = {}
    if not city:
        return prices

    # 缓存检查
    cache_key = f"{city}:{','.join(items[:3])}"
    cached = _gov_price_cache.get(cache_key)
    if cached and (_time.time() - cached["ts"] < _GOV_CACHE_TTL):
        return cached["prices"]

    try:
        for item in items[:3]:  # 最多查 3 种商品，减少超时概率
            query = f"{city} 今日菜价 {item} 批发"
            search_url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
            req = urllib.request.Request(
                search_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.0",
                    "Accept": "text/html",
                }
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                html = resp.read().decode("utf-8", errors="ignore")

            # 简单正则：从搜索结果中提取价格数字（如"鸡蛋 12.5 元/斤"）
            import re
            # 匹配 "鸡蛋 12.5 元" 或 "12.5元/斤" 等模式
            pattern = re.compile(
                rf"{re.escape(item)}\s*[:：]?\s*(\d+\.?\d*)\s*[元块]",
                re.IGNORECASE,
            )
            matches = pattern.findall(html)
            if matches:
                # 取出现次数最多的价格（过滤异常值）
                from collections import Counter
                price_counts = Counter([float(m) for m in matches if 0.5 < float(m) < 500])
                if price_counts:
                    prices[item] = round(price_counts.most_common(1)[0][0], 2)

    except Exception as e:
        # DuckDuckGo 从国内访问经常超时，这是已知问题，降为 DEBUG 级别
        logger.debug(f"[REGIONAL] 政府物价搜索未成功（已有RAG和常识兜底）: {e}")

    # 缓存结果（即使为空也缓存，避免短时间内重复超时）
    _gov_price_cache[cache_key] = {"ts": _time.time(), "prices": prices}
    return prices


# ════════════════════════════════════════════════════════
# 统一接口：生成区域物价上下文
# ════════════════════════════════════════════════════════

def build_regional_context() -> str:
    """
    生成区域物价上下文字符串，用于拼接到 life_planner 的 prompt 中。

    返回格式示例：
        用户所在区域：上海浦东
        周边主要商铺：盒马鲜生、永辉超市、钱大妈、浦东农贸市场...
        用户近期购物价格：
        - 鸡蛋 12.8元/盒（盒马，2026-07-20）
        - 猪肉 35元/斤（永辉，2026-07-18）
        政府参考价格（今日）：
        - 鸡蛋 8.5元/斤
        - 猪肉 28元/斤
        季节价格波动提示：
        ...
        消费档次提示：
        ...
    """
    loc = _get_user_location()
    province = loc.get("province", "").strip()
    city = loc.get("city", "").strip()
    district = loc.get("district", "").strip()
    county = loc.get("county", "").strip()
    amap_key = loc.get("amap_key", "").strip()

    if not city:
        return ""  # 用户未设置位置，不附加区域信息

    full_loc = f"{city}{district}" if district else city
    if county:
        full_loc += county
    lines = [f"用户所在区域：{full_loc}"]

    # ── 数据源 1：周边商铺 ──
    shops = _get_nearby_shops(city, amap_key) if amap_key else []
    if shops:
        shop_names = [s["name"] for s in shops[:5]]
        lines.append(f"周边主要商铺：{'、'.join(shop_names)}")
    else:
        lines.append("周边主要商铺：请根据城市常识推断")

    # ── 数据源 2：用户历史价格 ──
    history = _get_user_price_history(full_loc)
    if history:
        lines.append("用户近期购物价格（参考）：")
        for r in history:
            spec = f"/{r['spec']}" if r.get("spec") else ""
            lines.append(f"  - {r['item']}{spec} {r['price']}元（{r['merchant']}，{r['date']}）")
    else:
        lines.append("用户近期购物价格：暂无记录")

    # ── 数据源 3：政府参考价格 ──
    # 只查高频生鲜（避免搜索过多）
    gov_prices = _search_government_prices(city, ["鸡蛋", "猪肉", "青菜", "大米"])
    if gov_prices:
        lines.append("政府批发市场参考价格（今日，单位：元/斤或元/公斤）：")
        for item, price in gov_prices.items():
            lines.append(f"  - {item} 约{price}元")
    else:
        lines.append("政府参考价格：搜索未返回结果，请根据城市常识推断")

    # ── 数据源 4：季节价格波动 ──
    try:
        from core.seasonal_price import get_seasonal_prompt_snippet
        seasonal = get_seasonal_prompt_snippet()
        if seasonal:
            lines.append("")
            lines.append("━━━ 季节价格波动参考 ━━━")
            lines.append(seasonal)
    except Exception as e:
        logger.warning(f"[REGIONAL] 季节价格波动获取失败: {e}")

    # ── 数据源 5：消费档次 ──
    try:
        from core.consumption_tier import get_tier_prompt_snippet
        tier_snippet = get_tier_prompt_snippet(city)
        if tier_snippet:
            lines.append("")
            lines.append("━━━ 消费档次参考 ━━━")
            lines.append(tier_snippet)
    except Exception as e:
        logger.warning(f"[REGIONAL] 消费档次获取失败: {e}")

    return "\n".join(lines)


def get_user_city() -> str:
    """获取用户设置的城市（供外部调用）"""
    return _get_user_location().get("city", "")


def set_user_location(
    province: str = "", city: str = "", district: str = "", county: str = "",
    address: str = "", amap_key: str = ""
):
    """设置用户位置信息（供前端/API调用）"""
    _save_user_location(province, city, district, county, address, amap_key)
    logger.info(f"[REGIONAL] 用户位置已更新: {province}/{city}/{district}/{county}")


def get_location_settings() -> Dict[str, str]:
    """获取用户位置设置（供前端展示）"""
    return _get_user_location()