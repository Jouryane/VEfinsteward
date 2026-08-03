# VE5 区域物价 Workflow 实施计划

> 目标：让 life_planner 生成的购物清单贴合用户所在区域的真实物价

---

## 现状

- RAG 研报系统（`vector_store.py`）因 `chromadb.telemetry.product.posthog` 缺失降级为 JSON 关键词匹配
- 财务 RAG（`financial_rag.py`）独立运行，专存 OCR 截图
- 生活规划 skill 中所有物价均为 LLM "虚构"

---

## Phase 1：修复 ChromaDB（必须先做）

**问题：** `No module named 'chromadb.telemetry.product.posthog'`

**根因：** ChromaDB 0.5.x 的 telemetry 模块依赖 `posthog` 包，打包时未包含。

**修复：** 在 `vector_store.py` 初始化前 patch sys.modules，mock 掉 posthog，让 ChromaDB 跳过 telemetry 初始化。

**验证：** 启动后终端不再出现降级 warning，`ve4_kb_search()` 返回带 distance 的结果。

---

## Phase 2：新建消费价格 RAG

**文件：** `core/consumer_price_rag.py`

**存储结构：**
```json
{
  "item": "鸡蛋", "spec": "30枚/盒", "price": 12.8,
  "merchant": "盒马", "location": "上海浦东",
  "category": "食材", "source": "receipt|manual|government",
  "date": "2026-07-22"
}
```

**写入来源：**
| 来源 | 触发方式 |
|---|---|
| 小票 OCR | 用户拍购物小票 → OCR 提取 → 存入 RAG |
| 手动录入 | 前端表单（商品/价格/商家/日期） |
| 政府数据 | 通过 WebSearch 实时搜索政府公开物价（非爬虫） |

**查询接口：**
```python
ve5_price_rag_search("鸡蛋 上海浦东", n_results=5)
```

---

## Phase 3：区域物价 Workflow

**扩展：** `core/ve5_chatbot/skills/life_planner.py`

**新增数据来源（三重组合）：**

| 来源 | 方式 | 说明 |
|---|---|---|
| 高德地图 API | `place/around` 接口 | 按坐标查周边商铺（超市/菜市场/便利店），返回名称/距离/类型 |
| 政府物价 | WebSearch 实时搜索 | `"上海 今日菜价 鸡蛋 猪肉 批发"` 搜索政府公开数据 |
| 消费价格 RAG | 向量检索 | 查询用户历史购物价格记录 |

**Prompt 增强：**
在 `_STEP2_PROMPT` 中追加 `{regional_context}`：
```
周边商铺：盒马（1.2km）、永辉（800m）、钱大妈（300m）
你近期价格：鸡蛋12.8元/盒（盒马，7/20）、猪肉35元/斤（永辉，7/18）
政府参考：上海批发市场今日鸡蛋8.5元/斤、猪肉28元/斤
```

**效果：** LLM 据此知道去哪里买更便宜，生成贴合现实的购物清单。

---

## 政府物价搜索方案（替代爬虫）

**不做爬虫的原因：** 政府网站结构差异大、反爬、维护成本高。

**搜索方案：**
1. LLM 生成搜索词：`"{城市} {日期} 菜价 鸡蛋 猪肉 批发"`
2. 调用 `WebSearch` 实时搜索
3. 提取搜索结果中的价格数字
4. 清洗后存入消费价格 RAG（`source=government`）
5. 同时作为 prompt 上下文的一部分

**优点：** 按需获取、无需维护、覆盖面广。
**缺点：** 依赖搜索质量、结果不结构化。

---

## 实施顺序

1. Phase 1（1-2小时）→ 2. Phase 2（半天）→ 3. Phase 3（半天）

## 文件变更清单

| 文件 | 操作 | 说明 |
|---|---|---|
| `tactical/fundamental/knowledge/vector_store.py` | 修改 | 修复 posthog 兼容性 |
| `core/consumer_price_rag.py` | 新建 | 消费价格 RAG |
| `core/ve5_chatbot/skills/life_planner.py` | 修改 | 接入区域物价上下文 |
| `core/ai_gateway.py` 或 utils | 修改 | 集成 WebSearch 获取政府物价 |
| `pwa/index.html` | 修改 | 新增手动录入物价入口（可选） |
