"""
VE4 模型复核验证器
==================
在文件解析完成后，用本地轻量模型做质量复核和修正。

复核场景：
    1. OCR 复核 — 检查 OCR 文本是否看起来合理、是否混入了乱码
    2. 分类复核 — 对提取的交易记录分类做合理性检查
    3. 数据复核 — 检查金额、日期是否存在异常值
    4. 跨文件复核 — 同一银行不同批次数据的趋势一致性

所有复核遵循：
    - 只对"可疑"结果调模型，对清晰结果直接通过
    - 模型仅输出"yes/no/flag"级别判断，不做长文本分析
    - 单个文件复核最多调 1-2 次模型，避免性能下降
"""

import re
import json
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta

from model_client import ve4_model_get_client

logger = logging.getLogger("ve4.validator")


class ModelValidator:
    """模型复核验证器"""

    def __init__(self):
        self.client = ve4_model_get_client()

    # ──────────────────────────────────────────
    # 1. OCR 复核
    # ──────────────────────────────────────────

    def validate_ocr_text(self, raw_text: str) -> Tuple[bool, str]:
        """
        检查 OCR 结果是否合理。
        返回: (is_valid: bool, reason: str)

        跳过条件（不调模型）：
            - 文本为空 → invalid
            - 文本非常短（< 20 字符）→ 可能不完整
            - 文本含大量乱码字符 → invalid
            - 文本含明显的金融关键词 → valid
        """
        if not raw_text or len(raw_text.strip()) < 10:
            return (False, "OCR 文本为空或过短")

        # 存在明显的金融关键词 → 直接通过（不调模型）
        finance_kw = ["¥", "￥", "余额", "交易", "银行", "账户",
                      "金额", "收入", "支出", "存款", "卡号",
                      "流水", "基金", "股票", "盈亏", "持仓"]
        for kw in finance_kw:
            if kw in raw_text:
                logger.debug(f"[VALIDATE] OCR 含关键词 '{kw}' → 快速通过")
                return (True, f"含金融关键词：{kw}")

        # 乱码检测
        garbage_ratio = self._garbage_ratio(raw_text)
        if garbage_ratio > 0.3:
            return (False, f"OCR 乱码率过高 ({garbage_ratio:.0%})")

        # 模型复核（只有无关键词 + 无乱码时）
        logger.debug("[VALIDATE] OCR 无关键词，调模型复核")
        question = (
            f"以下 OCR 文本看起来是正常的金融账单文字吗？"
            f"如果是银行账单/消费记录/转账记录的内容，回答 yes；"
            f"如果是乱码/不完整/非金融内容，回答 no。\n\n"
            f"文本前 200 字：\n{raw_text[:200]}"
        )
        result = self.client.ask_yesno(question)
        if result is True:
            return (True, "模型确认为有效金融文本")
        elif result is False:
            return (False, "模型判断为非金融/无效文本")
        else:
            return (True, "模型未给出明确判断，默认通过")

    def _garbage_ratio(self, text: str) -> float:
        """计算乱码字符比例"""
        if not text:
            return 1.0
        # 常见乱码字符
        garbage_chars = set(chr(i) for i in range(128, 256))  # 扩展ASCII
        garbage_chars.update("□■◆◇○●◎◎※→←↑↓↖↗↘↙〓ⅰⅱⅲⅳⅴⅵⅶⅷⅸⅹ")
        count = sum(1 for c in text if c in garbage_chars)
        return count / len(text)

    # ──────────────────────────────────────────
    # 2. 分类复核
    # ──────────────────────────────────────────

    def validate_classification(self, records: List[Dict]) -> List[Dict]:
        """
        对已分类的记录做复核，修正明显错误的分类。
        只复核前 5 条（太多会超时），避免批量调模型。

        示例场景：
            - 记录含"美团"但分类不是"外卖" → 修正
            - 记录金额很大但分类是"早餐" → 标记可疑
            - 记录含"工资"但金额很小 → 标记可疑
        """
        if not records:
            return records

        # 规则修正（先走规则，不调模型）
        corrected = []
        for record in records:
            record = self._rule_based_correction(record)
            corrected.append(record)

        # 模型复核（只复核前 3 条中最可疑的 1 条）
        suspicious = []
        for record in corrected[:10]:
            score = self._suspicious_score(record)
            if score > 0:
                suspicious.append((score, record))

        if suspicious:
            suspicious.sort(key=lambda x: -x[0])
            _, worst = suspicious[0]
            corrected_record = self._model_review(worst)
            if corrected_record:
                # 找到原始位置替换
                for i, r in enumerate(corrected[:10]):
                    if r is worst:
                        corrected[i] = corrected_record
                        break

        return corrected

    def _rule_based_correction(self, record: Dict) -> Dict:
        """规则修正（不调模型）"""
        desc = str(record.get("description", ""))
        cat_pri = record.get("category_primary", "")
        cat_sec = record.get("category_secondary", "")

        rules = [
            # (关键词, 应修正的分类)
            (["美团", "饿了么", "外卖"], ("餐饮", "外卖")),
            (["滴滴", "高德", "打车", "地铁"], ("交通", "出行")),
            (["淘宝", "京东", "天猫"], ("购物", "电商")),
            (["星巴克", "瑞幸", "咖啡"], ("餐饮", "咖啡")),
            (["工资", "薪资", "奖金"], ("收入", "工资")),
        ]
        for keywords, (new_pri, new_sec) in rules:
            for kw in keywords:
                if kw in desc:
                    if cat_pri != new_pri or cat_sec != new_sec:
                        record["category_primary"] = new_pri
                        record["category_secondary"] = new_sec
                        record["_corrected"] = True
                    break
        return record

    def _suspicious_score(self, record: Dict) -> int:
        """计算可疑分数（0-3），越高越值得模型复核"""
        score = 0
        amount = record.get("amount", 0)
        desc = str(record.get("description", ""))
        cat_pri = record.get("category_primary", "")

        # 金额异常
        if amount > 100000 and cat_pri in ("餐饮", "交通"):
            score += 1
        if amount < 0.01 and cat_pri != "其他":
            score += 1
        # 描述为空但分类非空
        if not desc and cat_pri not in ("", "其他", "未分类"):
            score += 1
        # 金额为 0
        if amount == 0:
            score += 1
        return score

    def _model_review(self, record: Dict) -> Optional[Dict]:
        """调模型复核单条记录"""
        question = (
            f"这是一条交易记录：\n"
            f"日期：{record.get('transaction_date', '')}\n"
            f"金额：{record.get('amount', '')}\n"
            f"描述：{record.get('description', '')}\n"
            f"当前分类：{record.get('category_primary', '')} > {record.get('category_secondary', '')}\n"
            f"这条分类正确吗？回答 yes 或 no。如果 no，说出正确的分类名。"
        )
        result = self.client.ask(system="你是一个金融分类助手。", prompt=question, max_tokens=30)
        text = result.text.strip().lower()

        if text.startswith("no") or text.startswith("不"):
            # 尝试提取分类
            for pri_cat in ["收入", "餐饮", "交通", "购物", "居住", "通讯", "投资", "教育", "医疗", "其他"]:
                if pri_cat in text:
                    record["category_primary"] = pri_cat
                    record["category_secondary"] = "模型建议"
                    record["_model_reviewed"] = True
                    logger.info(f"[VALIDATE] 模型建议修改分类：{pri_cat}")
                    break
        return record

    # ──────────────────────────────────────────
    # 3. 数据稳定性复核
    # ──────────────────────────────────────────

    def validate_data_sanity(self, records: List[Dict]) -> List[str]:
        """
        检查数据整体合理性。
        返回：警告列表（空表示无异常）
        """
        warnings = []

        if not records:
            warnings.append("无有效记录")
            return warnings

        total_amount = sum(r.get("amount", 0) for r in records if r.get("transaction_type") == "expense")
        total_income = sum(r.get("amount", 0) for r in records if r.get("transaction_type") == "income")

        # 所有金额相同 → 明显不对
        amounts = [r.get("amount", 0) for r in records if r.get("amount", 0) > 0]
        if len(set(amounts)) == 1 and len(amounts) > 3:
            warnings.append(f"所有金额相同（{amounts[0]}）→ 可能是重复或错误数据")

        # 支出过低
        if total_amount > 0 and total_amount < 1:
            warnings.append(f"总支出仅 {total_amount} 元，数据可能不完整")

        # 记录数过多或过少
        if len(records) > 5000:
            warnings.append(f"单次导入 {len(records)} 条记录，请确认是否完整")

        # 日期范围
        dates = [r.get("transaction_date", "") for r in records if r.get("transaction_date")]
        if dates:
            try:
                min_date = min(dates)
                max_date = max(dates)
                span = (datetime.strptime(max_date, "%Y-%m-%d") -
                        datetime.strptime(min_date, "%Y-%m-%d"))
                if span.days > 365:
                    warnings.append(f"数据跨度 {span.days} 天，建议按年度分批导入")
            except Exception:
                pass

        return warnings

    # ──────────────────────────────────────────
    # 4. 批量总结
    # ──────────────────────────────────────────

    def summarize(self, records: List[Dict]) -> str:
        """
        生成一段简短的数据导入总结（用于推送到移动端）。
        只调一次模型，只给关键指标。
        """
        if not records:
            return "未提取到有效记录"

        total_expense = sum(r.get("amount", 0) for r in records if r.get("transaction_type") == "expense")
        total_income = sum(r.get("amount", 0) for r in records if r.get("transaction_type") == "income")
        count = len(records)

        # 简单总结（不调模型）
        summary = (f"导入 {count} 条记录，收入 {total_income:.0f} 元，支出 {total_expense:.0f} 元。")

        # 分类统计
        cats = {}
        for r in records:
            c = r.get("category_primary", "其他")
            cats[c] = cats.get(c, 0) + 1
        max_cat = max(cats, key=cats.get) if cats else ""
        if max_cat:
            summary += f"最多分类：{max_cat}。"
        else:
            summary += ""

        return summary


def ve4_validator_validate_pipeline_output(file_path: str, records: List[Dict],
                                           raw_text: str = "") -> Tuple[List[Dict], List[str]]:
    """
    便捷函数：对整个管道输出做一次复核。（模块安全命名）

    Args:
        file_path: 原始文件路径
        records: 已还原的记录列表
        raw_text: 原始提取文本（用于 OCR 复核）

    Returns:
        (修正后的记录列表, 警告列表)
    """
    validator = ModelValidator()
    warnings = []

    # OCR 复核
    if raw_text:
        is_valid, reason = validator.validate_ocr_text(raw_text)
        if not is_valid:
            warnings.append(f"OCR 质量警告：{reason}")

    # 分类复核
    records = validator.validate_classification(records)

    # 数据稳定性复核
    warnings.extend(validator.validate_data_sanity(records))

    return records, warnings


# 保留旧别名以便向后兼容（仅内部使用，不鼓励）
validate_pipeline_output = ve4_validator_validate_pipeline_output