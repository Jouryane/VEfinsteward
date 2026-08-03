# VE5 Chatbot API Surface — 数据可用性清单

> 本文档定义 VEchatbot LLM 可访问的全部数据点。
> 系统在每次对话时将动态生成「数据可用性清单」注入到 system prompt 中，
> 告知 LLM 哪些数据已预加载、哪些可按需引用、引用格式是什么。

---

## 一、数据分类

### 1. 财务概览（Financial Overview）✅ 已预加载

通过 `context_navigator.build_navigation_context()` 自动注入到所有 skill 的 prompt 中。

| 数据点 | 变量名 | 类型 | 说明 |
|--------|--------|------|------|
| 总资产 | `total_assets` | float | 所有持仓 current_value 之和（已过滤负值和已过期记录） |
| 持仓数 | `holdings_count` | int | 有效持仓条数 |
| 进取类总额 | `total_aggressive` | float | 股票型/混合型/指数型/QDII/R4-R5理财/ETF(非债) |
| 稳健类总额 | `total_stable` | float | 债券型/纯债/短债/R1-R3理财/固收类 |
| 流动类总额 | `total_liquid` | float | 活期/存款/朝朝宝/零钱/活钱/货币基金 |
| 保障类总额 | `total_protection` | float | 保险/另类(alternative)映射至此 |
| 月收入 | `monthly_income` | float | 最近有数据月份的收入（当月无数据自动回退） |
| 月支出 | `monthly_expense` | float | 最近有数据月份的支出 |
| 月结余 | `monthly_savings` | float | max(0, income - expense) |
| 储蓄率 | `savings_rate` | float | 0~1，monthly_savings / monthly_income |
| 数据月份 | `monthly_data_month` | str | YYYY-MM，实际数据来源月份 |
| 年度累计收入 | `ytd_income` | float | 当年所有月份收入之和 |
| 年度累计支出 | `ytd_expense` | float | 当年所有月份支出之和 |
| 年度累计储蓄 | `ytd_savings` | float | max(0, ytd_income - ytd_expense) |
| 年度投资收益 | `ytd_investment_return` | float | 当年投资收益类交易之和 |
| 交易条数 | `transaction_count` | int | 数据月份的交易记录数 |

**引用方式**：以上数据已注入到 prompt 的 `[系统导航上下文]` 中，LLM 可直接引用。

### 2. 目标进度（Goal Progress）✅ 已预加载

| 数据点 | 变量名 | 类型 | 说明 |
|--------|--------|------|------|
| 目标列表 | `goals` | List[Dict] | 从 goals.json 加载 |
| 目标名称 | `goal.name` | str | 如"置业安居"、"今年总资产增加15万" |
| 目标金额 | `goal.target_amount` | float | 目标所需金额 |
| 当前金额 | `goal.current_amount` | float | 积累型=YTD储蓄，金额型=总资产(上限目标额) |
| 进度百分比 | `goal.progress_pct` | float | 0~100 |
| 剩余金额 | `goal.remaining_amount` | float | max(0, target - current) |
| 预计月数 | `goal.months_needed` | float | remaining / monthly_savings |
| 目标状态 | `goal.status` | str | "已达成"/"进行中"/"未开始" |
| 目标类型 | `goal.goal_type` | str | "accumulation"(积累型) / "amount"(金额型) |
| YTD储蓄 | `goal.ytd_savings` | float | 仅积累型目标有此字段 |

**进度计算规则**：
- 积累型目标（名称含"增加"/"积累"/"储蓄"/"攒"/"存"）：`进度 = YTD储蓄 / 目标额 × 100%`
- 金额型目标（如购房）：`进度 = min(总资产, 目标额) / 目标额 × 100%`

### 3. 资产配置（Asset Allocation）✅ 已预加载

| 数据点 | 变量名 | 类型 | 说明 |
|--------|--------|------|------|
| 推荐配比 | `framework` | Dict | 由 allocation_engine 计算，含 liquid/stable/aggressive/protection_pct |
| 实际配比 | `actual` | Dict | 从持仓实时计算 |
| 综合评分 | `overall.total_score` | float | 0~100 |
| 健康状态 | `overall.status` | str | "优秀"/"良好"/"需关注"/"需调整" |
| 三维度评分 | `dimensions` | List[Dict] | 流动性/稳健性/进取性 各维度分数 |
| 系统建议 | `advices` | List[Dict] | 偏差分析和优化建议 |

**引用规则**：推荐配比和维度评分是引擎权威结果，LLM 应引用而非自行推算。

### 4. 持仓明细（Holdings）📋 可按需展开

| 数据点 | 变量名 | 类型 | 说明 |
|--------|--------|------|------|
| 持仓名称 | `holding.name` | str | 如"沪深300ETF" |
| 持仓代码 | `holding.code` | str | 基金/股票代码 |
| 当前市值 | `holding.current_value` | float | 最新市值 |
| 资产分类 | `holding.asset_class` | str | aggressive/stable/liquid/protection |
| 持仓占比 | `holding.holding_ratio` | float | 0~1 |
| 成本基础 | `holding.cost_basis` | float | 买入成本 |
| 持有天数 | `holding.holding_days` | int | 持有天数 |

**加载方式**：通过 `ctx.load_holdings()` 或 `asset_doctor` skill 加载。

### 5. 交易记录（Transactions）📋 可按需展开

| 数据点 | 变量名 | 类型 | 说明 |
|--------|--------|------|------|
| 交易日期 | `txn.date` | str | YYYY-MM-DD |
| 交易类型 | `txn.type` | str | "income"/"expense"/"return_*" |
| 金额 | `txn.amount` | float | 绝对值 |
| 分类 | `txn.category` | str | 一级分类 |
| 描述 | `txn.description` | str | 交易描述 |
| 商户 | `txn.counterparty` | str | 交易对手方 |
| 是否必需 | `txn.is_essential` | bool | 必需消费标记 |

**加载方式**：通过 `ctx.load_transactions(month)` 加载指定月份。

### 6. 用户画像（User Profile）📋 可按需展开

| 数据点 | 变量名 | 类型 | 说明 |
|--------|--------|------|------|
| 风险偏好 | `profile.risk_tolerance` | str | 保守/稳健/进取 |
| 投资期限 | `profile.investment_horizon` | str | 短期/中期/长期 |
| 保障配比 | `profile.protection_ratio` | float | 保障类目标占比 |

**加载方式**：从 `allocation_profile.json` 加载。

---

## 二、数据注入流程

```
用户消息 → 意图识别 → skill路由
                         ↓
              ┌──────────────────────────┐
              │  context_navigator        │
              │  build_navigation_context()│
              ├──────────────────────────┤
              │ 1. 财务概览（含YTD）      │ ← financial_data.py
              │ 2. 资产配置导航           │ ← allocation_engine
              │ 3. 目标进度（智能计算）    │ ← goals.json + financial_data
              │ 4. 数据可用性清单 ← NEW!  │ ← 告知LLM有哪些数据
              │ 5. 导航指引               │ ← 约束LLM行为
              └──────────────────────────┘
                         ↓
              注入到 skill 的 system prompt
                         ↓
              LLM 看到完整数据 + 知道还有什么可用
```

---

## 三、LLM 引用规则

1. **已预加载的数据**：直接引用数值，不要重新计算
   - 正确：`"您的总资产为 ¥950,000，月结余 ¥18,730"`
   - 错误：`"根据您的持仓计算，总资产约为..."`

2. **推荐配比和评分**：引用引擎结果，不要提出不同配比
   - 正确：`"系统推荐配比为活钱10% 稳健30% 进取60%，您的实际配比偏差较小"`
   - 错误：`"我建议您将进取类调整为70%"`

3. **目标进度**：引用系统计算的百分比
   - 正确：`"今年总资产增加15万的目标进度为12.5%"`
   - 错误：`"您的目标已经完成了"`（未引用具体百分比）

4. **月份回退**：当数据月份非当月时，需说明
   - 正确：`"根据7月数据，您的月收入为 ¥27,350"`

5. **积累型 vs 金额型**：解释进度计算方式
   - 积累型：`"年度累计储蓄 ¥18,730 / 目标 ¥150,000 = 12.5%"`
   - 金额型：`"当前资产 ¥950,000 / 目标 ¥2,000,000 = 47.5%"`

---

## 四、Skill 与数据映射

| Skill | 预加载数据 | 可按需展开 |
|-------|-----------|-----------|
| 🎯 goal_tracker | 财务概览 + YTD + 目标进度 + 资产配置 | 交易明细 |
| 💰 asset_doctor | 持仓明细 + 资产配置 + 用户画像 | 交易明细 |
| 📊 spending_analyst | 消费记录(3个月) + 商户统计 | 持仓明细 |
| 🍽 life_planner | 财务概览 + 消费记录 | 目标进度 |
| 💬 general_chat | 财务概览 + 资产配置 + 目标进度 | 所有 |

---

## 五、技术实现

### 共享数据模块

```python
# core/ve5_chatbot/financial_data.py
load_financial_summary()      # → 含月份回退 + YTD
calculate_goal_progress(goal)  # → 智能进度（积累型/金额型）
load_goals_with_progress()     # → 所有目标 + 进度
```

### 导航上下文注入

```python
# core/ve5_chatbot/context_navigator.py
build_navigation_context(intent, user_message)
# 返回 ~400-600 tokens 的紧凑上下文
# 包含：财务概览 + 资产配置 + 目标进度 + 数据清单 + 导航指引

get_data_surface()
# 返回动态数据可用性清单
# 告知LLM哪些数据已加载、哪些可按需展开
```

### 经验代码 API Surface

```python
# core/experience/api_surface.py
get_api_surface_text()    # → 供经验生成代码使用
SafeContext               # → 运行时受限执行上下文
```

经验代码的 API Surface 与 Chatbot 的数据清单共享同一数据源（`financial_data.py`），
但表现形式不同：经验代码通过 `ctx` 对象调用，Chatbot LLM 通过 prompt 引用。
