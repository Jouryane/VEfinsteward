# VE5 Experience Runtime — 架构设计文档

> 版本: V1 设计定稿 | 日期: 2026-07-24
>
> Experience Runtime 不是「记忆库」，而是一套 **运行时决策调度系统**。
> 它不需要知道所有答案，它只需要判断：
> 「这个场景是否已经从未知问题，变成了已知路径？」
> 是 → 绕过 LLM。否 → 交给 LLM，等待未来沉淀。

---

## 1. Experience 定义

```
Experience ≈ Personalized Executable Memory
            = 来自 LLM 输出 + 用户确认写入 + Confidence 控制执行 + 可编辑删除扩展的
              个性化、动态生成、可衰减的执行代码片段。

Experience ≠ 一个用户直接使用的功能模块
Experience ＝ VE 内部的「运行时基础设施层（Runtime Layer）」
              嵌入每个业务模块的生命周期（Recall → Assist → Formation）
```

### 1.1 它在系统中的地位

```
VE5 应用层
┌─────────────────────────────────────────┐
│  goal_tracker   life_planner   tactical  │  ← 业务 Skill
├─────────────────────────────────────────┤
│         Experience Runtime               │  ← 运行时基础设施
│  Encoder → Matcher → Controller → Exec  │
├─────────────────────────────────────────┤
│        Episode Store  │  Experience Store│  ← 持久化
├─────────────────────────────────────────┤
│            LLM Gateway                    │  ← 外部大脑
└─────────────────────────────────────────┘
```

Experience Runtime 是横切所有 Skill 的中间层，就像 CPU 调度器横切所有进程。

---

## 2. Skill vs Experience 的本质区别

| 维度 | Skill | Experience |
|------|-------|-----------|
| 本质 | 预训练的通用知识（通货）| 个体在重复场景中长出的肌肉记忆（私产）|
| 触发 | 用户显式调用 | Runtime 自动拦截并决策 |
| 适用 | 一次性/低频决策 | 高频/重复场景 |
| LLM 依赖 | 每次 | Confidence >= 阈值后 **绕过 LLM** |
| 来源 | 云端训练语料 | 该用户自己的 Episode 链 |
| 可编辑性 | 不可编辑 | 天然支持修改、删除、扩展 |
| 生命周期 | 一次执行，不存档 | raw → learning → automatic → 衰退 |

> Skill 是教科书——所有人都能买同一本。
> Experience 是你自己的笔记——它知道你上次在哪道题上犯了什么错误。

### 2.1 什么值得沉淀为 Experience

| 场景 | 沉淀？ | 理由 |
|------|:--:|------|
| 每天盯盘、分析持仓 | ✓ | 高频重复，规律性强 |
| 每周消费分析 | ✓ | 周期固定，变量有限 |
| 每月储蓄目标检查 | ✓ | trigger_frequency = recurring |
| 5 年买房规划 | ✗ | 偶发一次性决策 |
| 职业转型分析 | ✗ | 一生几次 |
| 量化策略调参（对量化用户）| ✓ | 对该用户是日常 |
| 协助中介卖房（对中介用户）| ✓ | 对该用户是高频 |

边界不是「任务复杂度」，而是 **对该用户的触发频率**。

---

## 3. 三层调度架构

> Experience Runtime 不是「记忆库」，而是**决策调度系统**。
> 判断"这个问题以前处理过没有"这件事本身，不能交给 LLM——
> 否则 Runtime 就失去了意义。这个判断必须是一个**低成本、确定性更高的系统组件**。

```
                      用户输入
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  Layer 1: State Encoder                                  │
│  问题: "现在发生了什么？"                                  │
│  不是判断经验，而是把当前环境转换成机器可比较的状态            │
│  输入: 用户输入 + 上下文                                    │
│  输出: 结构化 State Object                                │
└───────────────────────────┬─────────────────────────────┘
                            │
                            │ State Object
                            ▼
┌─────────────────────────────────────────────────────────┐
│  Layer 2: Experience Matcher                             │
│  问题: "有没有类似的经验？"                                 │
│  类似搜索引擎，不是 LLM                                    │
│  输入: State Object                                      │
│  输出: 候选经验列表（按相似度排序）                           │
│  算法: 规则匹配 + Embedding 余弦 + Metadata 过滤            │
└───────────────────────────┬─────────────────────────────┘
                            │
                            │ Candidate Experiences
                            ▼
┌─────────────────────────────────────────────────────────┐
│  Layer 3: Activation Controller                          │
│  问题: "虽然有经验，但现在应该用它吗？"                       │
│  最重要的一层——"找到经验" ≠ "执行经验"                     │
│  输入: Candidate + Context                               │
│  输出: YES (执行) / NO (转 LLM)                           │
│  判定: Confidence × Context Similarity × Safety Check    │
└───────────────────────────┬─────────────────────────────┘
                            │
              ┌─────────────┴─────────────┐
              │                           │
            激活                        不激活
              │                           │
              ▼                           ▼
┌─────────────────────┐     ┌─────────────────────┐
│ Layer 4: Executor    │     │ LLM Skill           │
│ 执行 Experience      │     │ 从零探索              │
│ Template 填充        │     │ 生成 Episode          │
│ Workflow 执行        │     │ → Compiler 候选       │
└──────────┬──────────┘     └──────────┬──────────┘
           │                           │
           └───────────┬───────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│ Feedback: 更新 success/failure → 重算 Confidence         │
└─────────────────────────────────────────────────────────┘
```

---

## 4. 四个模块

### 4.1 State Encoder — "现在发生了什么？"

```
文件: core/experience/encoder.py

输入: 用户输入 + 当前上下文
输出: 结构化 State Object

State Object:
{
  "intent": "spending_analysis",          // 意图分类
  "entities": {"time_range": "recent"},   // 提取实体
  "user_state": {                         // 用户当前状态
    "has_transaction_data": true,
    "last_analysis": "2026-07-01",
    "income_stable": true
  },
  "context": {                            // 环境上下文
    "module": "finance",
    "page": "dashboard",
    "trigger_type": "user_message"
  }
}
```

**实现策略：**
- **Phase 1**（冷启动，无经验）：LLM 做 intent + entity 抽取
- **Phase 2**（有经验积累）：高频 intent 走规则匹配（如 `if page=="dashboard" and action=="review" → intent="asset_review"`），低频走 LLM
- **Phase 3**（成熟期）：大部分 intent 已程序化，LLM 只处理 novel state

### 4.2 Experience Matcher — "有没有类似的经验？"

```
文件: core/experience/matcher.py

输入: State Object
输出: 候选经验列表 [{exp_id, similarity, experience}, ...]

算法：
  1. 规则匹配   — intent == exp.trigger_event → candidate = True
  2. Embedding   — cosine_similarity(state_embedding, exp_embedding)
  3. Metadata    — 过滤不相关的 type（不要把投资经验匹配给消费问题）
  4. 排序        — 综合 similarity + confidence 降序
```

**关键设计：**
- Matcher 不是 LLM，是搜索引擎
- 规则匹配先行（快速），Embedding 补充（语义泛化）
- 不支持的经验类型直接跳过（metadata filter）

### 4.3 Activation Controller — "现在应该用吗？"

```
文件: core/experience/controller.py

输入: Candidate Experience + Current State
输出: Decision { activate: bool, reason: str, fallback: str }

判定公式:
  Activation Score = Experience.Confidence × Context Similarity × Safety Gate

  其中:
    Experience.Confidence = PA × (0.50 + 0.20×UF + 0.15×UA + 0.15×R)
    Context Similarity    = Matcher 返回的余弦相似度
    Safety Gate           = {
      0: 用户状态剧变（换工作/大额异常）→ 强制走 LLM
      0.5: 环境部分匹配 → 降低激活概率
      1.0: 完全匹配 → 正常
    }

判定:
  if score >= 0.75: activate
  elif score >= 0.35: assist (LLM 辅助，注入 origin 信息)
  else: delegate to LLM
```

**核心逻辑：**
- Confidence 高 + 场景匹配 → 自动执行（绕过 LLM）
- Confidence 高 + 场景不匹配 → 不激活（即使经验很强，场景不对也不能用）
- 用户状态剧变（换工作、收入骤变、新增重大目标）→ Safety Gate = 0，强制走 LLM

### 4.4 Executor — "怎么执行？"

```
文件: core/experience/executor.py

输入: Experience + Context
输出: 执行结果（Template 填充后的文本 / JSON）

流程:
  if experience.level == "automatic":
    → fill_template(exp.template, context)
    → 直接返回，不调 LLM
  elif experience.level == "learning":
    → fill_template + inject_origin_into_prompt
    → 调 LLM 微调输出
  else:
    → delegate to full LLM exploration
```

---

## 5. Experience 嵌入 Skill 的三个位置

每个 Skill 不是被"改造成" Experience，而是在其生命周期的三个位置嵌入 Experience Runtime：

```
┌───────────────────────────────────────────────────────┐
│ Skill: goal_tracker.py                                │
│                                                       │
│  ① Input (Recall):                                    │
│     用户 "分析我的目标"                                  │
│     → Encoder → Matcher → Controller                  │
│     → 命中 monthly_saving_review → 直接执行             │
│                                                       │
│  ② Execution (Assist):                                │
│     即使 Experience 不完全替代 LLM                     │
│     → 在 prompt 中注入 Origin 信息                    │
│     → "用户风险偏好: 高风险, 不追涨, 偏好左侧投资"       │
│                                                       │
│  ③ Output (Formation):                                │
│     LLM 执行后 → 判断是否值得沉淀                       │
│     → 一次性（5年规划）→ 不沉淀                        │
│     → 连续5次每周复盘 → Compiler → Experience 候选     │
└───────────────────────────────────────────────────────┘
```

---

## 6. 文件结构

```
core/
├── experience/
│   ├── __init__.py              # 导出统一入口 exp_runtime_dispatch()
│   ├── encoder.py               # State Encoder — "现在发生了什么？"
│   ├── matcher.py               # Experience Matcher — "有没有类似的经验？"
│   ├── controller.py            # Activation Controller — "现在应该用吗？"
│   ├── executor.py              # Executor — "怎么执行？"
│   │
│   ├── store.py                 # (迁移自 experience_store.py) CRUD + Confidence
│   └── compiler.py              # (迁移自 episode_compiler.py) Episode → Experience
│
├── episode_store.py             # Episode 存储（不动）
├── experience_engine.py         # 定时调度入口（衰退线程等，轻量化）
│
api/
└── ve4_api_server.py            # 新增 exp_runtime 端点, 注册 Encoder/Matcher 钩子
```

---

## 7. 统一调度入口

```python
# core/experience/__init__.py

def exp_runtime_dispatch(user_input: str, context: Dict) -> Dict:
    """
    每个 Skill 入口调用此函数。
    返回:
      { "decision": "execute" | "assist" | "delegate",
        "experience": {...} or None,
        "state": {...},
        "output": "..." or None }
    """
    # Layer 1
    state = encoder.encode(user_input, context)

    # Layer 2
    candidates = matcher.match(state)

    # Layer 3
    decision = controller.decide(candidates, state)

    # Layer 4
    if decision["activate"]:
        return executor.execute(decision["experience"], state)
    elif decision["assist"]:
        return {"decision": "assist", "experience": decision["experience"], "state": state}
    else:
        return {"decision": "delegate", "state": state}
```

---

## 8. Confidence 公式 (V1)

```
Score = PA × (0.50 + 0.20×UF + 0.15×UA + 0.15×R)

PA = success_count / (success_count + failure_count + 1)   ← 主导因子
UF = min(frequency, 10) / 10                                ← 放大器
UA = (positive + 1) / (positive + negative + 2)             ← 用户接受度
R  = e^(-0.05 × Δt_days)                                    ← 新鲜度

→ Score >= 0.75: automatic（绕过 LLM）
→ Score >= 0.35: learning（LLM 辅助）
→ Score <  0.35: raw（完整 LLM）
```

PA 是指数级重要的——准确的经验快速自动化，不准确的永远卡在 raw。

---

## 9. Origin: Experience 必须可溯源

```json
{
  "origin": {
    "type": "user_confirmed",
    "episode_ids": ["ep001", "ep005", "ep008"],
    "created_reason": "用户连续3次使用目标追踪，LLM输出结构高度相似"
  }
}
```

Confidence 是量化信号，Origin 是质性证据。
LLM 需要 Origin 来判断「这个经验在什么背景下产生的、是否适用于当前场景」。

---

## 10. V0 → V1 升级路线

| 组件 | V0 | V1 |
|------|----|----|
| 架构 | 单一 store + engine | Encoder → Matcher → Controller → Executor 四层 |
| Encoder | 无 | 规则 + LLM 双模 |
| Matcher | Jaccard | Jaccard + Embedding 余弦 |
| Controller | exp_execute() | Activation Score (Conf × Sim × Safety) |
| Confidence | w1×Sim + w2×Freq + w3×Decay | PA × (0.50 + 0.20×UF + 0.15×UA + 0.15×R) |
| Skill 集成 | 手动调用 | exp_runtime_dispatch() 统一入口 |
| Origin | 无 | origin.type + episode_ids + created_reason |

---

## 11. 类比：人脑 vs CPU vs Experience Runtime

| 概念 | 人脑 | CPU | Experience Runtime |
|------|------|-----|--------------------|
| 感知输入 | 感觉皮层 | 中断向量 | State Encoder |
| 模式识别 | 小脑/基底神经节 | 分支预测器 | Experience Matcher |
| 执行决策 | 前额叶 | 调度器 | Activation Controller |
| 自动执行 | 肌肉记忆 | 微码 | Executor |
| 新问题处理 | 前额叶推理 | 异常处理/内核 | LLM |

Experience Runtime 不是 AI 大脑，而是 AI 应用在长期陪伴用户后，逐渐生长出来的「操作系统调度层」。
LLM 负责探索，Runtime 负责接管——这和 CPU 不会每次都问「这个程序以前运行过吗」是同一个道理。
