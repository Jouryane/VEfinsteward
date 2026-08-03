"""
VE5 Episode Store — 经历存储层
================================
每一次 LLM 与用户的交互都是一次 Episode（经历）。
Episode 是 Experience 的原材料：多次相似 Episode 被 Compiler 蒸馏为可复用的 Experience。

Episode ≠ Memory：
    Memory 是被动的记录，Episode 是主动的结构化——包含输入、环境、
    LLM 推理过程、执行结果、用户反馈，全部可被 Compiler 分析。

存储: SQLite ve5.db / ep_episodes
"""

import json
import logging
import sqlite3
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime

logger = logging.getLogger("ve5.episode_store")

from app_paths import DB_PATH


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ════════════════════════════════════════════════
# 初始化
# ════════════════════════════════════════════════

def ep_init():
    conn = _get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ep_episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_id TEXT NOT NULL UNIQUE,
                source_skill TEXT NOT NULL,
                source_report_id TEXT DEFAULT '',

                -- 用户侧
                user_input TEXT DEFAULT '',
                context_json TEXT DEFAULT '{}',

                -- LLM 侧
                llm_reasoning TEXT DEFAULT '',
                llm_action_steps TEXT DEFAULT '[]',

                -- 结果
                result_json TEXT DEFAULT '{}',

                -- 是否已被编译为 Experience
                compiled_to TEXT DEFAULT '',

                -- 用户反馈 (-1=负面, 0=无, 1=正面)
                user_feedback INTEGER DEFAULT 0,

                created_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ep_skill ON ep_episodes(source_skill)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ep_compiled ON ep_episodes(compiled_to)")
        conn.commit()
        logger.info("[EPISODE] 表初始化完成")
    finally:
        conn.close()


# ════════════════════════════════════════════════
# CRUD
# ════════════════════════════════════════════════

def ep_save(
    source_skill: str,
    source_report_id: str = "",
    user_input: str = "",
    context_json: Dict = None,
    llm_reasoning: str = "",
    llm_action_steps: List[str] = None,
    result_json: Dict = None,
) -> Optional[str]:
    """
    保存一次 Episode。在每次 skill 执行后自动调用。
    返回 episode_id。
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    # episode_id: "ep_goal_tracker_20260724_163000" (保证唯一)
    # 如果同一秒内多次调用，追加毫秒
    episode_id = f"ep_{source_skill}_{ts}"
    now = datetime.now().isoformat()

    conn = _get_conn()
    try:
        # 检查是否重复（同一秒内同一 skill）
        existing = conn.execute(
            "SELECT id FROM ep_episodes WHERE episode_id = ?",
            (episode_id,)
        ).fetchone()
        if existing:
            # 追加序号
            episode_id = f"ep_{source_skill}_{ts}_{existing[0]}"

        conn.execute("""
            INSERT INTO ep_episodes
            (episode_id, source_skill, source_report_id,
             user_input, context_json,
             llm_reasoning, llm_action_steps,
             result_json, compiled_to, user_feedback, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', 0, ?)
        """, (
            episode_id, source_skill, source_report_id,
            user_input,
            json.dumps(context_json or {}, ensure_ascii=False),
            llm_reasoning,
            json.dumps(llm_action_steps or [], ensure_ascii=False),
            json.dumps(result_json or {}, ensure_ascii=False),
            now,
        ))
        conn.commit()
        logger.debug(f"[EPISODE] 已保存: {episode_id} ({source_skill})")
        return episode_id
    except sqlite3.IntegrityError as e:
        logger.warning(f"[EPISODE] 保存失败 (id重复): {e}")
        return None
    finally:
        conn.close()


def ep_get(episode_id: str) -> Optional[Dict]:
    conn = _get_conn()
    try:
        r = conn.execute(
            "SELECT * FROM ep_episodes WHERE episode_id = ?",
            (episode_id,)
        ).fetchone()
        if r:
            d = dict(r)
            for field in ["context_json", "llm_action_steps", "result_json"]:
                try:
                    d[field] = json.loads(d.get(field, "[]") or "[]")
                except Exception:
                    d[field] = []
            return d
    finally:
        conn.close()
    return None


def ep_list(
    source_skill: str = None,
    compiled: bool = None,
    limit: int = 20,
    offset: int = 0,
) -> List[Dict]:
    """
    列出 Episode。
    - compiled=True: 只返回已被编译为经验的
    - compiled=False: 只返回未被编译的（供 Compiler 使用）
    - compiled=None: 全部
    """
    conn = _get_conn()
    try:
        sql = "SELECT * FROM ep_episodes WHERE 1=1"
        params: List[Any] = []
        if source_skill:
            sql += " AND source_skill = ?"
            params.append(source_skill)
        if compiled is True:
            sql += " AND compiled_to != ''"
        elif compiled is False:
            sql += " AND compiled_to = ''"
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(sql, params).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            for field in ["context_json", "llm_action_steps", "result_json"]:
                try:
                    d[field] = json.loads(d.get(field, "[]") or "[]")
                except Exception:
                    d[field] = []
            result.append(d)
        return result
    finally:
        conn.close()


def ep_mark_compiled(episode_id: str, experience_id: str) -> bool:
    """标记 Episode 已被编译为某个 Experience"""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE ep_episodes SET compiled_to = ? WHERE episode_id = ?",
            (experience_id, episode_id)
        )
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def ep_feedback(episode_id: str, feedback: int) -> bool:
    """用户反馈：1=正面, -1=负面"""
    if feedback not in (-1, 0, 1):
        return False
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE ep_episodes SET user_feedback = ? WHERE episode_id = ?",
            (feedback, episode_id)
        )
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def ep_stats() -> Dict[str, Any]:
    """Episode 统计"""
    conn = _get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM ep_episodes").fetchone()[0]
        compiled = conn.execute(
            "SELECT COUNT(*) FROM ep_episodes WHERE compiled_to != ''"
        ).fetchone()[0]
        by_skill = {}
        for r in conn.execute(
            "SELECT source_skill, COUNT(*) as cnt FROM ep_episodes GROUP BY source_skill"
        ).fetchall():
            by_skill[r[0]] = r[1]
        return {
            "total": total,
            "compiled": compiled,
            "uncompiled": total - compiled,
            "by_skill": by_skill,
        }
    finally:
        conn.close()


# 模块初始化
ep_init()
