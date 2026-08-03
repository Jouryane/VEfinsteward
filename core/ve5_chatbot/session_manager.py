"""
VE管家 会话管理器
================
管理chatbot的会话历史，支持多会话、持久化存储。
"""

import json
import time
import uuid
from pathlib import Path
from typing import List, Dict, Any, Optional
from app_paths import DATA_DIR

_SESSION_DIR = DATA_DIR / "chatbot" / "sessions"
_SESSION_DIR.mkdir(parents=True, exist_ok=True)


def _session_file(session_id: str) -> Path:
    return _SESSION_DIR / f"{session_id}.json"


def ve5_chat_session_create(title: str = "新会话") -> str:
    """创建新会话，返回session_id"""
    session_id = str(uuid.uuid4())[:8]
    data = {
        "session_id": session_id,
        "title": title,
        "created_at": time.time(),
        "updated_at": time.time(),
        "messages": [],
    }
    _session_file(session_id).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return session_id


def ve5_chat_session_get(session_id: str) -> List[Dict[str, Any]]:
    """获取会话历史消息"""
    f = _session_file(session_id)
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data.get("messages", [])
    except Exception:
        return []


def ve5_chat_session_append(session_id: str, role: str, content: str, skill_name: str = "", metadata: dict = None):
    """追加一条消息到会话"""
    f = _session_file(session_id)
    data = {"messages": [], "title": "新会话", "updated_at": time.time()}
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass

    msg = {
        "role": role,
        "content": content,
        "timestamp": time.time(),
        "skill": skill_name,
        "metadata": metadata or {},
    }
    data["messages"].append(msg)
    data["updated_at"] = time.time()

    # 自动更新标题（第一条用户消息）
    if role == "user" and data.get("title") == "新会话" and len(data["messages"]) == 1:
        data["title"] = content[:20] + ("..." if len(content) > 20 else "")

    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def ve5_chat_get_latest_session_id() -> str:
    """获取最近活跃的会话ID（用于恢复上次对话）"""
    sessions = ve5_chat_session_list()
    return sessions[0]["session_id"] if sessions else ""


def ve5_chat_session_list() -> List[Dict[str, Any]]:
    """列出所有会话"""
    result = []
    for f in sorted(_SESSION_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            result.append({
                "session_id": data.get("session_id", f.stem),
                "title": data.get("title", "未命名"),
                "updated_at": data.get("updated_at", 0),
                "message_count": len(data.get("messages", [])),
            })
        except Exception:
            pass
    return result


def ve5_chat_session_clear(session_id: str):
    """清空会话消息"""
    f = _session_file(session_id)
    if f.exists():
        data = json.loads(f.read_text(encoding="utf-8"))
        data["messages"] = []
        data["updated_at"] = time.time()
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def ve5_chat_session_delete(session_id: str):
    """删除会话文件（彻底删除，不可恢复）"""
    f = _session_file(session_id)
    if f.exists():
        f.unlink()
        return True
    return False
