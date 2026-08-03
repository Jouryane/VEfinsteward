# VE5 业务逻辑文档

> 以场景导入方式，描述当前已实现的所有业务逻辑。
> 修订日期：2026-07-11（v1 — VE5 桌面应用改编版）

---

## VE5 架构差异说明

> 本章节描述 VE5 相对于 VE4 的关键架构变化，帮助理解后续业务逻辑中路径、认证、部署方式的差异。

### 应用形态

| 维度 | VE4 | VE5 |
|------|-----|-----|
| **应用形态** | 浏览器 + 本地 HTTP 服务（PWA） | 桌面应用（pywebview + FastAPI + PyInstaller） |
| **服务端口** | 固定 `localhost:8000`，无认证 | 随机空闲端口 + session token 保护 |
| **启动方式** | 手动启动 `uvicorn` 或蓝牙 watcher 自动拉起 | 用户双击 `VE5.exe`（或 `python ve5_desktop_launcher.py`） |
| **前端载体** | 系统浏览器访问 `localhost:8000` | pywebview 原生桌面窗口（1280x820），回退到默认浏览器 |

### 数据目录策略

VE5 通过 `app_paths` 模块（`app_paths.py`）统一管理所有路径，实现代码与隐私数据的物理隔离：

| 路径 | 开发模式 | 打包模式（PyInstaller onedir） |
|------|---------|-------------------------------|
| **DATA_DIR** | `VE5/userdata/` | `%APPDATA%/VE5/`（可通过 `VE5_DATA_DIR` 环境变量覆盖） |
| **DB_PATH** | `DATA_DIR/ve5.db` | `DATA_DIR/ve5.db` |
| **PWA 前端** | `VE5/pwa/` | `dist/VE5/_internal/pwa/` |
| **配置文件** | `VE5/config/` | `dist/VE5/_internal/config/` |
| **战术模块** | `VE5/tactical/` | `dist/VE5/_internal/tactical/` |

**核心原则**：
- **代码目录**（`CODE_DIR`）：包含 `pwa/`、`config/`、`tactical/`、`receiver/`、`core/`、`api/` 等模块
- **数据目录**（`DATA_DIR`）：包含 `ve5.db`、`incoming/`、`processed/`、`financial_rag/`、`rag_chroma/`、`logs/`、`snapshots/`
- 代码与隐私数据物理隔离：打包后代码在 `_internal/` 临时解压目录，用户数据在 `%APPDATA%/VE5/`

### 安全机制

- **Session Token**：启动时生成 `secrets.token_urlsafe(32)`，通过 URL 参数 `?ve5_token={token}` 传递给前端，所有 API 请求需携带此 token
- **随机端口**：每次启动绑定 `127.0.0.0:0` 获取空闲端口，避免端口冲突和外部访问

### 同步触发方式

- **VE4**：依赖蓝牙 watcher（`watcher.py`）检测手机靠近后自动触发 `sync/run`
- **VE5**：不依赖蓝牙 watcher 自动启动，用户通过前端「同步更新」按钮主动触发 `POST /api/v1/sync/run`；截图文件放入 `DATA_DIR/incoming/images/` 目录

### 战术模块隐私数据

战术模块的数据源配置（`tactical/config/data_sources.yaml`）中的 token 等隐私信息，在 VE5 中存储在 `DATA_DIR`（userdata）而非代码目录，避免打包后敏感信息随代码分发。

---

## 场景一：用户导入截图

**入口**：用户将截图文件放入 `userdata/incoming/images/` 目录（通过 `app_paths.INCOMING_DIR` 定位），然后在前端面板点击「同步更新」按钮。

**触发 API**：`POST /api/v1/sync/run`

> **TODO（P1）**：计划增加 `POST /api/v1/sync/run?force=true` 强制重新处理模式。force 模式不先删旧数据，正常跑 pipeline（DELETE + INSERT 在同一事务内），写入成功后更新 `processed_files.processed_at`。中途报错自动 ROLLBACK，旧数据不丢失。

**执行模块**：`receiver/pipeline.py` → `ve5_pipeline_run()`

---

### Step 1 — 文件扫描与去重

pipeline 扫描 `INCOMING_DIR/images/` 目录（即 `userdata/incoming/images/`）中所有 `.png/.jpg/.jpeg/.bmp/.tiff/.webp` 文件。

**批次管理**：`ve5_pipeline_run()` 入口生成 `batch_id = datetime.now().isoformat()`，贯穿本次同步的所有文件处理。

**对每个文件**：

- 计算文件 MD5 哈希
- 查询 `processed_files` 表：是否存在 24 小时内相同哈希的记录
- **IF 已存在（且非 force 模式）** → 跳过（避免重复处理）
- **ELSE** → 进入处理流程

### Step 2 — OCR 文字识别

**执行模块**：`receiver/ocr_engine.py` → `ve5_ocr()`

对图片执行：
1. 灰度转换
2. CLAHE 对比度增强
3. 放大 2 倍（`INTER_CUBIC`）
4. 二值化（`THRESH_BINARY + OTSU`）
5. Tesseract OCR 识别（中文 `chi_sim` + 英文 `eng`）

**输出**：`{"raw_text": str, "engine": str}`

**硬短路**：**IF `raw_text` 长度 < 5 个字符 → 直接跳到 Step 6（归档），返回 `status: "skipped_blank"`，不触发 AI 调用和正则匹配。** 避免用户误传纯风景照/空白图时浪费 AI 探测超时（3s）和计算资源。

---

### Step 3 — 结构化数据提取（双轨依次执行）

OCR 文本会**依次尝试**持仓提取和消费提取两条路径（非并行，顺序调用）。

#### 路径 A：持仓提取

**执行模块**：`receiver/llm_extractor.py` → `ve5_llm_extract_holdings()`

- **优先 LLM**：通过 `ai_gateway` 调用 AI，prompt 要求提取 `[name, value, type, account]`
  - **IF AI 可用且返回有效 JSON** → 标准化为 `[{name, value, type, account}]`
  - **ELSE** → 进入正则兜底
- **正则兜底**：`pipeline._regex_extract()` 匹配 `产品名 + 金额` 模式
  - 正则：`(产品名)\s*[\s\S]{0,8}?([\d,]+\.\d{2})`
  - 过滤条件：金额 > 0（持仓金额不应为负）、名称长度 ≥ 2、排除 UI 词汇（"总资产"、"可用" 等）

#### 路径 B：消费记录提取

**执行模块**：`receiver/llm_extractor.py` → `ve5_llm_extract_expenses()`

- **优先 LLM**：通过 `ai_gateway` 调用 AI，prompt 要求提取 `[date, amount, counterparty, category, description]`
  - **IF AI 可用且返回有效 JSON** → 标准化为消费记录列表
  - **ELSE** → 进入正则兜底
- **正则兜底**：`pipeline._regex_extract_expenses()`
  - **先去噪**（按优先级依次执行）：
    1. 去除中文间空格：`购 物` → `购物`（`(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])`）
    2. 去除中文与英文/数字间空格：`支付宝 ¥285` → `支付宝¥285`
    3. **去除数字间空格**：`1 000.00` → `1000.00`（`(?<=\d)\s+(?=\d)`）
    4. 去除符号残留：`©}` `@` `oO` 等
  - **关键词门控（两级）**：
    - 去噪后文本中匹配到**强信号词**（"支出构成"、"交易明细"、"账单"、"收支"）≥ 1 个 → 放行
    - **ELSE IF** 匹配到**普通消费关键词**（"购物"、"餐饮"、"交通"、"服务"、"支付宝" 等 28 个）≥ 2 个 → 放行
    - **ELSE** → 返回空（不做消费提取，避免将持仓截图误判为消费截图）
  - **逐行匹配**：`[¥]?[\d,]+\.\d{2}` 提取金额，含 `%` 的行跳过（百分比不是金额）
  - **商户名提取**：金额前的文本，去除符号和日期/支付平台前缀
  - **自动分类**：根据商户名关键词映射（"美团"→餐饮、"滴滴"→交通、"淘宝"→购物 等）
  - **日期年份推断**：正则提取"X月X日"时，**IF 提取月份 > 当前月份 → 年份自动减 1**（处理跨年截图，如 12 月截图在 1 月导入）

---

### Step 4 — 数据库写入（事务保护）

**事务机制**：`_write_holdings` 和 `_write_expenses` 放在同一个 SQLite 事务中（`BEGIN TRANSACTION` → DELETE + INSERT → `COMMIT`），中途报错自动 `ROLLBACK`，旧数据不丢失。

**数据库文件**：`DATA_DIR/ve5.db`（通过 `app_paths.DB_PATH` 定位）

#### 路径 A 产物 → `asset_holdings` 表

**执行函数**：`pipeline._write_holdings()`

| 字段 | 值来源 |
|------|--------|
| `account_key` | LLM 提取的 account / 正则推断 |
| `source_file` | 文件名 |
| `product_name` | LLM 提取的 name / 正则匹配的产品名 |
| `product_code` | LLM 提取的产品代码 / 正则匹配（如有） |
| `asset_class` | LLM 提取的 type（equity/fixed_income/cash_equivalent/commodity/unknown）/ 正则默认 unknown |
| `current_value` | LLM 提取的 value / 正则匹配的金额 |
| `purchase_date` | LLM 提取的买入日期（如有） |
| `holding_return_pct` | 持仓收益率（如有，否则 NULL） |
| `unrealized_pnl` | 浮动盈亏（如有，否则 NULL） |
| `annualized_return_pct` | 年化收益率（如有，否则 NULL） |
| `is_classified` | 1（标记已分类） |
| `inference_source` | `snapshot:{filename}` |
| `batch_id` | 本次同步的批次标识 |

**写入策略**（防多文件数据重叠）：

1. **DELETE 条件**：先删除当前 `batch_id` 下所有旧记录（同批次全量替换）
2. **跨文件去重**：INSERT 前，对同 `account_key` + 同 `product_name` 的记录做 UPSERT（`INSERT OR REPLACE`），确保同一产品不因总览截图和明细截图同时存在而重复计算
3. **同名不同金额可疑项处理**：若两条记录的 `product_name` 完全相同但金额不同，进入多要素比对：
   - **比对维度**：`account_key`（账户名）、`product_name` 产品名细节（如"XX基金A"vs"XX基金A（持）"）、`source_file`（来源文件名推断截图类型——总览/明细）、`asset_class`（分类）
   - **判定规则**：
     - 比对结果确认是同一产品的不同截图 → 保留金额较大者（通常是总览数据，包含更多资产）
     - 比对结果发现产品名相似但实质不同（如"招商银行理财A" vs "招商银行理财B"、或同金额但不同渠道"支付宝余额" vs "余额宝"） → **视为不同产品，均保留**
   - **实现方式**：先做模糊匹配（名称相似度 > 80%），再结合 account_key 一致性确认是否同一产品

> **风险说明**：用户一次导入同一账户的总览截图 + 明细截图时，可能出现"总资产 50 万"和"基金 A 20 万 + 基金 B 30 万"同时存在。上述去重策略确保同名产品只保留一条记录。对于总览截图中的"总资产"摘要（如 product_name="总资产"），正则提取已通过 UI 词汇过滤排除。多要素比对避免将不同产品误判为重复（如同名不同渠道的理财产品）。

#### 路径 B 产物 → `transactions` 表

**执行函数**：`pipeline._write_expenses()`

| 字段 | 值来源 |
|------|--------|
| `transaction_date` | LLM 提取的 date / 正则匹配的"X月X日"（含年份推断） |
| `transaction_type` | 固定 `expense` |
| `amount` | LLM 提取的 amount / 正则匹配的金额 |
| `counterparty` | LLM 提取的 counterparty / 正则提取的商户名 |
| `category_primary` | LLM 提取的 category / 正则自动分类 |
| `category_secondary` | 固定 `生活消费`（生活消费型流动类资金） |
| `description` | LLM 提取的 description / 正则的原始行文本 |
| `source_file` | 文件名 |
| `batch_id` | 本次同步的批次标识 |

写入前同样 **DELETE 当前 batch_id 下的旧记录**。

> **TODO（下一阶段）**：补充收入提取路径（`transaction_type='income'`），支持工资条、转账记录等收入类截图的 OCR + LLM/正则提取。当前 pipeline 仅提取 expense 类型。

---

### Step 5 — 财务隐私 RAG 异步存储

**执行模块**：`receiver/financial_rag.py` → `ve5_financial_rag_store()`

- 将 OCR 原始文本存入 ChromaDB（`DATA_DIR/financial_rag/`，集合名 `ocr_records`）
- 嵌入模型：`bge-small-zh-v1.5`（本地离线）
- **系统定位**：用户的财务隐私数据仓库，存储持仓截图、消费流水等 OCR 原文，旨在供 AI 提取用户的投资偏好、生活消费习惯、资产配置风格等信息
- **AI 访问权限控制**：
  - **本地 LLM**（`local_alpha`）：可自由读取和分析此 RAG 数据（数据不出本地）
  - **云端 LLM**（`user_configured` / `cloud_beta`）：**仅在用户主动勾选「允许云端 AI 分析」授权后**方可访问此 RAG 数据；未授权时，云端 LLM 无法进入此信息的提取流程
  - 授权控制在前端持仓影响评估等入口处实现（`privacy_mode` 开关），后端据此决定是否将隐私 RAG 检索结果注入 prompt
- 此步骤独立于主流程，**失败不阻塞同步流程**
- 失败时记录 `logger.warning` 日志（含文件名和错误原因），`processed_files.rag_synced` 标记为 `0`
- 成功时 `rag_synced` 标记为 `1`
- **暂无自动重试机制**（P2 规划：指数退避重试）

### Step 6 — 文件归档

- 将文件从 `INCOMING_DIR/images/` 移动到 `PROCESSED_DIR/YYYYMMDD/` 目录（通过 `app_paths` 定位）
- 在 `processed_files` 表记录 `{file_hash, file_path, processed_at, batch_id, rag_synced}`

### Step 7 — 返回结果

**API 返回**：
```json
{
  "total_files": 3,
  "processed": 3,
  "skipped": 0,
  "results": [
    {"file": "xx.jpg", "holdings": 5, "expenses": 3, "status": "ok"},
    {"file": "yy.png", "holdings": 0, "expenses": 0, "status": "skipped_blank"},
    {"file": "zz.jpg", "holdings": 2, "expenses": 0, "status": "ok"}
  ]
}
```

> **status 枚举**：`ok`（正常提取）、`skipped_blank`（OCR 空文本跳过）、`ocr_empty`（OCR 识别失败）

---

## 场景二：用户查看 Dashboard（首页）

**入口**：前端面板 `index.html`，页面加载时自动调用。

**触发 API**：
- `GET /api/v1/dashboard/stats` — KPI 数据
- `GET /api/v1/accounts` — 账户列表
- `GET /api/v1/allocation/detail` — 资产配置详情
- `GET /api/v1/allocation/profile` — 投资画像
- `GET /api/v1/activities` — 最近活动

**降级策略**：API 不可用时，前端尝试读取 `DATA_DIR/snapshots/*.json` 本地快照。

---

### 2.1 KPI 卡片数据来源

**API 函数**：`ve5_api_compute_stats()`

| KPI | 数据来源 | 计算逻辑 | 状态 |
|-----|---------|---------|------|
| **总资产** | `asset_holdings` 表 | `SUM(current_value)` | 已实现 |
| **证券市值** | `asset_holdings` 表 | `SUM(current_value) WHERE asset_class='equity'` | 已实现 |
| **本月收入** | `transactions` 表 | `SUM(amount) WHERE transaction_type='income' AND 当月` | **前端置灰，标注"待开发"** |
| **本月支出** | `transactions` 表 | `SUM(amount) WHERE transaction_type='expense' AND 当月` | 已实现 |

> **说明**：当前 pipeline 仅提取 expense 记录，income 提取路径尚未实现。前端"本月收入"卡片置灰显示，避免用户看到永远为 0 的数据产生困惑。待收入提取功能完成后解除置灰。

---

### 2.2 推荐配置对比模块

**API 函数**：`ve5_api_compute_allocation_detail()`

**数据来源**：`asset_holdings` 表中所有记录的 `asset_class` 和 `current_value`

**分类映射**（`allocation_engine.py`）：

| 四级分类 | 包含的 asset_class | 映射规则 |
|---------|-------------------|---------|
| **流动类** | `cash_equivalent` | 银行活期、零钱通、余额宝等现金等价物 |
| **稳健类** | `fixed_income` | 债券基金、定期理财、固收+等 |
| **进取类** | `equity` | 股票基金、混合基金、股票等权益类 |
| **其他** | `commodity`, `unknown` | 黄金、商品及其他未分类 |

**前端展示逻辑**（推荐配置对比卡片）：

1. **流动类合理性判断（双维度）**

   **维度一：流动性需求底线**
   - 从 `dashboard.stats` 获取 `monthly_expense`（本月支出）
   - 日常备用金需求 = `monthly_expense × 3`
   - **IF 流动类金额 >= 日常备用金** → 显示"充足"（绿色）
   - **ELSE IF 流动类金额 < 日常备用金** → 显示"不足"（红色）
   - **ELSE（本月支出为 0）** → 显示"待评估"（灰色，提示导入消费记录）

   **维度二：画像目标对比**
   - 从 `allocation/profile` 获取 `liquid_pct`（流动类目标比例）
   - **IF 已设置画像** → 计算当前流动类占比 vs 目标占比
     - 差值 < 5% → "匹配"（绿色）
     - 当前 > 目标 → "偏高"（琥珀色）
     - 当前 < 目标 → "偏低"（琥珀色）
   - **IF 未设置画像** → 不显示画像对比行

   > **设计说明**：流动类同时展示两个维度——"充足/不足"反映绝对安全性（是否覆盖 3 个月支出），"匹配/偏高/偏低"反映用户配置意愿是否达成。两者独立判断，不互相覆盖。

2. **进取类与画像对比**
   - 从 `allocation/profile` 获取用户设置的 `aggressive_pct`（进取类目标比例）
   - **IF 未设置画像** → 显示"去设置"链接
   - **ELSE** → 计算当前进取类占比 vs 目标占比
     - 差值 < 5% → "匹配"（绿色）
     - 当前 > 目标 → "偏高"（琥珀色）
     - 当前 < 目标 → "偏低"（琥珀色）
     - 同时显示目标金额 = 投资总额 × 目标比例

3. **稳健类与画像对比** — 同进取类逻辑，使用 `stable_pct`

---

### 2.3 账户明细模块

**API 函数**：`ve5_api_compute_accounts()`

**数据来源**：
- **唯一来源**：`asset_holdings` 表按 `account_key` 分组 → `{账户名, SUM(current_value)}`
- **IF `asset_holdings` 为空** → 返回空账户列表，前端显示"暂无持仓数据"

> **设计说明**：账户明细仅展示持仓维度（按 `account_key` 汇总），不回退到 `transactions` 表。原因：交易流水按 `source_file`（截图文件名）分组与真实账户名语义不一致，混用会导致账户列表混乱。消费记录查询将在独立的"交易流水"模块中实现（待开发）。

**前端展示**：
- 左侧栏「银行账户/钱包」区块
- 每个账户一行：`{name, balance, type, last_sync}`
- 点击展开显示该账户下的持仓明细

---

### 2.4 最近活动模块

**API 函数**：`ve5_api_compute_activities()`

**数据来源**：
1. 从 `processed_files` 表取最近 N 条文件处理记录
2. 关联 `asset_holdings` 表，按 `source_file` 统计持仓数量
3. 每条活动包含：`{type, title, subtitle, timestamp}`

**前端展示**：Dashboard 右侧「最近活动」卡片列表。

---

## 场景三：用户查看资产配置页面

**入口**：前端面板左侧导航「资产配置」或「推荐配置」。

**页面文件**：`pwa/allocation.html`（通过 `app_paths.PWA_DIR` 定位）

**触发 API**：`GET /api/v1/allocation/detail`

---

### 3.1 四级分类报告

**API 函数**：`ve5_api_compute_allocation_detail()`

**执行模块**：`allocation/allocation_engine.py`

**逻辑**：
1. 从 `asset_holdings` 表读取所有持仓记录
2. 按 `product_name` 逐个匹配分类规则：
   - 先匹配**产品名称关键词**（如含"债"→稳健类，含"股"/"混合"→进取类）
   - 再按 `asset_class` 默认映射（`equity`→进取类，`fixed_income`→稳健类，`cash_equivalent`→流动类）
   - 都不匹配 → 其他
3. 按四级分类汇总：`{分类, 总金额, 占比, 包含的产品列表}`
4. 返回 JSON 报告

**前端展示**：
- 饼图/条形图展示各分类占比
- 每个分类下列出具体产品及金额

---

## 场景四：用户设置投资画像

**入口**：前端面板 `allocation-profile.html`

**触发 API**：
- `GET /api/v1/allocation/profile` — 读取画像
- `POST /api/v1/allocation/profile` — 保存画像

---

### 4.1 画像数据结构

存储位置：SQLite `ai_settings` 表，`setting_key='allocation_profile'`

```json
{
  "aggressive_pct": 60,
  "stable_pct": 25,
  "liquid_pct": 15,
  "risk_tolerance": "medium",
  "investment_horizon": "3-5年",
  "updated_at": "2026-07-11T12:00:00"
}
```

**约束**：`aggressive_pct + stable_pct + liquid_pct = 100`

### 4.2 画像用途

画像保存后，在 Dashboard 的「推荐配置对比」模块中使用（参见 2.2）：

| 画像字段 | 用途 | 说明 |
|---------|------|------|
| `aggressive_pct` | 进取类目标占比 | 与当前进取类占比对比 → 偏高/偏低/匹配 |
| `stable_pct` | 稳健类目标占比 | 与当前稳健类占比对比 → 偏高/偏低/匹配 |
| `liquid_pct` | 流动类目标占比 | 与当前流动类占比对比（维度二），独立于"支出×3"底线评估 |

> **设计说明**：三个字段均有实际用途。流动类评估采用双维度设计——底线评估（支出×3）反映安全性，画像对比（liquid_pct）反映配置意愿。详见 2.2 节。

---

## 场景五：用户配置 AI

**入口**：前端面板右上角设置按钮 → AI 设置弹窗。

**触发 API**：
- `GET /api/v1/ai-settings` — 读取配置
- `POST /api/v1/ai-settings` — 保存配置

---

### 5.1 配置存储

**存储位置**：SQLite `ai_settings` 表（统一数据库 `DATA_DIR/ve5.db`，通过 `app_paths.DB_PATH` 定位）

> **数据库统一约束**：所有模块（API server、pipeline、ai_gateway）必须使用同一数据库文件 `DATA_DIR/ve5.db`。路径通过 `app_paths.DB_PATH` 统一定义，确保指向同一物理文件。禁止创建额外的 `ve5.db` 文件。

**可配置项**：

| setting_key | 值 | 用途 |
|-------------|---|------|
| `llm_provider` | `openai` / `ollama` / `deepseek` / `claude` 等 | AI 服务商 |
| `llm_api_key` | 用户填写的 API Key | 认证 |
| `llm_base_url` | API 端点 URL | 如 `https://api.openai.com/v1` |
| `llm_model` | 模型名称 | 如 `gpt-4o-mini` |

### 5.2 配置加载流程

**执行模块**：`core/ai_gateway.py` → `AIGateway.load()`

1. `ai_gateway` 初始化时，注册内置 provider：
   - `local_alpha`：Ollama 本地模型（`localhost:11434`，模型 `qwen2:1.5b`），`priority=10`
   - `demo_echo`：调试用回声 provider，`priority=999`
2. 从 `ai_settings` 表读取用户配置
3. **IF 用户已配置 API** → 创建 `user_configured` provider（`priority=1`，最高优先级）
4. **IF 用户未配置** → 只有 `local_alpha`（Ollama 可用时）或 `demo_echo`

### 5.3 AI 可用性判断与探测

> **TODO（P1）**：当前可用性判断仅通过 catch 异常实现，缺乏主动探测和重试。计划改进为：

1. **配置保存时异步探测**：`POST /api/v1/ai-settings` 立即返回成功，后台异步发一个带 3 秒超时的探测请求，结果缓存到内存（不阻塞保存响应）
2. **首次调用时探测**：若配置保存时异步探测尚未完成，首次 `ve5_ai_call()` 同步等待探测结果
   - 成功 → 缓存 `available=True`
   - 失败 → 标记 `available=False`，自动回退到下一优先级 provider
3. **自动重试**：标记不可用后，后续请求间隔 30 秒自动重试一次探测
4. **持久化状态**：可用状态缓存在内存中（进程重启后重置），不写入数据库
5. **效果**：用户保存有效 API Key 后，后台完成探测；用户点击同步时直接读缓存（0 延迟），仅首次需要等待探测

### 5.4 AI 调用路由

**执行函数**：`ai_gateway.ve5_ai_call()`

```
ve5_ai_call(task_type, system, prompt, ...)
    ↓
查找 provider（按 priority 排序）：
    1. user_configured (priority=0) ← 用户配置的API（最高优先级，无条件优先）
       ├─ use_for=[] 表示支持所有任务类型
       └─ 不受 contains_privacy_data 路由限制（用户主动配置即授权）
    2. local_alpha (priority=1) ← Ollama 本地模型
       └─ 仅支持 use_for 中注册的任务类型
    3. cloud_beta (priority=2) ← 云端API（需环境变量）
    4. demo_echo (priority=999) ← 调试用（返回空，不阻塞）
    ↓
返回 VE5AiResult {success, text, provider, duration_ms}
    ↓
IF success=False → pipeline 自动回退到正则提取
```

---

## 场景六：用户在同步过程中使用 AI 提取

**入口**：用户点击「同步更新」后，pipeline 自动调用 AI（用户无需手动操作）。

**AI 被调用的位置**：

| 调用点 | 函数 | 任务类型 | prompt 内容 |
|--------|------|---------|------------|
| 持仓提取 | `ve5_llm_extract_holdings()` | `data_extraction` | "从OCR文本提取持仓信息，返回JSON" |
| 消费提取 | `ve5_llm_extract_expenses()` | `data_extraction` | "从OCR文本提取消费记录，返回JSON" |

**AI 调用参数**：
- `task_type`: `data_extraction`
- `system`: "你是财务数据提取助手。只返回JSON，不要解释。"
- `format_type`: `json`
- `contains_privacy_data`: `true`（标记含隐私数据）
- `temperature`: `0.0`（确保输出稳定）
- `max_tokens`: 500（持仓）/ 800（消费）

**路由逻辑**（依次尝试，非并行）：
1. **IF `user_configured` 内存缓存 available=True** → 使用用户配置的云端 API（0 延迟判断）
2. **ELSE IF Ollama 在线** → 使用本地 `qwen2:1.5b`
3. **ELSE** → 返回 None → pipeline 自动回退到正则提取（持仓用 `_regex_extract`，消费用 `_regex_extract_expenses`）

> **性能保障**：由于 AI 可用性判断走内存缓存，pipeline 不会因不可用的 API 而阻塞。仅首次调用可能需要 3 秒探测超时。

---

## 场景七：用户使用战术沙盘（部分实现）

> **状态标注**：研报上传与解析 ✅ 已实现 | RAG 存储 ✅ 已实现 | 影响评估 ❌ 待开发 | 策略执行 ❌ 待开发
>
> 研报场景是下一阶段开发重点，影响评估和策略执行将在此后实现。

**入口**：前端面板 `tactical-sandbox.html`（通过 `app_paths.PWA_DIR` 定位）

**触发 API**：
- `POST /api/v1/tactical/fundamental/parse-url` — URL 解析研报
- `POST /api/v1/tactical/fundamental/parse-text` — 粘贴文本解析
- `GET /api/v1/tactical/fundamental/knowledge` — 研报知识库列表
- `POST /api/v1/tactical/fundamental/search` — 语义检索
- `GET /api/v1/tactical/fundamental/knowledge/{id}/detail` — 研报详情
- `POST /api/v1/tactical/fundamental/holdings-impact` — 持仓影响评估
- `POST /api/v1/tactical/fundamental/chat` — AI 基本面对话

---

### 7.1 研报上传与解析 ✅

**上传 API**：`POST /api/v1/tactical/research/upload`

**流程**：
1. 用户上传 PDF/图片格式的研报文件
2. 后端保存到 `TACTICAL_DIR/fundamental/uploads/`（通过 `app_paths.TACTICAL_DIR` 定位）
3. 调用 OCR（Tesseract）提取文本
4. 通过 `ai_gateway` 调用 LLM 解析研报结构（标题、摘要、行业观点、评级）

### 7.2 研报 RAG 存储 ✅

**执行模块**：`tactical/fundamental/agents/report_agent.py` → `ve5_kb_upsert_report()`

- 解析后的研报内容存入独立 ChromaDB（`DATA_DIR/rag_chroma/`，集合名 `report_knowledge`）
- 嵌入模型：`bge-small-zh-v1.5`（本地离线）
- **系统定位**：公开市场信息仓库，存储研报/新闻的结构化分析结果（摘要、关键观点、投资逻辑、风险提示），旨在帮助用户提取研究报告的思想思路，辅助 LLM 分析有研报或分析支持的投资思路（**非直接投资建议**）
- **数据性质**：公开文档，**不含用户隐私数据**
- **AI 访问权限**：云端 LLM 和本地 LLM 均可自由访问（公开数据，无隐私风险）
- **与财务隐私 RAG 的关系**：

  | 维度 | 财务隐私 RAG | 研报 RAG |
  |------|-------------|---------|
  | 存储路径 | `DATA_DIR/financial_rag/` | `DATA_DIR/rag_chroma/` |
  | 集合名 | `ocr_records` | `report_knowledge` |
  | 数据内容 | 用户 OCR 原文（持仓/消费截图） | 研报结构化分析（摘要/观点/逻辑/风险） |
  | 数据性质 | **隐私数据** | **公开数据** |
  | 云端 LLM 访问 | 需用户主动授权 | 可自由访问 |
  | 用途 | 提取用户投资偏好/消费习惯 | 辅助分析有研报支持的投资思路 |

### 7.3 研报影响评估 ❌（待开发）

**核心思路**：将研报 RAG（公开市场信息）与用户持仓数据（隐私数据）结合，由 LLM 生成持仓影响分析。

**两套 RAG 的协同方式**：

```
研报影响评估触发
  │
  ├─ 1. 研报 RAG 检索（公开数据）
  │     └─ 从 report_knowledge 中查询与目标持仓相关的行业观点、投资逻辑
  │     └─ AI 访问权限：云端/本地 LLM 均可（公开数据）
  │
  ├─ 2. 财务隐私 RAG 检索（隐私数据）— 仅在用户授权时执行
  │     └─ 从 ocr_records 中查询用户历史持仓/消费模式
  │     └─ AI 访问权限：本地 LLM 自由访问；云端 LLM 需用户主动勾选授权
  │
  ├─ 3. LLM 综合分析
  │     ├─ 输入：研报观点 + 用户持仓列表 + （授权时）用户历史偏好
  │     ├─ 含隐私数据 → task_type: tactical_analysis → 路由至 local_alpha
  │     │   或 → 用户授权 + user_configured 云端 LLM
  │     └─ 输出：逐持仓影响评估（加仓/减仓/持有 + 理由）
  │
  └─ 4. 前端展示
        └─ 隐私开关：用户可随时关闭云端 LLM 对隐私 RAG 的访问权限
```

**隐私控制要点**：
- 研报观点分析（不含持仓）→ 云端 LLM 可直接执行
- 持仓影响评估（含用户持仓数据）→ 默认仅本地 LLM；用户勾选授权后云端 LLM 也可执行
- 前端 `privacy_mode` 开关控制此权限，状态在每次请求时传递给后端

### 7.4 量化策略执行 ❌（待开发）

**规划逻辑**：
1. CodeSandbox 沙盒中运行 Python 策略代码
2. 历史回测 + 实时信号生成
3. 策略结果推送到资产配置面板

---

## 附录 A：数据流全景图

```
用户截图
  │
  ├─[POST /sync/run]→ pipeline.ve5_pipeline_run(batch_id=...)
  │    │
  │    ├─ Step1: 扫描 INCOMING_DIR/images/ → MD5 去重
  │    │
  │    ├─ Step2: ocr_engine.ve5_ocr() → 原始文本
  │    │    └─ [raw_text < 5] → 跳到 Step6（硬短路）
  │    │
  │    ├─ Step3A: llm_extractor.ve5_llm_extract_holdings()  ← 依次执行
  │    │    ├─ [AI缓存可用] → ai_gateway → user_configured/ollama → JSON
  │    │    └─ [AI不可用] → pipeline._regex_extract() → 兜底
  │    │
  │    ├─ Step3B: llm_extractor.ve5_llm_extract_expenses()  ← 依次执行
  │    │    ├─ [AI缓存可用] → ai_gateway → user_configured/ollama → JSON
  │    │    └─ [AI不可用] → pipeline._regex_extract_expenses() → 兜底
  │    │
  │    ├─ Step4:  BEGIN TRANSACTION
  │    │    ├─ DELETE WHERE batch_id = ?（同批次全量替换）
  │    │    ├─ INSERT ... ON (account_key, product_name) UPSERT
  │    │    │    └─ 同名不同金额 → 多要素比对（名称相似度+account_key）决定合并或保留
  │    │    └─ COMMIT
  │    │
  │    ├─ Step5:  financial_rag.ve5_financial_rag_store() → ChromaDB（异步，失败记日志）
  │    │    └─ 存储路径: DATA_DIR/financial_rag/（隐私数据，云端LLM需用户授权）
  │    └─ Step6:  文件归档到 PROCESSED_DIR/YYYYMMDD/
  │
  └─ Dashboard 自动刷新
       │
       ├─ GET /dashboard/stats
       │    ├─ asset_holdings.SUM(current_value) → 总资产/证券市值
       │    ├─ transactions.SUM(amount) WHERE type=expense → 本月支出
       │    └─ transactions.SUM(amount) WHERE type=income → 本月收入（置灰：待开发）
       │
       ├─ GET /accounts → 账户列表
       │    └─ asset_holdings GROUP BY account_key（不回退到 transactions）
       │
       ├─ GET /allocation/detail → 四级分类报告
       │    ├─ asset_class → 四级映射（流动/稳健/进取/其他）
       │    └─ 计算各分类占比
       │
       ├─ GET /allocation/profile → 投资画像
       │    └─ ai_settings WHERE key='allocation_profile'
       │
       └─ GET /activities → 最近活动
            └─ processed_files + asset_holdings 统计
```

---

## 附录 B：数据库表结构

> 数据库文件：`DATA_DIR/ve5.db`（通过 `app_paths.DB_PATH` 统一定位）

### asset_holdings（持仓明细）

| 列 | 类型 | 说明 |
|----|------|------|
| id | INTEGER | 主键 |
| account_key | TEXT | 账户标识（基金账户/证券账户/银行账户） |
| source_file | TEXT | 来源截图文件名 |
| product_name | TEXT | 产品名称 |
| product_code | TEXT | 产品代码（基金代码/股票代码，如有） |
| asset_class | TEXT | 资产分类（equity/fixed_income/cash_equivalent/commodity/unknown） |
| current_value | REAL | 当前市值/金额 |
| purchase_date | TEXT | 买入日期（如有） |
| holding_return_pct | REAL | 持仓收益率（如有，否则 NULL） |
| unrealized_pnl | REAL | 浮动盈亏（如有，否则 NULL） |
| annualized_return_pct | REAL | 年化收益率（如有，否则 NULL） |
| is_classified | BOOLEAN | 是否已分类 |
| inference_source | TEXT | 推断来源 |
| batch_id | TEXT | 同步批次标识 |
| created_at | TEXT | 创建时间 |

> **去重约束**：`(account_key, product_name)` 联合唯一索引，实现跨文件 UPSERT。

### transactions（交易/消费记录）

| 列 | 类型 | 说明 |
|----|------|------|
| id | INTEGER | 主键 |
| transaction_date | TEXT | 交易日期（含年份推断） |
| transaction_type | TEXT | 类型（expense / income·待开发） |
| amount | REAL | 金额 |
| counterparty | TEXT | 商户/对方 |
| category_primary | TEXT | 一级分类（餐饮/交通/购物/服务等） |
| category_secondary | TEXT | 二级分类（生活消费） |
| description | TEXT | 备注 |
| source_file | TEXT | 来源截图文件名 |
| batch_id | TEXT | 同步批次标识 |

### ai_settings（AI 配置 — 两处实现略有差异）

**API Server 使用的表结构**（`api/ve5_api_server.py`）：

| 列 | 类型 | 说明 |
|----|------|------|
| id | INTEGER | 主键（单行，CHECK id=1） |
| provider | TEXT | AI 服务商 |
| api_key | TEXT | API Key |
| api_base | TEXT | API 端点 URL |
| model | TEXT | 模型名称 |
| updated_at | TEXT | 更新时间 |

> **注意**：`ai_gateway.py` 读取时使用 `setting_key / setting_value` 键值对模式，但实际建表用的是扁平列结构。两者通过适配层桥接（ai_gateway 内部直接读取 `provider, api_key, api_base, model` 列）。

### processed_files（已处理文件记录）

| 列 | 类型 | 说明 |
|----|------|------|
| id | INTEGER | 主键 |
| file_hash | TEXT | 文件 MD5 哈希 |
| file_path | TEXT | 原始路径 |
| processed_at | TEXT | 处理时间 |
| batch_id | TEXT | 同步批次标识 |
| rag_synced | BOOLEAN | RAG 是否同步成功，默认 0 |

---

## 附录 C：下一阶段开发路线

按优先级排列：

| 优先级 | 功能 | 涉及模块 | 状态 |
|--------|------|---------|------|
| ~~P0~~ | **[VE5 适配]** 路径统一（app_paths 模块替换硬编码路径） | `app_paths.py`, 全模块 | ✅ 已完成 |
| ~~P0~~ | **[VE5 适配]** Session Token 认证机制 | `ve5_desktop_launcher.py`, API server | ✅ 已完成 |
| ~~P0~~ | **[VE5 适配]** 随机端口 + pywebview 桌面窗口 | `ve5_desktop_launcher.py` | ✅ 已完成 |
| ~~P0~~ | **[VE5 适配]** PyInstaller 打包配置 | `VE5.pyinstaller.spec` | ✅ 已完成 |
| ~~P0~~ | **[VE5 适配]** 代码与隐私数据物理隔离 | `app_paths.py` | ✅ 已完成 |
| ~~P0~~ | **[VE5 适配]** 战术模块 data_sources.yaml 隐私信息迁移至 userdata | `tactical/config/` | ✅ 已完成 |
| ~~P0~~ | 本月收入卡片前端置灰 | `index.html` | ✅ 已完成 |
| ~~P0~~ | 账户明细去掉 transactions 回退 | `api/ve5_api_server.py` | ✅ 已完成 |
| ~~P0~~ | 消费关键词门控改为两级（强信号1个放行） | `pipeline.py` | ✅ 已完成 |
| ~~P0~~ | 流动类对比引入 `liquid_pct` 双维度展示 | `index.html` | ✅ 已完成 |
| ~~P0~~ | OCR 空文本硬短路（raw_text < 5 跳过） | `pipeline.py` | ✅ 已完成 |
| ~~P0~~ | 去噪增加数字间空格清理 | `pipeline.py` | ✅ 已完成 |
| ~~P0~~ | 跨文件 UPSERT 去重（INSERT OR REPLACE） | `pipeline.py` | ✅ 已完成 |
| ~~P1~~ | 数据库写入事务保护（BEGIN/COMMIT） | `pipeline.py` | ✅ 已完成 |
| ~~P1~~ | transaction_date 年份推断 | `pipeline.py` | ✅ 已完成 |
| ~~P1~~ | **[VE5 适配]** asset_holdings 表新增列（product_code, purchase_date, holding_return_pct, unrealized_pnl, annualized_return_pct） | DB schema, `pipeline.py` | ✅ 已完成 |
| **P0** | 多文件 batch_id 生成 + 贯穿全流程 | `pipeline.py`, API server | 待实现（batch_id 字段已加，但 pipeline 未生成和传递） |
| **P1** | AI 探测前置化（保存时异步探测 + 内存缓存） | `ai_gateway.py`, `api/ve5_api_server.py` | 待实现 |
| **P1** | force=true 强制重新处理（事务保护） | `pipeline.py`, `api/ve5_api_server.py` | 待实现 |
| **P1** | RAG 失败日志 + `rag_synced` 字段 | `financial_rag.py`, DB schema | 待实现 |
| **P1** | 研报 Agent LLM 接入（替换关键词提取） | `report_agent.py`, `holdings_impact_agent.py` | 待实现 |
| **P1** | 证券市值范围修正（仅 equity，去掉 alternative） | `api/ve5_api_server.py` | 待实现 |
| **P2** | 收入提取路径（工资条/转账） | `llm_extractor.py`, `pipeline.py` | AI 可用 |
| **P2** | 研报影响评估（观点→持仓映射 + 两套RAG协同） | `report_agent.py`, `holdings_impact_agent.py` | RAG + LLM |
| **P2** | 交易流水独立模块 | 新页面 + API | transactions 表 |
| **P2** | 策略结果导入 + LLM 分析 + strategy_signals 表 | 新页面 + API | LLM 可用 |
| **P3** | 量化策略信号执行（外部消费者） | 信号表 + 触发机制 | 信号表稳定 |