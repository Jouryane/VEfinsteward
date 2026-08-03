"""
VE4 智能文件路由
================
根据文件名、扩展名、文件头特征，决定处理通道：

快速通道（不调用模型，< 10ms）：
    - 已知银行格式的 CSV/Excel（YAML 配置匹配）
    - 关键词命名的截图（文件名包含 "银行"、"持仓"、"账单" 等）
    - 纯文本文件

模型辅助路由（调用本地轻量模型，< 2秒）：
    - 文件名无关键词的图片 → 问模型"这是什么截图？"
    - 未知列名的 CSV → 问模型"这些列名代表什么？"
    - 未知格式的文档 → 问模型"这是金融文档吗？"

用法：
    router = WorkflowRouter()
    decision = router.route(file_path)
    # decision = {
    #     "path": "direct_csv" | "direct_ocr" | "model_csv" | "model_image" | ...,
    #     "channel": "direct" | "model_assisted",
    #     "confidence": 0.0~1.0,
    #     "hint": {"bank_type": "cmb", "date_col": "交易日期", ...}
    # }
"""

import json
import csv
import logging
from pathlib import Path
from typing import Dict, Optional
from dataclasses import dataclass, field

from config import (
    INCOMING_DIR, TEXTS_DIR, IMAGES_DIR,
    ALL_SUPPORTED_TYPES,
)
from model_client import ve4_model_get_client, ve4_model_get_stats, ModelResponse

logger = logging.getLogger("ve4.router")


@dataclass
class RoutingDecision:
    """
    路由决策结果。

    path 取值及含义：
        "direct_csv"       → 已知格式 CSV，直接 pandas 解析
        "direct_excel"     → 已知格式 Excel（匹配银行 YAML 配置）
        "direct_ocr"       → 任何图片，直接 OCR
        "direct_txt"       → 纯文本，直接读取
        "direct_pdf"       → 已知金融 PDF（含"银行"、"对账单"等关键词）
        "model_csv"        → 未知 CSV，问模型列名含义后决定如何解析
        "model_image"      → 无关键词图片，问模型"这是什么截图"
        "model_pdf"        → 未知 PDF，问模型内容类型
        "model_general"    → 其他文件，让模型判断内容
        "unknown"          → 无法处理
    """
    path: str = "unknown"
    channel: str = "direct"  # "direct" | "model_assisted"
    confidence: float = 0.0
    hint: Dict = field(default_factory=dict)


class WorkflowRouter:
    """智能文件路由"""

    # 文件名快速关键词 → 对应处理路径
    FILENAME_KEYWORDS = {
        # 银行相关
        "银行": "direct_excel",
        "招商": "direct_excel",
        "工商": "direct_excel",
        "建设": "direct_excel",
        "农业": "direct_excel",
        "中国银行": "direct_excel",
        "流水": "direct_excel",
        "对账单": "direct_excel",
        "账单": "direct_excel",
        "交易明细": "direct_excel",
        "银行回单": "direct_excel",
        # 证券相关
        "持仓": "direct_ocr",
        "证券": "direct_excel",
        "股票": "direct_excel",
        "基金": "direct_excel",
        "华泰": "direct_excel",
        "中信": "direct_excel",
        "交割单": "direct_excel",
        "成交": "direct_excel",
        # 消费相关
        "支付宝": "direct_csv",
        "微信": "direct_csv",
        "消费": "direct_csv",
        "支出": "direct_csv",
        "收银": "direct_csv",
        # 通用
        "Screenshot": "direct_ocr",
        "截图": "direct_ocr",
        "资产": "direct_ocr",
    }

    # 快速 CSV 列名校验（匹配任一即走 direct_csv）
    KNOWN_CSV_COLUMNS = {
        frozenset(["交易日期", "交易金额", "交易类型"]),
        frozenset(["交易日期", "交易金额", "对方账户"]),
        frozenset(["日期", "金额", "类型"]),
        frozenset(["Date", "Amount", "Description"]),
        frozenset(["创建时间", "金额", "类型"]),
    }

    def route(self, file_path: Path, sample_lines: list[str] = None) -> RoutingDecision:
        """
        主入口：判断文件应该走哪条处理通道。

        Args:
            file_path: 文件路径
            sample_lines: 文件前几行（可选，内部自动读取）

        Returns:
            RoutingDecision
        """
        # Step 1: 扩展名快速过滤
        ext = file_path.suffix.lower()
        if ext not in ALL_SUPPORTED_TYPES:
            return RoutingDecision(path="unknown", confidence=0.0,
                                   hint={"reason": f"不支持的文件类型：{ext}"})

        # Step 2: 文件名关键词匹配（快速通道，不调模型）
        name_hit = self._match_keywords(file_path)
        if name_hit:
            logger.info(f"[ROUTER] 文件名关键词命中 → {name_hit.path} ({file_path.name})")
            return name_hit

        # Step 3: 根据扩展名做针对性判断
        if ext in {'.csv'}:
            return self._route_csv(file_path, sample_lines)
        elif ext in {'.xlsx', '.xls'}:
            return self._route_excel(file_path)
        elif ext in {'.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp'}:
            return self._route_image(file_path)
        elif ext == '.pdf':
            return self._route_pdf(file_path, sample_lines)
        else:
            return self._route_other(file_path)

    def _match_keywords(self, file_path: Path) -> Optional[RoutingDecision]:
        """文件名关键词快速匹配"""
        name = file_path.stem
        for keyword, path in self.FILENAME_KEYWORDS.items():
            if keyword in name:
                return RoutingDecision(
                    path=path,
                    channel="direct",
                    confidence=0.9,
                    hint={"matched_keyword": keyword, "file_name": name}
                )
        return None

    def _route_csv(self, file_path: Path, sample_lines: list[str] = None) -> RoutingDecision:
        """
        CSV 文件路由：
        1. 读取前 3 行检查列名
        2. 匹配已知银行格式 → direct_csv
        3. 未知格式 → 调用模型 ask_choice
        """
        if sample_lines is None:
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    sample_lines = [f.readline() for _ in range(3)]
            except Exception:
                pass

        if not sample_lines:
            return RoutingDecision(path="direct_csv", channel="direct",
                                   confidence=0.5, hint={"reason": "无法读取文件头"})

        # 检查列名
        first_line = sample_lines[0].strip()
        columns = [c.strip().strip('"') for c in first_line.split(',') if c.strip()]
        col_set = frozenset(columns)
        is_known = any(col_set.issuperset(kc) for kc in self.KNOWN_CSV_COLUMNS)

        if is_known:
            logger.info(f"[ROUTER] CSV 列名匹配已知格式 → direct_csv ({columns})")
            return RoutingDecision(
                path="direct_csv",
                channel="direct",
                confidence=0.95,
                hint={"columns": columns}
            )

        # 列名存在但不匹配已知 → 调模型判断
        logger.info(f"[ROUTER] CSV 列名未知，调模型判断：{columns}")
        client = ve4_model_get_client()
        question = (f"这份 CSV 的列名是：{columns}。前两行数据是：{sample_lines[1:3]}。"
                    f"文件名是：{file_path.name}。"
                    "这是哪种类型的金融数据？")
        choice = client.ask_choice(question, ["银行账单", "券商交割单", "消费账单", "非金融数据", "不确定"])

        if choice == "银行账单":
            return RoutingDecision(path="direct_csv", channel="model_assisted",
                                   confidence=0.7, hint={"file_type": "bank"})
        elif choice == "券商交割单":
            return RoutingDecision(path="direct_csv", channel="model_assisted",
                                   confidence=0.7, hint={"file_type": "security"})
        elif choice == "消费账单":
            return RoutingDecision(path="direct_csv", channel="model_assisted",
                                   confidence=0.7, hint={"file_type": "consumption"})
        else:
            return RoutingDecision(path="model_csv", channel="model_assisted",
                                   confidence=0.3, hint={"columns": columns})
    def _route_excel(self, file_path: Path) -> RoutingDecision:
        """
        Excel 文件路由：
        1. 读取 sheet 名
        2. 匹配已知银行关键词 → direct_excel
        3. 未知 → 调模型判断
        """
        try:
            import pandas as pd
            xls = pd.ExcelFile(file_path)
            sheets = xls.sheet_names
            sheet_str = ",".join(sheets)

            # Sheet名匹配已知银行
            bank_sheet_keywords = ["招商", "流水", "明细", "交易", "账单", "account", "transaction"]
            for kw in bank_sheet_keywords:
                if kw in sheet_str:
                    logger.info(f"[ROUTER] Excel sheet 名含关键词 '{kw}' → direct_excel")
                    return RoutingDecision(
                        path="direct_excel", channel="direct",
                        confidence=0.9,
                        hint={"sheets": sheets, "matched_keyword": kw}
                    )

            # 读取第一行列名
            df = xls.parse(sheets[0], nrows=2)
            columns = list(df.columns)
            col_str = ",".join([str(c) for c in columns[:8]])

            # 调模型判断
            logger.info(f"[ROUTER] Excel 未知格式，调模型判断：{file_path.name}")
            client = ve4_model_get_client()
            question = (f"这份 Excel 文件：文件名={file_path.name}，"
                        f"Sheet={sheet_str}，列名={col_str}。"
                        "这是哪种金融数据？")
            choice = client.ask_choice(question, ["银行账单", "券商交割单", "财务明细", "非金融数据", "不确定"])

            if choice in ("银行账单", "券商交割单", "财务明细"):
                return RoutingDecision(
                    path="direct_excel", channel="model_assisted",
                    confidence=0.7,
                    hint={"sheets": sheets, "columns": columns, "file_type": choice}
                )
            else:
                return RoutingDecision(path="unknown", confidence=0.2,
                                       hint={"reason": f"模型判定为：{choice}"})

        except Exception as e:
            logger.warning(f"[ROUTER] Excel 读取失败：{e}")
            return RoutingDecision(path="direct_excel", channel="direct",
                                   confidence=0.5, hint={"reason": f"fallback: {e}"})

    def _route_image(self, file_path: Path) -> RoutingDecision:
        """
        图片路由：
        1. 文件名含关键词 → direct_ocr
        2. 文件名无关键词 → 调模型判断内容
        """
        name = file_path.stem

        # 文件名常见 OCR 场景关键词
        ocr_keywords = ["银行", "持仓", "账单", "证券", "股票", "基金",
                        "资产", "Screenshot", "截图", "买入", "卖出",
                        "余额", "收益", "明细"]
        for kw in ocr_keywords:
            if kw in name:
                logger.info(f"[ROUTER] 图片文件名含关键词 '{kw}' → direct_ocr")
                return RoutingDecision(
                    path="direct_ocr", channel="direct", confidence=0.9,
                    hint={"ocr_target": f"含关键词：{kw}"}
                )

        # 无关键词 → 调模型
        logger.info(f"[ROUTER] 图片无关键词，调模型判断：{file_path.name}")
        client = ve4_model_get_client()
        question = (f"文件名：{file_path.name}。"
                    "这是哪种截图？只从以下选项选一个输出："
                    "银行、证券持仓、消费账单、日程、其他")
        choice = client.ask_choice(question, ["银行", "证券持仓", "消费账单", "日程", "其他"])

        if choice in ("银行", "证券持仓", "消费账单"):
            return RoutingDecision(
                path="direct_ocr", channel="model_assisted",
                confidence=0.7,
                hint={"ocr_target": choice}
            )
        elif choice == "日程":
            return RoutingDecision(path="unknown", channel="model_assisted",
                                   confidence=0.3, hint={"ocr_target": "schedule", "reason": "非金融数据"})
        else:
            return RoutingDecision(path="direct_ocr", channel="model_assisted",
                                   confidence=0.5, hint={"ocr_target": "general"})

    def _route_pdf(self, file_path: Path, sample_lines: list[str] = None) -> RoutingDecision:
        """PDF 路由"""
        name = file_path.stem
        bank_kw = ["银行", "对账单", "账单", "statement", "流水", "交割"]
        for kw in bank_kw:
            if kw in name:
                return RoutingDecision(path="direct_pdf", channel="direct",
                                       confidence=0.85, hint={"pdf_type": "financial"})

        return RoutingDecision(path="direct_pdf", channel="model_assisted",
                               confidence=0.5, hint={"pdf_type": "general"})

    def _route_other(self, file_path: Path) -> RoutingDecision:
        """其他文件：TXT/JSON/XML/MD 等 → 直通"""
        ext = file_path.suffix.lower()
        if ext in {'.txt', '.md'}:
            return RoutingDecision(path="direct_txt", channel="direct", confidence=0.8)
        if ext in {'.json', '.xml'}:
            return RoutingDecision(path="direct_txt", channel="model_assisted",
                                   confidence=0.6, hint={"format": ext})
        return RoutingDecision(path="unknown", confidence=0.0,
                               hint={"reason": f"无对应路由：{ext}"})

    def channel_summary(self, routing: RoutingDecision) -> str:
        """生成路由摘要（用于日志）"""
        if routing.channel == "direct":
            return f"✈ 快速通道 → {routing.path} (置信度:{routing.confidence:.0%})"
        else:
            return f"🧠 模型辅助 → {routing.path} (置信度:{routing.confidence:.0%})"


# 单次测试
if __name__ == "__main__":
    router = WorkflowRouter()
    test_files = [
        Path("招商银行流水_202406.csv"),
        Path("Screenshot_20240623_145032.jpg"),
        Path("微信支付账单(1).csv"),
        Path("IMG_0012.png"),
        Path("document.pdf"),
        Path("unknown_file.xyz"),
    ]
    for f in test_files:
        d = router.route(f)
        print(f"{f.name:40s} → {router.channel_summary(d):40s} hint={d.hint}")