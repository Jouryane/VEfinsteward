# VE5 数据提取流程诊断文档

> 最后更新：2026-07-13
> 目的：完整记录同步处理的每一个环节，定位名称/金额反复出错的根因

---

## 一、完整数据流

```
截图文件 (.jpg)
  │
  ▼
[Step 1] OCR 引擎 (receiver/ocr_engine.py → ve4_ocr())
  │  输出：raw_text（纯文本，约500-800字符）
  │  引擎：RapidOCR-OpenVINO (本地)
  │
  ▼
[Step 2] 提取结构化数据（两条并行路径）
  │
  ├── 路径A：LLM 提取 (receiver/llm_extractor.py → ve4_llm_extract())
  │   │
  │   │  ┌─ 第1步 ──────────────────────────────────────┐
  │   │  │ ai_gateway.py → ve4_ai_call()                │
  │   │  │ format_type="text"                           │
  │   │  │ system="金融App截图的OCR文本理解专家"           │
  │   │  │ 输出：markdown表格（中间产物）                  │
  │   │  │ 存储：data/intermediate_tables/xxx.md        │
  │   │  └─────────────────────────────────────────────┘
  │   │
  │   │  ┌─ 第2步 ──────────────────────────────────────┐
  │   │  │ ai_gateway.py → ve4_ai_call()                │
  │   │  │ format_type="json"                           │
  │   │  │ payload["response_format"] = {"type":"json_object"} │
  │   │  │ 输出：JSON对象                                │
  │   │  └─────────────────────────────────────────────┘
  │   │
  │   │  ┌─ Pydantic 校验 ─────────────────────────────┐
  │   │  │ receiver/extraction_models.py                │
  │   │  │ PortfolioData.model_validate(parsed)        │
  │   │  │ → 名称补全 + 市值≈数量×现价校验              │
  │   │  │ → to_legacy_holdings() 转换为旧格式          │
  │   │  └─────────────────────────────────────────────┘
  │   │
  │   └── 路径B：正则提取 (receiver/pipeline.py → _regex_extract())
  │       证券截图：行首正则匹配产品名 + 排除6位代码 + 取最后大金额
  │       银行截图：产品名 + "持仓金额"跨行匹配
  │       基金截图：产品名 + 下一行纯数字
  │
  ▼
[Step 2.5] 合并 (_merge_holdings)
  │  LLM 结果优先，正则补充 LLM 未覆盖的产品（按名称去重）
  │
  ▼
[Step 2.6] 补充汇总项
  │  _extract_summary() 提取可用资金、证券市值
  │  补充到 holdings 列表（如果 LLM/正则没有同名项）
  │
  ▼
[Step 3] 写入数据库 (pipeline.py → _write_holdings_tx)
  │  表：asset_holdings
  │  去重策略：同 source_file 全量替换，同名产品保留金额较大者
  │
  ▼
[Step 4] 记录活动 → [Step 5] RAG → [Step 6] 归档
```

---

## 二、已观察到的错误模式

### 错误1：正则提取把数字拼入产品名称

**现象**：`XD`、`ETF`、`投资 31938.67`、`证券市值` 作为产品名出现

**根因分析**：

证券截图的正则产品名提取规则（pipeline.py 第255行）：
```python
name_match = re.match(r'^([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9]*(?:ETF|QDII|基金|股票|债券|理财|黄金)?)', line)
```

这个正则的问题：
1. `[\u4e00-\u9fffA-Za-z]` — 要求以中文或字母开头。但 OCR 行可能是 `XD 大秦铁 3000 4.680...`，`XD` 是两个大写字母，匹配成功
2. `[\u4e00-\u9fffA-Za-z0-9]*` — 匹配任意中文/字母/数字。会一直匹配到空格或非字母数字字符
3. `(?:ETF|...)?` — 只匹配行尾，不是行中的

**实际OCR行**（东方财富证券）：
```
XD 大秦铁30004.680-/20.0814040.00
```

OCR把所有内容粘连成了一行（空格不规则），正则匹配到 `XD` 就停止了（因为 `XD` 后面是空格），但 `_is_meta_line` 没有过滤掉这行。

**关键疑问**：如果 LLM 提取正确，正则应该被 LLM 的结果覆盖。为什么正则结果还是出现了？

→ 答案在 `_merge_holdings` 函数：它用**名称去重**，如果 LLM 返回"大秦铁路"但正则返回"XD"，两个名称不同，两个都会被写入。

### 错误2：金额反复出错

**现象**：
- `XD` 对应 ¥38,004,920.00（应为 ¥14,040.00）
- `ETF` 对应 ¥140,000.49（应为 ¥5,558.00）
- `投资 31938.67` 对应 ¥31,507.99

**根因分析**：

正则的金额提取逻辑（pipeline.py 第262-272行）：
```python
amounts = []
for m in re.finditer(r'[¥￥]?\s*([\d,]+\.\d{1,2}|[\d,]+)', line):
    g1 = m.group(1) or ""
    if not g1.strip():
        continue
    if re.match(r'^\d{6}$', g1.replace(',', '')):
        continue  # 排除6位基金代码
    val = _parse_num(g1)
    if val > 1000:
        amounts.append(val)
if amounts:
    holdings.append({"name": name, "value": amounts[-1], ...})
```

以 `XD 大秦铁30004.680-/20.0814040.00` 为例：
- OCR 粘连后，`30004.680` 被当作一个数字 → `_parse_num` 得到 30004.68
- `-/20.08` 中提取不到有效金额
- `14040.00` 提取到 14040.00
- `amounts = [30004.68, 14040.00]`
- `amounts[-1]` = 14040.00（正确！）

但等等——如果金额取的是最后一个 >1000 的数字，应该是对的。那 `38004920.00` 是怎么来的？

**可能的原因**：OCR 把多行粘连成了一行，或者 `re.sub(r'(?<=\d)\s+(?=\d)', '', text)` 预处理把本应分开的数字连在了一起。

例如原始 OCR 可能是：
```
XD 大秦铁 3000 4.680 -/20.08 14040.00
```
预处理后变成：
```
XD大秦铁30004.680-/20.0814040.00
```
这里 `3000` 和 `4.680` 被合并成了 `30004.680`。

但即使如此，`amounts[-1]` 应该是 `14040.00`，不是 `38004920`。所以这个金额很可能是**跨行累积**导致的。

### 错误3：汇总项"证券市值"被当作产品名

**根因**：`_extract_summary` + `_regex_extract` 都会生成 `{"name": "证券市值", ...}` 项。如果 LLM 也生成了 `securities_value`，`to_legacy_holdings` 会生成"证券可用资金"和"证券市值"，再与正则的汇总项合并时，如果名称不完全匹配，就会重复。

### 错误4：银行截图始终提取失败

**根因**：`is_bank` 判断依赖关键词 `"朝朝宝"`, `"活期存款"`, `"今日收益"`, `"持仓金额"`, `"招商银行"` 中至少一个出现在 OCR 文本中。如果 OCR 没有识别到这些词（被识别为乱码），`is_bank` 为 False，银行截图会走到基金提取逻辑或被完全跳过。

---

## 三、LLM 两步法的实际表现

### 第1步输出（format_type="text"）

在当前 ai_gateway.py 的实现中，`format_type="text"` 时：
- **不添加** `response_format` 参数
- DeepSeek 推理模型可能返回 `reasoning_content`（思考过程）+ `content`（最终回答）
- `content` 可能包含 markdown 表格

### 第2步输出（format_type="json"）

- 添加 `payload["response_format"] = {"type": "json_object"}`
- DeepSeek 被强制在 `content` 字段返回 JSON
- 如果 `content` 为空，从 `reasoning_content` 中用正则提取 `{...}`

### 潜在问题

**第1步的问题**：`format_type="text"` 时，`_call_openai` 的逻辑是：
```python
if format_type == "json":
    payload["response_format"] = {"type": "json_object"}
    ...
```

这意味着第1步**不会**添加 `response_format`。对于 DeepSeek 推理模型：
- 它会返回 `reasoning_content`（思考过程）和 `content`（最终回答）
- `content` 应该是 markdown 表格
- 但如果模型认为表格太简单，可能直接在 `reasoning_content` 中就输出了完整回答，`content` 为空

**当前代码对 format_type="text" 时 content 为空的处理**：
```python
content = (msg.get("content") or "").strip()
if not content and msg.get("reasoning_content"):
    content = msg["reasoning_content"].strip()
    logger.info(f"[AI-GATEWAY] content为空，回退使用reasoning_content")
```

等等——这段代码只在 `format_type="json"` 分支之后，是**无条件执行**的。也就是说，对于 `format_type="text"`，如果 `content` 为空，也会回退使用 `reasoning_content`。这实际上是**正确**的——`reasoning_content` 中也包含模型想输出的内容。

但问题是：`reasoning_content` 可能包含**推理过程**（"我们分析OCR文本..."），而不是最终的 markdown 表格。如果 DeepSeek 把推理过程放在 `reasoning_content` 而把最终答案放在 `content`（或反过来），我们可能拿到了错误的文本。

---

## 四、正则提取始终生效的问题

**这是最关键的设计问题**。

当前流程是：
```python
llm_holdings = _try_llm_extract(raw_text, str(file_path))    # 可能返回空列表
regex_holdings = _regex_extract(raw_text, str(file_path))      # 总是执行
holdings = _merge_holdings(llm_holdings, regex_holdings)       # 合并
```

`_merge_holdings` 只按名称去重。如果 LLM 返回了"大秦铁路"但正则返回了"XD"，两个都会被写入。

**即使 LLM 提取完全正确，正则的垃圾数据也会污染最终结果。**

---

## 五、待确认的问题清单

1. **正则提取是否应该在 LLM 成功时完全跳过？**
   - 当前设计是"合并"，但合并不精确（按名称去重无法匹配"XD"和"大秦铁路"）
   - 建议：LLM 返回有效结果时，完全跳过正则

2. **第1步 format_type="text" 时，DeepSeek 返回的 content 和 reasoning_content 分别是什么？**
   - 需要查看实际日志确认
   - 如果 content 中已经有正确的 markdown 表格，那第1步是成功的
   - 如果 content 为空、reasoning_content 中是推理过程，那第1步就失败了

3. **OCR 预处理 `re.sub(r'(?<=\d)\s+(?=\d)', '', text)` 是否导致数字错误粘连？**
   - 例如 `3000 4.680` → `30004.680`
   - 这个预处理本意是处理 `1 000.00` → `1000.00`，但对 `3000 4.680` 会造成错误

4. **`_is_meta_line` 是否正确过滤了所有元数据行？**
   - "XD 大秦铁..." 这种行是否应该被过滤？

5. **汇总项（证券市值、可用资金）是否应该从持仓列表中分离？**
   - 当前"证券市值"既在正则结果中，也在 _extract_summary 的补充中，可能导致重复

---

## 六、关键代码位置索引

| 模块 | 文件 | 关键行 | 功能 |
|------|------|--------|------|
| 主流程 | receiver/pipeline.py | L47-151 | process_file：OCR→提取→合并→写入 |
| LLM提取 | receiver/llm_extractor.py | L120-185 | 两步法：表格→JSON→Pydantic |
| Pydantic模型 | receiver/extraction_models.py | L12-227 | StockHolding/ETFHolding/PortfolioData |
| 正则提取 | receiver/pipeline.py | L223-330 | _regex_extract：证券/银行/基金三种格式 |
| 合并逻辑 | receiver/pipeline.py | L167-176 | _merge_holdings：按名称去重 |
| 汇总提取 | receiver/pipeline.py | L405-445 | _extract_summary：可用资金/证券市值 |
| 数据库写入 | receiver/pipeline.py | L639-709 | _write_holdings_tx：去重+插入 |
| AI网关 | core/ai_gateway.py | L522-580 | _call_openai：response_format+reasoning_content |
| 前端显示 | pwa/index.html | ve4_loadStats/ve4_loadAccounts | dashboard 展示 |