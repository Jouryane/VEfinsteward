"""
VE5 LLM 结构化提取器（两步法：先理解后提取）
=============================================
第1步：LLM读取OCR文本 → 输出结构化markdown表格（中间产物，用于RAG）
第2步：LLM读取markdown表格 → 按Pydantic模型输出JSON

两步解耦的优势：
- 第1步允许LLM自由理解、纠错、补全（不强制JSON，减少格式约束干扰理解）
- 第2步输入已清洗的结构化表格，提取JSON准确率极高
- 中间产物（markdown表格）可直接用于RAG

命名规范：ve5_llm_{功能}
"""
import json
import logging
from typing import Optional

logger = logging.getLogger("ve5.llm_extractor")

from receiver.extraction_models import PortfolioData, ScreenshotCategory, ExpenseData, IncomeData


_STEP0_SYSTEM = """你是一位金融App截图的OCR文本理解专家。

你的任务：仔细阅读OCR文本，用一段**连续的数字文字**描述图片中的所有内容。

**输出格式要求（严格遵守）：**
你需要用一段连续的文字介绍图片中的内容，依次包含以下信息：
1. 处理这个文档的时间
2. 截图的文件名
3. 是什么截图（哪个App、什么页面）
4. 依次陈述所有数据（总资产、可用资金、当日盈亏等汇总数据，然后逐个列出每只持仓的名称、数量、现价、成本、盈亏、市值等）

**示例输出：**
2026年1月1日0时01分处理了截图Screenshot_2025_1231_200001.jpg，这是一张东方财富的账户信息截图，用户总资产为200000，当日盈亏为+3000，其中证券市值150000，持仓盈亏+20000，可用资金50000，股票持仓如下：1.招商银行，市值20000，持仓500，可用500，现价40，成本35，持仓盈亏+2500/14.285%......场内基金持仓如下：道琼斯，市值30000，持仓20000，可用0，现价1.500，成本1.505，持仓盈亏-7.5/0.025%......

**重要规则：**
- 只输出这段描述文字，不要输出任何其他内容（不要表格、不要JSON、不要解释）
- 数据必须从OCR文本中提取，不要编造
- 如果OCR中有乱码或缺失，用你的专业知识纠正（如"XD大秦铁"→"大秦铁路"）
- 注意："道琼斯"和"纳指100"是两只不同的基金，不要混淆
- 所有数字必须准确，盈亏要带正负号
- 每只持仓的信息尽量完整"""

_STEP0_PROMPT = """请阅读以下OCR文本，用一段连续的文字描述图片中的所有财务信息。

文件名：{file_name}
处理时间：{process_time}

OCR文本：
{ocr_text}"""


def ve5_llm_describe(text: str, source: str) -> str:
    """
    Step 0: LLM 用连续文字描述截图内容。
    返回描述文本（用于日志记录、 userdata 存储、以及作为后续提取的输入）。
    """
    try:
        from core.ai_gateway import ve4_ai_call
    except Exception as e:
        logger.warning(f"[LLM-DESCRIBE] ai_gateway 不可用: {e}")
        return ""

    from pathlib import Path
    from datetime import datetime

    file_name = Path(source).name if source else "unknown"
    process_time = datetime.now().strftime("%Y年%m月%d日%H时%M分")

    logger.info(f"[LLM-DESCRIBE] 开始生成截图描述: {file_name}")
    result = ve4_ai_call(
        task_type="general",
        system=_STEP0_SYSTEM,
        prompt=_STEP0_PROMPT.format(
            file_name=file_name,
            process_time=process_time,
            ocr_text=text[:6000],
        ),
        format_type="text",
        contains_privacy_data=True,
        complexity="high",
        max_tokens=4096,
        temperature=0.05,
    )

    if not result.success or not result.text:
        logger.warning(f"[LLM-DESCRIBE] LLM调用无返回")
        return ""

    description = result.text.strip()
    logger.info(f"[LLM-DESCRIBE] 描述生成完成 ({len(description)}字符)")

    # ── 如果描述异常长（>2000字符），可能混入了 reasoning 推理过程 ──
    # DeepSeek R1 有时 content 为空，reasoning_content 被当作输出
    # reasoning_content 前半段是推理（含自我质疑），后半段才是最终答案
    if len(description) > 2000:
        # 尝试找到最后一个完整的描述段落（以"截图"、"持仓"等关键词开头的段落）
        import re as _re
        # 按换行分段，找到最后一段包含"截图"且长度合理的段落
        paragraphs = [p.strip() for p in description.split('\n') if p.strip()]
        best = None
        for p in reversed(paragraphs):
            if ('截图' in p or '处理' in p) and len(p) > 50 and len(p) < 1500:
                best = p
                break
        if best:
            logger.info(f"[LLM-DESCRIBE] 从{len(description)}字符中截取最终描述 ({len(best)}字符)")
            description = best
        else:
            # 没有好的段落分隔，取最后 1500 字符
            description = description[-1500:]
            logger.info(f"[LLM-DESCRIBE] 描述过长，截取尾部 {len(description)} 字符")

    # ── 1. 记录到用户日志（完整版）──
    _save_description_log(file_name, result.text.strip(), raw_ocr=text)

    # ── 2. 存入 userdata ──
    _save_description_to_userdata(file_name, description, source)

    return description


def _save_description_log(file_name: str, description: str, raw_ocr: str = ""):
    """将 LLM 描述写入用户日志（JSONL 格式），同时保存原始 OCR 供 RAG 检索。"""
    try:
        from app_paths import DATA_DIR
        from datetime import datetime
        import json

        log_dir = DATA_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "screenshot_descriptions.jsonl"

        record = {
            "timestamp": datetime.now().isoformat(),
            "file_name": file_name,
            "description": description,
            "ocr_text": raw_ocr[:3000] if raw_ocr else description,  # 原始 OCR 截取前3000字符
        }
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        logger.info(f"[LLM-DESCRIBE] 日志已记录: {log_file.name}")
    except Exception as e:
        logger.debug(f"[LLM-DESCRIBE] 日志记录失败（不影响主流程）: {e}")


def _save_description_to_userdata(file_name: str, description: str, source: str):
    """将 LLM 描述存入 userdata/screenshot_descriptions/ 目录。"""
    try:
        from app_paths import DATA_DIR
        from datetime import datetime
        from pathlib import Path

        desc_dir = DATA_DIR / "screenshot_descriptions"
        desc_dir.mkdir(parents=True, exist_ok=True)

        base_name = Path(file_name).stem if file_name else "unknown"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        desc_file = desc_dir / f"{base_name}_{ts}.txt"

        desc_file.write_text(description, encoding="utf-8")
        logger.info(f"[LLM-DESCRIBE] 描述已存入: {desc_file}")
    except Exception as e:
        logger.debug(f"[LLM-DESCRIBE] userdata存储失败（不影响主流程）: {e}")


# ════════════════════════════════════════════════════════
# 第1步：OCR → 结构化表格（自由文本输出）
# ════════════════════════════════════════════════════════

_STEP1_SYSTEM = """你是一位金融App截图的OCR文本理解专家。

你的任务：仔细阅读以下文本，理解其中所有的财务信息，然后以清晰的markdown表格形式输出。

**输入说明：**
文本可能包含两段：【LLM 摘要描述】是 LLM 对截图内容的纠错后摘要，【原始 OCR 文本】是机器识别的原始文本。
- 如果两段都有，以原始 OCR 为准提取数据，用摘要描述辅助理解（如纠正乱码、确认上下文）
- 如果只有一段，直接从中提取

**工作步骤：**
1. 判断这是什么类型的截图（证券/银行/基金）
2. 识别出所有的持仓产品，纠正OCR错误
3. 将结果整理为表格

**OCR错误纠正规则：**
- "XD大秦铁" → "大秦铁路"
- "道琥斯" → "道琼斯"（仅纠正乱码，道琼斯和纳指100是两只不同的ETF，不要混淆）
- "低波红利" → "红利低波ETF"
- "工银黄金" → "工银黄金ETF"
- 金额中的千位分隔符逗号是格式符号不是小数点，如"17,182.14"就是17182.14元，不要丢弃逗号前的数字

**忽略项（不要写入表格）：**
广告文字（如"上月支出报告已出"）
功能按钮（如"查看收益"）
非持仓的汇总标签（如"持仓金额"本身不是产品）

**输出要求：**
直接输出markdown表格，不要任何解释。表格上方写一行"## 截图类型：证券/银行/基金"。
如果是证券截图，表格列头为：| 产品名称 | 持仓数量 | 现价 | 成本价 | 盈亏 | 市值 |
如果是银行截图，表格列头为：| 产品名称 | 持仓金额 |
如果是基金截图，表格列头为：| 产品名称 | 持仓金额 |
表格中必须包含所有持仓产品（包括ETF、股票、基金等）。
表格最后一行单独写：| 证券可用资金 | - | - | - | - | 金额 |（仅证券截图）
表格下方写一行汇总信息：总资产、可用资金等。"""

_STEP1_PROMPT = """请阅读以下OCR文本，以markdown表格形式输出所有财务信息。

OCR文本：
{ocr_text}"""


# ════════════════════════════════════════════════════════
# 第2步：结构化表格 → JSON（强制JSON输出）
# ════════════════════════════════════════════════════════

_STEP2_SYSTEM = """你是一位数据格式化专家。你会收到一份已经整理好的markdown表格，其中包含金融持仓信息。

你的任务：将表格中的数据转换为指定的JSON格式。

**分类规则（严格按此规则判断asset_class）：**
- aggressive（权益）：股票、偏股基金、指数基金、QDII、权益类ETF、海外ETF
- stable（固收）：债券、纯债基金、短债基金、银行理财、固收类产品、国债ETF
- liquid（流动）：现金、活期存款、货币基金、货币ETF、可用资金
- protection（保障）：黄金（包括黄金ETF、纸黄金）、保险、年金、避险资产
- **重要**：黄金属于保障类，不是权益类！工银黄金ETF、黄金ETF都属于protection。
- **重要**：货币ETF属于liquid，不是aggressive！
- **重要**：国债ETF属于stable，不是aggressive！

**规则：**
1. 严格按照表格中的数据填写，不要添加或删除任何条目
2. 产品名称直接使用表格中的名称
3. 金额直接使用表格中的数字
4. **每个持仓必须包含asset_class字段**（aggressive/stable/liquid/protection之一）

**输出：只输出JSON，不要任何解释。**"""

_STEP2_PROMPT = """请将以下表格数据转换为JSON格式。

{structured_table}

请按以下格式输出JSON：

{{
  "screenshot_type": "broker" 或 "bank" 或 "fund",
  "total_assets": 总资产数字,
  "available_cash": 可用资金数字,
  "stocks": [{{"name":"名称","asset_class":"aggressive/stable/liquid/protection","quantity":数量,"current_price":现价,"cost_price":成本价,"profit":盈亏,"market_value":市值}}],
  "etfs": [{{"name":"名称","asset_class":"aggressive/stable/liquid/protection","quantity":数量,"current_price":现价,"cost_price":成本价,"profit":盈亏,"market_value":市值}}],
  "bank_holdings": [{{"name":"名称","asset_class":"aggressive/stable/liquid/protection","market_value":金额}}],
  "fund_holdings": [{{"name":"名称","asset_class":"aggressive/stable/liquid/protection","quantity":数量,"current_price":现价,"cost_price":成本价,"profit":盈亏,"market_value":市值}}]
}}

**注意**：
1. 每个持仓必须有asset_class字段
2. 如果表格中有"证券可用资金"这一行，提取其金额作为available_cash，不要把这一行放入stocks/etfs数组
3. 如果表格中没有"证券可用资金"，从表格下方的汇总文字中提取，如"可用资金: 45287.55"

{existing_context}"""


def _build_existing_context(source_file: str) -> str:
    """构建现有数据上下文（供 LLM 判断更新/替换/新增）"""
    try:
        import sqlite3
        from app_paths import DB_PATH
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        fn = Path(source_file).stem
        rows = conn.execute(
            "SELECT product_name, current_value, asset_class, source_file FROM asset_holdings ORDER BY current_value DESC LIMIT 20"
        ).fetchall()
        conn.close()

        # 按来源分组（截图文件名前缀匹配）
        same = []
        other = []
        for r in rows:
            rf = r["source_file"] or ""
            if any(w in rf for w in fn.split("_")[:3] if len(w) > 2):
                same.append(r)
            else:
                other.append(r)

        if not same and not other:
            return ""

        lines = ["\n**当前系统中已有数据**（来自你的历史截图处理）："]
        if same:
            lines.append(f"来自同一来源（{fn[:20]}...）：")
            for r in same[:10]:
                lines.append(f"  - {r['product_name']}: ¥{r['current_value']:,.2f} [{r['asset_class']}]")
        if other:
            lines.append(f"来自其他来源：")
            for r in other[:5]:
                lines.append(f"  - {r['product_name']}: ¥{r['current_value']:,.2f} [{r['asset_class']}]")
        lines.append("\n⚠️ **处理决策**：")
        lines.append("- 如果新截图是同一账户的**新快照**（时间更新），旧数据需要被**替换**——在输出中仅包含新截图的数据")
        lines.append("- 如果新截图是**新账户**或新类型的数据，则作为**新增**输出")
        lines.append("- 如果新旧数据有重叠，保留最新截图中的数值")
        return "\n".join(lines)
    except Exception:
        return ""


# ════════════════════════════════════════════════════════
# 核心函数
# ════════════════════════════════════════════════════════

def ve4_llm_extract(text: str, source: str) -> list:
    """
    LLM 提取持仓（两步法）。
    保持原有函数签名，内部实现为：OCR → 表格 → JSON → Pydantic校验 → 旧格式。

    Returns:
        list of dicts: [{"name": ..., "value": ..., "type": ..., "account": ...}]
    """
    try:
        portfolio = ve5_llm_extract_portfolio(text, source)
        if portfolio:
            return portfolio.to_legacy_holdings()
        return []
    except Exception as e:
        logger.warning(f"[LLM-EXT] 提取失败: {e}")
        return []


def ve5_llm_extract_portfolio(text: str, source: str) -> Optional[PortfolioData]:
    """
    两步法提取：第1步理解 → 第2步格式化
    """
    try:
        from core.ai_gateway import ve4_ai_call
    except Exception as e:
        logger.warning(f"[LLM-EXT] ai_gateway 不可用: {e}")
        return None

    # ── Step 0: LLM 描述截图内容（日志记录 + 数据存档）──
    description = ve5_llm_describe(text, source)

    # ── 第1步：描述文本 + 原始OCR → 结构化表格 ──
    # 同时提供描述（已纠错、有上下文）和原始 OCR（完整数据），避免 Step0 遗漏信息
    if description:
        step1_input = "【LLM 摘要描述】\n" + description + "\n\n【原始 OCR 文本】\n" + text
    else:
        step1_input = text
    logger.info(f"[LLM-EXT] 第1步：描述+OCR → 结构化表格 (输入{len(step1_input)}字符)")
    step1_result = ve4_ai_call(
        task_type="general",
        system=_STEP1_SYSTEM,
        prompt=_STEP1_PROMPT.format(ocr_text=step1_input),
        format_type="text",
        contains_privacy_data=True,
        complexity="high",
        max_tokens=4096,
        temperature=0.1,
    )

    if not step1_result.success or not step1_result.text:
        logger.warning(f"[LLM-EXT] 第1步失败：LLM调用无返回")
        return None

    structured_table = step1_result.text.strip()
    logger.info(f"[LLM-EXT] 第1步完成，得到结构化表格 ({len(structured_table)}字符)")

    # 存储中间产物用于RAG
    _save_intermediate_table(source, structured_table)

    # ── 第2步：结构化表格 → JSON ──
    logger.info(f"[LLM-EXT] 第2步：结构化表格 → JSON")
    existing_ctx = _build_existing_context(source)
    step2_prompt = _STEP2_PROMPT.format(
        structured_table=structured_table,
        existing_context=existing_ctx
    )
    step2_result = ve4_ai_call(
        task_type="json",
        system=_STEP2_SYSTEM,
        prompt=step2_prompt,
        format_type="json",
        contains_privacy_data=True,
        complexity="high",
        max_tokens=4096,
        temperature=0.0,
    )

    if not step2_result.success or not step2_result.text:
        logger.warning(f"[LLM-EXT] 第2步失败：LLM调用无返回")
        return None

    # 解析JSON
    parsed = _parse_llm_json(step2_result.text)
    if not parsed:
        logger.warning(f"[LLM-EXT] 第2步JSON解析失败")
        return None

    # Pydantic 强制校验
    try:
        portfolio = PortfolioData.model_validate(parsed)
        logger.info(f"[LLM-EXT] Pydantic校验通过: {portfolio.screenshot_type}, "
                   f"stocks={len(portfolio.stocks)}, etfs={len(portfolio.etfs)}, "
                   f"bank={len(portfolio.bank_holdings)}, fund={len(portfolio.fund_holdings)}")
        return portfolio
    except Exception as e:
        logger.warning(f"[LLM-EXT] Pydantic校验失败: {e}")
        return _try_partial_parse(parsed)


def _save_intermediate_table(source: str, table_text: str):
    """将第1步的中间产物（结构化表格）存储，供RAG使用。"""
    try:
        from pathlib import Path
        from datetime import datetime

        # 存储到 data/intermediate_tables/ 目录
        from app_paths import DATA_DIR
        table_dir = DATA_DIR / "intermediate_tables"
        table_dir.mkdir(parents=True, exist_ok=True)

        # 用源文件名作为基础名
        base_name = Path(source).stem if source else "unknown"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        table_file = table_dir / f"{base_name}_{ts}.md"

        table_file.write_text(table_text, encoding="utf-8")
        logger.info(f"[LLM-EXT] 中间产物已存储: {table_file}")
    except Exception as e:
        logger.debug(f"[LLM-EXT] 中间产物存储失败（不影响主流程）: {e}")


def _try_partial_parse(parsed: dict) -> Optional[PortfolioData]:
    """当完整校验失败时，尝试部分解析有效数据。"""
    try:
        for key in ["stocks", "etfs", "bank_holdings", "fund_holdings"]:
            items = parsed.get(key, [])
            if isinstance(items, list):
                parsed[key] = [item for item in items if item.get("name")]
        return PortfolioData.model_validate(parsed)
    except Exception:
        return None


def _parse_llm_json(text: str) -> Optional[dict]:
    """从LLM输出中提取JSON对象。"""
    text = text.strip()
    if not text:
        return None

    # 去除markdown代码块
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    # 策略1：直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 策略2：在文本中搜索JSON对象
    import re
    for match in re.finditer(r'\{', text):
        start = match.start()
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == '\\':
                escape = True
                continue
            if ch == '"' and not escape:
                in_string = not in_string
                continue
            if not in_string:
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:i+1]
                        try:
                            parsed = json.loads(candidate)
                            if isinstance(parsed, dict) and "screenshot_type" in parsed:
                                return parsed
                        except json.JSONDecodeError:
                            pass
                        break

    return None


# ════════════════════════════════════════════════════════
# Step 0：截图属性预分类（资产 / 消费 / 收入 / 其它）
# ════════════════════════════════════════════════════════

_CLASSIFY_SYSTEM = """你是一位金融App截图分类专家。

你的任务：阅读OCR文本，判断这张截图属于以下哪种类型：

1. **asset（资产持仓）**：展示用户的资产、持仓、理财产品、股票基金等
   - 关键词：持仓、市值、总资产、收益、盈亏、朝朝宝、理财、基金、证券、股票
   - 特征：展示"你拥有什么"、"值多少钱"

2. **expense（消费支出）**：展示用户的消费账单、支出明细、交易流水
   - 关键词：支出、消费、账单、交易明细、付款、扣款、订单、商户
   - 特征：展示"你花了什么"、"花了多少钱"

3. **income（收入）**：展示用户的收入、到账、工资、退款
   - 关键词：收入、到账、工资、转入、退款、报销
   - 特征：展示"你收到了什么钱"

4. **other（其它）**：不包含有用财务信息的截图（广告、验证码、聊天记录等）

**重要**：如果截图同时包含资产和消费信息（如银行App总览页），优先判断为"asset"。
如果截图是"上月支出报告"或"支出构成"类页面，判断为"expense"。

**输出：只输出JSON，不要任何解释。**"""

_CLASSIFY_PROMPT = """请判断以下OCR文本来自哪种类型的截图：

{ocr_text}

请按以下格式输出JSON：
{{"category": "asset"或"expense"或"income"或"other", "confidence": 0.9, "reason": "一句话理由"}}"""


def ve5_llm_classify_screenshot(text: str) -> Optional[ScreenshotCategory]:
    """
    Step 0：LLM 预分类截图属性。
    Returns: ScreenshotCategory 或 None（LLM不可用时返回None，走原有逻辑）
    """
    try:
        from core.ai_gateway import ve4_ai_call
    except Exception as e:
        logger.warning(f"[LLM-CLASSIFY] ai_gateway 不可用: {e}")
        return None

    try:
        # 截取前2000字符即可判断类型（无需全文）
        truncated = text[:2000] if len(text) > 2000 else text
        result = ve4_ai_call(
            task_type="json",
            system=_CLASSIFY_SYSTEM,
            prompt=_CLASSIFY_PROMPT.format(ocr_text=truncated),
            format_type="json",
            contains_privacy_data=False,  # 分类不需要隐私标记
            complexity="low",
            max_tokens=200,
            temperature=0.0,
        )

        if not result.success or not result.text:
            return None

        parsed = _parse_llm_json(result.text)
        if not parsed:
            return None

        # 强制 category 字段合法
        cat = parsed.get("category", "other")
        if cat not in ("asset", "expense", "income", "other"):
            cat = "other"

        confidence = min(1.0, max(0.0, float(parsed.get("confidence", 0.9))))
        reason = parsed.get("reason", "")

        classified = ScreenshotCategory(
            category=cat, confidence=confidence, reason=reason
        )
        logger.info(f"[LLM-CLASSIFY] 截图分类: {cat} (置信度{confidence:.0%}) - {reason}")
        return classified

    except Exception as e:
        logger.warning(f"[LLM-CLASSIFY] 分类失败: {e}")
        return None


# ════════════════════════════════════════════════════════
# 消费记录 LLM 提取
# ════════════════════════════════════════════════════════

_EXPENSE_EXTRACT_SYSTEM = """你是一位消费账单OCR文本提取专家。

你的任务：从OCR文本中提取所有消费/支出记录，输出为JSON格式。

【第一步：判断截图类型】

首先判断这张截图属于哪种类型：

**类型A：明细页（交易明细/账单流水）**
- 特征：有多笔具体交易，每笔包含日期、商户名、金额
- 例如："7月15日 早餐店 -¥12.50"、"07-10 兰州拉面 ¥18.00"

**类型B：汇总页（支出构成/分类统计）**
- 特征：显示"支出构成"、"分类统计"等标题，下面是各个分类+金额/百分比
- 例如："餐饮美食 28.7% ¥784.09"、"旅行 ¥723.00"
- 没有具体的商户名，只有分类名称

**【重要】如果是汇总页，千万不要编造商户名！** 汇总页没有单笔交易，你应该按"分类汇总"的方式输出，每个分类一条记录，counterparty 字段填写 "分类汇总：XX"（XX是分类名）。

---

**核心规则：优先使用截图原有的分类标签！**
微信/支付宝账单截图本身就有分类标签（如"餐饮美食"、"交通出行"等）。
你必须**优先保留**这些原始分类标签，而不是自己重新分类。

**分类映射规则（将截图原始分类映射到标准分类）：**
- 餐饮/美食/食/饭/外卖/饮品/零食/早餐/奶茶/咖啡 → "餐饮"
- 交通/出行/打车/滴滴/公交/地铁/高铁/机票/加油/停车 → "交通"
- 购物/淘宝/京东/拼多多/商场/服饰/数码 → "购物"
- 日用/超市/便利店/家居/生活服务/百货 → "日用"
- 娱乐/电影/游戏/视频/会员/KTV/运动/健身/休闲/文化 → "娱乐"
- 居住/房租/水电/物业/燃气/宽带/生活缴费 → "居住"
- 月供/房贷/车贷/还款/分期付款 → "月供"
- 医疗/医院/药店/体检/保健/健康 → "医疗"
- 教育/培训/课程/书本/学校/学习 → "教育"
- 通讯/话费/流量/充值 → "通讯"
- 旅行/酒店/景点/门票/旅行团/民宿/航空 → "旅行"
- 服务/转账/其他/无法归类 → "其他"

**必需消费判定规则（is_essential）：**

必需消费 = 维持基本生活所必需的支出，即使缩减也必须保留的支出。
弹性消费 = 可以削减或推迟的支出，不影响基本生活。

**判定标准（按优先级）：**
1. **商户名和描述是关键**（仅适用于明细页）。例如：
   - "永辉超市" "盒马" "沃尔玛" "菜市场" 购买食材/日用品 → 必需（即使分类是"购物"）
   - "淘宝" "京东" "商场" 购买服饰/数码/奢侈品 → 弹性
   - "美团外卖" "肯德基" "沙县小吃" → 必需（餐饮）
   - "星巴克" "喜茶" "海底捞" → 弹性（非必要享受型餐饮）
   - "地铁" "公交" "滴滴" "加油站" → 必需（交通）
   - "房租" "水电" "物业" "燃气" → 必需（居住）
   - "医院" "药店" "体检" → 必需（医疗）
   - "电影票" "游戏充值" "KTV" → 弹性（娱乐）
   - "酒店" "机票" "旅行团" → 弹性（旅行）
   - "话费" "流量" "宽带" → 必需（通讯）

2. **汇总页按分类整体判定**：
   - 必需分类：餐饮、交通、日用、居住、月供、医疗、教育、通讯
   - 弹性分类：娱乐、旅行
   - 购物/其他：默认保守估计为必需（因为其中可能包含超市买菜等必需支出）

3. **如果无法判断，默认保守估计为必需消费**（is_essential = true）。

**工作步骤：**
1. 判断截图类型（明细页 / 汇总页）
2. 如果是明细页：提取每笔消费的商户名、金额、日期、分类
3. 如果是汇总页：提取每个分类的名称和金额，counterparty 填 "分类汇总：XX"
4. 为每条记录判定 is_essential
5. 提取截图中的总支出金额（total_expense_from_summary）

**绝对禁止：**
- 不要从OCR噪声中编造商户名（如"Laie"、"WD t"、"® Ht"等明显不是中文商户名的内容）
- 不要把功能按钮、广告文字、月度小结等当成消费记录
- 汇总页不要编造商户名，直接用分类名

**输出：只输出JSON，不要任何解释。**"""

_EXPENSE_EXTRACT_PROMPT = """请从以下OCR文本中提取所有消费记录。

OCR文本：
{ocr_text}

请按以下格式输出JSON：
{{
  "total_expense": 所有记录的金额求和,
  "total_expense_from_summary": 截图中显示的汇总总支出金额（如"总支出 ¥3,256.80"中的3256.80），截图没有则填0,
  "records": [
    {{"date": "YYYY-MM 或 MM-DD", "counterparty": "商户名 或 分类汇总：XX", "amount": 金额数字, "category_primary": "标准分类", "is_essential": true或false, "description": "原始行文本"}}
  ]
}}

**重要提醒**：
- 先判断是明细页还是汇总页
- 汇总页的 counterparty 填 "分类汇总：XX"，不要编造商户名
- 只有百分比没有金额的分类不要提取
- category_primary 必须使用标准分类名称
- is_essential 按必需消费规则判定
- total_expense_from_summary 是从截图汇总行直接读取的金额（最准确），不是records的求和

如果无法提取到有效记录，返回 {{"total_expense": 0, "total_expense_from_summary": 0, "records": []}}。"""


def ve5_llm_extract_expenses(text: str, source: str) -> list:
    """
    LLM 提取消费记录（替代原来被禁用的空函数）。
    Returns: list of dicts（兼容 _write_expenses_tx 的旧格式）
    """
    try:
        from core.ai_gateway import ve4_ai_call
    except Exception as e:
        logger.warning(f"[LLM-EXPENSE] ai_gateway 不可用: {e}")
        return []

    try:
        logger.info(f"[LLM-EXPENSE] 开始提取消费记录")
        result = ve4_ai_call(
            task_type="json",
            system=_EXPENSE_EXTRACT_SYSTEM,
            prompt=_EXPENSE_EXTRACT_PROMPT.format(ocr_text=text[:4000]),
            format_type="json",
            contains_privacy_data=True,
            complexity="high",
            max_tokens=4096,
            temperature=0.0,
        )

        if not result.success or not result.text:
            logger.warning(f"[LLM-EXPENSE] LLM调用无返回")
            return []

        parsed = _parse_llm_json(result.text)
        if not parsed:
            logger.warning(f"[LLM-EXPENSE] JSON解析失败")
            return []

        # Pydantic 校验
        expense_data = ExpenseData.model_validate(parsed)
        logger.info(f"[LLM-EXPENSE] 提取到 {len(expense_data.records)} 条消费记录，"
                    f"合计 ¥{expense_data.total_expense:,.2f}")

        return expense_data.to_legacy_expenses()

    except Exception as e:
        logger.warning(f"[LLM-EXPENSE] 提取失败: {e}")
        return []


# ════════════════════════════════════════════════════════
# 收入记录 LLM 提取
# ════════════════════════════════════════════════════════

_INCOME_EXTRACT_SYSTEM = """你是一位收入/到账信息OCR文本提取专家。

你的任务：从OCR文本中提取所有收入记录，输出为JSON格式。

**分类规则：**
- 工资：薪资、工资、薪金
- 理财收益：利息、分红、收益到账
- 转账收入：他人转账
- 退款：退货退款、取消订单退款
- 其他：无法归类

**输出：只输出JSON，不要任何解释。**"""

_INCOME_EXTRACT_PROMPT = """请从以下OCR文本中提取所有收入记录。

OCR文本：
{ocr_text}

请按以下格式输出JSON：
{{
  "total_income": 汇总收入金额数字,
  "records": [
    {{"date": "MM-DD", "source": "收入来源", "amount": 金额数字, "category_primary": "分类", "description": "原始行文本"}}
  ]
}}

如果无法提取到具体记录，返回 {{"total_income": 0, "records": []}}。"""


def ve5_llm_extract_income(text: str, source: str) -> list:
    """
    LLM 提取收入记录。
    Returns: list of dicts（兼容 _write_expenses_tx 的旧格式，但 transaction_type='income'）
    """
    try:
        from core.ai_gateway import ve4_ai_call
    except Exception as e:
        logger.warning(f"[LLM-INCOME] ai_gateway 不可用: {e}")
        return []

    try:
        logger.info(f"[LLM-INCOME] 开始提取收入记录")
        result = ve4_ai_call(
            task_type="json",
            system=_INCOME_EXTRACT_SYSTEM,
            prompt=_INCOME_EXTRACT_PROMPT.format(ocr_text=text[:4000]),
            format_type="json",
            contains_privacy_data=True,
            complexity="medium",
            max_tokens=2048,
            temperature=0.0,
        )

        if not result.success or not result.text:
            return []

        parsed = _parse_llm_json(result.text)
        if not parsed:
            return []

        income_data = IncomeData.model_validate(parsed)
        logger.info(f"[LLM-INCOME] 提取到 {len(income_data.records)} 条收入记录")

        # 转换为兼容格式
        result_list = []
        for r in income_data.records:
            if r.amount > 0:
                result_list.append({
                    "date": r.date,
                    "amount": r.amount,
                    "counterparty": r.source,
                    "category": r.category_primary,
                    "description": r.description,
                    "transaction_type": "income",  # 标记为收入
                })
        return result_list

    except Exception as e:
        logger.warning(f"[LLM-INCOME] 提取失败: {e}")
        return []