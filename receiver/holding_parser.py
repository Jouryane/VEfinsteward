"""
VE4 持仓快照解析器
==================
从 OCR 提取的文本中识别基金/证券/银行账户持仓信息，
转换为 asset_holdings 表记录。

支持的快照类型：
    - fund_platform   : 基金平台持仓（如支付宝、天天基金）
    - broker          : 证券账户持仓（如东方财富、华泰）
    - bank_overview   : 银行账户总览（如招商银行）

设计原则：
    - 只提取文本中明确存在的字段，不猜测、不推断
    - 无法识别的格式返回空列表，由 pipeline 存入 RAG 原始文本
"""

import re
import logging
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

from core.asset_classification_rules import (
    VE4_ALLOC_RULES, VE4_LEGACY_TO_FOUR_LEVEL,
    ve4_alloc_rules_classify, ve4_alloc_rules_legacy_convert,
)

logger = logging.getLogger("ve4.holding_parser")


def ve4_parse_holdings(raw_text: str, source_file: str = "") -> Dict:
    """
    从 OCR 文本中解析持仓信息。

    Args:
        raw_text: OCR 提取的原始文本
        source_file: 来源文件名

    Returns:
        dict: {
            "snapshot_type": str,       # fund_platform / broker / bank_overview / unknown
            "account_summary": dict,     # 账户级汇总（总资产等）
            "holdings": [dict],          # 持仓明细列表
            "liquid_items": [dict],      # 活钱/流动资产明细
        }
    """
    text = raw_text.strip()
    if not text:
        return {"snapshot_type": "unknown", "account_summary": {}, "holdings": [], "liquid_items": []}

    # ── OCR 噪声预处理：去除中文字符间空格、去除常见幻觉符号 ──
    text = _denoise_ocr(text)
    logger.info(f"[HOLDING-PARSER] 去噪后文本 {len(text)} 字符，前100: {repr(text[:100])}")

    # 按优先级尝试各解析器（证券先于基金，因为证券关键词更严格）
    for parser_fn in [_parse_bank_overview, _parse_broker, _parse_fund_platform]:
        result = parser_fn(text, source_file)
        if result and result["snapshot_type"] != "unknown":
            logger.info(f"[HOLDING-PARSER] 识别为 {result['snapshot_type']}，"
                        f"提取 {len(result['holdings'])} 条持仓 + {len(result['liquid_items'])} 条活钱")
            return result
        elif result:
            logger.info(f"[HOLDING-PARSER] {parser_fn.__name__} 返回 unknown")

    return {"snapshot_type": "unknown", "account_summary": {}, "holdings": [], "liquid_items": []}


# ══════════════════════════════════════════════════════════
# 解析器：证券账户
# ══════════════════════════════════════════════════════════

def _parse_broker(text: str, source_file: str) -> Optional[Dict]:
    """识别证券账户持仓（东方财富、华泰等）"""
    # 特征词：至少需要2个以上才判定为证券账户
    # 含 OCR 容错变体（如"当日盈亏"可能被识别为"当日盗亏"）
    broker_keywords = [
        "证券市值", "仓位", "当日盈亏", "当日盗亏",
        "证券", "委托成交", "分时", "持仓盈亏",
        "理财资产", "可用", "可取",
    ]
    kw_count = sum(1 for kw in broker_keywords if kw in text)
    if kw_count < 2:
        return None

    summary = {}
    holdings = []
    liquid_items = []

    lines = text.split("\n")

    # ── 解析账户汇总（支持多行：标签行 + 下一行数值） ──
    for i, line in enumerate(lines):
        line = line.strip()
        # 同行匹配：总资产：170,782.81
        m = re.search(r"总资产[：:\s]*([\d,]+\.?\d*)", line)
        if m:
            summary["total_assets"] = _parse_number(m.group(1))
            summary["account_key"] = "证券账户"
            continue
        # 多行匹配：总资产在当前行，数值在下一行
        if "总资产" in line and i + 1 < len(lines):
            m = re.search(r"([\d,]+\.\d{2})", lines[i + 1].strip())
            if m and "total_assets" not in summary:
                summary["total_assets"] = _parse_number(m.group(1))
                summary["account_key"] = "证券账户"
                continue
        # 证券市值
        m = re.search(r"证券市值[：:\s]*([\d,]+\.?\d*)", line)
        if m:
            summary["securities_value"] = _parse_number(m.group(1))
            continue
        if "证券市值" in line and i + 1 < len(lines):
            # 下一行可能包含多个数字：证券市值 盈亏 可用
            nums = re.findall(r"([\d,]+\.\d{2})", lines[i + 1].strip())
            if nums:
                if "securities_value" not in summary:
                    summary["securities_value"] = _parse_number(nums[0])
                if len(nums) >= 3 and "available_cash" not in summary:
                    summary["available_cash"] = _parse_number(nums[-1])
                continue
        # 可用
        m = re.search(r"可用[：:\s]*([\d,]+\.?\d*)", line)
        if m:
            summary["available_cash"] = _parse_number(m.group(1))
            continue

    # ── 可用资金作为 liquid_item ──
    if summary.get("available_cash") and summary["available_cash"] > 0:
        liquid_items.append({
            "product_name": "证券可用资金",
            "current_value": summary["available_cash"],
            "asset_class": "cash_equivalent",
            "liquidity_level": "high",
            "account_key": summary.get("account_key", "证券账户"),
            "is_classified": 1,
            "inference_source": f"broker_snapshot:{source_file}",
        })

    # ── 解析持仓明细 ──
    # 格式: 名称 — 持仓N, 现价X, 成本Y, 盈亏Z (P%)
    holding_block = False
    for line in lines:
        line = line.strip()
        if "持仓" in line and ("/" in line or "股票" in line or "基金" in line):
            holding_block = True
            continue

        if holding_block and "—" in line:
            parts = line.split("—")
            if len(parts) >= 2:
                name = parts[0].strip()
                detail = parts[1].strip()

                # ── 置信度检查：名称含过多乱码符号则跳过 ──
                if _name_has_excessive_garbage(name):
                    logger.debug(f"[BROKER] 跳过乱码名称: {repr(name)}")
                    continue

                info = _parse_holding_detail(detail)
                if info:
                    info["product_name"] = name
                    info["source_file"] = source_file
                    info["account_key"] = summary.get("account_key", "证券账户")
                    info["asset_class"] = _classify_equity(name)
                    info["is_classified"] = 1
                    info["inference_source"] = f"broker_snapshot:{source_file}"
                    holdings.append(info)

    return {
        "snapshot_type": "broker",
        "account_summary": summary,
        "holdings": holdings,
        "liquid_items": liquid_items,
    }


# ══════════════════════════════════════════════════════════
# 解析器：基金平台持仓
# ══════════════════════════════════════════════════════════

def _parse_fund_platform(text: str, source_file: str) -> Optional[Dict]:
    """识别基金平台持仓（支付宝基金、天天基金等）"""
    kw_checks = {kw: (kw in text) for kw in ["基金持仓", "持仓收益", "昨日收益"]}
    logger.info(f"[FUND] 关键词检查: {kw_checks}")
    if not any(kw_checks.values()):
        return None

    summary = {}
    holdings = []

    lines = text.split("\n")

    # ── 解析账户汇总 ──
    for line in lines:
        line = line.strip()
        # 总资产
        m = re.search(r"([\d,]+\.\d{2})", line)
        if m and "总资产" in text[:text.find(m.group(1)) + 20 if m.group(1) in text else 0]:
            summary["total_assets"] = _parse_number(m.group(1))
            summary["account_key"] = "基金账户"
            continue
        # 昨日收益
        m = re.search(r"昨日收益[^:：]*[：:]\s*([+-]?[\d,]+\.?\d*)", line)
        if m and "summary" not in str(summary):
            summary["daily_pnl"] = _parse_number(m.group(1))

    # 找到总资产行后面的数字作为 total_assets
    for i, line in enumerate(lines):
        if "总资产" in line:
            # 检查下一行
            if i + 1 < len(lines):
                m = re.match(r"^([\d,]+\.\d{2})$", lines[i + 1].strip())
                if m:
                    summary["total_assets"] = _parse_number(m.group(1))
                    summary["account_key"] = "基金账户"
            break

    # ── 解析持仓明细 ──
    # 基金平台格式：基金名（独占一行）+ 金额行 + 收益行 + 涨跌幅行
    # OCR 噪声变体：金额可能和基金名在同一行，如 "摩根日本精选 A 2,882.09元"
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        # 先检查当前行是否直接包含 "XXXX.XX元" 或 "XXXX.XX" 模式（基金名+金额同行）
        am = re.search(r"([\d,]+\.\d{2})\s*(元|$)", line)
        if am:
            amount_prefix = line[:am.start()].strip()
            # 如果金额前面有中文/字母文字，说明基金名和金额在同一行
            if amount_prefix and not re.match(r"^[\d,+\-.\s%]+$", amount_prefix):
                name = amount_prefix
                # 置信度检查：跳过乱码名称
                if _name_has_excessive_garbage(name):
                    i += 1
                    continue
                # 额外检查：名称不应以 + 或 - 或纯数字开头（那是收益数据行）
                if re.match(r"^[+\-\d]", name.strip()):
                    i += 1
                    continue
                current_value = _parse_number(am.group(1))
                logger.info(f"[FUND] 同行匹配: name={repr(name)}, value={current_value}")
                # 查找持仓收益和涨跌幅（在后续行中）
                holding_pnl = 0.0
                return_pct = 0.0
                for j in range(i + 1, min(i + 4, len(lines))):
                    jl = lines[j].strip()
                    pm = re.search(r"持仓收益[：:]\s*([+-]?[\d,]+\.?\d*)", jl)
                    if pm:
                        holding_pnl = _parse_number(pm.group(1))
                    rm = re.search(r"持仓涨跌幅[：:]\s*([+-]?[\d,]+\.?\d*)%?", jl)
                    if rm:
                        return_pct = _parse_number(rm.group(1))

                cost_basis = current_value - holding_pnl if holding_pnl else current_value

                holdings.append({
                    "product_name": name,
                    "source_file": source_file,
                    "account_key": summary.get("account_key", "基金账户"),
                    "asset_class": _classify_fund(name),
                    "current_value": current_value,
                    "cost_basis": max(0, cost_basis),
                    "unrealized_pnl": holding_pnl,
                    "holding_return_pct": return_pct,
                    "is_classified": 1,
                    "inference_source": f"fund_snapshot:{source_file}",
                })
                i += 1
                continue
            # 如果金额前面没有有效文字，可能是纯金额行，留给下方逻辑处理

        # 基金名：排除只包含数字/标点/百分号的行，排除汇总行
        # 不再排除"我的持有"（OCR可能把它和基金名粘连）
        if not re.match(r"^[\d,+\-.\s%]+$", line) and not any(kw in line for kw in [
            "总资产", "昨日收益", "持仓收益", "累计收益", "币种", "基金持仓",
            "证券市值", "持仓盈亏", "可用", "可取", "委托成交", "分时",
            "理财资产", "当日盈亏", "当日盗亏",
        ]):
            # 置信度检查：跳过乱码名称
            if _name_has_excessive_garbage(line):
                i += 1
                continue
            # 检查下一行是否有金额
            if i + 1 < len(lines):
                amount_line = lines[i + 1].strip()
                m = re.search(r"([\d,]+\.\d{2})\s*(元|$)", amount_line)
                if m:
                    name = line
                    current_value = _parse_number(m.group(1))
                    # 查找持仓收益和涨跌幅
                    holding_pnl = 0.0
                    return_pct = 0.0
                    for j in range(i + 2, min(i + 5, len(lines))):
                        jl = lines[j].strip()
                        pm = re.search(r"持仓收益[：:]\s*([+-]?[\d,]+\.?\d*)", jl)
                        if pm:
                            holding_pnl = _parse_number(pm.group(1))
                        rm = re.search(r"持仓涨跌幅[：:]\s*([+-]?[\d,]+\.?\d*)%?", jl)
                        if rm:
                            return_pct = _parse_number(rm.group(1))

                    cost_basis = current_value - holding_pnl if holding_pnl else current_value

                    holdings.append({
                        "product_name": name,
                        "source_file": source_file,
                        "account_key": summary.get("account_key", "基金账户"),
                        "asset_class": _classify_fund(name),
                        "current_value": current_value,
                        "cost_basis": max(0, cost_basis),
                        "unrealized_pnl": holding_pnl,
                        "holding_return_pct": return_pct,
                        "is_classified": 1,
                        "inference_source": f"fund_snapshot:{source_file}",
                    })
                    i += 3  # 跳过金额行和收益行
                    continue
        i += 1

    if not holdings:
        return None

    return {
        "snapshot_type": "fund_platform",
        "account_summary": summary,
        "holdings": holdings,
        "liquid_items": [],
    }


# ══════════════════════════════════════════════════════════
# 解析器：银行账户总览
# ══════════════════════════════════════════════════════════

def _parse_bank_overview(text: str, source_file: str) -> Optional[Dict]:
    """识别银行账户总览（招商银行等）"""
    if not any(kw in text for kw in ["账户总览", "活钱", "投资"]):
        return None

    summary = {}
    holdings = []
    liquid_items = []

    lines = text.split("\n")

    # ── 解析活钱 ──
    # OCR 噪声变体：金额可能在"活钱"同一行（空格分隔），如 "活钱 23,888.30"
    for i, line in enumerate(lines):
        line = line.strip()
        if "活钱" in line:
            # 优先在当前行搜索金额
            m = re.search(r"([\d,]+\.\d{2})", line)
            if m:
                liquid_items.append({
                    "product_name": "活钱",
                    "current_value": _parse_number(m.group(1)),
                    "asset_class": "cash_equivalent",
                    "liquidity_level": "high",
                    "account_key": "银行账户",
                    "is_classified": 1,
                    "inference_source": f"bank_snapshot:{source_file}",
                })
                break
            # 回退：检查下一行
            if i + 1 < len(lines):
                m = re.match(r"^([\d,]+\.\d{2})$", lines[i + 1].strip())
                if m:
                    liquid_items.append({
                        "product_name": "活钱",
                        "current_value": _parse_number(m.group(1)),
                        "asset_class": "cash_equivalent",
                        "liquidity_level": "high",
                        "account_key": "银行账户",
                        "is_classified": 1,
                        "inference_source": f"bank_snapshot:{source_file}",
                    })
            break

    # ── 解析投资持仓明细 ──
    # 优先尝试单行匹配（理想格式），失败则用多行扫描
    single_line_found = False

    for line in lines:
        line = line.strip()
        m = re.match(
            r"^(.+?)\s*[—\-]\s*"
            r"昨日收益\s*([+-]?[\d,]+\.?\d*)\s*[,\s，]+\s*"
            r"持仓收益\s*([+-]?[\d,]+\.?\d*)\s*[,\s，]+\s*"
            r"持仓金额\s*([\d,]+\.?\d*)",
            line
        )
        if m:
            single_line_found = True
            name = m.group(1).strip()
            holding_pnl = _parse_number(m.group(3))
            current_value = _parse_number(m.group(4))
            cost_basis = current_value - holding_pnl if holding_pnl else current_value
            return_pct = round((holding_pnl / cost_basis * 100) if cost_basis > 0 else 0, 2)
            holdings.append({
                "product_name": name, "source_file": source_file,
                "account_key": "银行账户", "asset_class": _classify_fund(name),
                "current_value": current_value, "cost_basis": max(0, cost_basis),
                "unrealized_pnl": holding_pnl, "holding_return_pct": return_pct,
                "is_classified": 1, "inference_source": f"bank_snapshot:{source_file}",
            })

    # 多行扫描模式：基金名独占一行，周围行包含"持仓金额"和数值
    if not single_line_found:
        _bank_noise_keywords = [
            "昨日收益", "昨目收益",  # OCR 可能将"日"识别为"目"
            "持仓收益", "持仓金额", "活钱", "投资", "基金",
            "快速赎回", "数字人民币", "多享基金", "朝朝宝",
            "活期存款", "直接可用", "账户总览", "总资产",
            "总盈亏", "累计收益", "市值占比",
            "委托", "成交", "分时", "五日", "一月", "三月", "一年",
            "可赎回", "确认份额", "七日年化",
        ]
        for i, line in enumerate(lines):
            line = line.strip()
            # 识别基金名行：以中文/英文开头，不含噪声关键词
            if (line and re.match(r"^[\u4e00-\u9fff\u3400-\u4dbfA-Za-z]", line)
                    and not any(kw in line for kw in _bank_noise_keywords)
                    and len(line) > 2  # 至少3个字符（过滤掉单字噪声行）
                    and not re.match(r"^[\d,+\-.\s%]+$", line)):  # 不全是数字/标点
                # 置信度检查：名称含过多乱码符号则跳过
                if _name_has_excessive_garbage(line):
                    continue
                # 在后续 3 行中搜索持仓金额和数值
                current_value = 0.0
                holding_pnl = 0.0
                for j in range(i + 1, min(i + 4, len(lines))):
                    jl = lines[j].strip()
                    # "持仓金额X" 或 "持仓金额 X" 或 "金额X"
                    vm = re.search(r"持仓金额\s*([+-]?[\d,]+\.?\d*)", jl)
                    if vm:
                        current_value = _parse_number(vm.group(1))
                    pm = re.search(r"持仓收益\s*([+-]?[\d,]+\.?\d*)", jl)
                    if pm:
                        holding_pnl = _parse_number(pm.group(1))
                    # 如果行尾有独立数字（可能是持仓金额被打散）
                    if not vm:
                        tm = re.search(r"([\d,]+\.\d{2})\s*$", jl)
                        if tm and current_value == 0:
                            current_value = _parse_number(tm.group(1))

                if current_value > 0:
                    cost_basis = current_value - holding_pnl if holding_pnl else current_value
                    return_pct = round((holding_pnl / cost_basis * 100) if cost_basis > 0 else 0, 2)
                    holdings.append({
                        "product_name": line, "source_file": source_file,
                        "account_key": "银行账户", "asset_class": _classify_fund(line),
                        "current_value": current_value, "cost_basis": max(0, cost_basis),
                        "unrealized_pnl": holding_pnl, "holding_return_pct": return_pct,
                        "is_classified": 1, "inference_source": f"bank_snapshot:{source_file}",
                    })

    if not holdings and not liquid_items:
        return None

    return {
        "snapshot_type": "bank_overview",
        "account_summary": summary,
        "holdings": holdings,
        "liquid_items": liquid_items,
    }


# ══════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════

def _denoise_ocr(text: str) -> str:
    """去除 Tesseract OCR 噪声：字符间空格、幻觉符号、多余换行"""
    import unicodedata
    # 去除中文字符之间的空格（Tesseract 常见问题）
    text = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', text)
    # 去除 ASCII 范围内的幻觉符号
    text = re.sub(r'[<>¢©®§¶•·→←↑↓★☆◆●■□▪▫»«]', '', text)
    # 去除全角标点后的杂乱字符
    text = re.sub(r'[）)]\s*$', '', text, flags=re.MULTILINE)
    # 合并连续空行为单个换行
    text = re.sub(r'\n{3,}', '\n\n', text)
    # 去除行首尾空白
    text = '\n'.join(line.strip() for line in text.split('\n'))
    return text.strip()


def _name_has_excessive_garbage(name: str) -> bool:
    """
    判断名称是否含有过多 OCR 乱码（置信度过低）。
    规则：名称中非中文/英文/数字/常见标点的字符占比超过 30% 则判定为乱码。
    """
    if not name or len(name) < 2:
        return True
    # 统计有效字符（中文、英文、数字、常见括号/斜杠/点）
    valid = re.sub(r'[\u4e00-\u9fff\u3400-\u4dbfA-Za-z0-9\(\)（）/ ．\.\-—]', '', name)
    garbage_ratio = len(valid) / len(name)
    # 含特殊乱码模式
    garbage_patterns = ['巳', 'aiisiz', '"', '$', '~-']
    if any(p in name for p in garbage_patterns):
        return True
    return garbage_ratio > 0.30


def _has_keyword(text: str, keywords: list, fuzzy: bool = True) -> bool:
    """检查文本中是否包含关键词（支持 OCR 噪声模糊匹配）"""
    clean = _denoise_ocr(text) if fuzzy else text
    for kw in keywords:
        if kw in clean:
            return True
        # 模糊匹配：去除关键词中的空格再匹配
        if fuzzy and re.sub(r'\s', '', kw) in re.sub(r'\s', '', clean):
            return True
    return False


def _parse_number(s: str) -> float:
    """解析数字字符串（去逗号、处理正负号）"""
    if not s:
        return 0.0
    try:
        return float(s.replace(",", "").replace(" ", ""))
    except ValueError:
        return 0.0


def _parse_holding_detail(detail: str) -> Optional[Dict]:
    """解析 '持仓N, 现价X, 成本Y, 盈亏Z (P%)' 格式"""
    info = {}
    patterns = {
        "holding_quantity": r"持仓\s*([\d,]+)",
        "current_price": r"现价\s*([\d,]+\.?\d*)",
        "cost_basis_price": r"成本\s*([\d,]+\.?\d*)",
        "unrealized_pnl": r"盈亏\s*([+-]?[\d,]+\.?\d*)",
        "holding_return_pct": r"\(([+-]?[\d,]+\.?\d*)%\)",
    }
    for key, pattern in patterns.items():
        m = re.search(pattern, detail)
        if m:
            val = _parse_number(m.group(1))
            if key == "holding_return_pct":
                info[key] = val
            else:
                info[key] = val

    if "current_price" not in info and "holding_quantity" not in info:
        return None

    # 计算 current_value 和 cost_basis
    qty = info.get("holding_quantity", 0)
    price = info.get("current_price", 0)
    cost_price = info.get("cost_basis_price", 0)

    info["current_value"] = round(qty * price, 2)
    info["cost_basis"] = round(qty * cost_price, 2) if cost_price > 0 else info["current_value"]

    return info


def _classify_equity(name: str) -> str:
    """根据名称推断证券资产类别（使用统一规则）"""
    four_level = ve4_alloc_rules_classify(name)
    mapping = {"liquid": "cash", "stable": "fixed_income", "aggressive": "equity", "protection": "alternative"}
    return mapping.get(four_level, "equity")


def _classify_fund(name: str) -> str:
    """根据基金名称推断资产类别（使用统一规则）"""
    four_level = ve4_alloc_rules_classify(name)
    mapping = {"liquid": "cash", "stable": "fixed_income", "aggressive": "equity", "protection": "alternative"}
    return mapping.get(four_level, "equity")
