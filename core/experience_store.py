"""
VE5 Experience Store — V1 经验存储引擎
========================================
Experience ≈ Personalized Executable Memory

一种来自 LLM 最初输出并被用户确认写入、由 Confidence 控制是否绕过 LLM、
可编辑删除扩展的、个性化、动态生成、可衰减的执行代码片段。

V1 核心公式：
    Score = PA × (0.50 + 0.20×UF + 0.15×UA + 0.15×R) × DS

    PA = success_count / (success_count + failure_count + 1)  ← 预测准确率
    UF = min(frequency, 10) / 10                               ← 使用频率
    UA = (positive_fb + 1) / (positive_fb + negative_fb + 2)  ← 用户接受度
    R  = e^(-λ * Δt)                                           ← 新鲜度
    DS = _estimate_data_stability(exp_id)                      ← 数据稳定性

Confidence 的唯一职责：决定是否绕过 LLM 直接执行。
"""

import json
import math
import logging
import sqlite3
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta

logger = logging.getLogger("ve5.experience_store")

from app_paths import DB_PATH


# ════════════════════════════════════════════════
# 超参数 (V1)
# ════════════════════════════════════════════════

_LAMBDA_DECAY = 0.05           # 时间衰退系数，约14天半衰期
_AUTOMATIC_THRESHOLD = 0.75    # 绕过 LLM
_LEARNING_THRESHOLD = 0.35     # LLM 辅助
_FREQUENCY_CAP = 10            # UF 归一化上限
_SIMILARITY_THRESHOLD = 0.15   # 最低命中相似度


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ════════════════════════════════════════════════
# 初始化
# ════════════════════════════════════════════════

def exp_init():
    conn = _get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS exp_experiences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exp_id TEXT NOT NULL UNIQUE,
                type TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                source_report_id TEXT DEFAULT '',

                -- ▸ Origin（经验来源，V1新增）
                origin_json TEXT DEFAULT '{}',

                -- ▸ Trigger
                trigger_event TEXT DEFAULT '',
                trigger_frequency TEXT DEFAULT 'recurring',
                trigger_condition TEXT DEFAULT '',

                -- ▸ Workflow + Template + Rules
                workflow TEXT DEFAULT '[]',
                template_json TEXT DEFAULT '{}',
                decision_rules TEXT DEFAULT '[]',
                exception_rules TEXT DEFAULT '[]',

                -- ▸ Context
                context_variables TEXT DEFAULT '[]',
                tags TEXT DEFAULT '[]',

                -- ▸ Confidence（V1新算法）
                confidence REAL DEFAULT 0.25,
                prediction_accuracy REAL DEFAULT 0.5,

                -- ▸ 统计
                frequency INTEGER DEFAULT 0,
                last_used TEXT DEFAULT '',
                total_usage INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                failure_count INTEGER DEFAULT 0,

                -- ▸ LLM 依赖
                llm_required INTEGER DEFAULT 1,

                -- ▸ 可编辑性
                is_user_edited INTEGER DEFAULT 0,
                user_extensions TEXT DEFAULT '{}',

                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS exp_activations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exp_id TEXT NOT NULL,
                triggered_by TEXT DEFAULT '',
                score_breakdown TEXT DEFAULT '{}',
                confidence_before REAL,
                confidence_after REAL,
                result_success INTEGER DEFAULT 0,
                output_text TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_type ON exp_experiences(type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_confidence ON exp_experiences(confidence)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_activations ON exp_activations(exp_id)")

        # 迁移：补充 V1 新增列
        _migrate_v1(conn)

        conn.commit()
        logger.info("[EXPERIENCE] 表初始化完成 (V1)")
    finally:
        conn.close()


def _migrate_v1(conn):
    """V0 → V1 列迁移"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(exp_experiences)").fetchall()]
    adds = {
        "origin_json": "TEXT DEFAULT '{}'",
        "trigger_frequency": "TEXT DEFAULT 'recurring'",
        "prediction_accuracy": "REAL DEFAULT 0.5",
        "failure_count": "INTEGER DEFAULT 0",
        "decision_rules": "TEXT DEFAULT '[]'",
        "exception_rules": "TEXT DEFAULT '[]'",
        "context_variables": "TEXT DEFAULT '[]'",
        "is_user_edited": "INTEGER DEFAULT 0",
        "user_extensions": "TEXT DEFAULT '{}'",
        "code_path": "TEXT DEFAULT ''",
        "code_generated_at": "TEXT DEFAULT ''",
    }
    for col, dtype in adds.items():
        if col not in cols:
            conn.execute(f"ALTER TABLE exp_experiences ADD COLUMN {col} {dtype}")
            logger.info(f"[EXPERIENCE] 迁移: 新增列 {col}")


# ════════════════════════════════════════════════
# V1 Confidence — LLM Bypass Gate
# ════════════════════════════════════════════════

def _compute_confidence_v1(
    success_count: int,
    failure_count: int,
    frequency: int,
    positive_fb: int,
    negative_fb: int,
    last_used: str,
    data_stability: float = 1.0,
) -> Tuple[float, float, float, float, float]:
    """
    V1 公式: Score = PA × (0.50 + 0.20×UF + 0.15×UA + 0.15×R) × DS

    返回 (score, pa, uf, r, ds)

    DS (Data Stability): 0 ~ 1.0, 由 _estimate_data_stability() 提供。
    当关键财务数据(收入/支出)变化超过阈值时, DS 下降 → confidence 被拉低。
    """
    # PA: Prediction Accuracy
    pa = success_count / (success_count + failure_count + 1)

    # UF: Usage Frequency (放大器，不是证据)
    uf = min(frequency, _FREQUENCY_CAP) / _FREQUENCY_CAP

    # UA: User Acceptance
    ua = (positive_fb + 1) / (positive_fb + negative_fb + 2)

    # R: Recency
    if last_used:
        try:
            last = datetime.fromisoformat(last_used)
            delta_days = (datetime.now() - last).total_seconds() / 86400.0
        except Exception:
            delta_days = 0
    else:
        delta_days = 0
    r = math.exp(-_LAMBDA_DECAY * delta_days)

    # DS: Data Stability (钳制)
    ds = max(0.1, min(1.0, data_stability))

    # 综合 (加权型：PA 主导，其他因子放大器)
    # Score = PA × (0.50 + 0.20×UF + 0.15×UA + 0.15×R) × DS
    # PA 占 50% 基础权重，UF/UA/R 提供最高 ±50% 的微调
    booster = 0.50 + 0.20 * uf + 0.15 * ua + 0.15 * r
    score = pa * booster * ds

    # 钳制到 [0, 1]
    score = max(0.01, min(score, 0.98))

    return round(score, 4), round(pa, 4), round(uf, 4), round(r, 4), round(ds, 4)


def _estimate_data_stability(exp_id: str) -> float:
    """
    估算当前数据与经验创建时的数据漂移程度。

    返回 0 ~ 1.0 (1 = 数据完全稳定, 经验可靠)。
    当关键财务数据(收入/支出/资产)变化超过阈值时, 稳定性下降。

    注意：比较基准使用"最近有数据的月份"与其前一月份，
    而非硬编码当月 vs 上月（当月无数据会导致误判 100% 变化）。
    """
    try:
        import sqlite3
        from app_paths import DB_PATH
        from datetime import datetime

        if not DB_PATH.exists():
            return 1.0

        exp = exp_get(exp_id)
        if not exp:
            return 1.0

        conn = sqlite3.connect(str(DB_PATH))

        # ── 确定"最近有数据的月份"（当月无数据时回退）──
        from core.ve5_chatbot.financial_data import find_latest_month_with_data
        cur_month = find_latest_month_with_data(conn, "income")
        if not cur_month:
            conn.close()
            return 1.0

        # 前一月份（用日期运算）
        year, month_num = int(cur_month[:4]), int(cur_month[5:7])
        if month_num == 1:
            prev_month = f"{year-1}-12"
        else:
            prev_month = f"{year}-{month_num-1:02d}"

        cur_income = conn.execute(
            "SELECT SUM(ABS(amount)) FROM transactions WHERE transaction_type='income' AND transaction_date LIKE ?",
            (f"{cur_month}%",)
        ).fetchone()
        prev_income = conn.execute(
            "SELECT SUM(ABS(amount)) FROM transactions WHERE transaction_type='income' AND transaction_date LIKE ?",
            (f"{prev_month}%",)
        ).fetchone()
        cur_income = float(cur_income[0] or 0)
        prev_income = float(prev_income[0] or 0)

        cur_expense = conn.execute(
            "SELECT SUM(ABS(amount)) FROM transactions WHERE transaction_type='expense' AND transaction_date LIKE ?",
            (f"{cur_month}%",)
        ).fetchone()
        prev_expense = conn.execute(
            "SELECT SUM(ABS(amount)) FROM transactions WHERE transaction_type='expense' AND transaction_date LIKE ?",
            (f"{prev_month}%",)
        ).fetchone()
        cur_expense = float(cur_expense[0] or 0)
        prev_expense = float(prev_expense[0] or 0)

        # 检测 is_superseded 列是否存在
        ah_cols = [r[1] for r in conn.execute("PRAGMA table_info(asset_holdings)").fetchall()]
        has_superseded = "is_superseded" in ah_cols
        sf = "is_superseded=0 AND " if has_superseded else ""
        cur_assets = conn.execute(
            f"SELECT SUM(current_value) FROM asset_holdings WHERE {sf}current_value > 0"
        ).fetchone()
        cur_assets = float(cur_assets[0] or 0)
        conn.close()

        # 收入变化率
        income_change = abs(cur_income - prev_income) / max(prev_income, 1)
        # 支出变化率
        expense_change = abs(cur_expense - prev_expense) / max(prev_expense, 1)

        # 冷启动：前一月份无数据 → 无比较基准，视为稳定（数据模式尚未建立）
        if prev_income == 0 and prev_expense == 0:
            return 1.0

        # 综合稳定度: 变化越大越低
        max_change = max(income_change, expense_change)
        # 阈值: 10% 以内全稳定, 30% 以上全不稳定
        if max_change <= 0.10:
            stability = 1.0
        elif max_change >= 0.30:
            stability = 0.3
        else:
            stability = 1.0 - (max_change - 0.10) / 0.20 * 0.7

        return max(0.1, min(1.0, stability))
    except Exception:
        return 1.0  # 数据不可用时不惩罚


def _level_from_confidence(conf: float) -> str:
    if conf >= _AUTOMATIC_THRESHOLD:
        return "automatic"
    elif conf >= _LEARNING_THRESHOLD:
        return "learning"
    return "raw"


def _get_level(confidence: float) -> str:
    return _level_from_confidence(confidence)


# ════════════════════════════════════════════════
# Jaccard 相似度（V0 retrieval，V1 升级为 embedding）
# ════════════════════════════════════════════════

def _compute_similarity(tags_a: List[str], tags_b: List[str]) -> float:
    sa, sb = set(tags_a), set(tags_b)
    if not sa and not sb:
        return 0.5
    if not sa or not sb:
        return 0.1
    return len(sa & sb) / len(sa | sb)


# ════════════════════════════════════════════════
# 幂等性检查：防止短时间内重复创建相似经验
# ════════════════════════════════════════════════

_DEDUP_WINDOW_MINUTES = 5       # 同名+同类型 5 分钟内视为重复
_DEDUP_AUTO_WINDOW_MINUTES = 10 # 自动+手动同时创建的 10 分钟窗口
_DEDUP_TAG_OVERLAP_THRESHOLD = 3  # 标签重叠数阈值


def _check_dedup(
    conn: sqlite3.Connection,
    exp_type: str,
    name: str,
    tags: List[str],
) -> Optional[str]:
    """
    检查近期是否已创建过相似的经验。

    两级检查:
      1. 精确匹配：同 type + 同 name，在 _DEDUP_WINDOW_MINUTES 内 → 重复
      2. 模糊匹配：同 type + 标签重叠 ≥ _DEDUP_TAG_OVERLAP_THRESHOLD，在 _DEDUP_AUTO_WINDOW_MINUTES 内 → 重复

    返回已存在的 exp_id，或 None。
    """
    if not exp_type or not name:
        return None

    now = datetime.now()

    # ── 1. 精确匹配：同名+同类型 ──
    cutoff_exact = (now - timedelta(minutes=_DEDUP_WINDOW_MINUTES)).isoformat()
    row = conn.execute(
        "SELECT exp_id FROM exp_experiences WHERE type = ? AND name = ? AND created_at >= ?",
        (exp_type, name, cutoff_exact),
    ).fetchone()
    if row:
        return row["exp_id"]

    # ── 2. 模糊匹配：同类型 + 高标签重叠（防止自动+手动同时创建）──
    if not tags:
        return None

    cutoff_fuzzy = (now - timedelta(minutes=_DEDUP_AUTO_WINDOW_MINUTES)).isoformat()
    rows = conn.execute(
        "SELECT exp_id, tags FROM exp_experiences WHERE type = ? AND created_at >= ?",
        (exp_type, cutoff_fuzzy),
    ).fetchall()

    for r in rows:
        existing_tags = set()
        try:
            existing_tags = set(json.loads(r["tags"] or "[]"))
        except Exception:
            pass
        overlap = len(set(tags) & existing_tags)
        if overlap >= _DEDUP_TAG_OVERLAP_THRESHOLD:
            return r["exp_id"]

    return None


# ════════════════════════════════════════════════
# CRUD
# ════════════════════════════════════════════════

def exp_create(
    source_report_id: str = "",
    exp_type: str = "",
    name: str = "",
    description: str = "",
    # Origin (V1)
    origin: Dict = None,
    # Trigger
    trigger_event: str = "",
    trigger_frequency: str = "recurring",
    trigger_condition: str = "",
    # Workflow + Rules
    workflow: List[Dict] = None,
    template_json: Dict = None,
    decision_rules: List[Dict] = None,
    exception_rules: List[Dict] = None,
    # Context
    context_variables: List[str] = None,
    tags: List[str] = None,
    # Confidence
    llm_required: bool = True,
    # Code (V2: AI Coding)
    code_path: str = "",
) -> Optional[str]:
    """创建新经验"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    exp_id = f"{exp_type}_{ts}"
    now = datetime.now().isoformat()

    # 默认 Origin
    if origin is None:
        origin = {"type": "compiler_auto", "episode_ids": [], "created_reason": ""}

    conn = _get_conn()
    try:
        # ── 幂等性检查：防止短时间内重复创建 ──
        dedup_exp_id = _check_dedup(conn, exp_type, name, tags or [])
        if dedup_exp_id:
            logger.info(f"[EXPERIENCE] 去重：复用已有经验 {dedup_exp_id}")
            return dedup_exp_id

        # ── V2: 代码文件重命名（temp_id → 正式 exp_id）──
        if code_path:
            try:
                from core.experience.code_generator import rename_code_file
                renamed = rename_code_file(code_path, exp_id)
                if renamed and renamed != code_path:
                    code_path = renamed
            except Exception as e:
                logger.debug(f"[EXPERIENCE] 代码文件重命名跳过: {e}")

        conn.execute("""
            INSERT INTO exp_experiences (
                exp_id, type, name, description, source_report_id,
                origin_json,
                trigger_event, trigger_frequency, trigger_condition,
                workflow, template_json, decision_rules, exception_rules,
                context_variables, tags,
                llm_required, confidence, prediction_accuracy,
                frequency, last_used, total_usage,
                success_count, failure_count,
                is_user_edited, user_extensions,
                code_path, code_generated_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            exp_id, exp_type, name, description, source_report_id,
            json.dumps(origin or {}, ensure_ascii=False),
            trigger_event, trigger_frequency, trigger_condition,
            json.dumps(workflow or [], ensure_ascii=False),
            json.dumps(template_json or {}, ensure_ascii=False),
            json.dumps(decision_rules or [], ensure_ascii=False),
            json.dumps(exception_rules or [], ensure_ascii=False),
            json.dumps(context_variables or [], ensure_ascii=False),
            json.dumps(tags or [], ensure_ascii=False),
            1 if llm_required else 0,
            0.25, 0.5,  # 初始 confidence / prediction_accuracy
            0, "", 0,   # frequency / last_used / total_usage
            0, 0,       # success_count / failure_count
            0, "{}",    # is_user_edited / user_extensions
            code_path, now if code_path else "",  # code_path / code_generated_at
            now, now,
        ))
        conn.commit()
        logger.info(f"[EXPERIENCE] 创建: {exp_id} ({name}) code={'Y' if code_path else 'N'}")
        return exp_id
    except sqlite3.IntegrityError:
        logger.warning(f"[EXPERIENCE] exp_id 重复: {exp_id}")
        return None
    finally:
        conn.close()


def _row_to_dict(r) -> Dict:
    """将 SQLite row 转为完整 dict，自动反序列化 JSON 字段"""
    d = dict(r)
    json_fields = [
        "origin_json", "workflow", "template_json",
        "decision_rules", "exception_rules",
        "context_variables", "tags", "user_extensions",
    ]
    for field in json_fields:
        try:
            d[field] = json.loads(d.get(field, "[]") or "[]")
        except Exception:
            d[field] = [] if "list" in field else {}
    # 别名：origin → origin_json 的反序列化结果
    d["origin"] = d.get("origin_json", {})
    d["level"] = _level_from_confidence(d.get("confidence", 0))
    # code_path 默认空字符串
    if "code_path" not in d:
        d["code_path"] = ""
    if "code_generated_at" not in d:
        d["code_generated_at"] = ""
    return d


def exp_get(exp_id: str) -> Optional[Dict]:
    conn = _get_conn()
    try:
        r = conn.execute("SELECT * FROM exp_experiences WHERE exp_id=?", (exp_id,)).fetchone()
        if r:
            return _row_to_dict(r)
    finally:
        conn.close()
    return None


def exp_list(exp_type: str = None, level: str = None, limit: int = 20) -> List[Dict]:
    conn = _get_conn()
    try:
        sql = "SELECT * FROM exp_experiences WHERE 1=1"
        params: List[Any] = []
        if exp_type:
            sql += " AND type = ?"
            params.append(exp_type)
        if level == "automatic":
            sql += " AND confidence >= ?"
            params.append(_AUTOMATIC_THRESHOLD)
        elif level == "learning":
            sql += " AND confidence >= ? AND confidence < ?"
            params.extend([_LEARNING_THRESHOLD, _AUTOMATIC_THRESHOLD])
        elif level == "raw":
            sql += " AND confidence < ?"
            params.append(_LEARNING_THRESHOLD)
        sql += " ORDER BY confidence DESC, frequency DESC LIMIT ?"
        params.append(limit)
        return [_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def exp_delete(exp_id: str) -> bool:
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM exp_experiences WHERE exp_id=?", (exp_id,))
        conn.execute("DELETE FROM exp_activations WHERE exp_id=?", (exp_id,))
        conn.commit()
        return True
    finally:
        conn.close()


def exp_update(exp_id: str, **kwargs) -> bool:
    """更新经验字段。V1 新增 origin_json 和可编辑性字段。"""
    allowed = [
        "name", "description", "trigger_event", "trigger_frequency",
        "trigger_condition", "llm_required",
        "workflow", "template_json", "decision_rules", "exception_rules",
        "context_variables", "tags", "origin_json",
        "is_user_edited", "user_extensions",
        "code_path", "code_generated_at",
    ]
    updates = {}
    for k in allowed:
        if k in kwargs:
            updates[k] = kwargs[k]
    serializable = [
        "workflow", "template_json", "decision_rules", "exception_rules",
        "context_variables", "tags", "origin_json", "user_extensions",
    ]
    for k in serializable:
        if k in updates:
            updates[k] = json.dumps(updates[k], ensure_ascii=False)
    if "is_user_edited" in updates and isinstance(updates["is_user_edited"], bool):
        updates["is_user_edited"] = 1 if updates["is_user_edited"] else 0

    if not updates:
        return False
    updates["updated_at"] = datetime.now().isoformat()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [exp_id]

    conn = _get_conn()
    try:
        conn.execute(f"UPDATE exp_experiences SET {set_clause} WHERE exp_id=?", values)
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


# ════════════════════════════════════════════════
# 核心：命中 + 执行 + 反馈 (V1)
# ════════════════════════════════════════════════

def exp_hit(trigger_event: str, context_tags: List[str] = None,
            limit: int = 5) -> List[Dict]:
    """
    命中算法 (V1)：按 trigger_event + 标签相似度检索。
    返回的每条经验附带 confidence + origin，供调用方决策。
    """
    candidates = exp_list()
    ct = context_tags or []
    scored = []

    for exp in candidates:
        tags = exp.get("tags", [])
        sim = _compute_similarity(ct, tags)

        # 过滤完全不匹配的（没有公共标签且 context 非空）
        if sim < _SIMILARITY_THRESHOLD and ct:
            continue

        # 触发事件精确匹配加分
        if trigger_event and exp.get("trigger_event") == trigger_event:
            sim = min(1.0, sim + 0.15)

        scored.append((sim, exp))

    scored.sort(key=lambda x: x[0], reverse=True)
    result = []
    for sim, exp in scored[:limit]:
        exp["hit_similarity"] = round(sim, 4)
        result.append(exp)

    return result


def exp_execute(exp_id: str, context: Dict = None, success: bool = None) -> Dict:
    """
    执行经验 (V2)：
    - automatic 级别：纯 Template 填充 + Workflow → 直接返回
    - learning/raw 级别：LLM 辅助（由调用方负责调 LLM）

    执行后更新 success_count / failure_count 并重新计算 confidence。

    参数:
        exp_id: 经验 ID
        context: 环境变量（用于填充 template）
        success: True=成功, False=失败, None=未反馈

    返回:
        {"success": True, "decision": "execute"|"assist"|"delegate", "output": {...}, "needs_llm": bool, ...}
    """
    exp = exp_get(exp_id)
    if not exp:
        return {"success": False, "error": "经验不存在"}

    old_conf = exp.get("confidence", 0.25)
    old_level = exp.get("level", "raw")

    # ── 用真正的 executor 执行 ──
    try:
        from core.experience.executor import execute as _exec
        # 构建最小 State Object
        state = context or {}
        state.setdefault("intent", "experience_manual")
        state.setdefault("raw_input", context.get("user_input", "") if context else "")
        state.setdefault("entities", {})
        state.setdefault("user_state", {})
        state.setdefault("context", {})
        state.setdefault("embedding", [])
        exec_result = _exec(exp, state)
        output = exec_result.get("output", {})
        decision = exec_result.get("decision", "delegate")
        needs_llm = exec_result.get("needs_llm", True)
        # 透传 exec_context 和 data_changes 供前端使用
        exec_context = exec_result.get("exec_context", {})
        data_changes = exec_result.get("data_changes", [])
        replay = exec_result.get("replay", False)
        # 注入 origin
        output["_origin"] = exp.get("origin", {})
        if needs_llm:
            output["_llm_assisted"] = True
    except Exception as e:
        # executor 不可用时回退到简单模板填充
        logger.warning(f"[EXPERIENCE] executor 异常，回退模板: {e}")
        template = exp.get("template_json", {})
        output = _fill_template(template, context or {}) if template and context else {}
        needs_llm = old_conf < _AUTOMATIC_THRESHOLD
        decision = "execute" if not needs_llm else "assist"
        exec_context = {}
        data_changes = []
        replay = False
        if needs_llm:
            output["_llm_assisted"] = True
        output["_origin"] = exp.get("origin", {})

    # ── 自动反馈检测 ──
    if success is None:
        # 检测工作流执行结果和模板填充质量
        _fb_ok = _auto_detect_feedback(output, exp)
        if _fb_ok is not None:
            success = _fb_ok
            logger.debug(f"[EXPERIENCE] 自动反馈: {exp_id} → {'success' if success else 'failure'}")

    # ── 更新统计 ──
    now = datetime.now().isoformat()
    freq = exp.get("frequency", 0) + 1
    total = exp.get("total_usage", 0) + 1
    succ = exp.get("success_count", 0)
    fail = exp.get("failure_count", 0)

    if success is True:
        succ += 1
    elif success is False:
        fail += 1

    # ── 数据稳定性 ──
    ds = _estimate_data_stability(exp_id)

    # V1 Confidence 重算 (含 DS)
    new_conf, pa, uf, r_decay, ds = _compute_confidence_v1(
        success_count=succ,
        failure_count=fail,
        frequency=freq,
        positive_fb=succ,
        negative_fb=fail,
        last_used=now,
        data_stability=ds,
    )
    new_level = _level_from_confidence(new_conf)
    new_llm = 0 if new_level == "automatic" else 1

    conn = _get_conn()
    try:
        conn.execute("""
            UPDATE exp_experiences SET
                confidence = ?, prediction_accuracy = ?,
                frequency = ?, total_usage = ?,
                success_count = ?, failure_count = ?,
                last_used = ?, llm_required = ?, updated_at = ?
            WHERE exp_id = ?
        """, (new_conf, pa, freq, total, succ, fail, now, new_llm, now, exp_id))

        # 记录激活
        conn.execute("""
            INSERT INTO exp_activations
            (exp_id, triggered_by, score_breakdown, confidence_before, confidence_after,
             result_success, output_text, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            exp_id,
            context.get("triggered_by", "") if context else "",
            json.dumps({"pa": pa, "uf": uf, "r": r_decay, "ds": ds, "score": new_conf}),
            old_conf, new_conf,
            1 if success else 0,
            json.dumps(output, ensure_ascii=False)[:2000],
            now,
        ))
        conn.commit()
    finally:
        conn.close()

    level_changed = old_level != new_level
    if level_changed:
        if new_level == "automatic":
            logger.info(
                f"[EXPERIENCE] ⚡ bypass LLM: {exp_id} ({exp['name']}) "
                f"{old_level} → {new_level} (conf={new_conf:.3f}, pa={pa:.3f})"
            )
        else:
            logger.info(
                f"[EXPERIENCE] 退化: {exp_id} ({exp['name']}) "
                f"{old_level} → {new_level} (conf={new_conf:.3f})"
            )

    return {
        "success": True,
        "exp_id": exp_id,
        "decision": decision,
        "level": new_level,
        "confidence": new_conf,
        "confidence_before": old_conf,
        "prediction_accuracy": pa,
        "level_changed": level_changed,
        "needs_llm": needs_llm,
        "output": output,
        "exec_context": exec_context,
        "data_changes": data_changes,
        "replay": replay,
    }


def exp_decay_tick():
    """全局衰退 tick：对所有活跃经验重新计算 confidence（含数据稳定性）。"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT exp_id, success_count, failure_count, frequency, last_used FROM exp_experiences WHERE frequency > 0"
        ).fetchall()

        for r in rows:
            ds = _estimate_data_stability(r["exp_id"])
            conf, pa, uf, r_decay, _ = _compute_confidence_v1(
                success_count=r["success_count"] or 0,
                failure_count=r["failure_count"] or 0,
                frequency=r["frequency"] or 0,
                positive_fb=r["success_count"] or 0,
                negative_fb=r["failure_count"] or 0,
                last_used=r["last_used"] or "",
                data_stability=ds,
            )
            new_llm = 0 if conf >= _AUTOMATIC_THRESHOLD else 1
            conn.execute(
                "UPDATE exp_experiences SET confidence=?, prediction_accuracy=?, llm_required=? WHERE exp_id=?",
                (conf, pa, new_llm, r["exp_id"])
            )
            logger.debug(
                f"[EXPERIENCE] tick: {r['exp_id']} conf={conf:.3f} pa={pa:.3f} ds={ds:.3f} r={r_decay:.3f}"
            )

        conn.commit()
    finally:
        conn.close()


# ════════════════════════════════════════════════
# 自动反馈检测
# ════════════════════════════════════════════════

def _auto_detect_feedback(output: Dict, exp: Dict) -> Optional[bool]:
    """
    自动检测经验执行是否成功，无需外部传入 success 标志。

    检测逻辑:
      0. 回放模式: _replay_mode=True → 直接判定成功（习惯回放 = 成功执行）
      1. 模板残留检测: output 中是否残存未填充的 {variable} 占位符
      2. 空值检测: 关键字段是否为空
      3. 数量指标: 填充成功的变量数 vs 期望变量数

    返回:
      True  → 执行成功
      False → 执行失败
      None  → 无法判断（不更新 success/failure 计数）
    """
    if not output or not isinstance(output, dict):
        return None

    # ── 回放模式：习惯回放 = 成功执行 ──
    if output.get("_replay_mode"):
        logger.debug(f"[EXPERIENCE] 回放模式检测 → success")
        return True

    context_vars = exp.get("context_variables", [])
    if not context_vars:
        return None  # 无期望变量，无法判断

    import re

    filled_count = 0
    unfilled_count = 0
    template_pattern = re.compile(r'\{(\w+)')

    for key, val in output.items():
        if not isinstance(val, str):
            continue
        # 检测残留模板标记
        residual = template_pattern.findall(val)
        has_residual = any(v in context_vars for v in residual)

        if has_residual:
            unfilled_count += 1
        elif val.strip() and val != '(空)':
            filled_count += 1

    total = filled_count + unfilled_count
    if total == 0:
        return None  # 无法判断

    # 超过 50% 填充成功 → success
    fill_rate = filled_count / total
    if fill_rate >= 0.5:
        return True
    elif fill_rate < 0.2:
        return False

    return None  # 不确定


# ════════════════════════════════════════════════
# 模板填充
# ════════════════════════════════════════════════

def _fill_template(template: Dict, context: Dict) -> Dict:
    """模板填充: {key} 或 {key:format} → value。右→左替换避免位置漂移。"""
    import re
    result = {}
    for key, val in template.items():
        if isinstance(val, str):
            s = val
            matches = list(re.finditer(r'\{(\w+)(?::([^}]*))?\}', s))
            for match in reversed(matches):
                var_name = match.group(1)
                fmt_spec = match.group(2)
                full = match.group(0)
                if var_name in context:
                    cv = context[var_name]
                    if fmt_spec and isinstance(cv, (int, float)):
                        try:
                            s = s[:match.start()] + f"{cv:{fmt_spec}}" + s[match.end():]
                        except (ValueError, TypeError):
                            s = s[:match.start()] + f"¥{cv:,.0f}" + s[match.end():]
                    elif isinstance(cv, (int, float)):
                        if var_name.endswith(('_pct','_progress','_rate','_count','_frequency',
                                               '_years','_months','_days','_age','_index'))\
                            or var_name in ('progress','goal_progress','months_needed','months_early','gap'):
                            s = s[:match.start()] + str(cv) + s[match.end():]
                        else:
                            prefix = '' if match.start() > 0 and val[match.start()-1] == '\u00a5' else '¥'
                            s = s[:match.start()] + prefix + f"{cv:,.0f}" + s[match.end():]
                    else:
                        s = s[:match.start()] + str(cv) + s[match.end():]
                else:
                    s = s[:match.start()] + '' + s[match.end():]
            result[key] = s
        else:
            result[key] = val
    return result


# 模块初始化
exp_init()
