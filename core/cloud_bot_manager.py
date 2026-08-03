"""
VE5 Cloud Bot Manager — 云端Bot/渠道管理
========================================
管理跨设备消息推送渠道（飞书/DingTalk/微信企业/QQ/自定义 群机器人），
集中配置 bot 密钥和 workflow 触发逻辑。

渠道类型（channel_type）内置 URL 模板：
    - feishu:      `https://open.feishu.cn/open-apis/bot/v2/hook/{access_token}`
    - dingtalk:    `https://oapi.dingtalk.com/robot/send?access_token={access_token}`
                   自动 HMAC-SHA256 加签（timestamp + sign）
    - wecom:       `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={access_token}`
    - qq:          (预留，URL 格式待定)
    - custom:      用户提供完整 webhook URL，secret 作为自定义请求头

用户只需提供 access_token 和 secret，前端根据渠道类型组装或让用户填写完整URL。

存储：SQLite ve5.db / cloud_bots + cloud_bot_logs

设计：
    CREATE TABLE cloud_bots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_id TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        source_module TEXT NOT NULL,        -- life_planner / asset_tactical
        channel_type TEXT NOT NULL,          -- feishu / dingtalk / wecom / qq / custom
        access_token TEXT DEFAULT '',        -- 渠道的 access_token 或 key
        secret TEXT DEFAULT '',              -- 签名密钥（DingTalk HMAC / 飞书签名）
        webhook_url TEXT DEFAULT '',         -- 仅 custom 使用（完整 URL）
        is_active INTEGER DEFAULT 1,
        trigger_rules TEXT DEFAULT '{}',
        message_recovery INTEGER DEFAULT 0,
        recovery_config TEXT DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
"""

import hmac
import base64
import hashlib
import time
import urllib.parse
import sqlite3
import json
import logging
import re
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger("ve5.cloud_bot")

from app_paths import DB_PATH

# ── 渠道 URL 模板 ──
_CHANNEL_URL_TEMPLATES: Dict[str, str] = {
    "feishu":    "https://open.feishu.cn/open-apis/bot/v2/hook/{access_token}",
    "dingtalk":  "https://oapi.dingtalk.com/robot/send?access_token={access_token}",
    "wecom":     "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={access_token}",
    "qq":        "",   # 预留
    "custom":    "",   # 用户提供完整 URL
}


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA JOURNAL_MODE=WAL")
    return conn


def _build_bot_webhook_url(channel_type: str, access_token: str, secret: str = "") -> str:
    """根据渠道类型构建 webhook URL（含签名）"""
    if channel_type == "custom":
        # 由用户直接填写的 webhook_url 字段返回
        return ""  # 调用方自己取 webhook_url

    template = _CHANNEL_URL_TEMPLATES.get(channel_type)
    if not template:
        return ""

    base_url = template.format(access_token=access_token)

    # DingTalk HMAC 加签
    if channel_type == "dingtalk" and secret:
        timestamp_ms = str(int(time.time() * 1000))
        string_to_sign = f"{timestamp_ms}\n{secret}"
        signature = base64.b64encode(
            hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
        ).decode("utf-8")
        sign_encoded = urllib.parse.quote_plus(signature)
        base_url += f"&timestamp={timestamp_ms}&sign={sign_encoded}"

    return base_url


# ════════════════════════════════════════════════
# 初始化表
# ════════════════════════════════════════════════

def cloud_bot_init():
    conn = _get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cloud_bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                source_module TEXT NOT NULL,
                channel_type TEXT NOT NULL DEFAULT 'feishu',
                access_token TEXT DEFAULT '',
                secret TEXT DEFAULT '',
                webhook_url TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1,
                trigger_rules TEXT DEFAULT '{}',
                message_recovery INTEGER DEFAULT 0,
                recovery_config TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS cloud_bot_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id TEXT NOT NULL,
                message_type TEXT NOT NULL,
                content TEXT,
                status TEXT DEFAULT 'sent',
                error TEXT DEFAULT '',
                sent_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bot_logs_bot ON cloud_bot_logs(bot_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bot_logs_time ON cloud_bot_logs(sent_at)")

        # 兼容旧表结构
        try:
            conn.execute("ALTER TABLE cloud_bots ADD COLUMN access_token TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass

        conn.commit()
        logger.info("[CLOUD-BOT] 表初始化完成")
    finally:
        conn.close()


# ════════════════════════════════════════════════
# Bot CRUD
# ════════════════════════════════════════════════

def bot_list() -> List[Dict]:
    conn = _get_conn()
    try:
        rows = conn.execute("SELECT * FROM cloud_bots ORDER BY created_at DESC").fetchall()
        bots = []
        for r in rows:
            d = dict(r)
            d["trigger_rules"] = json.loads(d.get("trigger_rules", "{}") or "{}")
            d["recovery_config"] = json.loads(d.get("recovery_config", "{}") or "{}")
            bots.append(d)
        return bots
    finally:
        conn.close()


def bot_get(bot_id: str) -> Optional[Dict]:
    conn = _get_conn()
    try:
        r = conn.execute("SELECT * FROM cloud_bots WHERE bot_id=?", (bot_id,)).fetchone()
        if r:
            d = dict(r)
            d["trigger_rules"] = json.loads(d.get("trigger_rules", "{}") or "{}")
            d["recovery_config"] = json.loads(d.get("recovery_config", "{}") or "{}")
            return d
    finally:
        conn.close()
    return None


def bot_create(bot_id: str, name: str, source_module: str, channel_type: str = "feishu",
               access_token: str = "", secret: str = "", webhook_url: str = "",
               trigger_rules: Dict = None, message_recovery: bool = False,
               recovery_config: Dict = None) -> bool:
    now = datetime.now().isoformat()
    conn = _get_conn()
    try:
        conn.execute("""
            INSERT INTO cloud_bots
            (bot_id, name, source_module, channel_type, access_token, secret, webhook_url,
             trigger_rules, message_recovery, recovery_config, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            bot_id, name, source_module, channel_type,
            access_token or "", secret or "", webhook_url or "",
            json.dumps(trigger_rules or {}, ensure_ascii=False),
            1 if message_recovery else 0,
            json.dumps(recovery_config or {}, ensure_ascii=False),
            now, now
        ))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        logger.warning(f"[CLOUD-BOT] bot_id 已存在: {bot_id}")
        return False
    except Exception as e:
        logger.error(f"[CLOUD-BOT] 创建失败: {e}")
        return False
    finally:
        conn.close()


def bot_update(bot_id: str, **kwargs) -> bool:
    allowed = ["name", "channel_type", "access_token", "secret", "webhook_url",
               "is_active", "message_recovery"]
    updates = {}
    for k in allowed:
        if k in kwargs:
            updates[k] = kwargs[k]
    if "trigger_rules" in kwargs:
        updates["trigger_rules"] = json.dumps(kwargs["trigger_rules"], ensure_ascii=False)
    if "recovery_config" in kwargs:
        updates["recovery_config"] = json.dumps(kwargs["recovery_config"], ensure_ascii=False)

    if not updates:
        return False

    updates["updated_at"] = datetime.now().isoformat()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [bot_id]

    conn = _get_conn()
    try:
        conn.execute(f"UPDATE cloud_bots SET {set_clause} WHERE bot_id=?", values)
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"[CLOUD-BOT] 更新失败: {e}")
        return False
    finally:
        conn.close()


def bot_delete(bot_id: str) -> bool:
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM cloud_bots WHERE bot_id=?", (bot_id,))
        conn.execute("DELETE FROM cloud_bot_logs WHERE bot_id=?", (bot_id,))
        conn.commit()
        return True
    finally:
        conn.close()


def bot_toggle(bot_id: str) -> Optional[Dict]:
    conn = _get_conn()
    try:
        r = conn.execute("SELECT is_active FROM cloud_bots WHERE bot_id=?", (bot_id,)).fetchone()
        if not r:
            return None
        new_state = 0 if r[0] else 1
        conn.execute("UPDATE cloud_bots SET is_active=?, updated_at=? WHERE bot_id=?",
                     (new_state, datetime.now().isoformat(), bot_id))
        conn.commit()
        return bot_get(bot_id)
    finally:
        conn.close()


# ════════════════════════════════════════════════
# 消息日志
# ════════════════════════════════════════════════

def bot_log(bot_id: str, message_type: str, content: str,
            status: str = "sent", error: str = "") -> int:
    conn = _get_conn()
    try:
        cur = conn.execute("""
            INSERT INTO cloud_bot_logs (bot_id, message_type, content, status, error, sent_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (bot_id, message_type, content, status, error, datetime.now().isoformat()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def bot_get_logs(bot_id: str, limit: int = 20) -> List[Dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM cloud_bot_logs WHERE bot_id=? ORDER BY sent_at DESC LIMIT ?",
            (bot_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ════════════════════════════════════════════════
# 消息发送（核心引擎）
# ════════════════════════════════════════════════

def _send_feishu_webhook(webhook_url: str, content: str) -> Dict:
    """通过飞书 webhook 发送消息（交互式卡片）"""
    try:
        import requests
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": "VE5 智能助手"},
                    "template": "blue"
                },
                "elements": [
                    {"tag": "markdown", "content": content}
                ]
            }
        }
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code == 200:
            return {"success": True}
        return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _send_dingtalk_webhook(webhook_url: str, content: str) -> Dict:
    """通过 DingTalk webhook 发送消息（markdown 格式）"""
    try:
        import requests
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": "VE5 智能助手",
                "text": content
            }
        }
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code == 200:
            body = resp.json()
            if body.get("errcode") == 0:
                return {"success": True}
            return {"success": False, "error": f"DingTalk errcode={body.get('errcode')}: {body.get('errmsg', '')}"}
        return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _send_wecom_webhook(webhook_url: str, content: str) -> Dict:
    """通过微信企业 webhook 发送消息（markdown 格式）"""
    try:
        import requests
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": "**VE5 智能助手**\n" + content
            }
        }
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code == 200:
            body = resp.json()
            if body.get("errcode") == 0:
                return {"success": True}
            return {"success": False, "error": f"WxWork errcode={body.get('errcode')}: {body.get('errmsg', '')}"}
        return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _send_custom_webhook(webhook_url: str, secret: str, content: str) -> Dict:
    """自定义 webhook 发送"""
    try:
        import requests
        headers = {}
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        payload = {
            "msgtype": "text",
            "text": {"content": content}
        }
        resp = requests.post(webhook_url, json=payload, headers=headers, timeout=10)
        if resp.status_code == 200:
            return {"success": True}
        return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def bot_send_message(bot_id: str, content: str, message_type: str = "manual") -> Dict:
    """向指定 bot 发送消息"""
    bot = bot_get(bot_id)
    if not bot:
        return {"success": False, "error": "Bot 不存在"}
    if not bot.get("is_active"):
        return {"success": False, "error": "Bot 已禁用"}

    channel = bot.get("channel_type", "feishu")
    access_token = bot.get("access_token", "")
    secret = bot.get("secret", "")

    # 构建 webhook URL
    if channel == "custom":
        webhook_url = bot.get("webhook_url", "")
    else:
        webhook_url = _build_bot_webhook_url(channel, access_token, secret)

    if not webhook_url:
        bot_log(bot_id, message_type, content, "failed",
                f"webhook_url 为空（channel={channel}, has_token={bool(access_token)}）")
        return {"success": False, "error": f"webhook_url 未配置（渠道 {channel} 需要填写 access_token）"}

    # 按渠道分发
    if channel == "feishu":
        result = _send_feishu_webhook(webhook_url, content)
    elif channel == "dingtalk":
        result = _send_dingtalk_webhook(webhook_url, content)
    elif channel == "wecom":
        result = _send_wecom_webhook(webhook_url, content)
    elif channel == "custom":
        result = _send_custom_webhook(webhook_url, secret, content)
    else:
        result = {"success": False, "error": f"不支持的渠道: {channel}"}

    status = "sent" if result.get("success") else "failed"
    error = result.get("error", "")
    bot_log(bot_id, message_type, content, status, error)

    if result.get("success"):
        logger.info(f"[CLOUD-BOT] 消息已发送: {bot_id} ({message_type})")
    else:
        logger.warning(f"[CLOUD-BOT] 发送失败: {bot_id}: {error}")

    return result


def bot_trigger_life_plan(bot_id: str, plan_data: Dict) -> Dict:
    """触发生活管家播报"""
    bot = bot_get(bot_id)
    if not bot or bot.get("source_module") != "life_planner":
        return {"success": False, "error": "Bot 不是生活管家类型"}

    content = _format_life_plan_full(plan_data)
    return bot_send_message(bot_id, content, "life_plan_full")


def bot_trigger_investment_signal(bot_id: str, signal_data: Dict) -> Dict:
    """触发资产配置战术播报"""
    bot = bot_get(bot_id)
    if not bot or bot.get("source_module") != "asset_tactical":
        return {"success": False, "error": "Bot 不是资产配置战术类型"}

    content = _format_investment_signal(signal_data)
    return bot_send_message(bot_id, content, "investment_signal")


# ════════════════════════════════════════════════
# 消息格式化
# ════════════════════════════════════════════════

def _format_life_plan_full(plan_data: Dict) -> str:
    """格式化生活管家播报消息（与 life_planner 保持一致）"""
    lines = ["**本周生活计划**\n"]
    weekly = plan_data.get("weekly_budget", 0)
    lines.append(f"**预算**: ¥{weekly}/周\n")

    recipes = plan_data.get("recipes", [])
    if recipes:
        lines.append("**食谱菜单**:")
        for r in recipes[:7]:
            if isinstance(r, dict):
                name = ""
                cost = r.get("estimated_cost") or r.get("cost") or r.get("budget") or 0
                day = r.get("day", "")
                bf = r.get("breakfast", "")
                lh = r.get("lunch", "")
                dn = r.get("dinner", "")
                if day and (bf or lh or dn):
                    parts = [day]
                    if bf: parts.append(f"早餐: {bf}")
                    if lh: parts.append(f"午餐: {lh}")
                    if dn: parts.append(f"晚餐: {dn}")
                    name = " | ".join(parts)
                if not name:
                    for k in ("recipe", "name", "meal", "dish", "title", "description", "day"):
                        v = r.get(k, "")
                        if v and isinstance(v, str) and v.strip():
                            name = v.strip()
                            break
                if not name:
                    text_parts = []
                    for k, v in r.items():
                        if k in ("estimated_cost", "cost", "budget"):
                            continue
                        if isinstance(v, str) and v.strip():
                            text_parts.append(f"{k}: {v}")
                    name = ", ".join(text_parts)
                if not name:
                    name = "(无名称)"
                lines.append(f"- {name} (约¥{cost})")
            else:
                lines.append(f"- {str(r)}")
        lines.append("")

    shopping = plan_data.get("shopping_list", [])
    if shopping:
        lines.append("**购物清单**:")
        for s in shopping[:10]:
            if isinstance(s, dict):
                item = s.get("item") or s.get("name") or s.get("product") or ""
                qty = s.get("quantity") or s.get("qty") or 1
                price = s.get("estimated_price") or s.get("price") or s.get("unit_price") or 0
                priority = s.get("priority", "")
                cat = s.get("category", "")
                desc = item
                if qty and qty != 1: desc += f" x{qty}"
                if price: desc += f" 约¥{price}"
                if priority: desc += f" [{priority}优先]"
                if cat: desc += f" ({cat})"
                lines.append(f"- {desc}")
            else:
                lines.append(f"- {str(s)}")
        lines.append("")

    entertainment = plan_data.get("entertainment", [])
    if entertainment:
        lines.append("**娱乐安排**:")
        for e in entertainment[:5]:
            if isinstance(e, dict):
                activity = e.get("activity") or e.get("name") or e.get("title") or ""
                day = e.get("day", "")
                budget = e.get("budget") or e.get("estimated_cost") or e.get("price") or 0
                reason = e.get("reason", "")
                parts = [activity] if activity else []
                if day: parts.append(day)
                if budget: parts.append(f"预算¥{budget}")
                if reason: parts.append(reason)
                if not parts:
                    for k, v in e.items():
                        if isinstance(v, str) and v.strip():
                            parts.append(f"{k}: {v}")
                lines.append(f"- {' | '.join(parts) if parts else '(无信息)'}")
            else:
                lines.append(f"- {str(e)}")
        lines.append("")

    if plan_data.get("tips"):
        lines.append(f"\n**小贴士**: {plan_data['tips']}")

    return "\n".join(lines)


def _format_investment_signal(signal_data: Dict) -> str:
    lines = ["**资产配置信号**\n"]
    action = signal_data.get("action", signal_data.get("signal", ""))
    lines.append(f"**操作**: {action}\n")
    detail = signal_data.get("detail", signal_data.get("reason", ""))
    if detail:
        lines.append(f"**理由**: {detail}\n")
    tickers = signal_data.get("tickers", signal_data.get("symbols", []))
    if tickers:
        lines.append("**涉及标的**: " + ", ".join(tickers))
    return "\n".join(lines)


# ════════════════════════════════════════════════
# 消息回收接口
# ════════════════════════════════════════════════

def bot_recover_message(bot_id: str, user_message: str) -> bool:
    bot = bot_get(bot_id)
    if not bot or not bot.get("message_recovery"):
        return False

    bot_log(bot_id, "recovered_message", user_message, "recovered")

    source = bot.get("source_module", "")
    try:
        if source == "life_planner":
            from core.rag_sqlite_store import expense_store
            amount_match = re.search(r'[¥￥]?\s*(\d+\.?\d*)\s*元?', user_message)
            amount = float(amount_match.group(1)) if amount_match else 0
            expense_store(
                transaction_date=datetime.now().strftime("%Y-%m-%d"),
                transaction_type="expense",
                amount=amount,
                counterparty=source,
                category_primary="bot_recovered",
                description=user_message[:200],
                source_file=f"bot_{bot_id}",
            )
        elif source == "asset_tactical":
            from core.rag_sqlite_store import fin_store
            fin_store(
                source_file=f"bot_{bot_id}",
                ocr_text=user_message[:500],
                description=f"Bot回收消息: {user_message[:200]}",
            )
        logger.info(f"[CLOUD-BOT] 消息已回收: {bot_id}")
        return True
    except Exception as e:
        logger.error(f"[CLOUD-BOT] 消息回收失败: {e}")
        return False


# 模块初始化
cloud_bot_init()
