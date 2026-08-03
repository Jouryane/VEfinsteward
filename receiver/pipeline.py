"""
VE4 数据处理管道（精简版）
========================
截图 → OCR → 提取 → 写入 → RAG → 刷新

砍掉的：WorkflowRouter, ModelValidator, DataRestorer, SQLStore, RAGStore, ProfileUpdater,
         4后端注册表, 双轨评分, 3解析器优先级链, 置信度过滤。

保留的：图像预处理, hash去重, ai_gateway统一配置。
新增的：LLM结构化提取, 独立RAG模块。
"""
import json
import re

import os
import sys
import hashlib
import shutil
import sqlite3
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional

# 路径
_RECEIVER_DIR = Path(__file__).parent.resolve()
if str(_RECEIVER_DIR) not in sys.path:
    sys.path.insert(0, str(_RECEIVER_DIR))

from receiver.config import INCOMING_DIR, PROCESSED_DIR, FAILED_DIR, ensure_dirs
from core.asset_classification_rules import ve4_alloc_rules_classify, VE4_LEGACY_TO_FOUR_LEVEL
ensure_dirs()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(INCOMING_DIR.parent / "logs" / "pipeline.log"), encoding="utf-8", mode="a"),
    ]
)
logger = logging.getLogger("ve4.pipeline")

from app_paths import DB_PATH, DATA_DIR
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# 消费提取日志文件（JSONL 格式，长期保存）
_EXPENSE_LOG_FILE = DATA_DIR / "logs" / "expense_extraction_log.jsonl"
_EXPENSE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def process_file(file_path: Path) -> Dict:
    """
    处理单个文件。一步到底。

    Returns:
        {"status": "success"|"failed"|"skipped", "records": int, "error": str|None}
    """
    logger.info(f"[PIPE] {file_path.name}")

    # ── hash 去重 ──
    file_hash = _hash_file(file_path)
    if _is_processed(file_hash):
        logger.info(f"[PIPE] 跳过（重复）: {file_path.name}")
        _move(file_path, PROCESSED_DIR)
        return {"status": "skipped", "records": 0, "error": None}

    try:
        # ── Step 1: OCR ──
        from receiver.ocr_engine import ve4_ocr
        ocr_result = ve4_ocr(file_path)
        if not ocr_result:
            _move(file_path, FAILED_DIR / "ocr_failed")
            return {"status": "failed", "records": 0, "error": "OCR提取失败"}

        raw_text = ocr_result["raw_text"]
        engine_info = {"engine": ocr_result["engine"], "preprocess": ocr_result.get("preprocess", "")}

        # ── OCR 硬短路：空文本/几乎空白截图 → 跳过提取，直接归档 ──
        if len(raw_text.strip()) < 5:
            logger.info(f"[PIPE] OCR文本过短（{len(raw_text.strip())}字符），跳过: {file_path.name}")
            _mark_processed(file_hash, str(file_path))
            _move(file_path, PROCESSED_DIR / datetime.now().strftime("%Y%m%d"))
            return {"status": "skipped_blank", "records": 0, "error": None}

        holdings = []
        expenses = []
        account_summary = {}
        total_from_summary = 0.0

        # ── Step 0: 截图属性预分类（资产/消费/收入/其它）──
        screenshot_category = None
        try:
            from receiver.llm_extractor import ve5_llm_classify_screenshot
            screenshot_category = ve5_llm_classify_screenshot(raw_text)
        except Exception as e:
            logger.warning(f"[PIPE] 截图预分类失败，走默认资产提取: {e}")

        # 按分类走不同提取通道
        if screenshot_category and screenshot_category.category in ("expense", "income"):
            # ── 消费/收入截图：跳过资产提取，走消费/收入通道 ──
            if screenshot_category.category == "expense":
                # 先检测是否为汇总页（支出构成），汇总页直接走正则分类提取
                # 跳过 LLM，避免 LLM 从汇总页 OCR 中提取垃圾数据
                if _is_expense_summary_page(raw_text):
                    logger.info(f"[PIPE] 检测到消费汇总页，跳过 LLM 直接走正则分类提取")
                    expenses = _regex_extract_expenses(raw_text, str(file_path))
                    total_from_summary = getattr(expenses, '_total_from_summary', 0.0) if expenses else 0.0
                else:
                    expenses = _try_llm_extract_expenses(raw_text, str(file_path))
                    total_from_summary = getattr(expenses, '_total_from_summary', 0.0) if expenses else 0.0
                    if not expenses:
                        expenses = _regex_extract_expenses(raw_text, str(file_path))
                        total_from_summary = getattr(expenses, '_total_from_summary', 0.0) if expenses else 0.0
                # 消费截图不产生持仓
                holdings = []
                logger.info(f"[PIPE] 截图分类为「消费」，提取到 {len(expenses)} 条消费记录")
            else:
                # 收入截图
                expenses = _try_llm_extract_income(raw_text, str(file_path))
                holdings = []
                logger.info(f"[PIPE] 截图分类为「收入」，提取到 {len(expenses)} 条收入记录")
        elif screenshot_category and screenshot_category.category == "other":
            # ── 其它/无关截图：跳过所有提取，直接归档 ──
            logger.info(f"[PIPE] 截图分类为「其它」（{screenshot_category.reason}），直接归档")
            _mark_processed(file_hash, str(file_path))
            _move(file_path, PROCESSED_DIR / datetime.now().strftime("%Y%m%d"))
            return {"status": "skipped_other", "records": 0, "error": None}
        else:
            # ── 资产截图（默认）：走原有资产提取流程 ──
            pass  # 继续执行下方原有的 Step 2 代码

        # ── Step 2: 提取结构化数据（仅资产截图或预分类不可用）──
        # 当预分类为 asset 或预分类不可用时执行
        if not expenses:  # 消费/收入通道已提取了数据则跳过资产提取
            # 优先 LLM，只有 LLM 没有抽到真实产品时才回退正则。
            llm_holdings = _filter_holdings(_try_llm_extract(raw_text, str(file_path)))
            if _has_real_product_holdings(llm_holdings):
                holdings = llm_holdings
                logger.info(f"[PIPE] LLM 提取到可信持仓，跳过正则兜底: {len(holdings)} 条")
            else:
                regex_holdings = _filter_holdings(_regex_extract(raw_text, str(file_path)))
                holdings = _merge_holdings(llm_holdings, regex_holdings)

            # ── Step 2a: LLM 复核已禁用 ──
            # 原因：复核不仅耗时（每次10-20秒），还经常返回非JSON格式导致数据丢失。
            # 正则兜底 + 写入时的黑名单过滤已足够。
            # if holdings:
            #     holdings = _try_llm_review(holdings)

            account_summary = _extract_summary(raw_text)

            # 判断是否为证券截图（用于控制汇总项的添加）
            is_broker_screenshot = any(kw in raw_text for kw in ["证券市值", "持仓盈亏", "东方财富", "分时", "委托成交", "场内基金"])
            if not is_broker_screenshot:
                # "股票" 需要独立出现（不是产品名的一部分）
                for m in re.finditer(r'股票', raw_text):
                    before_ok = m.start() == 0 or not re.match(r'[\u4e00-\u9fffA-Za-z0-9]', raw_text[m.start()-1])
                    after_ok = m.end() >= len(raw_text) or not re.match(r'[\u4e00-\u9fffA-Za-z0-9]', raw_text[m.end()])
                    if before_ok and after_ok:
                        is_broker_screenshot = True
                        break

            # 将账户汇总（可用资金、证券市值）补充为持仓记录
            # 仅证券截图添加这些汇总项，避免银行/基金截图被错误识别
            summary_items = []
            if is_broker_screenshot:
                if account_summary.get("available_cash", 0) > 0:
                    summary_items.append({"name": "证券可用资金", "value": account_summary["available_cash"], "type": "liquid", "account": "证券账户"})
                if account_summary.get("securities_value", 0) > 0:
                    summary_items.append({"name": "证券市值", "value": account_summary["securities_value"], "type": "equity", "account": "证券账户"})
            existing_names = {h.get("name", "").strip() for h in holdings}
            for si in summary_items:
                if si["name"] not in existing_names:
                    holdings.append(si)
            holdings = _filter_holdings(holdings)

        # ── 后处理：将 LLM 的通用 account 名替换为实际来源 ──
        holdings = _fix_account_names(holdings, raw_text)

        # ── Step 2b: 消费记录提取（仅资产截图时做正则兜底）──
        if not expenses and (not screenshot_category or screenshot_category.category == "asset"):
            expenses_regex = _regex_extract_expenses(raw_text, str(file_path))
            expenses.extend(expenses_regex)

        # ── Step 3: 写入数据库（事务保护）──
        conn_write = sqlite3.connect(str(DB_PATH))
        try:
            n_holdings = _write_holdings_tx(conn_write, holdings, str(file_path))
            n_expenses = _write_expenses_tx(conn_write, expenses, str(file_path), total_from_summary)
            conn_write.commit()
        except Exception:
            conn_write.rollback()
            raise
        finally:
            conn_write.close()
        n = n_holdings + n_expenses
        logger.info(f"[PIPE] 写入 {n} 条记录（持仓 {n_holdings} + 消费 {n_expenses}）")

        # ── Step 3a: 消费提取本地日志（JSONL，长期保存）──
        if expenses and screenshot_category and screenshot_category.category == "expense":
            try:
                # 判断提取方式：汇总页走正则，明细页走 LLM（失败则正则兜底）
                is_summary = _is_expense_summary_page(raw_text)
                extraction_method = "regex_summary" if is_summary else "llm_or_regex_detail"
                _log_expense_extraction(
                    source=str(file_path),
                    screenshot_category=screenshot_category.category,
                    records=expenses,
                    total_from_summary=total_from_summary,
                    extraction_method=extraction_method
                )
            except Exception as e:
                logger.debug(f"[PIPE] 消费提取日志记录失败（不影响主流程）: {e}")

        # ── Step 4: 记录业务活动（有意义的摘要）──
        try:
            _record_activity(file_path.name, raw_text, holdings, account_summary, n_holdings, n_expenses, screenshot_category.category if screenshot_category else "asset")
        except Exception:
            pass

        # ── Step 5: RAG（独立模块，异步） ──
        from receiver.financial_rag import ve4_financial_rag_store
        ve4_financial_rag_store(raw_text, str(file_path), {
            "holdings": holdings,
            "account_summary": account_summary,
            "snapshot_type": "mixed",
        }, engine_info)

        # ── Step 6: 标记完成 ──
        # 如果提取到数据，正常归档；如果 0 条数据且为资产截图，移到 failed（可能是 LLM 超时）
        total_records = len(holdings) + len(expenses)
        if total_records == 0 and (not screenshot_category or screenshot_category.category == "asset"):
            logger.warning(f"[PIPE] 资产截图提取到 0 条记录，移入 failed（可能 LLM 超时）: {file_path.name}")
            _mark_processed(file_hash, str(file_path))
            _move(file_path, FAILED_DIR / "llm_timeout")
            return {"status": "failed", "records": 0, "error": "LLM 超时或提取失败，0 条记录"}
        _mark_processed(file_hash, str(file_path))
        _move(file_path, PROCESSED_DIR / datetime.now().strftime("%Y%m%d"))

        # ── 广播：记录写入时间戳，前端检测变化后自动刷新 ──
        try:
            marker = DATA_DIR / "last_pipeline_success.txt"
            marker.write_text(datetime.now().isoformat())
        except Exception:
            pass

        return {"status": "success", "records": n, "error": None}

    except Exception as e:
        logger.error(f"[PIPE] 失败: {file_path.name} - {e}", exc_info=True)
        _move(file_path, FAILED_DIR / "error")
        return {"status": "failed", "records": 0, "error": str(e)}


# ══════════════════════════════════════════════
# 提取逻辑
# ══════════════════════════════════════════════

def _try_llm_extract(text: str, source: str) -> list:
    """LLM 提取持仓（可选）"""
    try:
        from receiver.llm_extractor import ve4_llm_extract
        return ve4_llm_extract(text, source) or []
    except Exception:
        return []


def _merge_holdings(llm_items: list, regex_items: list) -> list:
    """合并 LLM 和正则提取结果：LLM 优先，正则补充未覆盖的产品。"""
    result = list(llm_items) if llm_items else []
    if not regex_items:
        return result
    existing_names = {h.get("name", "").strip() for h in result}
    for ri in regex_items:
        name = ri.get("name", "").strip()
        if name and name not in existing_names:
            result.append(ri)
    return result



_SUMMARY_HOLDING_NAMES = {"证券可用资金", "证券市值"}
_INVALID_EXACT_NAMES = {
    "", "XD", "XR", "DR", "股票", "基金", "持仓", "投资", "金额", "市值", "盈亏",
    "总资产", "可用资金", "持仓金额", "持仓市值", "证券账户", "理财资产",
}
_INVALID_NAME_KEYWORDS = (
    "上月支出报告", "看看钱花在哪", "广告", "快速赎回", "查看收益",
    "委托成交", "当日盈亏", "持仓盈亏", "昨日收益", "累计收益",
    "确认份额", "七日年化", "直接可用", "转入", "赎回", "买入",
    "卖出", "撤单", "周享", "多享", "账户总览", "上月支出",
)
# 合法的代码模式（不应被过滤）：6位纯数字股票/基金代码、JY+数字理财代码
_VALID_CODE_PATTERN = re.compile(r'^(?:\d{6}|JY\d+|[A-Z]{2}\d{4,6})$')


def _normalize_name_for_dedup(name: str) -> str:
    """产品名归一化，用于跨账户去重。
    
    规则：
    - 去除空格
    - 去除 (QDII) 后缀
    - 去除 "股票"/"债券" 等二级类型词
    - 去除末尾的A/B/C/E等份额类型字母
    - 统一全角/半角括号
    """
    n = name.replace(' ', '').replace('（', '(').replace('）', ')')
    # 去除 (QDII) 等括号标注
    n = re.sub(r'\([^)]*\)', '', n)
    # 去除常见的二级类型词
    n = n.replace('股票', '').replace('债券', '').replace('指数', '').replace('发起', '')
    # 去除末尾的份额类型字母（A/B/C/E/H等单个大写字母）
    n = re.sub(r'([A-Z])$', '', n)
    return n.strip()


def _has_real_product_holdings(items: list) -> bool:
    """Return True when extraction contains at least one non-summary asset."""
    return any((item.get("name") or "").strip() not in _SUMMARY_HOLDING_NAMES for item in items or [])


def _is_valid_holding(item: dict) -> bool:
    name = (item.get("name") or "")
    if not isinstance(name, str):
        return False
    name = name.strip()
    if name in _INVALID_EXACT_NAMES:
        return False
    if any(kw in name for kw in _INVALID_NAME_KEYWORDS):
        return False
    if re.fullmatch(r"[A-Z]{1,3}", name):
        return False
    if re.fullmatch(r"投资\s*[\d,]+(?:\.\d+)?", name):
        return False
    # 拦截名称中包含4位以上连续数字的粘连产物
    # 但排除合法代码格式（6位纯数字、JY020212）
    if re.search(r'\d{4,}', name) and not _VALID_CODE_PATTERN.match(name):
        return False
    # 纯代码（无中文字符）不是合法产品名
    # 但汇总项（证券可用资金、证券市值）允许通过
    if name not in _SUMMARY_HOLDING_NAMES:
        if not re.search(r'[\u4e00-\u9fff]', name):
            return False
    value = _parse_num(str(item.get("value", "")))
    if value <= 0 or value > 1_000_000_000:
        return False
    return True


def _filter_holdings(items: list) -> list:
    """Normalize and drop obvious OCR/regex false positives before merge/write."""
    result = []
    seen = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        value = _parse_num(str(item.get("value", "")))
        normalized = {**item, "name": name, "value": value}
        if not _is_valid_holding(normalized):
            logger.info(f"[PIPE] 过滤疑似非持仓项: {name} / {value}")
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result
def _try_llm_review(new_items: list) -> list:
    """LLM 复核持仓（去重 + 过滤非持仓 + 补充分类）

    读取数据库中已有持仓，与新提取数据一并交给 LLM 复核。
    LLM 不可用时返回原始 new_items（不阻塞流程）。
    """
    if not new_items:
        return new_items
    try:
        from receiver.llm_extractor import ve4_llm_review_holdings

        # 读取已有持仓（仅 product_name + current_value + account_key）
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        existing_rows = conn.execute(
            "SELECT product_name, current_value, account_key FROM asset_holdings"
        ).fetchall()
        conn.close()

        existing = [
            {"name": r["product_name"], "value": r["current_value"], "account": r["account_key"]}
            for r in existing_rows
        ]

        reviewed = ve4_llm_review_holdings(new_items, existing)
        if reviewed is not None:
            return reviewed
        # LLM 不可用或复核失败 → 返回原始数据
        return new_items
    except Exception as e:
        logger.warning(f"[PIPE] LLM复核异常: {e}")
        return new_items


def _try_llm_extract_expenses(text: str, source: str) -> list:
    """LLM 提取消费记录（可选，AI不可用时跳过）"""
    try:
        from receiver.llm_extractor import ve5_llm_extract_expenses
        return ve5_llm_extract_expenses(text, source) or []
    except Exception:
        return []


def _try_llm_extract_income(text: str, source: str) -> list:
    """LLM 提取收入记录（可选，AI不可用时跳过）"""
    try:
        from receiver.llm_extractor import ve5_llm_extract_income
        return ve5_llm_extract_income(text, source) or []
    except Exception:
        return []


def _regex_extract(text: str, source: str) -> list:
    """正则提取（兜底）：支持证券、银行、基金三种截图格式。"""
    import re

    lines_raw = [l.strip() for l in text.split('\n') if l.strip()]
    # 预处理：逐行去除中文间空格（不在行间操作，避免多行粘连）
    lines = []
    for line in lines_raw:
        # 去除中文间空格（"招 商 银 行" → "招商银行"）
        line = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', line)
        lines.append(line)
    holdings = []

    # ── 截图类型判断（使用原始文本判断，不受预处理影响）──
    is_broker = any(kw in text for kw in ["证券市值", "持仓盈亏", "可用资金", "分时", "委托成交", "场内基金"])
    # "股票" 需要独立出现（不是产品名的一部分如"摩根日本精选股票"）
    if not is_broker:
        for m in re.finditer(r'股票', text):
            # 检查前后是否有中文字符（说明是产品名的一部分）
            before_ok = m.start() == 0 or not re.match(r'[\u4e00-\u9fffA-Za-z0-9]', text[m.start()-1])
            after_ok = m.end() >= len(text) or not re.match(r'[\u4e00-\u9fffA-Za-z0-9]', text[m.end()])
            if before_ok and after_ok:
                is_broker = True
                break
    is_bank = any(kw in text for kw in ["朝朝宝", "活期存款", "持仓金额", "招商银行"])
    is_fund = any(kw in text for kw in ["基金持仓", "昨日收益", "累计收益", "摩根", "天弘", "南方"])

    # 优先级互斥：证券 > 银行 > 基金
    if is_broker:
        is_bank = False

    # ══════════════════════════════════════════════
    # 证券截图提取（东方财富等）
    # 格式：产品名 代码 数量 单价 盈亏 市值
    # ══════════════════════════════════════════════
    if is_broker and not is_bank:
        for line in lines:
            if _is_meta_line(line) or re.match(r'^[\d,+\-.\s%]+$', line):
                continue
            # 去除除权除息前缀（XD/XR/DR/ST），保留后续产品名（有无空格均处理）
            cleaned_line = re.sub(r'^(?:XD|XR|DR|ST|\*ST)\s*', '', line)
            if not cleaned_line:
                continue
            # Phase 1: 提取行首产品名区域（中文开头 + 中文/字母/数字，遇空格停止）
            full_match = re.match(r'^([\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9]+)', cleaned_line)
            if not full_match:
                continue
            raw_name = full_match.group(1).strip()
            # Phase 2: 剥离尾部代码（6位股票/基金代码、JY理财代码）
            name = re.sub(r'\d{6}$', '', raw_name)
            name = re.sub(r'JY\d*$', '', name)
            if len(name) < 2:
                continue
            # 提取行中产品名区域之后的金额，排除代码格式
            amounts = []
            name_end = full_match.end()
            for m in re.finditer(r'[¥￥]?\s*([\d,]+\.\d{1,2}|[\d,]+)', cleaned_line[name_end:]):
                g1 = (m.group(1) or "").strip()
                if not g1:
                    continue
                cleaned = g1.replace(',', '')
                # 排除6位纯数字（股票代码 600036、基金代码 512690）
                if re.match(r'^\d{6}$', cleaned):
                    continue
                # 排除JY+数字格式（理财代码 JY020212）
                if re.match(r'^JY\d+$', cleaned):
                    continue
                val = _parse_num(g1)
                if val > 100:
                    amounts.append(val)
            if amounts:
                holdings.append({"name": name, "value": amounts[-1], "type": _classify(name), "account": "证券账户"})
        return holdings

    # ══════════════════════════════════════════════
    # 银行截图提取（招商银行等）
    # 格式：产品名 + 跨行金额（"持仓金额 XXXX.XX"）
    # ══════════════════════════════════════════════
    if is_bank:
        i = 0
        while i < len(lines):
            line = lines[i]
            if _is_meta_line(line) or re.match(r'^[\d,+\-.\s%]+$', line):
                i += 1; continue

            # 清理产品名中的尾部代码（如"天弘标普500A 000961" → "天弘标普500A"）
            clean_name = re.sub(r'\s+\d{6}$', '', line).strip()
            # 理财代码如"理财JY020212"
            clean_name = re.sub(r'\s+JY\d+$', '', clean_name).strip()

            # 策略1：当前行是产品名，下一行有"持仓金额"
            if i + 1 < len(lines):
                nxt = lines[i + 1]
                if '持仓金额' in nxt:
                    m = re.search(r'持仓金额\s*([\d,]+\.?\d*)', nxt)
                    if m:
                        val = _parse_num(m.group(1))
                        if val >= 10:
                            holdings.append({"name": clean_name, "value": val, "type": _classify(clean_name), "account": "招商银行"})
                            i += 2
                            continue
                # 策略2：下一行是单独的大额数字（基金持仓常见）
                am = _find_amount_in_line(nxt)
                if am and am['value'] >= 1000 and not _is_meta_line(nxt):
                    holdings.append({"name": clean_name, "value": am['value'], "type": _classify(clean_name), "account": "招商银行"})
                    i += 2
                    continue
            i += 1
        return holdings

    # ══════════════════════════════════════════════
    # 基金截图提取（摩根/天弘等）
    # 格式：产品名单独行 + 金额下行
    # ══════════════════════════════════════════════
    i = 0
    while i < len(lines):
        line = lines[i]
        if _is_meta_line(line) or re.match(r'^[\d,+\-.\s%]+$', line):
            i += 1; continue

        # 清理产品名中的尾部代码（如"摩根中国优势A 000053" → "摩根中国优势A"）
        clean_name = re.sub(r'\s+\d{6}$', '', line).strip()
        clean_name = re.sub(r'\s+JY\d+$', '', clean_name).strip()
        if len(clean_name) < 2:
            i += 1; continue

        # 当前行是产品名，下一行是单独数字
        if i + 1 < len(lines):
            nxt = lines[i + 1]
            # 下一行中可能也包含代码，需要剥离后检查是否为纯数字
            nxt_clean = re.sub(r'\s+\d{6}$', '', nxt).strip()
            nxt_clean = re.sub(r'\s+JY\d+$', '', nxt_clean).strip()
            # 检查下一行是否是纯数字（基金金额格式）
            if re.match(r'^[\d,]+\.?\d*$', nxt_clean.replace(' ', '')):
                val = _parse_num(nxt_clean)
                if val >= 10:
                    account = _infer_account(text)
                    holdings.append({"name": clean_name, "value": val, "type": _classify(clean_name), "account": account})
                    i += 2
                    continue

        i += 1

    return holdings


def _find_amount_in_line(line: str) -> dict:
    """从单行文本中提取第一个金额，支持整数、一位小数、两位小数、千位分隔符。"""
    # 金额格式：支持 ¥/￥ 前缀，支持逗号千位分隔符，支持1-2位小数或整数
    m = re.search(r'[¥￥]?\s*([\d,]+\.\d{1,2}|[\d,]+)\s*(?:元|￥|¥|$)', line)
    if not m:
        m = re.search(r'[¥￥]?\s*([\d,]+\.\d{1,2}|[\d,]+)', line)
    if m:
        g1 = m.group(1) or ""
        if g1.strip():
            val = _parse_num(g1)
            if val > 0:
                return {"value": val, "start": m.start(), "end": m.end(), "raw": m.group(0)}
    return None


def _find_market_value_in_line(line: str, min_val: float = 100) -> dict:
    """从证券持仓行中提取市值金额。
    策略：从后向前匹配，排除6位基金代码，优先带两位小数，过滤过小金额。
    """
    all_matches = list(re.finditer(r'[¥￥]?\s*([\d,]+\.\d{1,2}|[\d,]+)', line))
    if not all_matches:
        return None
    # 从后向前找第一个有效金额（市值通常在最后）
    for m in reversed(all_matches):
        g1 = m.group(1) or ""
        if not g1.strip():
            continue
        val = _parse_num(g1)
        if val < min_val:
            continue
        # 跳过6位纯整数（基金代码，如 512690、513100）
        if re.match(r'^\d{6}$', g1.replace(',', '')):
            continue
        return {"value": val, "start": m.start(), "end": m.end(), "raw": m.group(0)}
    return None


def _extract_product_name(line: str, amount_start: int) -> str:
    """从金额位置向前提取产品名。"""
    prefix = line[:amount_start].strip()
    # 去除常见前缀符号
    prefix = re.sub(r'^[+\-\d$￥¥\s]+', '', prefix)
    # 去除末尾的 "份"、"股"、"元" 等
    prefix = re.sub(r'[份股元\s]+$', '', prefix)
    return prefix


def _is_meta_line(line: str) -> bool:
    """判断是否为元数据行（非产品名行）。"""
    meta_kws = ["总资产", "昨日收益", "累计收益", "币种", "基金持仓",
                "证券市值", "持仓盈亏", "委托成交", "当日盈亏", "当日盗亏", "理财资产",
                "账户总览", "快速赎回", "转入", "赎回", "确认份额", "七日年化",
                "查看收益", "本月剩余", "应还", "直接可用", "周享", "多享",
                "今日收益", "持仓收益", "持仓金额", "可用", "可取"]
    return any(kw in line for kw in meta_kws)


def _infer_account(raw_text: str) -> str:
    """从 OCR 原文推断账户来源名称（非通用名）。"""
    # 精确关键词 → 具体机构名
    if "东方财富" in raw_text:
        return "东方财富证券"
    if "招商银行" in raw_text:
        return "招商银行"
    if "建设银行" in raw_text:
        return "建设银行"
    if "工商银行" in raw_text:
        return "工商银行"
    if "农业银行" in raw_text:
        return "农业银行"
    if "摩根" in raw_text:
        return "摩根基金"
    if "天弘" in raw_text:
        return "天弘基金"
    if "南方" in raw_text:
        return "南方基金"
    if "广发" in raw_text:
        return "广发基金"
    if "华泰" in raw_text:
        return "华泰证券"
    if "中信" in raw_text:
        return "中信证券"
    if "支付宝" in raw_text or "余额宝" in raw_text:
        return "支付宝"
    if "朝朝宝" in raw_text or "活期存款" in raw_text:
        return "银行账户"
    # 兜底逻辑（保持向后兼容）
    if "证券市值" in raw_text or "持仓盈亏" in raw_text:
        return "证券账户"
    if "基金" in raw_text[:200]:
        return "基金账户"
    return "银行账户"


def _fix_account_names(holdings: list, raw_text: str) -> list:
    """后处理：将 LLM 产出的通用 account 名替换为具体来源名。"""
    real_account = _infer_account(raw_text)
    # 如果是通用名（"证券账户"/"基金账户"）则替换
    if real_account not in ("证券账户", "基金账户", "银行账户"):
        for h in holdings:
            acc = h.get("account", "")
            # 如果当前是通用名，替换为具体名
            if acc in ("证券账户", "基金账户", "银行账户", "未知账户"):
                h["account"] = real_account
    return holdings


def _extract_summary(text: str) -> dict:
    """提取账户级汇总"""
    import re
    text_clean = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', text)
    summary = {}

    # 总资产
    m = re.search(r'总资产[：:\s]*([\d,]+\.?\d*)', text_clean)
    if m:
        summary["total_assets"] = _parse_num(m.group(1))

    # 证券市值
    if '证券市值' in text_clean:
        idx = text_clean.index('证券市值')
        rest = text_clean[idx:]
        nm = re.search(r'([\d,]+\.\d{2})', rest[:100])
        if nm:
            summary["securities_value"] = _parse_num(nm.group(1))

    # 可用资金：多层容错匹配
    # 1. 精确匹配"可用资金"后跟金额
    am = re.search(r'可用资金[：:\s]*([\d,]+\.?\d*)', text_clean)
    # 2. 匹配"可用"（可能OCR把"资金"识别错）后跟金额
    if not am:
        for m in re.finditer(r'可用[资金资\s]*[：:\s]*([\d,]+\.?\d*)', text_clean):
            val = _parse_num(m.group(1))
            if val > 100:
                am = m
                break
    # 3. 匹配"可 用"等OCR断字情况
    if not am:
        text_no_space = text_clean.replace(' ', '')
        am = re.search(r'可用资金[：:]*([\d,]+\.?\d*)', text_no_space)
    # 4. 查找独立的"可用"关键词附近的大额数字
    if not am:
        for m in re.finditer(r'可用', text_clean):
            # 在"可用"之后100字符内找最大金额
            after = text_clean[m.end():m.end()+100]
            nums = re.findall(r'[\d,]+\.\d{2}', after)
            if nums:
                max_val = max(_parse_num(n) for n in nums)
                if max_val > 100:
                    summary["available_cash"] = max_val
                    break
    if am:
        summary["available_cash"] = _parse_num(am.group(1))

    return summary


def _summary_to_liquid(summary: dict) -> list:
    """将汇总中的可用资金转为活钱记录"""
    items = []
    for key, label in [("available_cash", "证券可用资金")]:
        if summary.get(key, 0) > 0:
            items.append({"name": label, "value": summary[key], "type": "liquid", "account": "证券账户"})
    return items


def _classify(name: str) -> str:
    """简单分类（使用统一规则）"""
    four_level = ve4_alloc_rules_classify(name)
    mapping = {"liquid": "cash", "stable": "fixed_income", "aggressive": "equity", "protection": "alternative"}
    return mapping.get(four_level, "equity")


def _parse_num(s: str) -> float:
    if not s:
        return 0.0
    cleaned = s.replace(",", "").strip()
    if not cleaned:
        return 0.0
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _record_activity(file_name: str, raw_text: str, holdings: list, account_summary: dict, n_holdings: int, n_expenses: int = 0, screenshot_category: str = "asset"):
    """记录有意义的业务活动摘要到 activity_log"""
    try:
        import os
        import urllib.request
        import json

        port = os.environ.get("VE5_PORT")
        if not port:
            return

        # 推断账户类型
        account_name = "未知账户"
        if "东方财富" in raw_text:
            account_name = "东方财富证券"
        elif "招商银行" in raw_text or "朝朝宝" in raw_text:
            account_name = "招商银行"
        elif "摩根" in raw_text:
            account_name = "摩根基金"
        elif "天弘" in raw_text:
            account_name = "天弘基金"
        elif "证券市值" in raw_text or "持仓盈亏" in raw_text:
            account_name = "证券账户"
        elif "基金" in raw_text[:200]:
            account_name = "基金账户"

        # 从文件名推断日期（Screenshot_2026_0710_205801.jpg → 2026-07-10）
        date_str = ""
        import re as _re
        dm = _re.search(r'(\d{4})_(\d{2})(\d{2})', file_name)
        if dm:
            date_str = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}"

        # 构建摘要
        if screenshot_category == "expense":
            parts = [f"提取了消费账单"]
            if date_str:
                parts.append(f" {date_str}")
            parts.append(f"，获得 {n_expenses} 条消费记录")
        elif screenshot_category == "income":
            parts = [f"提取了收入记录"]
            if date_str:
                parts.append(f" {date_str}")
            parts.append(f"，获得 {n_expenses} 条收入记录")
        else:
            parts = [f"提取了{account_name}"]
            if date_str:
                parts.append(f" {date_str}")
            parts.append(f"的截图，获得 {n_holdings} 条持仓记录")
            if account_summary.get("total_assets", 0) > 0:
                parts.append(f"（总资产 ¥{account_summary['total_assets']:,.2f}）")
        text = "".join(parts)

        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/v1/activities/record",
            data=json.dumps({"text": text, "badge": "已入库", "badge_type": "success"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass


def _log_expense_extraction(source: str, screenshot_category: str, records: list,
                            total_from_summary: float = 0.0, extraction_method: str = "unknown"):
    """消费提取本地日志（JSONL 格式，长期保存）。

    日志路径：DATA_DIR/logs/expense_extraction_log.jsonl
    每条记录为独立 JSON 行，便于后续审计、分析和数据恢复。
    """
    try:
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "source_file": Path(source).name if source else "",
            "screenshot_category": screenshot_category,
            "extraction_method": extraction_method,
            "total_from_summary": float(total_from_summary),
            "records_count": len(records) if records else 0,
            "records": []
        }
        for r in records or []:
            if isinstance(r, dict):
                log_entry["records"].append({
                    "date": r.get("date", ""),
                    "counterparty": r.get("counterparty", ""),
                    "amount": float(r.get("amount", 0)),
                    "category": r.get("category", ""),
                    "is_essential": bool(r.get("is_essential", False)),
                    "description": r.get("description", "")
                })

        with open(_EXPENSE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

        logger.info(f"[PIPE-LOG] 消费提取日志已记录: {Path(source).name}, {len(records)} 条记录")
    except Exception as e:
        logger.debug(f"[PIPE-LOG] 日志写入异常（不影响主流程）: {e}")


# ══════════════════════════════════════════════
# 消费记录正则提取（兜底）
# ══════════════════════════════════════════════

def _regex_extract_expenses(text: str, source: str) -> list:
    """
    正则兜底提取消费记录。
    自动识别两种截图类型：
    - 汇总页（支出构成/分类统计）→ 按分类提取，每个分类一条记录
    - 明细页（商户名+金额的流水）→ 逐条提取商户记录
    仅在文本包含消费关键词时触发，避免误匹配持仓截图。
    """
    import re

    # 先去噪（中文间空格去除），再做关键词检测
    # 注意：只用 [ \t]+ 匹配空格和制表符，保留换行符 \n，避免行粘连
    text_clean = re.sub(r'(?<=[\u4e00-\u9fff])[ \t]+(?=[\u4e00-\u9fff])', '', text)
    text_clean = re.sub(r'(?<=[\u4e00-\u9fff])[ \t]+(?=[a-zA-Z0-9¥])', '', text_clean)
    text_clean = re.sub(r'(?<=[a-zA-Z0-9¥])[ \t]+(?=[\u4e00-\u9fff])', '', text_clean)
    # 去除数字间空格：1 000.00 → 1000.00
    text_clean = re.sub(r'(?<=\d)[ \t]+(?=\d)', '', text_clean)

    # ── 两级关键词门控 ──
    # 级别1：强信号词（出现1个即放行）
    strong_signals = ["支出构成", "交易明细", "账单", "收支"]
    if any(s in text_clean for s in strong_signals):
        pass  # 放行
    else:
        # 级别2：普通消费关键词（需≥2个才放行）
        normal_kw = ["支付", "消费", "支出", "扣款", "付款", "转账", "充值", "红包",
                     "外卖", "打车", "滴滴", "美团", "饿了么", "微信支付", "支付宝",
                     "商户", "订单", "交通", "购物", "餐饮", "娱乐", "服务",
                     "居住", "医疗", "教育"]
        if sum(1 for kw in normal_kw if kw in text_clean) < 2:
            return []

    # ── 截图类型检测：汇总页 vs 明细页 ──
    is_summary = _is_expense_summary_page(text_clean)
    if is_summary:
        logger.info(f"[PIPE-EXPENSE] 检测到消费汇总页，走分类提取路径")
        return _regex_extract_expense_summary(text_clean, source)
    else:
        logger.info(f"[PIPE-EXPENSE] 检测到消费明细页，走商户提取路径")
        return _regex_extract_expense_detail(text_clean, source)


def _is_expense_summary_page(text: str) -> bool:
    """
    判断是否为消费汇总页（支出构成/分类统计），而非明细流水页。

    汇总页特征：
    - 包含"支出构成"和百分比
    - 包含多个标准分类名+百分比（如"餐饮美食 28.7%"）
    - 有环形图/饼图相关区域（OCR 产生大面积噪声）
    - 没有日期+商户+金额的三元组模式

    明细页特征：
    - 有"MM月DD日"或"MM-DD"日期前缀
    - 有明确的商户名（3+汉字）
    - 有具体金额（带负号或¥前缀）
    """
    import re

    # 强信号：直接包含"支出构成"
    if "支出构成" in text:
        return True

    # 检测百分比行的数量：≥3 个"分类名 XX.XX%"格式 → 汇总页
    pct_pattern = re.compile(r'[\u4e00-\u9fff]{2,6}\s*[\d.]+%')
    pct_matches = pct_pattern.findall(text)
    if len(pct_matches) >= 3:
        return True

    # 检测是否有日期+金额的明细模式
    # 明细页通常有 "7月15日" 或 "07-10" 这种日期格式
    detail_date_pattern = re.compile(r'\d{1,2}月\d{1,2}日|\d{2}-\d{2}')
    detail_dates = detail_date_pattern.findall(text)
    # 如果有 3 个以上日期，很可能是明细页
    if len(detail_dates) >= 3:
        return False

    # 如果既没有3+百分比也没有3+日期，但有大额总支出+分类统计 → 汇总页
    # 例如支付宝月度概览页
    total_match = re.search(r'总[支出][^\d]*[¥￥]?\s*([\d,]+\.?\d*)', text)
    if total_match and len(pct_matches) >= 2:
        return True

    return False


def _regex_extract_expense_summary(text: str, source: str) -> list:
    """
    从消费汇总页（支出构成/分类统计）中按分类提取消费记录。

    策略：
    - 提取总支出金额
    - 匹配"分类名 金额"或"分类名 XX.XX%"格式
    - 每个分类生成一条记录，counterparty = "分类汇总：XX"
    - is_essential 按分类标准判定
    - total_from_summary 通过返回列表的属性传递
    """
    import re

    # 标准分类映射（汇总页分类名 → 系统标准分类）
    summary_cat_map = {
        "餐饮": "餐饮", "餐饮美食": "餐饮", "美食": "餐饮", "正餐": "餐饮",
        "零食": "餐饮", "早餐": "餐饮", "炸鸡": "餐饮", "烧烤": "餐饮",
        "咖啡": "餐饮", "奶茶": "餐饮", "生鲜水果": "日用",
        "交通": "交通", "交通出行": "交通", "出行": "交通",
        "火车": "交通", "打车": "交通", "公交地铁": "交通", "公交": "交通", "地铁": "交通",
        "购物": "购物", "日用百货": "日用", "日用": "日用",
        "娱乐": "娱乐", "文化休闲": "娱乐", "休闲": "娱乐",
        "居住": "居住", "生活缴费": "居住", "居住缴费": "居住",
        "房租": "居住", "水电": "居住", "物业": "居住", "燃气": "居住",
        "月供": "月供", "房贷": "月供", "车贷": "月供", "还款": "月供",
        "医疗": "医疗", "医疗健康": "医疗", "健康": "医疗",
        "教育": "教育", "学习": "教育",
        "通讯": "通讯", "话费": "通讯", "流量": "通讯",
        "旅行": "旅行",
        "服务": "其他",
        "转账": "其他",
        "其他": "其他", "其它": "其他",
    }

    # 必需消费分类（汇总页级别，按分类整体判定）
    essential_cats = {"餐饮", "交通", "日用", "居住", "月供", "医疗", "教育", "通讯"}

    # 弹性消费分类
    elastic_cats = {"娱乐", "旅行", "购物"}

    expenses = []
    lines = [l.strip() for l in text.split('\n') if l.strip()]

    # 提取总支出金额
    total_amount = 0.0
    for line in lines:
        tm = re.search(r'总[支出入][^\d]*[¥￥]?\s*([\d,]+\.?\d*)', line)
        if tm:
            try:
                total_amount = float(tm.group(1).replace(',', ''))
            except ValueError:
                pass
            break

    # 提取分类行：匹配"分类名 金额"或"分类名 XX.XX%"
    # 常见格式：
    #   "旅行 ¥723.00"    (微信)
    #   "文化休闲 51.4% ¥1405.00" (支付宝)
    #   "餐饮美食 28.7% ¥784.09"  (支付宝)
    cat_amounts = {}  # {标准分类: 金额}
    for line in lines:
        # 跳过百分比统计行（只有百分比没有金额）
        if re.match(r'^[\d.]+%\s*$', line):
            continue
        # 跳过总支出行
        if re.match(r'^总', line):
            continue

        # 模式1: "分类名 XX.XX% ¥金额" （支付宝格式）
        m1 = re.search(r'([\u4e00-\u9fff]{2,8})\s*[\d.]+%\s*[¥￥]?\s*([\d,]+\.?\d*)', line)
        # 模式2: "分类名 ¥金额" （微信格式）
        m2 = re.search(r'([\u4e00-\u9fff]{2,8})\s*[¥￥]\s*([\d,]+\.?\d*)', line)
        # 模式3: "分类名 金额"（无¥前缀）
        m3 = re.search(r'^([\u4e00-\u9fff]{2,6})\s+([\d,]+\.\d{2})\s*$', line)

        match = m1 or m2 or m3
        if not match:
            continue

        raw_cat = match.group(1)
        try:
            amount = float(match.group(2).replace(',', ''))
        except ValueError:
            continue

        if amount <= 0 or amount > 10000000:
            continue

        # 映射到标准分类
        std_cat = "其他"
        for kw, mapped in summary_cat_map.items():
            if kw in raw_cat:
                std_cat = mapped
                break

        # 合并同分类金额
        if std_cat in cat_amounts:
            cat_amounts[std_cat] += amount
        else:
            cat_amounts[std_cat] = amount

    # 生成记录：每个分类一条
    now_month = f"{datetime.now().year}-{datetime.now().month:02d}"
    for cat, amount in cat_amounts.items():
        # 必需/弹性判定
        if cat in elastic_cats:
            is_essential = False
        elif cat in essential_cats:
            is_essential = True
        else:
            # "其他"、"购物"等模糊分类 → 保守估计为必需
            is_essential = True

        expenses.append({
            "date": now_month,
            "amount": amount,
            "counterparty": f"分类汇总：{cat}",
            "category": cat,
            "is_essential": is_essential,
            "description": f"汇总页提取 - {cat}",
        })

    # 生成 SummaryList（带 _total_from_summary 属性），即使 total_amount=0 也返回
    class SummaryList(list):
        pass
    wrapped = SummaryList(expenses)
    wrapped._total_from_summary = total_amount if total_amount > 0 else sum(e['amount'] for e in expenses)
    logger.info(f"[PIPE-EXPENSE] 汇总页提取: {len(wrapped)} 个分类, 合计 ¥{sum(e['amount'] for e in wrapped):,.2f}, 截图汇总金额 ¥{total_amount:,.2f}")
    return wrapped


def _regex_extract_expense_detail(text: str, source: str) -> list:
    """正则提取消费明细页（商户名+金额的流水格式）。"""
    import re

    lines = [l.strip() for l in text.split('\n') if l.strip()]
    expenses = []
    date_str = ""

    # 分类映射
    cat_map = {
        "餐饮": "餐饮", "美食": "餐饮", "食": "餐饮", "饭": "餐饮",
        "外卖": "餐饮", "饿了么": "餐饮", "美团": "餐饮",
        "交通": "交通", "打车": "交通", "滴滴": "交通", "出行": "交通",
        "地铁": "交通", "公交": "交通", "加油": "交通", "停车": "交通",
        "购物": "购物", "淘宝": "购物", "京东": "购物", "拼多多": "购物",
        "超市": "日用", "便利": "日用",
        "娱乐": "娱乐", "电影": "娱乐", "游戏": "娱乐", "视频": "娱乐",
        "居住": "居住", "房租": "居住", "物业": "居住",
        "月供": "月供",
        "房贷": "月供",
        "车贷": "月供",
        "还款": "月供",
        "水费": "居住", "电费": "居住",
        "医疗": "医疗", "医院": "医疗", "药": "医疗",
        "教育": "教育", "学校": "教育", "培训": "教育",
        "通讯": "通讯", "话费": "通讯", "流量": "通讯",
    }
    def _map_cat(merchant: str) -> str:
        for kw, cat in cat_map.items():
            if kw in merchant:
                return cat
        return "其他"

    # 必需消费关键词：根据商户名/描述判断（优先级高于分类）
    _ESSENTIAL_MERCHANT_KEYWORDS = {
        # 超市/生鲜（食材日用品）
        "永辉", "盒马", "沃尔玛", "山姆", "costco", "麦德龙", "大润发", "家乐福",
        "物美", "联华", "苏果", "中百", "超市", "菜市场", "农贸市场", "生鲜",
        "水果店", "肉铺", "菜店", "粮油", "干货",
        # 餐饮必需（基础饮食）
        "沙县", "兰州拉面", "拉面", "早点", "早餐", "快餐", "食堂", "小吃",
        "包子", "馒头", "饼", "面馆", "粥", "豆浆", "煎饼", "米饭", "炒菜",
        "美团外卖", "饿了么", "外卖", "肯德基", "麦当劳", "必胜客", "汉堡",
        # 交通必需
        "地铁", "公交", "滴滴", "出租车", "加油站", "中石油", "中石化", "中海油",
        "停车", "高速", "通行费", "火车票", "高铁",
        # 居住必需
        "房租", "租房", "房东", "中介", "水电", "电费", "水费", "物业",
        "燃气", "煤气", "宽带", "取暖",
        # 医疗必需
        "医院", "诊所", "药店", "药房", "体检", "挂号", "疫苗", "牙科", "眼科",
        # 教育必需
        "学费", "书本", "文具", "教材", "打印", "复印", "考试",
        # 通讯必需
        "话费", "流量", "移动", "联通", "电信", "广电", "宽带", "通讯",
        # 日用品
        "便利店", "711", "全家", "罗森", "日用", "洗发水", "牙膏", "纸巾",
        "洗洁精", "洗衣粉", "肥皂",
    }

    # 弹性消费关键词：明确可削减的享受型消费
    _ELASTIC_MERCHANT_KEYWORDS = {
        # 享受型餐饮
        "星巴克", "喜茶", "奈雪", "茶颜", "瑞幸", "精品咖啡", "海底捞",
        "西贝", "日料", "寿司", "烤肉", "烧烤店", "酒吧", "酒馆", "清吧",
        "米其林", "黑珍珠", "高端", "会所", "私房菜",
        # 非必需购物
        "商场", "购物中心", "百货", "奢侈品", "金店", "珠宝", "手表",
        # 娱乐
        "电影", "影院", "游戏", "steam", "ktv", "酒吧", "演唱会", "演出",
        "剧本杀", "密室", "桌游", "网吧", "电玩", "游乐场", "动物园", "景区",
        # 旅行
        "酒店", "宾馆", "民宿", "机票", "旅行团", "旅行社", "携程", "去哪儿",
        "飞猪", "同程", "景点", "门票", "航空", "度假",
        # 会员订阅（注意：避免使用单独"会员"，太宽泛会误伤"山姆会员店"等）
        "视频会员", "音乐会员", "订阅服务", "健身会员", "瑜伽", "私教",
        # 美容时尚
        "理发", "美容", "美甲", "spa", "护肤", "化妆品", "香水", "造型",
        # 其它弹性
        "彩票", "打赏", "捐赠", "香火",
    }

    # 分类兜底：当商户名无法判断时，按分类保守估计
    _ESSENTIAL_CATEGORIES = {"餐饮", "交通", "日用", "居住", "月供", "医疗", "教育", "通讯"}

    def _is_essential(merchant: str, cat: str) -> bool:
        """
        必需消费判定（商户名优先 + 分类兜底）。
        规则：商户名包含弹性关键词 → 弹性；包含必需关键词 → 必需；否则按分类兜底。
        """
        m = merchant.lower()
        # 优先级1：商户名明确是弹性消费
        for kw in _ELASTIC_MERCHANT_KEYWORDS:
            if kw.lower() in m:
                return False
        # 优先级2：商户名明确是必需消费
        for kw in _ESSENTIAL_MERCHANT_KEYWORDS:
            if kw.lower() in m:
                return True
        # 优先级3：分类兜底
        return cat in _ESSENTIAL_CATEGORIES

    for line in lines:
        # 跳过百分比行（如 "购物30.38%"）
        if '%' in line and re.search(r'[\d.]+%', line):
            continue

        # 提取日期
        dm = re.search(r'(\d{1,2})月(\d{1,2})日', line)
        if dm:
            m_month = int(dm.group(1))
            now_year = datetime.now().year
            now_month = datetime.now().month
            # 跨年推断：提取月份 > 当前月份 → 年份减1
            use_year = now_year - 1 if m_month > now_month else now_year
            date_str = f"{use_year}-{dm.group(1).zfill(2)}-{dm.group(2).zfill(2)}"

        # 提取金额（支持 ¥ 前缀和负数）
        am = re.search(r'[¥\-]?([\d,]+\.\d{2})', line)
        if not am:
            am = re.search(r'[¥\-]?([\d,]+\.\d)', line)
        if not am:
            am = re.search(r'[¥\-]?([\d,]+)', line)
        if not am:
            continue

        amount_str = am.group(1)
        try:
            amount = abs(float(amount_str.replace(",", "")))
        except ValueError:
            continue

        if amount <= 0 or amount > 1000000:
            continue

        # 提取商户名（金额前面的非数字文本）
        prefix = line[:am.start()].strip()
        # 去除常见前缀
        prefix = re.sub(r'^[\-\*·\s¥%©@oO}]+', '', prefix)
        prefix = re.sub(r'(\d{1,2}月\d{1,2}日?\s*)', '', prefix)
        prefix = re.sub(r'(\d{2}:\d{2}[:\d]*\s*)', '', prefix)
        # 去除 MM-DD 日期前缀（如 "07-10 商户名"）
        prefix = re.sub(r'^\d{2}-\d{2}\s*', '', prefix)
        prefix = re.sub(r'(微信支付|支付宝|扣款|支付成功|消费|支出构成|转账|收款|退款|自动|先用后付|分期付款)\s*', '', prefix)
        # 去除百分比和数字后缀
        prefix = re.sub(r'[\d.]+%\s*$', '', prefix)
        prefix = re.sub(r'[\d]+\.?[\d]*\s*$', '', prefix)
        prefix = prefix.strip('- \t>©@oO')
        # 去除常见OCR噪声和截断
        prefix = re.sub(r'[<>_#@©]', '', prefix)
        prefix = prefix.strip()

        # ── 过滤汇总行（只包含分类统计/总支出/合计金额的行，没有具体商户）──
        _SUMMARY_KEYWORDS = {
            "总支出", "共支出", "支出合计", "支出总计", "本月支出", "年度支出",
            "分类统计", "支出明细", "账单月份", "月度小结", "本月收入",
            "支出构成", "收支分析", "共收入", "收入合计", "支出",
            # 纯分类名称（后面只跟金额，没有商户）
            "餐饮", "美食", "购物", "交通", "出行", "日用", "娱乐",
            "居住", "生活缴费", "医疗", "教育", "通讯", "旅行", "其它", "其他",
            "运动", "健康", "美容", "美发", "电影",
            # 组合分类名（支付宝统计行常见）
            "美容美发", "运动健康",
        }
        # 如果清理后的prefix完全是汇总关键词，跳过
        if prefix in _SUMMARY_KEYWORDS:
            continue
        # 如果prefix以汇总词开头且很短（如"本月支出共计"），也跳过
        if any(prefix.startswith(kw) for kw in ("本月支出", "总支出", "共支出", "支出合计", "分类统计", "月度小结")):
            continue

        # 过滤掉明显不是商户名的内容
        if len(prefix) < 2:
            continue
        # 过滤掉纯数字或纯符号
        if re.match(r'^[\d\W]+$', prefix):
            continue
        # 过滤掉过长（可能是OCR噪声段落）
        if len(prefix) > 30:
            # 尝试取最后几个有意义的词作为商户名
            words = prefix.split()
            if len(words) > 1:
                prefix = words[-1] if len(words[-1]) >= 2 else prefix[:20]
            else:
                prefix = prefix[:20]

        cat = _map_cat(prefix)
        expenses.append({
            "date": date_str,
            "amount": amount,
            "counterparty": prefix,
            "category": cat,
            "is_essential": _is_essential(prefix, cat),
            "description": line.strip()[:80],
        })

    return expenses


# ══════════════════════════════════════════════
# 数据库操作
# ══════════════════════════════════════════════

def _ensure_column(conn, table: str, col: str, dtype: str):
    """如果列不存在则添加（SQLite 自动迁移）"""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {dtype}")
    except Exception:
        pass  # 列已存在


def _write_holdings_tx(conn, items: list, source_file: str) -> int:
    """写入 asset_holdings（使用外部事务连接）

    去重策略（文档 3.1 节 Step 4）：
      1. DELETE 当前 source_file 的所有旧记录（同文件全量替换）
      2. INSERT 前对已有记录做跨文件去重：
         - 同 product_name + 金额相同 → 跳过（已存在完全相同的记录）
         - 同 product_name + 金额不同 → 保留金额较大者（通常是更完整的总览数据）
    """
    items = _filter_holdings(items)
    if not items:
        return 0
    conn.execute("""CREATE TABLE IF NOT EXISTS asset_holdings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_key TEXT, source_file TEXT, product_name TEXT, product_code TEXT,
        asset_class TEXT DEFAULT 'unclassified', liquidity_level TEXT DEFAULT 'unknown',
        risk_level TEXT DEFAULT 'unknown', is_classified BOOLEAN DEFAULT 0,
        classification_confidence REAL DEFAULT 0,
        holding_quantity REAL, cost_basis REAL, current_value REAL,
        unrealized_pnl REAL, holding_return_pct REAL, annualized_return_pct REAL,
        purchase_date TEXT, holding_days INTEGER, inference_source TEXT,
        user_overridden BOOLEAN DEFAULT 0, user_note TEXT,
        batch_id TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    # 自动迁移：确保新列存在
    _ensure_column(conn, "asset_holdings", "unrealized_pnl", "REAL DEFAULT 0")
    _ensure_column(conn, "asset_holdings", "holding_days", "INTEGER DEFAULT 0")
    _ensure_column(conn, "asset_holdings", "user_overridden", "BOOLEAN DEFAULT 0")
    _ensure_column(conn, "asset_holdings", "user_note", "TEXT")
    _ensure_column(conn, "asset_holdings", "is_superseded", "INTEGER DEFAULT 0")
    _ensure_column(conn, "asset_holdings", "superseded_by", "TEXT DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_asset_account ON asset_holdings(account_key)")
    conn.execute("DELETE FROM asset_holdings WHERE source_file = ?", (source_file,))

    # 跨文件去重：读取当前已有记录，用于判断同名产品是否已存在
    # 如果新产品值更大/来源更权威 → INSERT 新记录 + 标记旧 is_superseded=1
    # 这样用户删除新截图的数据时，旧数据可复活
    existing = {}
    for row in conn.execute("SELECT id, product_name, current_value, source_file, account_key FROM asset_holdings WHERE is_superseded=0").fetchall():
        norm = _normalize_name_for_dedup(row[1])
        if norm not in existing:
            existing[norm] = {"id": row[0], "name": row[1], "value": row[2], "source": row[3], "account": row[4]}

    _BANK_PRIORITY = {"招商银行", "工商银行", "建设银行", "农业银行", "中国银行"}
    skipped = 0
    superseded_ids = []  # 被新记录覆盖的旧记录 id
    for item in items:
        name = item["name"]
        value = item["value"]
        norm = _normalize_name_for_dedup(name)
        if norm in existing:
            ex = existing[norm]
            current_account = item.get("account", "")
            ex_is_bank = ex["account"] in _BANK_PRIORITY
            new_is_bank = current_account in _BANK_PRIORITY
            should_supersede = False
            if ex_is_bank and not new_is_bank:
                skipped += 1
                continue
            elif not ex_is_bank and new_is_bank:
                should_supersede = True
            elif ex["value"] >= value:
                skipped += 1
                continue
            else:
                should_supersede = True

            if should_supersede:
                # 标记旧记录为被覆盖
                conn.execute("UPDATE asset_holdings SET is_superseded=1, superseded_by=? WHERE id=?",
                             (source_file, ex["id"]))
                superseded_ids.append(ex["id"])
                del existing[norm]

        # 插入新记录
        holding_days = 0
        purchase_date = item.get("purchase_date", "")
        if purchase_date:
            try:
                from datetime import datetime as _dt
                pd = _dt.strptime(purchase_date, "%Y-%m-%d")
                holding_days = (_dt.now() - pd).days
            except Exception:
                pass
        conn.execute(
            """INSERT INTO asset_holdings
            (account_key, source_file, product_name, asset_class, current_value,
             is_classified, inference_source, batch_id, purchase_date, holding_days,
             product_code, cost_basis, holding_return_pct, unrealized_pnl)
            VALUES (?,?,?,?,?,1,?,?,?,?,?,?,?,?)""",
            (
                item.get("account", "未知"), source_file, name,
                item.get("type", "unknown"), value,
                f"snapshot:{source_file}",
                item.get("batch_id", ""), purchase_date, holding_days,
                item.get("product_code", ""),
                item.get("cost_basis") or 0,
                item.get("holding_return_pct") or 0,
                item.get("unrealized_pnl") or 0,
            )
        )

    if superseded_ids:
        logger.info(f"[PIPE] 标记 {len(superseded_ids)} 条旧记录为 is_superseded（可由删除操作复活）")
    return len(items) - skipped


def _write_expenses_tx(conn, items: list, source_file: str, total_from_summary: float = 0.0) -> int:
    """写入 transactions（使用外部事务连接）"""
    if not items:
        return 0
    # 确保新列存在（向后兼容旧数据库）
    try:
        conn.execute("ALTER TABLE transactions ADD COLUMN is_essential INTEGER DEFAULT 0")
    except Exception:
        pass
    conn.execute("""CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        transaction_date TEXT, transaction_type TEXT, amount REAL,
        counterparty TEXT, category_primary TEXT, category_secondary TEXT,
        is_essential INTEGER DEFAULT 0,
        description TEXT, raw_data_hash TEXT UNIQUE,
        source_file TEXT, restored_at TEXT, batch_id TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("DELETE FROM transactions WHERE source_file = ?", (source_file,))
    for item in items:
        # 日期标准化：统一为 YYYY-MM-DD 格式
        raw_date = item.get("date", "")
        normalized_date = raw_date
        if raw_date and len(raw_date) <= 5 and '-' in raw_date:
            # MM-DD 格式 → YYYY-MM-DD
            try:
                mm, dd = raw_date.split('-')
                from datetime import datetime as _dt
                now = _dt.now()
                # 推断年份：如果月份 > 当前月份，可能是去年
                year = now.year - 1 if int(mm) > now.month else now.year
                normalized_date = f"{year}-{mm.zfill(2)}-{dd.zfill(2)}"
            except Exception:
                normalized_date = raw_date
        elif not raw_date:
            from datetime import datetime as _dt
            normalized_date = _dt.now().strftime("%Y-%m-%d")

        conn.execute(
            "INSERT OR IGNORE INTO transactions (transaction_date, transaction_type, amount, counterparty, category_primary, category_secondary, is_essential, description, source_file, restored_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                normalized_date,
                item.get("transaction_type", "expense"),
                item["amount"],
                item.get("counterparty", ""),
                item.get("category", "其他"),
                "生活消费",
                1 if item.get("is_essential") else 0,
                item.get("description", ""),
                source_file,
                datetime.now().isoformat(),
            )
        )
    # 保存截图原始汇总金额到 processed_files 表的 extra_data 字段
    if total_from_summary > 0:
        try:
            conn.execute("ALTER TABLE processed_files ADD COLUMN extra_data TEXT")
        except Exception:
            pass
        conn.execute(
            "UPDATE processed_files SET extra_data = ? WHERE source_file = ?",
            (json.dumps({"total_from_summary": total_from_summary}), source_file)
        )
    return len(items)


def _write_holdings(items: list, source_file: str) -> int:
    """写入 asset_holdings（向后兼容 wrapper）"""
    if not items:
        return 0
    conn = sqlite3.connect(str(DB_PATH))
    try:
        n = _write_holdings_tx(conn, items, source_file)
        conn.commit()
        return n
    finally:
        conn.close()


def _write_expenses(items: list, source_file: str, total_from_summary: float = 0.0) -> int:
    """写入 transactions 表（向后兼容 wrapper）"""
    if not items:
        return 0
    conn = sqlite3.connect(str(DB_PATH))
    try:
        n = _write_expenses_tx(conn, items, source_file, total_from_summary)
        conn.commit()
        return n
    finally:
        conn.close()


# ══════════════════════════════════════════════
# hash 去重
# ══════════════════════════════════════════════

def _hash_file(f: Path) -> str:
    h = hashlib.md5()
    with open(f, "rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_processed(file_hash: str) -> bool:
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("CREATE TABLE IF NOT EXISTS processed_files (file_hash TEXT PRIMARY KEY, source_file TEXT, processed_at TEXT)")
        row = conn.execute("SELECT processed_at FROM processed_files WHERE file_hash=?", (file_hash,)).fetchone()
        conn.close()
        if row:
            return (datetime.now() - datetime.fromisoformat(row[0])).total_seconds() < 86400
    except Exception:
        pass
    return False


def _mark_processed(file_hash: str, source: str):
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("CREATE TABLE IF NOT EXISTS processed_files (file_hash TEXT PRIMARY KEY, source_file TEXT, processed_at TEXT)")
        conn.execute("INSERT OR REPLACE INTO processed_files VALUES (?,?,?)", (file_hash, source, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    except Exception:
        pass


# ══════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════

def _move(f: Path, dest: Path):
    dest.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(f), str(dest / f.name))
    except Exception as e:
        logger.debug(f"[PIPE] 移动失败: {e}")
