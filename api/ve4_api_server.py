"""
VE5 本地数据 API
================
为 PWA 前端提供真实数据接口，实现"即配置即显示"。
VE5 桌面版：随机端口 + session token 保护 + pywebview 窗口。

接口列表：
    GET /api/v1/dashboard/stats      → 总览 KPI 数据
    GET /api/v1/accounts             → 账户列表（从 asset_holdings 聚合）
    GET /api/v1/activities           → 最近活动日志
    GET /api/v1/bluetooth/status     → 蓝牙接收器状态
    GET /api/v1/allocation           → 资产配置分布
    GET /api/v1/model/stats          → 本地模型调用统计

用法（桌面版由 ve5_desktop_launcher.py 自动启动）：
    uvicorn api.ve4_api_server:app --host 127.0.0.1 --port {随机端口}

命名规范：
    - 路由函数: ve4_api_{endpoint}
    - 数据模型: VE4Api{ModelName}
"""

import os
import sys
import json
import shutil
import sqlite3
import logging
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from dataclasses import dataclass, asdict

# 确保 receiver / tactical 模块可导入
PROJECT_ROOT = Path(__file__).parent.parent.resolve()  # VE5/ 目录
ASSETS_DIR = PROJECT_ROOT / "assets"
sys.path.insert(0, str(PROJECT_ROOT))

from app_paths import (
    DATA_DIR, DB_PATH, PROFILE_PATH, SNAPSHOTS_DIR, TACTICAL_OUTPUT_DIR,
    PWA_DIR, CONFIG_DIR, TACTICAL_DIR, ensure_data_dirs,
)
EMERGENCY_CONFIG_PATH = DATA_DIR / "emergency_config.json"
from receiver.config import INCOMING_DIR, PROCESSED_DIR, FAILED_DIR, ensure_dirs
from api.strategy_center import router as strategy_router
# ─── FastAPI ───
try:
    from fastapi import FastAPI, UploadFile, File, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
    from fastapi.staticfiles import StaticFiles
except ImportError:
    print("[ERROR] FastAPI 未安装：pip install fastapi uvicorn")
    sys.exit(1)

app = FastAPI(title="VE5 Local API", version="0.1.0")
logger = logging.getLogger("ve5.api")


@app.on_event("startup")
async def ve5_startup():
    """启动后初始化后台线程"""
    # 启动经验引擎衰退线程
    try:
        from core.experience_engine import exp_engine_start
        exp_engine_start()
    except Exception:
        pass

# ─── 静态文件服务（开发模式禁用缓存） ───
if PWA_DIR.exists():
    app.mount("/pwa", StaticFiles(directory=str(PWA_DIR)), name="pwa")

# 挂载 userdata/snapshots 作为 /snapshots（供前端离线读取）
if SNAPSHOTS_DIR.exists():
    app.mount("/snapshots", StaticFiles(directory=str(SNAPSHOTS_DIR)), name="snapshots")

# 挂载战术输出目录（策略图表、结果文件）
if TACTICAL_OUTPUT_DIR.exists():
    app.mount("/tactical-output", StaticFiles(directory=str(TACTICAL_OUTPUT_DIR)), name="tactical-output")

# 挂载 assets 目录（头像、图标等静态资源）
if ASSETS_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")

def _ve5_expected_token() -> str:
    return os.environ.get("VE5_SESSION_TOKEN", "")


@app.get("/")
def ve4_api_root(ve5_token: str = ""):
    """根路径重定向到财务中枢"""
    response = RedirectResponse(url="/pwa/index.html")
    expected = _ve5_expected_token()
    if expected and ve5_token == expected:
        response.set_cookie(
            "ve5_session_token",
            expected,
            httponly=True,
            samesite="strict",
        )
    return response

@app.get("/favicon.ico")
def ve4_api_favicon():
    return JSONResponse(content={})

# 允许 PWA 前端跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def ve5_api_session_guard(request, call_next):
    expected = _ve5_expected_token()
    protected_prefixes = ("/api/", "/snapshots/", "/tactical-output/")
    if expected and request.url.path.startswith(protected_prefixes):
        provided = request.headers.get("x-ve5-token") or request.cookies.get("ve5_session_token")
        if provided != expected:
            return JSONResponse(content={"error": "unauthorized"}, status_code=401)
    return await call_next(request)


# 开发模式：禁用静态文件缓存（pywebview/Edge WebView2 会忽略普通 no-cache）
@app.middleware("http")
async def ve4_api_no_cache_middleware(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/pwa/") and request.url.path.endswith((".html", ".js", ".css")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["ETag"] = ""
    return response

# ─── 数据模型 ───

@dataclass
class VE4ApiDashboardStats:
    total_assets: float = 0.0
    monthly_income: float = 0.0
    monthly_expense: float = 0.0
    monthly_essential_expense: float = 0.0
    securities_value: float = 0.0
    currency: str = "CNY"
    updated_at: str = ""


@dataclass
class VE4ApiAccount:
    id: str = ""
    name: str = ""
    type: str = ""  # "bank" | "securities" | "fund" | "wallet"
    balance: float = 0.0
    last_sync: str = ""
    icon: str = ""
    icon_color: str = ""
    has_override: bool = False


@dataclass
class VE4ApiActivity:
    time: str = ""
    text: str = ""
    badge: str = ""
    badge_type: str = ""  # "success" | "warn"


@dataclass
class VE4ApiAllocation:
    liquidity: float = 0.0
    aggressive: float = 0.0
    defensive: float = 0.0
    liquidity_value: float = 0.0
    aggressive_value: float = 0.0
    defensive_value: float = 0.0


# ─── 数据库工具 ───

def ve4_api_get_db() -> sqlite3.Connection:
    """获取数据库连接（自动创建表）"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    # 确保基础表存在
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_date TEXT,
            transaction_type TEXT,
            amount REAL,
            counterparty TEXT,
            category_primary TEXT,
            category_secondary TEXT,
            description TEXT,
            raw_data_hash TEXT UNIQUE,
            source_file TEXT,
            restored_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_key TEXT UNIQUE,
            name TEXT,
            type TEXT,
            icon TEXT,
            icon_color TEXT,
            balance REAL DEFAULT 0,
            last_sync TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT,
            text TEXT,
            badge TEXT,
            badge_type TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT UNIQUE,
            strategy_text TEXT NOT NULL,
            strategy_params TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            text_result TEXT,
            csv_result TEXT,
            images_json TEXT,
            llm_analysis TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES strategy_sessions(session_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_chat (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES strategy_sessions(session_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS asset_holdings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_key TEXT DEFAULT '',
            product_name TEXT DEFAULT '',
            asset_class TEXT DEFAULT '',
            sub_class TEXT DEFAULT '',
            liquidity_level TEXT DEFAULT '',
            risk_level TEXT DEFAULT '',
            current_value REAL DEFAULT 0,
            quantity REAL DEFAULT 0,
            cost_basis REAL DEFAULT 0,
            product_code TEXT DEFAULT '',
            source_file TEXT DEFAULT '',
            is_classified INTEGER DEFAULT 0,
            classification_confidence REAL DEFAULT 0,
            user_overridden INTEGER DEFAULT 0,
            holding_return_pct REAL DEFAULT 0,
            unrealized_pnl REAL DEFAULT 0,
            annualized_return_pct REAL DEFAULT 0,
            inference_source TEXT DEFAULT '',
            batch_id TEXT DEFAULT '',
            purchase_date TEXT DEFAULT '',
            holding_days INTEGER DEFAULT 0,
            source_bank TEXT DEFAULT '',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_profile (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            age INTEGER,
            career_stage TEXT DEFAULT '',
            risk_preference TEXT DEFAULT '',
            monthly_expense REAL DEFAULT 0,
            monthly_income REAL DEFAULT 0,
            emergency_months INTEGER DEFAULT 3,
            updated_at TEXT
        )
    """)
    conn.commit()
    # ─── 数据库迁移：检查并补充缺失列 ───
    try:
        existing = [r[1] for r in conn.execute("PRAGMA table_info(asset_holdings)").fetchall()]
        for col, typ in [
            ("product_code", "TEXT DEFAULT ''"),
            ("inference_source", "TEXT DEFAULT ''"),
            ("batch_id", "TEXT DEFAULT ''"),
            ("purchase_date", "TEXT DEFAULT ''"),
            ("source_bank", "TEXT DEFAULT ''"),
            ("holding_return_pct", "REAL DEFAULT 0"),
            ("unrealized_pnl", "REAL DEFAULT 0"),
            ("annualized_return_pct", "REAL DEFAULT 0"),
            ("holding_days", "INTEGER DEFAULT 0"),
        ]:
            if col not in existing:
                conn.execute(f"ALTER TABLE asset_holdings ADD COLUMN {col} {typ}")
    except Exception as _mig_err:
        pass  # 表可能不存在，忽略
    conn.commit()
    return conn


def ve4_api_safe_query(conn: sqlite3.Connection, query: str, params=()):
    """安全查询，表不存在时返回空列表。SUM查询保证返回至少一行。"""
    try:
        cursor = conn.execute(query, params)
        rows = [dict(row) for row in cursor.fetchall()]
        if not rows and "SUM(" in query.upper():
            return [{"total": 0}]
        return rows
    except sqlite3.OperationalError:
        if "SUM(" in query.upper():
            return [{"total": 0}]
        return []


# ─── 数据聚合逻辑 ───

def ve4_api_compute_stats() -> VE4ApiDashboardStats:
    """从 transactions + asset_holdings 表计算总览数据"""
    conn = ve4_api_get_db()
    try:
        # ── 交易流水数据（transactions 表）──
        # 首页"本月支出"使用全部消费记录（兼容历史截图）
        income_rows = ve4_api_safe_query(
            conn,
            "SELECT SUM(amount) as total FROM transactions WHERE transaction_type='income'"
        )
        monthly_income = income_rows[0]["total"] or 0.0

        expense_rows = ve4_api_safe_query(
            conn,
            "SELECT SUM(amount) as total FROM transactions WHERE transaction_type='expense'"
        )
        monthly_expense = expense_rows[0]["total"] or 0.0

        # 必需消费（全部数据）
        essential_rows = ve4_api_safe_query(
            conn,
            "SELECT SUM(amount) as total FROM transactions WHERE transaction_type='expense' AND is_essential = 1"
        )
        monthly_essential_expense = essential_rows[0]["total"] or 0.0

        # ── 持仓资产数据（asset_holdings 表）──
        # 总资产 = 所有已分类 + 未分类持仓的 current_value 总和
        asset_rows = ve4_api_safe_query(
            conn, "SELECT SUM(current_value) as total FROM asset_holdings"
        )
        total_assets = asset_rows[0]["total"] or 0.0

        # 证券市值 = equity + alternative 类别，或来自证券账户的持仓
        sec_rows = ve4_api_safe_query(
            conn,
            """SELECT SUM(current_value) as total FROM asset_holdings
               WHERE asset_class IN ('equity', 'alternative')
                  OR account_key LIKE '%证券%'"""
        )
        securities_value = sec_rows[0]["total"] or 0.0

        # 总资产唯一来源：asset_holdings 表（文档规定不回退到 transactions）

        return VE4ApiDashboardStats(
            total_assets=round(total_assets, 2),
            monthly_income=round(monthly_income, 2),
            monthly_expense=round(monthly_expense, 2),
            monthly_essential_expense=round(monthly_essential_expense, 2),
            securities_value=round(securities_value, 2),
            updated_at=datetime.now().isoformat(),
        )
    finally:
        conn.close()


def ve4_api_compute_accounts() -> List[VE4ApiAccount]:
    """
    从 asset_holdings 表聚合账户信息。
    唯一数据来源：asset_holdings 按 account_key 分组，不回退到 transactions 表。
    """
    conn = ve4_api_get_db()
    try:
        # ── 优先从 asset_holdings 聚合账户 ──
        ah_rows = ve4_api_safe_query(
            conn,
            """
            SELECT account_key,
                   SUM(current_value) as total_value,
                   MAX(updated_at) as last_sync,
                   MAX(CASE WHEN user_overridden = 1 THEN 1 ELSE 0 END) as has_override
            FROM asset_holdings
            WHERE account_key != ''
            GROUP BY account_key
            ORDER BY total_value DESC
            LIMIT 10
            """
        )

        accounts = []
        icon_map = {
            "招商": ("招", "cyan"), "工商": ("工", "indigo"), "建设": ("建", "emerald"),
            "农业": ("农", "green"), "中国": ("中", "rose"), "支付宝": ("支", "emerald"),
            "微信": ("微", "green"), "华泰": ("华", "amber"), "东方": ("东", "rose"),
            "中信": ("中", "indigo"), "基金": ("基", "rose"), "股票": ("股", "amber"),
            "财富": ("财", "amber"),
        }
        type_map = {
            "招商": "bank", "工商": "bank", "建设": "bank", "农业": "bank", "中国": "bank",
            "支付宝": "wallet", "微信": "wallet",
            "华泰": "securities", "东方": "securities", "中信": "securities",
            "基金": "fund", "股票": "securities", "财富": "securities",
        }

        if ah_rows:
            for row in ah_rows:
                name = row.get("account_key") or "未知账户"
                icon, color = "账", "indigo"
                acc_type = "bank"
                for kw, (ic, col) in icon_map.items():
                    if kw in name:
                        icon, color = ic, col
                        acc_type = type_map.get(kw, "bank")
                        break
                # 如果关键词没匹配到但account_key含"证券/财富"，标记为securities
                if acc_type == "bank" and any(k in name for k in ["证券", "财富", "股票"]):
                    acc_type = "securities"
                    icon, color = "证", "amber"
                elif acc_type == "bank" and "基金" in name:
                    acc_type = "fund"
                    icon, color = "基", "rose"

                accounts.append(VE4ApiAccount(
                    id=name[:30], name=name, type=acc_type,
                    balance=abs(row.get("total_value") or 0),
                    last_sync=row.get("last_sync") or "",
                    icon=icon, icon_color=color,
                    has_override=bool(row.get("has_override")),
                ))

        return accounts
    finally:
        conn.close()


def ve4_api_compute_activities(limit: int = 10) -> List[VE4ApiActivity]:
    """从 activity_log 表读取最近活动，时间从 created_at 提取确保真实"""
    conn = ve4_api_get_db()
    try:
        rows = ve4_api_safe_query(
            conn,
            "SELECT created_at, time, text, badge, badge_type FROM activity_log ORDER BY created_at DESC LIMIT ?",
            (limit,)
        )
        if not rows:
            return [
                VE4ApiActivity(time="--", text="暂无活动记录，请通过蓝牙上传截图文件", badge="提示", badge_type="warn")
            ]
        result = []
        for r in rows:
            # 优先使用 created_at 格式化时间，回退到 time 字段
            created = r.get("created_at") or r.get("time") or ""
            if created and len(created) >= 16:  # 2026-07-13 14:32:00 格式
                display_time = created[11:16]  # 提取 HH:MM
            elif created and len(created) == 5:  # 已经是 HH:MM
                display_time = created
            else:
                display_time = "--"
            result.append(VE4ApiActivity(
                time=display_time,
                text=r.get("text", ""),
                badge=r.get("badge", ""),
                badge_type=r.get("badge_type", ""),
            ))
        return result
    finally:
        conn.close()


def ve4_api_compute_allocation() -> VE4ApiAllocation:
    """从 transactions 表计算资产配置分布"""
    conn = ve4_api_get_db()
    try:
        # 按分类统计
        rows = ve4_api_safe_query(
            conn,
            "SELECT category_primary, SUM(amount) as total FROM transactions WHERE transaction_type='expense' OR transaction_type='income' GROUP BY category_primary"
        )

        liquidity = 0.0
        aggressive = 0.0
        defensive = 0.0

        for row in rows:
            cat = row["category_primary"] or "其他"
            amt = abs(row["total"] or 0)
            if cat in ("餐饮", "交通", "购物", "居住", "通讯", "其他"):
                liquidity += amt
            elif cat in ("投资", "理财", "股票", "基金"):
                aggressive += amt
            elif cat in ("医疗", "教育", "保险"):
                defensive += amt
            else:
                liquidity += amt

        total = liquidity + aggressive + defensive or 1.0

        return VE4ApiAllocation(
            liquidity=round(liquidity / total * 100, 1),
            aggressive=round(aggressive / total * 100, 1),
            defensive=round(defensive / total * 100, 1),
            liquidity_value=round(liquidity, 2),
            aggressive_value=round(aggressive, 2),
            defensive_value=round(defensive, 2),
        )
    finally:
        conn.close()


# ─── 路由 ───

@app.get("/api/v1/activities")
def ve4_api_activities(limit: int = 10):
    activities = ve4_api_compute_activities(limit)
    return JSONResponse(content=[asdict(a) for a in activities])


@app.get("/api/v1/transactions")
def ve4_api_transactions(limit: int = 50, category: str = ""):
    """查询交易/消费记录"""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row

        if category:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE category_primary=? ORDER BY transaction_date DESC, id DESC LIMIT ?",
                (category, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM transactions ORDER BY transaction_date DESC, id DESC LIMIT ?",
                (limit,)
            ).fetchall()
        conn.close()

        result = []
        for r in rows:
            result.append({
                "id": r["id"],
                "date": r["transaction_date"],
                "type": r["transaction_type"],
                "amount": r["amount"],
                "counterparty": r["counterparty"],
                "category": r["category_primary"],
                "category_secondary": r["category_secondary"],
                "description": r["description"],
                "source_file": r["source_file"],
            })
        return JSONResponse(content={"success": True, "transactions": result, "count": len(result)})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})

@app.get("/api/v1/transactions/monthly")
def ve4_api_transactions_monthly(month: str = None):
    """月度支出汇总（按一级分类分组）。

    参数:
        month: 查询月份，格式 "YYYY-MM"。不传则查询全部数据（兼容历史截图）。
    """
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        now = datetime.now()

        # ── 确定查询日期范围 ──
        if month:
            # 查询指定月份
            start_date = f"{month}-01"
            y, m = int(month.split("-")[0]), int(month.split("-")[1])
            if m == 12:
                end_date = f"{y+1}-01-01"
            else:
                end_date = f"{y}-{m+1:02d}-01"
            query_label = month
        else:
            # 默认查询全部数据（不限制时间，兼容历史截图）
            start_date = "1970-01-01"
            end_date = None
            query_label = "全部"

        # ── 总支出 ──
        if end_date:
            total_row = conn.execute(
                "SELECT SUM(amount) as total, COUNT(*) as cnt FROM transactions WHERE transaction_type='expense' AND transaction_date >= ? AND transaction_date < ?",
                (start_date, end_date)
            ).fetchone()
            essential_row = conn.execute(
                "SELECT SUM(amount) as total, COUNT(*) as cnt FROM transactions WHERE transaction_type='expense' AND transaction_date >= ? AND transaction_date < ? AND is_essential = 1",
                (start_date, end_date)
            ).fetchone()
            income_row = conn.execute(
                "SELECT SUM(amount) as total FROM transactions WHERE transaction_type='income' AND transaction_date >= ? AND transaction_date < ?",
                (start_date, end_date)
            ).fetchone()
            cat_rows = conn.execute(
                """SELECT category_primary, SUM(amount) as total, COUNT(*) as cnt,
                          SUM(CASE WHEN is_essential = 1 THEN amount ELSE 0 END) as essential_amount
                   FROM transactions WHERE transaction_type='expense' AND transaction_date >= ? AND transaction_date < ?
                   GROUP BY category_primary ORDER BY total DESC""",
                (start_date, end_date)
            ).fetchall()
            income_cat_rows = conn.execute(
                """SELECT category_primary, SUM(amount) as total, COUNT(*) as cnt
                   FROM transactions WHERE transaction_type='income' AND transaction_date >= ? AND transaction_date < ?
                   GROUP BY category_primary ORDER BY total DESC""",
                (start_date, end_date)
            ).fetchall()
        else:
            total_row = conn.execute(
                "SELECT SUM(amount) as total, COUNT(*) as cnt FROM transactions WHERE transaction_type='expense' AND transaction_date >= ?",
                (start_date,)
            ).fetchone()
            essential_row = conn.execute(
                "SELECT SUM(amount) as total, COUNT(*) as cnt FROM transactions WHERE transaction_type='expense' AND transaction_date >= ? AND is_essential = 1",
                (start_date,)
            ).fetchone()
            income_row = conn.execute(
                "SELECT SUM(amount) as total FROM transactions WHERE transaction_type='income' AND transaction_date >= ?",
                (start_date,)
            ).fetchone()
            cat_rows = conn.execute(
                """SELECT category_primary, SUM(amount) as total, COUNT(*) as cnt,
                          SUM(CASE WHEN is_essential = 1 THEN amount ELSE 0 END) as essential_amount
                   FROM transactions WHERE transaction_type='expense' AND transaction_date >= ?
                   GROUP BY category_primary ORDER BY total DESC""",
                (start_date,)
            ).fetchall()
            income_cat_rows = conn.execute(
                """SELECT category_primary, SUM(amount) as total, COUNT(*) as cnt
                   FROM transactions WHERE transaction_type='income' AND transaction_date >= ?
                   GROUP BY category_primary ORDER BY total DESC""",
                (start_date,)
            ).fetchall()

        total_expense = total_row["total"] or 0.0
        total_count = total_row["cnt"] or 0
        essential_expense = essential_row["total"] or 0.0
        essential_count = essential_row["cnt"] or 0
        non_essential_expense = total_expense - essential_expense
        non_essential_count = total_count - essential_count
        total_income = income_row["total"] or 0.0

        # ── 按分类分组（含必需/弹性金额拆分） ──
        categories = []
        for r in cat_rows:
            cat_total = r["total"] or 0
            cat_essential = r["essential_amount"] or 0
            cat_elastic = cat_total - cat_essential
            categories.append({
                "name": r["category_primary"],
                "amount": round(cat_total, 2),
                "count": r["cnt"],
                "essential": round(cat_essential, 2),
                "elastic": round(cat_elastic, 2),
                "pct": round(cat_total / total_expense * 100, 1) if total_expense > 0 else 0,
            })

        # ── 最近10条消费记录 ──
        recent = conn.execute(
            """SELECT id, transaction_date as date, counterparty, amount, category_primary as category, is_essential, description
               FROM transactions WHERE transaction_type='expense'
               ORDER BY transaction_date DESC, id DESC LIMIT 10"""
        ).fetchall()
        recent_list = [{"id": r["id"], "date": r["date"], "counterparty": r["counterparty"], "amount": r["amount"],
                        "category": r["category"], "is_essential": bool(r["is_essential"]),
                        "description": r["description"]} for r in recent]

        # ── 收入分类 ──
        income_categories = []
        for r in income_cat_rows:
            income_categories.append({
                "name": r["category_primary"], "amount": r["total"],
                "count": r["cnt"], "pct": round(r["total"] / total_income * 100, 1) if total_income > 0 else 0,
            })

        # ── 最近收入记录 ──
        income_recent_rows = conn.execute(
            """SELECT id, transaction_date as date, counterparty, amount, category_primary as category, description
               FROM transactions WHERE transaction_type='income'
               ORDER BY transaction_date DESC, id DESC LIMIT 10"""
        ).fetchall()
        income_recent = [{"id": r["id"], "date": r["date"], "counterparty": r["counterparty"], "amount": r["amount"],
                          "category": r["category"], "description": r["description"]} for r in income_recent_rows]

        # ── 统计截图原始汇总金额 ──
        screenshot_summary_total = 0.0
        try:
            pf_rows = conn.execute(
                "SELECT extra_data FROM processed_files WHERE extra_data IS NOT NULL"
            ).fetchall()
            for pf in pf_rows:
                try:
                    extra = json.loads(pf["extra_data"])
                    val = float(extra.get("total_from_summary", 0))
                    if val > 0:
                        screenshot_summary_total += val
                except Exception:
                    pass
        except Exception:
            pass

        conn.close()

        return JSONResponse(content={
            "success": True,
            "month": query_label,
            "total_expense": round(total_expense, 2),
            "total_income": round(total_income, 2),
            "expense_count": total_count,
            "essential_expense": round(essential_expense, 2),
            "essential_count": essential_count,
            "non_essential_expense": round(non_essential_expense, 2),
            "non_essential_count": non_essential_count,
            "categories": categories,
            "recent": recent_list,
            "screenshot_summary_total": round(screenshot_summary_total, 2),
            "income_categories": income_categories,
            "income_recent": income_recent,
        })
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.get("/api/v1/allocation")
def ve4_api_allocation():
    alloc = ve4_api_compute_allocation()
    return JSONResponse(content=asdict(alloc))


# ─── 数据流监控（替代蓝牙状态） ───

@app.get("/api/v1/dataflow/status")
def ve5_api_dataflow_status():
    """返回数据流状态（incoming/processed/failed 目录统计）"""
    ensure_dirs()
    def _count_dir(d: Path):
        if not d.exists(): return 0
        return sum(1 for _ in d.rglob("*") if _.is_file())
    return JSONResponse(content={
        "incoming": _count_dir(INCOMING_DIR),
        "processed": _count_dir(PROCESSED_DIR),
        "failed": _count_dir(FAILED_DIR),
        "data_dir": str(DATA_DIR),
    })


# ─── 数据同步 ───

def _read_pipeline_marker() -> str:
    """读取最后一次 pipeline 成功写入的时间戳"""
    marker = DATA_DIR / "last_pipeline_success.txt"
    if marker.exists():
        try:
            return marker.read_text().strip()
        except Exception:
            pass
    return ""


@app.get("/api/v1/sync/status")
def ve4_api_sync_status():
    """统计待处理文件（incoming 目录中尚未被 pipeline 处理的文件）"""
    ensure_dirs()
    pending_files = []
    total_size = 0
    if INCOMING_DIR.exists():
        for f in INCOMING_DIR.rglob("*"):
            if f.is_file() and f.suffix.lower() in {'.png','.jpg','.jpeg','.bmp','.gif','.webp',
                                                       '.txt','.csv','.json','.xml','.md',
                                                       '.pdf','.docx','.xlsx','.xls'}:
                pending_files.append({"name": f.name, "size": f.stat().st_size,
                                       "type": f.suffix.lower(), "path": str(f.relative_to(INCOMING_DIR))})
                total_size += f.stat().st_size

    # 也检查 Windows 默认蓝牙接收目录
    extra_dir = Path.home() / "Documents" / "Bluetooth Received Files"
    if extra_dir.exists():
        for f in extra_dir.iterdir():
            if f.is_file() and f.suffix.lower() in {'.png','.jpg','.jpeg','.bmp','.gif','.webp',
                                                       '.txt','.csv','.json','.xml','.md',
                                                       '.pdf','.docx','.xlsx','.xls'}:
                pending_files.append({"name": f.name, "size": f.stat().st_size,
                                       "type": f.suffix.lower(), "path": str(f),
                                       "source": "bluetooth_default"})
                total_size += f.stat().st_size

    return JSONResponse(content={
        "success": True,
        "pending_count": len(pending_files),
        "pending_files": pending_files[:20],
        "total_size_mb": round(total_size / (1024*1024), 2),
        "last_updated": _read_pipeline_marker(),
    })


@app.get("/api/v1/sync/files")
def ve4_api_sync_files():
    """返回所有文件的处理状态，含精细化状态：待分析 / 已保留 / 已覆盖 / 处理失败"""
    ensure_dirs()
    import sqlite3 as _sql
    from pathlib import Path as _Path
    conn = _sql.connect(str(DB_PATH))
    conn.row_factory = _sql.Row

    # 读取已处理文件记录（按文件名索引，因为 processed_files 存的是完整路径）
    processed_db = {}
    try:
        for row in conn.execute("SELECT file_path, processed_at, rag_synced FROM processed_files").fetchall():
            fname = _Path(row["file_path"]).name
            processed_db[fname] = {"processed_at": row["processed_at"], "rag_synced": row["rag_synced"]}
    except Exception:
        pass

    # 查询每个 source_file 在 asset_holdings 中的记录数（判断数据是否仍被保留）
    holdings_by_source = {}
    try:
        for row in conn.execute("SELECT source_file, COUNT(*) as cnt FROM asset_holdings GROUP BY source_file").fetchall():
            holdings_by_source[_Path(row["source_file"]).name] = row["cnt"]
    except Exception:
        pass

    # 查询每个 source_file 在 transactions 中的记录数和类型
    tx_by_source = {}
    try:
        for row in conn.execute("SELECT source_file, transaction_type, COUNT(*) as cnt FROM transactions GROUP BY source_file, transaction_type").fetchall():
            fname = _Path(row["source_file"]).name
            if fname not in tx_by_source:
                tx_by_source[fname] = {}
            tx_by_source[fname][row["transaction_type"]] = row["cnt"]
    except Exception:
        pass
    conn.close()

    files = []

    # 1. incoming 目录（待分析）
    if INCOMING_DIR.exists():
        for f in INCOMING_DIR.rglob("*"):
            if f.is_file():
                rel = str(f.relative_to(INCOMING_DIR))
                files.append({
                    "name": f.name,
                    "path": rel,
                    "size": f.stat().st_size,
                    "status": "pending",
                    "status_label": "待分析",
                    "status_color": "warning",
                    "processed_at": None,
                    "extracted_count": 0,
                    "source": "upload",
                })

    # 2. processed 目录（已处理）—— 细分为"已保留"和"已覆盖"
    processed_dir = INCOMING_DIR.parent / "processed"
    if processed_dir.exists():
        for f in processed_dir.rglob("*"):
            if f.is_file():
                rel = str(f.relative_to(processed_dir))
                db_info = processed_db.get(f.name, {})
                holding_count = holdings_by_source.get(f.name, 0)
                tx_info = tx_by_source.get(f.name, {})
                tx_count = sum(tx_info.values())
                tx_types = []
                if tx_info.get("expense", 0) > 0:
                    tx_types.append(f"消费{tx_info['expense']}条")
                if tx_info.get("income", 0) > 0:
                    tx_types.append(f"收入{tx_info['income']}条")

                if holding_count > 0 or tx_count > 0:
                    status = "retained"
                    status_label = "已保留"
                    status_color = "success"
                    detail_parts = []
                    if holding_count > 0:
                        detail_parts.append(f"持仓 {holding_count} 条")
                    if tx_types:
                        detail_parts.append("、".join(tx_types))
                    detail = "数据库中保留 " + "，".join(detail_parts)
                else:
                    status = "overwritten"
                    status_label = "已覆盖"
                    status_color = "info"
                    detail = "数据已被后续文件更新覆盖"
                files.append({
                    "name": f.name,
                    "path": rel,
                    "size": f.stat().st_size,
                    "status": status,
                    "status_label": status_label,
                    "status_color": status_color,
                    "processed_at": db_info.get("processed_at"),
                    "rag_synced": db_info.get("rag_synced", False),
                    "detail": detail,
                    "source": "processed",
                })

    # 3. failed 目录（失败）—— 区分 LLM 超时和其他错误
    failed_dir = INCOMING_DIR.parent / "failed"
    if failed_dir.exists():
        for f in failed_dir.rglob("*"):
            if f.is_file():
                rel = str(f.relative_to(failed_dir))
                is_timeout = "llm_timeout" in rel or "\\llm_timeout\\" in rel
                files.append({
                    "name": f.name,
                    "path": rel,
                    "size": f.stat().st_size,
                    "status": "llm_timeout" if is_timeout else "expired",
                    "status_label": "LLM超时" if is_timeout else "失效",
                    "status_color": "warn" if is_timeout else "error",
                    "processed_at": None,
                    "detail": "LLM 调用超时，建议重试" if is_timeout else "提取异常或数据格式已失效，建议重新上传",
                    "source": "failed",
                })

    # 统计
    stats = {
        "pending": sum(1 for f in files if f["status"] == "pending"),
        "retained": sum(1 for f in files if f["status"] == "retained"),
        "overwritten": sum(1 for f in files if f["status"] == "overwritten"),
        "expired": sum(1 for f in files if f["status"] == "expired"),
        "llm_timeout": sum(1 for f in files if f["status"] == "llm_timeout"),
    }

    return JSONResponse(content={
        "success": True,
        "files": files,
        "stats": stats,
    })


@app.post("/api/v1/sync/retry")
def ve4_api_sync_retry(file_name: str = ""):
    """将失败的截图文件移回 incoming 目录，等待重新处理。"""
    if not file_name:
        return JSONResponse(content={"success": False, "error": "请指定 file_name"})
    
    import shutil
    
    # 在 failed/ 目录中查找文件
    found = None
    if FAILED_DIR.exists():
        for f in FAILED_DIR.rglob(file_name):
            if f.is_file():
                found = f
                break
    
    if not found:
        return JSONResponse(content={"success": False, "error": f"未找到文件: {file_name}"})
    
    try:
        # 移回 incoming/images/
        target = INCOMING_DIR / "images" / file_name
        INCOMING_DIR.mkdir(parents=True, exist_ok=True)
        (INCOMING_DIR / "images").mkdir(parents=True, exist_ok=True)
        shutil.move(str(found), str(target))
        
        # 删除数据库中的失败记录（如果有）
        import sqlite3
        conn = sqlite3.connect(str(DB_PATH))
        try:
            conn.execute("DELETE FROM processed_files WHERE file_path LIKE ?", (f"%{file_name}",))
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()
        
        logger.info(f"[API] 重试: {file_name} 已移回 incoming")
        return JSONResponse(content={"success": True, "file": file_name})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.delete("/api/v1/sync/data")
def ve4_api_sync_delete_data(source: str = "", file_name: str = ""):
    """删除提取数据，同时删除磁盘上的截图文件。

    两种模式：
    1. source=all → 删除所有提取数据 + 删除所有截图文件
    2. file_name=xxx → 删除指定文件的数据 + 删除该截图文件
    """
    import sqlite3 as _sql
    from pathlib import Path as _Path

    if not source and not file_name:
        return JSONResponse(content={"success": False, "error": "请指定 source=all 清空全部数据，或 file_name=xxx 删除特定文件数据"})

    conn = _sql.connect(str(DB_PATH))
    deleted_counts = {}
    deleted_files = []
    try:
        if source == "all":
            # 清空所有与文件提取相关的表
            for table in ["asset_holdings", "transactions", "processed_files"]:
                try:
                    deleted_counts[table] = conn.execute(f"DELETE FROM {table}").rowcount
                except Exception:
                    deleted_counts[table] = 0
            # 同时清空 SQLite RAG 数据
            try:
                from core.rag_sqlite_store import fin_clear, expense_stats
                fin_clear()
                deleted_counts["rag_financial"] = "cleared"
            except Exception:
                pass
            # 也清空 screenshot_descriptions
            try:
                import shutil
                desc_dir = DATA_DIR / "screenshot_descriptions"
                if desc_dir.exists():
                    shutil.rmtree(desc_dir)
                    desc_dir.mkdir()
                deleted_counts["descriptions"] = "cleared"
            except Exception:
                pass
            try:
                int_dir = DATA_DIR / "intermediate_tables"
                if int_dir.exists():
                    import shutil
                    shutil.rmtree(int_dir)
                    int_dir.mkdir()
            except Exception:
                pass
            # 删除磁盘上的所有截图文件（processed/ 和 failed/ 目录）
            for base_dir in [PROCESSED_DIR, FAILED_DIR]:
                if base_dir.exists():
                    for f in base_dir.rglob("*"):
                        if f.is_file():
                            try:
                                f.unlink()
                                deleted_files.append(str(f.name))
                            except Exception:
                                pass
                    # 清理空子目录
                    for sub in sorted(base_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                        if sub.is_dir() and sub != base_dir:
                            try:
                                sub.rmdir()
                            except Exception:
                                pass

        elif file_name:
            # 删除特定文件的数据
            pattern = f"%{file_name}"
            for table in ["asset_holdings", "transactions", "processed_files"]:
                try:
                    cur = conn.execute(f"DELETE FROM {table} WHERE source_file LIKE ?", (pattern,))
                    deleted_counts[table] = cur.rowcount
                except Exception:
                    deleted_counts[table] = 0
            # 清理 processed_files 中的 file_path 记录
            try:
                cur = conn.execute("DELETE FROM processed_files WHERE file_path LIKE ?", (pattern,))
                deleted_counts["processed_files_path"] = cur.rowcount
            except Exception:
                pass
            # 清理 SQLite RAG + descriptions
            try:
                from core.rag_sqlite_store import fin_delete_by_source
                fin_delete_by_source(pattern)
                deleted_counts["rag_financial"] = "deleted"
            except Exception:
                pass
            try:
                desc_dir = DATA_DIR / "screenshot_descriptions"
                if desc_dir.exists():
                    base = Path(file_name).stem if file_name else ""
                    for f in desc_dir.glob(f"*{base}*"):
                        f.unlink()
            except Exception:
                pass
            # 删除磁盘上的截图文件（在 processed/ 和 failed/ 中查找）
            for base_dir in [PROCESSED_DIR, FAILED_DIR]:
                if base_dir.exists():
                    for f in base_dir.rglob(file_name):
                        if f.is_file():
                            try:
                                f.unlink()
                                deleted_files.append(str(f.name))
                            except Exception:
                                pass

        conn.commit()
        logger.info(f"[API] 删除数据完成: source={source}, file_name={file_name}, counts={deleted_counts}, files={deleted_files}")

        # 记录活动日志
        try:
            _record_activity_internal(
                text=f"清空了全部数据（{len(deleted_files)} 个文件）" if source == "all" else f"删除了截图 {file_name} 及其提取数据",
                badge="已删除",
                badge_type="warn",
            )
        except Exception:
            pass

        return JSONResponse(content={
            "success": True,
            "deleted": deleted_counts,
            "deleted_files": deleted_files,
            "message": f"全部数据已清空（删除 {len(deleted_files)} 个文件）" if source == "all" else f"文件 {file_name} 及其截图已删除",
        })
    except Exception as e:
        conn.rollback()
        logger.error(f"[API] 删除数据失败: {e}")
        return JSONResponse(content={"success": False, "error": str(e)}, status_code=500)
    finally:
        conn.close()


@app.get("/api/v1/ocr/backends")
def ve4_api_ocr_backends():
    """返回可用 OCR 引擎列表及状态（含双轨模式和预处理依赖信息）"""
    from receiver.ocr_backends import ve4_ocr_get_settings
    settings = ve4_ocr_get_settings()
    # 同时读取用户隐私设置
    allow_cloud = _read_ocr_privacy_setting()
    settings["allow_cloud_for_privacy"] = allow_cloud
    return JSONResponse(content={"success": True, **settings})


@app.post("/api/v1/ocr/set-backend")
def ve4_api_ocr_set_backend(body: dict):
    """启用/禁用 OCR 引擎"""
    from receiver.ocr_backends import ve4_ocr_set_backend
    name = body.get("name", "")
    enabled = body.get("enabled", True)
    ok = ve4_ocr_set_backend(name, enabled)
    return JSONResponse(content={"success": ok, "name": name, "enabled": enabled})


@app.post("/api/v1/ocr/privacy-setting")
def ve4_api_ocr_privacy_setting(body: dict):
    """设置是否允许云端模型处理含隐私数据的截图"""
    import sqlite3
    allow = body.get("allow_cloud_for_privacy", False)
    db_path = str(DB_PATH)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ocr_settings (key TEXT PRIMARY KEY, value TEXT)
    """)
    conn.execute(
        "INSERT OR REPLACE INTO ocr_settings (key, value) VALUES (?, ?)",
        ("allow_cloud_for_privacy", "true" if allow else "false")
    )
    conn.commit()
    conn.close()
    return JSONResponse(content={"success": True, "allow_cloud_for_privacy": allow})


def _read_ocr_privacy_setting() -> bool:
    """读取是否允许云端模型处理含隐私数据的截图"""
    import sqlite3
    try:
        db_path = str(DB_PATH)
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT value FROM ocr_settings WHERE key = 'allow_cloud_for_privacy'"
            ).fetchone()
            conn.close()
            if row:
                return row[0].lower() in ('true', '1', 'yes')
        except Exception:
            conn.close()
    except Exception:
        pass
    return False


@app.post("/api/v1/upload")
async def ve5_api_upload_files(files: List[UploadFile] = File(...)):
    """
    直接上传文件至 incoming 目录。
    支持图片(png/jpg/jpeg/bmp/gif/webp)和文本(txt/csv/json/xml/md)。
    上传后可调用 /api/v1/sync/run 触发处理。
    """
    ensure_dirs()
    saved = []
    failed = []
    allowed_exts = {'.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp',
                    '.txt', '.csv', '.json', '.xml', '.md'}

    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in allowed_exts:
            failed.append({"file": f.filename, "error": f"不支持的文件类型: {ext}"})
            continue
        # 图片放 images/，文本放 texts/
        subdir = "images" if ext in {'.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp'} else "texts"
        dest_dir = INCOMING_DIR / subdir
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / (f.filename or "unnamed")
        # 避免覆盖同名文件
        if dest.exists():
            stem = dest.stem
            dest = dest_dir / f"{stem}_{datetime.now().strftime('%H%M%S')}{ext}"
        try:
            content = await f.read()
            with open(dest, "wb") as fp:
                fp.write(content)
            saved.append({"file": dest.name, "path": str(dest.relative_to(DATA_DIR))})
            logger.info(f"[UPLOAD] 保存文件: {dest.name} -> {dest}")
        except Exception as e:
            failed.append({"file": f.filename, "error": str(e)})

    return JSONResponse(content={
        "success": len(saved) > 0,
        "uploaded": len(saved),
        "failed": len(failed),
        "saved": saved,
        "errors": failed,
    })


@app.post("/api/v1/sync/run")
async def ve4_api_sync_run():
    """触发同步：扫描 incoming 目录，逐个文件调用 pipeline 处理"""
    ensure_dirs()
    try:
        from receiver.pipeline import process_file

        processed = 0
        skipped = 0
        failed = 0
        errors = []

        # 收集待处理文件
        files_to_process = []
        if INCOMING_DIR.exists():
            for f in INCOMING_DIR.rglob("*"):
                if f.is_file() and f.suffix.lower() in {'.png','.jpg','.jpeg','.bmp','.gif','.webp',
                                                           '.txt','.csv','.json','.xml','.md',
                                                           '.pdf','.docx','.xlsx','.xls'}:
                    files_to_process.append(f)

        # Windows 默认蓝牙目录
        extra_dir = Path.home() / "Documents" / "Bluetooth Received Files"
        if extra_dir.exists():
            for f in list(extra_dir.iterdir()):
                if f.is_file() and f.suffix.lower() in {'.png','.jpg','.jpeg','.bmp','.gif','.webp',
                                                           '.txt','.csv','.json','.xml','.md',
                                                           '.pdf','.docx','.xlsx','.xls'}:
                    dest = INCOMING_DIR / f.name
                    if not dest.exists():
                        shutil.copy2(str(f), str(dest))
                        files_to_process.append(dest)

        for f in files_to_process:
            try:
                result = process_file(f)
                if result and result.get("status") == "success":
                    processed += 1
                elif result and result.get("status") == "skipped":
                    skipped += 1
                else:
                    failed += 1
                    errors.append({"file": f.name, "error": (result or {}).get("error", "未知错误")})
            except Exception as e:
                failed += 1
                errors.append({"file": f.name, "error": str(e)})
                logger.error(f"[SYNC] 处理失败: {f.name} - {e}")

        # 记录批量同步汇总活动
        if processed > 0 or failed > 0:
            try:
                _record_activity_internal(
                    text=f"批量同步完成：处理 {processed} 个文件（成功 {processed}，跳过 {skipped}，失败 {failed}）",
                    badge="同步完成",
                    badge_type="success" if failed == 0 else "warn",
                )
            except Exception:
                pass

        return JSONResponse(content={
            "success": True,
            "processed": processed,
            "skipped": skipped,
            "failed": failed,
            "errors": errors[:10],
        })
    except ImportError as e:
        return JSONResponse(content={"success": False, "error": f"pipeline 模块不可用: {e}"})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.get("/api/v1/model/stats")
def ve4_api_model_stats():
    """返回本地模型调用统计（从 model_client 单例读取）"""
    try:
        from receiver.model_client import ve4_model_get_stats
        stats = ve4_model_get_stats()
        return JSONResponse(content={
            "total_calls": stats.total_calls,
            "cache_hits": stats.cache_hits,
            "avg_duration_ms": stats.avg_duration_ms,
            "cache_rate": stats.cache_rate,
        })
    except Exception as e:
        return JSONResponse(content={"error": str(e), "total_calls": 0})


# ─── 数据写入接口（供 pipeline 调用）───

def _record_activity_internal(text: str, badge: str = "", badge_type: str = ""):
    """内部辅助：记录活动日志（不依赖 HTTP 请求）"""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("""
            INSERT INTO activity_log (time, text, badge, badge_type)
            VALUES (?, ?, ?, ?)
        """, (datetime.now().strftime("%H:%M"), text, badge, badge_type))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"[API] 记录活动日志失败: {e}")


@app.post("/api/v1/activities/record")
def ve4_api_record_activity(activity: dict):
    """
    记录活动日志（供 pipeline.py 在处理完成后调用）。
    示例 payload:
        {"text": "提取了东方财富截图，获得 8 条持仓", "badge": "已入库", "badge_type": "success"}
    """
    conn = ve4_api_get_db()
    try:
        conn.execute("""
            INSERT INTO activity_log (time, text, badge, badge_type)
            VALUES (?, ?, ?, ?)
        """, (datetime.now().strftime("%H:%M"), activity.get("text"), activity.get("badge"), activity.get("badge_type")))
        conn.commit()
        return {"status": "ok"}
    finally:
        conn.close()


# ─── 模块2: 资产分类接口 ───

@app.get("/api/v1/asset/summary")
def ve4_api_asset_summary():
    """已分类资产摘要 + 未分类资产统计"""
    try:
        from receiver.asset_classifier import VE4AssetClassifier
        classifier = VE4AssetClassifier()
        return JSONResponse(content=classifier.ve4_asset_get_classified_summary())
    except Exception as e:
        return JSONResponse(content={"classified": {}, "unclassified": {"value": 0, "count": 0}, "error": str(e)})


@app.get("/api/v1/asset/unclassified")
def ve4_api_asset_unclassified(limit: int = 20):
    """未分类资产清单（供用户补全）"""
    try:
        from receiver.asset_classifier import VE4AssetClassifier
        classifier = VE4AssetClassifier()
        return JSONResponse(content=classifier.ve4_asset_get_unclassified_list(limit))
    except Exception as e:
        return JSONResponse(content=[])


@app.get("/api/v1/asset/holdings")
def ve4_api_asset_holdings(asset_class: str = None, limit: int = 50):
    """全部持仓列表，可按资产大类筛选"""
    try:
        conn = ve4_api_get_db()
        conn.row_factory = sqlite3.Row
        if asset_class:
            rows = conn.execute("""
                SELECT * FROM asset_holdings
                WHERE asset_class = ? AND is_classified = 1
                ORDER BY current_value DESC
                LIMIT ?
            """, (asset_class, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM asset_holdings
                ORDER BY is_classified DESC, current_value DESC
                LIMIT ?
            """, (limit,)).fetchall()
        conn.close()
        return JSONResponse(content=[dict(r) for r in rows])
    except Exception as e:
        return JSONResponse(content=[])


@app.get("/api/v1/account/overview")
def ve4_api_account_overview():
    """账户总览：返回所有持仓完整信息，含来源、位置等"""
    try:
        conn = ve4_api_get_db()
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT id, product_name, current_value, asset_class, sub_class,
                   liquidity_level, risk_level, account_key, source_file,
                   inference_source, batch_id, purchase_date, product_code,
                   quantity, cost_basis, holding_return_pct, unrealized_pnl,
                   annualized_return_pct, updated_at, created_at
            FROM asset_holdings
            WHERE current_value > 0
            ORDER BY current_value DESC
        """).fetchall()
        conn.close()

        result = []
        for r in rows:
            d = dict(r)
            # 从 source_file 文件名推断时间戳
            d["source_time"] = _infer_source_time(d.get("source_file", ""))
            # 格式化 updated_at（iso → 友好格式）
            raw_updated = d.get("updated_at", "")
            if raw_updated and "T" in raw_updated:
                try:
                    d["updated_at"] = raw_updated[:10].replace("-", "-") + " " + raw_updated[11:16]
                except Exception:
                    pass
            # 从 account_key 推断位置名称
            d["account_name"] = _map_account_key(d.get("account_key", ""))
            d["account_type"] = _infer_account_type(d.get("account_key", ""))
            result.append(d)

        return JSONResponse(content={
            "success": True,
            "total": len(result),
            "total_value": sum(r.get("current_value", 0) for r in result),
            "holdings": result,
        })
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e), "holdings": []})


def _infer_source_time(source_file: str) -> str:
    """从文件名推断截图时间（手动更新的条目不推断，由 updated_at 展示）"""
    import re
    if not source_file or "手动" in source_file:
        return ""
    # 匹配 Screenshot_2026_0710_205801.jpg 格式
    m = re.search(r'(\d{4})_(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})', source_file)
    if m:
        y, mo, d, h, mi, s = m.groups()
        return f"{y}-{mo}-{d} {h}:{mi}:{s}"
    # 匹配 YYYYMMDD 或类似格式
    m = re.search(r'(\d{4})[\-_]?(\d{2})[\-_]?(\d{2})', source_file)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""


def _map_account_key(key: str) -> str:
    """将 account_key 映射为可读的账户名"""
    if not key:
        return "未分类"
    mapping = {
        "招商银行": "招商银行", "工行": "工商银行", "建行": "建设银行",
        "农行": "农业银行", "中行": "中国银行", "交行": "交通银行",
        "华泰": "华泰证券", "中信": "中信证券", "国泰": "国泰君安",
        "海通": "海通证券", "广发": "广发证券", "东方财富": "东方财富",
        "微信": "微信零钱", "支付宝": "支付宝余额", "余额宝": "余额宝",
    }
    for k, v in mapping.items():
        if k in key:
            return v
    return key


def _infer_account_type(key: str) -> str:
    """推断账户类型：bank/securities/wallet/fund/other"""
    if not key:
        return "unknown"
    bank_kw = ["银行", "招商", "工行", "建行", "农行", "中行", "交行", "邮政", "浦发"]
    for kw in bank_kw:
        if kw in key:
            return "bank"
    sec_kw = ["证券", "华泰", "中信", "国泰", "海通", "广发", "东方财富"]
    for kw in sec_kw:
        if kw in key:
            return "securities"
    wallet_kw = ["微信", "支付宝", "余额宝", "零钱"]
    for kw in wallet_kw:
        if kw in key:
            return "wallet"
    fund_kw = ["基金", "fund", "天天基金", "蚂蚁财富", "且慢", "蛋卷"]
    for kw in fund_kw:
        if kw in key:
            return "fund"
    return "other"


@app.post("/api/v1/asset/classify")
def ve4_api_asset_classify_manual(payload: dict):
    """
    用户手动补全分类。
    payload: {"id": 123, "asset_class": "equity", "liquidity": "high", "risk": "medium_high"}
    """
    try:
        conn = ve4_api_get_db()
        conn.execute("""
            UPDATE asset_holdings
            SET asset_class = ?, liquidity_level = ?, risk_level = ?,
                is_classified = 1, classification_confidence = 1.0,
                user_overridden = 1, updated_at = ?
            WHERE id = ?
        """, (
            payload.get("asset_class"),
            payload.get("liquidity"),
            payload.get("risk"),
            datetime.now().isoformat(),
            payload.get("id"),
        ))
        conn.commit()
        conn.close()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/v1/holdings")
def ve4_api_add_holding(payload: dict):
    """
    手动新增持仓记录。
    payload: {
        product_name, current_value, account_key, asset_class,
        sub_class, liquidity_level, risk_level, product_code, source_bank,
        quantity, cost_basis
    }
    """
    from datetime import datetime as _dt
    try:
        conn = ve4_api_get_db()
        now = _dt.now().isoformat()
        conn.execute("""
            INSERT INTO asset_holdings
                (product_name, current_value, account_key, asset_class,
                 sub_class, liquidity_level, risk_level, product_code, source_bank,
                 quantity, cost_basis, source_file, is_classified, user_overridden,
                 inference_source, updated_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '用户手动更新', 1, 1, '用户手动更新', ?, ?)
        """, (
            payload.get("product_name", ""),
            payload.get("current_value", 0),
            payload.get("account_key", ""),
            payload.get("asset_class", ""),
            payload.get("sub_class", ""),
            payload.get("liquidity_level", ""),
            payload.get("risk_level", ""),
            payload.get("product_code", ""),
            payload.get("source_bank", ""),
            payload.get("quantity", 0),
            payload.get("cost_basis", 0),
            now, now
        ))
        # 确保 accounts 表有对应条目
        acct_key = payload.get("account_key", "")
        if acct_key:
            existing = conn.execute("SELECT id FROM accounts WHERE account_key = ?", (acct_key,)).fetchone()
            if not existing:
                acct_type = "securities" if "证券" in acct_key else "bank" if "银行" in acct_key else "other"
                conn.execute(
                    "INSERT OR IGNORE INTO accounts (account_key, name, type, icon, icon_color, last_sync) VALUES (?, ?, ?, '', '', ?)",
                    (acct_key, acct_key, acct_type, now)
                )
        conn.commit()
        row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        return {"status": "ok", "id": row_id}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/v1/transactions")
def ve4_api_add_transaction(payload: dict):
    """
    手动新增收支记录。
    payload: {
        transaction_date, transaction_type ('income'|'expense'),
        amount, counterparty, category_primary, category_secondary,
        description, is_essential
    }
    """
    from datetime import datetime as _dt
    try:
        conn = ve4_api_get_db()
        # 确保 is_essential 列存在
        cols = [r[1] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()]
        if "is_essential" not in cols:
            conn.execute("ALTER TABLE transactions ADD COLUMN is_essential INTEGER DEFAULT 0")

        now = _dt.now().isoformat()
        is_essential = 1 if payload.get("is_essential") else 0
        conn.execute("""
            INSERT INTO transactions
                (transaction_date, transaction_type, amount, counterparty,
                 category_primary, category_secondary, description,
                 source_file, is_essential, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, '用户手动更新', ?, ?)
        """, (
            payload.get("transaction_date", now[:10]),
            payload.get("transaction_type", "expense"),
            payload.get("amount", 0),
            payload.get("counterparty", ""),
            payload.get("category_primary", ""),
            payload.get("category_secondary", ""),
            payload.get("description", ""),
            is_essential, now
        ))
        row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        conn.close()
        return {"status": "ok", "id": row_id}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.put("/api/v1/holdings/{holding_id}")
def ve4_api_override_holding(holding_id: int, payload: dict):
    """
    用户手动修改持仓数据。支持修改任意字段。
    当金额被修改时，自动生成"其它自定义项"记录差额。
    payload: {
        product_name, current_value, account_key, asset_class,
        sub_class, product_code, quantity, cost_basis, note
    }
    """
    try:
        conn = ve4_api_get_db()
        row = conn.execute("SELECT * FROM asset_holdings WHERE id = ?", (holding_id,)).fetchone()
        if not row:
            conn.close()
            return {"status": "error", "message": "持仓记录不存在"}

        old_value = row["current_value"]
        old_name = row["product_name"]
        old_account = row["account_key"]
        now = datetime.now().isoformat()

        # 构建更新字段
        fields = []
        params = []
        updatable = ["product_name", "current_value", "account_key", "asset_class",
                     "sub_class", "liquidity_level", "risk_level", "product_code",
                     "quantity", "cost_basis", "source_bank"]
        for key in updatable:
            if key in payload:
                fields.append(f"{key} = ?")
                params.append(payload[key])
        fields.append("user_overridden = 1")
        fields.append("updated_at = ?")
        params.append(now)
        params.append(holding_id)

        conn.execute(f"UPDATE asset_holdings SET {', '.join(fields)} WHERE id = ?", params)

        new_value = payload.get("current_value", old_value)
        diff = new_value - old_value

        # 金额有变化时，生成"其它自定义项"记录差额
        if abs(diff) > 0.01:
            adj_name = f"其它自定义项({old_name}调整)"
            conn.execute("""
                INSERT INTO asset_holdings
                    (product_name, current_value, account_key, asset_class,
                     sub_class, source_file, is_classified, user_overridden,
                     inference_source, updated_at, created_at)
                VALUES (?, ?, ?, 'alternative', '', '差额自动调整', 1, 1, '金额调整差额', ?, ?)
            """, (adj_name, diff, old_account, now, now))
            adj_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        else:
            adj_id = None

        conn.commit()
        conn.close()

        _save_user_correction({
            "holding_id": holding_id,
            "product_name": old_name,
            "account_key": old_account,
            "old_value": old_value,
            "new_value": new_value,
            "diff": diff,
            "adjustment_id": adj_id,
            "note": payload.get("note", ""),
            "corrected_at": now,
        })

        return {"status": "ok", "old_value": old_value, "new_value": new_value, "diff": diff, "adjustment_id": adj_id}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.delete("/api/v1/holdings/{holding_id}")
def ve5_api_delete_holding(holding_id: int):
    """删除持仓记录，同时复活被它覆盖的旧记录"""
    try:
        conn = ve4_api_get_db()
        row = conn.execute("SELECT id, source_file, product_name FROM asset_holdings WHERE id = ?", (holding_id,)).fetchone()
        if not row:
            conn.close()
            return {"status": "error", "message": "持仓记录不存在"}

        src = row["source_file"]
        # 删除当前记录
        conn.execute("DELETE FROM asset_holdings WHERE id = ?", (holding_id,))

        # 复活被同一 source_file 覆盖的旧记录
        reactivated = conn.execute(
            "UPDATE asset_holdings SET is_superseded=0, superseded_by='' WHERE superseded_by=? AND is_superseded=1",
            (src,)
        ).rowcount

        conn.commit()
        conn.close()
        msg = "已删除"
        if reactivated > 0:
            msg += f"，同时复活了 {reactivated} 条被覆盖的旧数据"
        # 重新生成 allocation 快照
        try: ve4_api_allocation_detail_generate()
        except: pass
        return {"status": "ok", "message": msg, "reactivated": reactivated}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.put("/api/v1/transactions/{transaction_id}")
def ve5_api_update_transaction(transaction_id: int, payload: dict):
    """
    修改收支记录。
    payload: {transaction_date, transaction_type, amount, counterparty, category_primary, category_secondary, description, is_essential}
    """
    try:
        conn = ve4_api_get_db()
        row = conn.execute("SELECT id FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
        if not row:
            conn.close()
            return {"status": "error", "message": "记录不存在"}

        fields = []
        params = []
        updatable = ["transaction_date", "transaction_type", "amount", "counterparty",
                     "category_primary", "category_secondary", "description", "is_essential"]
        for key in updatable:
            if key in payload:
                fields.append(f"{key} = ?")
                params.append(payload[key])
        if not fields:
            conn.close()
            return {"status": "error", "message": "没有要修改的字段"}

        fields.append("updated_at = ?")
        params.append(datetime.now().isoformat())
        params.append(transaction_id)

        conn.execute(f"UPDATE transactions SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()
        conn.close()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.delete("/api/v1/transactions/{transaction_id}")
def ve5_api_delete_transaction(transaction_id: int):
    """删除收支记录"""
    try:
        conn = ve4_api_get_db()
        row = conn.execute("SELECT id FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
        if not row:
            conn.close()
            return {"status": "error", "message": "记录不存在"}
        conn.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,))
        conn.commit()
        conn.close()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _save_user_correction(record: dict):
    """追加用户修正记录到 user_corrections.json"""
    import json as _json
    from app_paths import DATA_DIR
    corrections_file = DATA_DIR / "user_corrections.json"
    corrections = []
    if corrections_file.exists():
        try:
            corrections = _json.loads(corrections_file.read_text(encoding="utf-8"))
        except Exception:
            corrections = []
    corrections.append(record)
    corrections_file.write_text(_json.dumps(corrections, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/api/v1/holdings/pnl_summary")
def ve4_api_pnl_summary():
    """获取各账户的持仓盈亏汇总"""
    try:
        conn = ve4_api_get_db()
        rows = ve4_api_safe_query(conn, """
            SELECT account_key,
                   SUM(unrealized_pnl) as total_pnl,
                   SUM(CASE WHEN unrealized_pnl > 0 THEN unrealized_pnl ELSE 0 END) as total_profit,
                   SUM(CASE WHEN unrealized_pnl < 0 THEN ABS(unrealized_pnl) ELSE 0 END) as total_loss
            FROM asset_holdings
            WHERE unrealized_pnl != 0
            GROUP BY account_key
        """)
        conn.close()
        result = []
        for row in rows:
            result.append({
                "account_key": row.get("account_key", ""),
                "total_pnl": round(row.get("total_pnl", 0), 2),
                "total_profit": round(row.get("total_profit", 0), 2),
                "total_loss": round(row.get("total_loss", 0), 2),
            })
        return result
    except Exception as e:
        return []


@app.put("/api/v1/accounts/override_balance")
def ve4_api_override_account_balance(payload: dict):
    """
    用户手动调整账户总余额。
    按比例缩放该账户下所有持仓的 current_value。
    标记 user_overridden=1，记录到 user_corrections.json。
    payload: {"account_key": "招商银行", "new_balance": 65000, "note": "手动调整"}
    """
    try:
        account_key = payload.get("account_key", "")
        new_balance = float(payload.get("new_balance", 0))
        note = payload.get("note", "手动调整账户余额")
        if not account_key or new_balance <= 0:
            return {"status": "error", "message": "参数无效"}

        conn = ve4_api_get_db()
        # 获取当前总和
        row = conn.execute(
            "SELECT SUM(current_value) as total FROM asset_holdings WHERE account_key = ?",
            (account_key,)
        ).fetchone()
        old_total = row["total"] or 0
        if old_total <= 0:
            conn.close()
            return {"status": "error", "message": "该账户无持仓数据"}

        # 按比例缩放
        ratio = new_balance / old_total
        conn.execute("""
            UPDATE asset_holdings
            SET current_value = ROUND(current_value * ?, 2),
                unrealized_pnl = ROUND(unrealized_pnl * ?, 2),
                user_overridden = 1, user_note = ?, updated_at = ?
            WHERE account_key = ? AND user_overridden = 0
        """, (ratio, ratio, note, datetime.now().isoformat(), account_key))
        conn.commit()
        conn.close()

        _save_user_correction({
            "account_key": account_key,
            "old_balance": old_total,
            "new_balance": new_balance,
            "ratio": round(ratio, 6),
            "note": note,
            "corrected_at": datetime.now().isoformat(),
        })

        return {"status": "ok", "old_balance": old_total, "new_balance": new_balance, "ratio": round(ratio, 4)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ─── AI 配置中心 ───

@app.get("/api/v1/ai/config")
def ve4_api_ai_config():
    """返回当前 AI 配置摘要（provider 列表、状态、是否可用）"""
    try:
        from core.ai_gateway import ve4_ai_get_providers

        providers = ve4_ai_get_providers()
        # 健康检查（并发所有 provider）
        import concurrent.futures
        health_results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            future_map = {
                pool.submit(ve4_ai_health_check_single, name): name
                for name in providers
            }
            for future in concurrent.futures.as_completed(future_map):
                name = future_map[future]
                try:
                    health_results[name] = future.result()
                except Exception as e:
                    health_results[name] = {"status": "error", "error": str(e)}

        return JSONResponse(content={
            "providers": providers,
            "health": health_results,
            "defaults": ve4_ai_get_defaults_call(),
        })
    except ImportError:
        return JSONResponse(content={
            "providers": {},
            "health": {},
            "defaults": {},
            "warning": "AI 配置中心未安装（pip install pyyaml）",
        })
    except Exception as e:
        return JSONResponse(content={"error": str(e)})


def ve4_ai_health_check_single(name: str) -> dict:
    """单个 provider 的健康检查（线程安全）"""
    try:
        from core.ai_gateway import ve4_ai_health_check
        result = ve4_ai_health_check(name)
        return result.get(name, {"status": "error", "error": "no result"})
    except Exception as e:
        return {"status": "error", "error": str(e)}


def ve4_ai_get_defaults_call() -> dict:
    """获取配置中心默认参数"""
    try:
        from core.ai_gateway import ve4_ai_get_defaults
        return ve4_ai_get_defaults()
    except Exception:
        return {}


# ─── 离线快照导出 ───

def ve4_api_save_snapshot(name: str, data: dict):
    """
    将 API 数据保存为离线快照 JSON（前端离线时读取）。
    快照存入 userdata/snapshots/，与代码隔离。
    """
    try:
        SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        filepath = SNAPSHOTS_DIR / f"{name}.json"
        data["_snapshot_at"] = datetime.now().isoformat()
        data["_snapshot_version"] = "1.0"
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.debug(f"[SNAPSHOT] 已保存快照：{filepath.name}")
    except Exception as e:
        logger.debug(f"[SNAPSHOT] 保存失败：{e}")


# 在关键 API 端点添加快照导出（替换原返回逻辑）

@app.get("/api/v1/dashboard/stats")
def ve4_api_dashboard_stats():
    stats = ve4_api_compute_stats()
    ve4_api_save_snapshot("dashboard_stats", asdict(stats))
    return JSONResponse(content=asdict(stats))


@app.get("/api/v1/accounts")
def ve4_api_accounts():
    accounts = ve4_api_compute_accounts()
    ve4_api_save_snapshot("accounts", [asdict(a) for a in accounts])
    return JSONResponse(content=[asdict(a) for a in accounts])


@app.api_route("/api/v1/allocation/report", methods=["GET", "POST"])
async def ve4_api_allocation_report(request: Request, age: int = None, risk_preference: str = None, emergency_months: int = 3):
    try:
        from core.allocation_engine import ve4_alloc_generate_report, VE4RiskPreference
        emergency_target = 0.0
        if request.method == "POST":
            try:
                body = await request.json()
                emergency_target = float(body.get("emergency_target", 0) or 0)
            except Exception:
                pass
        # 如果前端未传 emergency_target，从 userdata/emergency_config.json 读取
        if emergency_target == 0 and EMERGENCY_CONFIG_PATH.exists():
            try:
                with open(EMERGENCY_CONFIG_PATH, "r", encoding="utf-8") as f:
                    ec = json.load(f)
                emergency_target = sum(s.get("amount", 0) for s in ec.get("selected", []))
            except Exception:
                pass
        rp = None
        if risk_preference:
            mapping = {"low": VE4RiskPreference.LOW, "medium": VE4RiskPreference.MEDIUM,
                       "high": VE4RiskPreference.HIGH}
            rp = mapping.get(risk_preference.lower())
        report = ve4_alloc_generate_report(age=age, risk_preference=rp,
                                            emergency_months=max(1, min(6, emergency_months)),
                                            emergency_target=emergency_target)
        ve4_api_save_snapshot("allocation_report", report.to_dict())
        return JSONResponse(content=report.to_dict())
    except Exception as e:
        import traceback
        return JSONResponse(content={"error": str(e), "detail": traceback.format_exc()})


@app.get("/api/v1/allocation/detail")
def ve4_api_allocation_detail(emergency_months: int = 3, use_ai: bool = False):
    """
    四级资产配置当前分步（只读）。
    优先从离线快照读取，不触发规则引擎。
    快照由 /api/v1/allocation/detail-generate 生成。
    """
    try:
        import json as _json
        snap_path = SNAPSHOTS_DIR / "allocation_detail.json"
        if snap_path.exists():
            with open(snap_path, "r", encoding="utf-8") as f:
                return JSONResponse(content=_json.load(f))
        # 首次无快照时，执行一次生成
        from core.asset_allocation_detail import ve4_alloc_detail_generate
        report = ve4_alloc_detail_generate(
            emergency_months=max(1, min(6, emergency_months)),
            use_ai=use_ai
        )
        ve4_api_save_snapshot("allocation_detail", report.to_dict())
        return JSONResponse(content=report.to_dict())
    except Exception as e:
        import traceback
        return JSONResponse(content={"error": str(e), "detail": traceback.format_exc()})


@app.get("/api/v1/allocation/detail-generate")
def ve4_api_allocation_detail_generate(emergency_months: int = 3, use_ai: bool = False):
    """
    四级资产配置：强制执行规则引擎，重新生成报告。
    仅在用户主动进入"资产配置"页面时调用。
    """
    try:
        from core.asset_allocation_detail import ve4_alloc_detail_generate
        report = ve4_alloc_detail_generate(
            emergency_months=max(1, min(6, emergency_months)),
            use_ai=use_ai
        )
        ve4_api_save_snapshot("allocation_detail", report.to_dict())
        return JSONResponse(content=report.to_dict())
    except Exception as e:
        import traceback
        return JSONResponse(content={"error": str(e), "detail": traceback.format_exc()})


# ─── 用户画像配置 ───

@app.get("/api/v1/allocation/profile")
def ve4_api_get_profile():
    """读取用户资产配置画像（进取/稳健比例），存储在 userdata/allocation_profile.json"""
    try:
        if PROFILE_PATH.exists():
            with open(PROFILE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return JSONResponse(content=data)
        return JSONResponse(content={"aggressive_pct": None, "stable_pct": None, "note": "no_profile_set"})
    except Exception as e:
        return JSONResponse(content={"error": str(e)})


@app.post("/api/v1/allocation/profile")
def ve4_api_save_profile(payload: dict):
    """保存用户资产配置画像到 userdata/"""
    try:
        PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload["saved_at"] = datetime.now().isoformat()
        with open(PROFILE_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info(f"Profile saved: aggressive={payload.get('aggressive_pct')}%, stable={payload.get('stable_pct')}%")
        return JSONResponse(content={"status": "saved", "data": payload})
    except Exception as e:
        return JSONResponse(content={"error": str(e)})


@app.delete("/api/v1/allocation/profile")
def ve4_api_delete_profile():
    """清除用户画像配置"""
    try:
        if PROFILE_PATH.exists():
            PROFILE_PATH.unlink()
        return JSONResponse(content={"status": "cleared"})
    except Exception as e:
        return JSONResponse(content={"error": str(e)})


# ─── 应急场景配置（持久化到 userdata）───

@app.get("/api/v1/allocation/emergency")
def ve4_api_get_emergency():
    """读取应急场景配置，存储在 userdata/emergency_config.json"""
    try:
        if EMERGENCY_CONFIG_PATH.exists():
            with open(EMERGENCY_CONFIG_PATH, "r", encoding="utf-8") as f:
                return JSONResponse(content=json.load(f))
        return JSONResponse(content={"scenarios": [], "selected": []})
    except Exception as e:
        return JSONResponse(content={"error": str(e)})


@app.post("/api/v1/allocation/emergency")
def ve4_api_save_emergency(payload: dict):
    """保存应急场景配置到 userdata/"""
    try:
        EMERGENCY_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload["saved_at"] = datetime.now().isoformat()
        with open(EMERGENCY_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info(f"Emergency config saved: {len(payload.get('selected', []))} scenarios selected")
        return JSONResponse(content={"status": "saved"})
    except Exception as e:
        return JSONResponse(content={"error": str(e)})


# ─── 战术规划：持仓分类 ───

@app.get("/api/v1/tactical/overview")
def ve4_api_tactical_overview():
    """获取战术规划总览（旭日图数据）"""
    try:
        conn = ve4_api_get_db()
        rows = ve4_api_safe_query(conn, """
            SELECT product_name, asset_class, current_value
            FROM asset_holdings
            ORDER BY current_value DESC
        """)
        conn.close()
        holdings = [{"name": r["product_name"], "asset_class": r["asset_class"], "current_value": r["current_value"] or 0} for r in rows]
        from config.market_sector_rules import classify_all_holdings
        result = classify_all_holdings(holdings)
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"[TACTICAL] overview 失败: {e}")
        return JSONResponse(content={"error": str(e), "markets": [], "concentration": {}})


@app.post("/api/v1/tactical/classify-rules")
def ve4_api_classify_rules():
    """规则分类（基于关键词匹配）"""
    try:
        conn = ve4_api_get_db()
        rows = ve4_api_safe_query(conn, """
            SELECT product_name, asset_class, current_value
            FROM asset_holdings ORDER BY current_value DESC
        """)
        conn.close()
        holdings = [{"name": r["product_name"], "asset_class": r["asset_class"], "current_value": r["current_value"] or 0} for r in rows]
        from config.market_sector_rules import classify_all_holdings
        result = classify_all_holdings(holdings)
        # 不自动保存——让用户在前端确认后再保存
        return JSONResponse(content={"status": "classified", "data": result, "note": "规则分类完成，请在前端确认后保存"})
    except Exception as e:
        return JSONResponse(content={"error": str(e)})


@app.post("/api/v1/tactical/classify-llm")
def ve4_api_classify_llm():
    """LLM 智能分类：对未手动分类的持仓调用 LLM"""
    try:
        conn = ve4_api_get_db()
        rows = ve4_api_safe_query(conn, """
            SELECT product_name, asset_class, current_value
            FROM asset_holdings ORDER BY current_value DESC
        """)
        conn.close()
        holdings = [{"name": r["product_name"], "asset_class": r["asset_class"], "current_value": r["current_value"] or 0} for r in rows]
        
        # 构造 LLM prompt
        names = [h["name"] for h in holdings if h["asset_class"] != "liquid"]
        prompt = (
            "以下是用户的金融持仓产品名称。请为每个产品分类为「资本市场」和「行业板块」。\n"
            "资本市场选项：A股、美股、港股、债券、黄金、QDII混合\n"
            "行业板块选项（仅股票类）：科技/半导体、消费、医药、金融、新能源、红利/价值、基建/地产、交运/公用、其他\n"
            "非股票类（债券/黄金）板块填 null。\n\n"
            "请严格按以下 JSON 格式返回（不要解释，不要 markdown）:\n"
            "{\"classifications\": {\"产品名\": {\"market\": \"市场\", \"sector\": \"板块或null\"}}}\n\n"
            "持仓列表:\n" + "\n".join(f"- {n}" for n in names)
        )
        from core.ai_gateway import ve4_ai_call
        result = ve4_ai_call(
            task_type="general",
            system="你是金融产品分类专家。只返回 JSON，不要任何解释文字。",
            prompt=prompt,
            format_type="text",
            contains_privacy_data=False,
            complexity="low",
            max_tokens=2000,
            temperature=0.0,
        )
        if result.success and result.text:
            import json, re
            text = result.text.strip()
            # 提取 JSON
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m:
                llm_data = json.loads(m.group())
                return JSONResponse(content={"status": "llm_classified", "data": llm_data.get("classifications", {}), "raw": text})
            else:
                return JSONResponse(content={"status": "parse_failed", "raw": text})
        else:
            return JSONResponse(content={"status": "llm_failed", "error": result.error or "LLM返回为空"})
    except Exception as e:
        logger.error(f"[TACTICAL] LLM分类失败: {e}")
        return JSONResponse(content={"error": str(e)})


@app.post("/api/v1/tactical/classifications")
def ve4_api_save_classifications(payload: dict):
    """保存持仓分类（手动确认后的批量保存）"""
    try:
        from pathlib import Path
        import json as _json
        config_path = DATA_DIR / "holding_classifications.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        payload["saved_at"] = datetime.now().isoformat()
        with open(config_path, "w", encoding="utf-8") as f:
            _json.dump(payload, f, ensure_ascii=False, indent=2)
        return JSONResponse(content={"status": "saved"})
    except Exception as e:
        return JSONResponse(content={"error": str(e)})


@app.get("/api/v1/tactical/classifications")
def ve4_api_get_classifications():
    """获取已保存的持仓分类"""
    try:
        from pathlib import Path
        import json as _json
        config_path = DATA_DIR / "holding_classifications.json"
        if config_path.exists():
            data = _json.loads(config_path.read_text(encoding="utf-8"))
            return JSONResponse(content=data)
        return JSONResponse(content={"classifications": {}})
    except Exception as e:
        return JSONResponse(content={"error": str(e)})


# ─── 系统文件/文件夹对话框（供前端调用） ───
# 使用 PowerShell + .NET Windows Forms 打开原生对话框
# 不使用 sys.executable（PyInstaller 打包后指向 VE5.exe，会启动新实例）

@app.post("/api/v1/system/file-dialog")
def ve4_api_file_dialog(payload: dict = None):
    """打开系统文件选择对话框，返回选中的文件路径"""
    try:
        import subprocess

        filetypes = payload.get("filetypes", [("All files", "*.*")]) if payload else [("All files", "*.*")]
        # 构造 PowerShell Filter: "Executable files|*.exe|All files|*.*"
        filter_str = "|".join(f"{name}|{pattern}" for name, pattern in filetypes)

        # PowerShell 脚本：使用 .NET Windows Forms OpenFileDialog
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms\n"
            "$d = New-Object System.Windows.Forms.OpenFileDialog\n"
            f'$d.Filter = "{filter_str}"\n'
            '$d.Title = "选择文件"\n'
            'if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {\n'
            '    [Console]::Out.Write($d.FileName)\n'
            '} else {\n'
            '    [Console]::Out.Write("")\n'
            '}'
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps_script],
            capture_output=True, text=True, timeout=60,
            creationflags=0x08000000  # CREATE_NO_WINDOW
        )
        path = result.stdout.strip()
        return JSONResponse(content={"path": path})
    except Exception as e:
        return JSONResponse(content={"error": str(e)})

@app.post("/api/v1/system/folder-dialog")
def ve4_api_folder_dialog():
    """打开系统文件夹选择对话框"""
    try:
        import subprocess

        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms\n"
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog\n"
            '$d.Description = "选择文件夹"\n'
            "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {\n"
            "    [Console]::Out.Write($d.SelectedPath)\n"
            "} else {\n"
            "    [Console]::Out.Write(\"\")\n"
            "}"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps_script],
            capture_output=True, text=True, timeout=60,
            creationflags=0x08000000  # CREATE_NO_WINDOW
        )
        path = result.stdout.strip()
        return JSONResponse(content={"path": path})
    except Exception as e:
        return JSONResponse(content={"error": str(e)})


# ─── 用户设定的生活必需流动资金 ───

@app.get("/api/v1/allocation/essential-liquid")
def ve4_api_get_essential_liquid():
    """读取用户手动设定的生活必需流动资金"""
    try:
        config_path = DATA_DIR / "essential_liquid_config.json"
        if config_path.exists():
            data = json.loads(config_path.read_text(encoding="utf-8"))
            return JSONResponse(content=data)
        return JSONResponse(content={"essential_liquid": 0, "note": "no_manual_setting"})
    except Exception as e:
        return JSONResponse(content={"error": str(e)})


@app.post("/api/v1/allocation/essential-liquid")
def ve4_api_save_essential_liquid(payload: dict):
    """保存用户手动设定的生活必需流动资金"""
    try:
        config_path = DATA_DIR / "essential_liquid_config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        payload["saved_at"] = datetime.now().isoformat()
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info(f"生活必需流动资金设定: ¥{payload.get('essential_liquid', 0)}")
        return JSONResponse(content={"status": "saved", "data": payload})
    except Exception as e:
        return JSONResponse(content={"error": str(e)})


@app.delete("/api/v1/allocation/essential-liquid")
def ve4_api_delete_essential_liquid():
    """清除用户设定的生活必需流动资金（恢复自动计算）"""
    try:
        config_path = DATA_DIR / "essential_liquid_config.json"
        if config_path.exists():
            config_path.unlink()
        return JSONResponse(content={"status": "cleared", "note": "恢复自动计算"})
    except Exception as e:
        return JSONResponse(content={"error": str(e)})


# ─── 资产配置对话（LLM解读）───

@app.post("/api/v1/allocation/chat")
def ve4_api_allocation_chat(payload: dict):
    """
    资产配置对话接口，支持多种上下文类型的LLM解读。
    
    context_type:
      - protection: 保障管理解读
      - liquid: 流动类解读
      - stable: 稳健类解读
      - aggressive: 进取类解读
      - emergency: 应急资金场景
      - goal_analysis: 已捕获目标分析
    """
    try:
        context_type = payload.get("context_type", "protection")
        user_message = payload.get("message", "")
        
        # 构建上下文数据
        context_data = _build_allocation_context(context_type)
        
        # 构建 system prompt
        system_prompt = _build_allocation_system_prompt(context_type, context_data)
        
        # 调用AI网关（使用同步 ve4_ai_call）
        from core.ai_gateway import ve4_ai_call
        result = ve4_ai_call(
            task_type="allocation_chat",
            system=system_prompt,
            prompt=user_message,
            format_type="text",
            contains_privacy_data=True,
            complexity="medium",
            temperature=0.7,
            max_tokens=1024,
        )
        
        if result.success:
            return JSONResponse(content={
                "status": "success",
                "response": result.text,
                "context_type": context_type,
                "provider": result.provider,
            })
        else:
            return JSONResponse(content={
                "error": result.error or "AI调用失败",
                "context_type": context_type
            }, status_code=500)
            
    except Exception as e:
        import traceback
        logger.error(f"[ALLOC_CHAT] 对话失败: {traceback.format_exc()}")
        return JSONResponse(content={
            "error": str(e),
        }, status_code=500)


# ─── 目标获取（LLM生成小行星目标）───

@app.post("/api/v1/allocation/goals/generate")
def ve4_api_generate_goals(payload: dict):
    """LLM 根据用户财务情况生成个性化短期目标（小行星）"""
    try:
        from core.ai_gateway import ve4_ai_call
        from core.allocation_engine import ve4_alloc_generate_report
        
        report = ve4_alloc_generate_report()
        actual = report.actual
        liquidity = report.liquidity
        protection = report.protection_management or {}
        
        system = """你是VE5财务生活助手。根据用户的财务状况，生成2个个性化、有吸引力的短期目标（3-12个月可达成）。
这些目标应该是用户在当前财务基础上可以努力实现的、能提升生活质量的愿望。
要求：
1. 目标要具体、可衡量、有吸引力
2. 每个目标必须给出合理的预估花费金额（estimated_cost）
3. 金额要基于中国一线城市实际消费水平，合理务实
4. 每个目标配一个合适的 emoji 图标
5. 风格温馨有感染力
6. 不要重复常见的退休、教育、买房等长期目标

以 JSON 数组格式返回（恰好如下元素）：[{"name":"目标名","icon":"🎵","desc":"一句话描述","horizon":"时间范围","estimated_cost":金额}]
金额单位为人民币元。只返回 JSON，不要其他文字。
参考示例：
[{"name":"佛得角旅行计划","icon":"✈","desc":"去一趟佛得角吧,见证海神之子诞生的地方","horizon":"3个月","estimated_cost":50000},
 {"name":"六盘水的夏夜","icon":"🌒","desc":"在凉都的晚风中看夜空的银河","horizon":"1个月","estimated_cost":3000}]
"""
        
        prompt = f"""用户当前财务情况：
- 总资产：¥{actual.total_all:,.0f}
- 流动类：¥{actual.liquid_value:,.0f}（可分配盈余 ¥{protection.get('surplus_amount', 0):,.0f}）
- 进取类：¥{actual.aggressive_value:,.0f}
- 稳健类：¥{actual.stable_value:,.0f}
- 保障类：¥{actual.protection_value:,.0f}
- 应急覆盖率：{liquidity.coverage_ratio*100:.0f}%

请生成2个个性化短期目标。"""
        
        result = ve4_ai_call(
            task_type="goal_generation",
            system=system,
            prompt=prompt,
            format_type="json",
            contains_privacy_data=True,
            complexity="medium",
            temperature=0.8,
            max_tokens=512,
        )
        
        if result.success and result.text:
            import json as _json
            text = result.text.strip()
            # 提取 JSON 部分
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
            goals = _json.loads(text)
            # 补充 id 和 color
            colors = ["#22d3ee", "#f59e0b", "#34d399", "#6366f1"]
            for i, g in enumerate(goals):
                g["id"] = f"astro_{int(time.time())}_{i}"
                g["color"] = colors[i % len(colors)]
                g["source"] = "LLM推荐"
                if "estimated_cost" not in g:
                    g["estimated_cost"] = 0
            return JSONResponse(content={"status": "success", "goals": goals})
        
        return JSONResponse(content={"error": result.error or "生成失败"}, status_code=500)
    except Exception as e:
        import traceback
        return JSONResponse(content={"error": str(e)}, status_code=500)


# ─── 已捕获目标分析（LLM路径规划）───

@app.post("/api/v1/allocation/goals/analyze")
def ve4_api_analyze_goals(payload: dict):
    """LLM 分析已捕获目标的达成路径"""
    try:
        from core.ai_gateway import ve4_ai_call
        from core.allocation_engine import ve4_alloc_generate_report
        
        goals = payload.get("goals", [])
        if not goals:
            return JSONResponse(content={"error": "无已捕获目标"}, status_code=400)
        
        report = ve4_alloc_generate_report()
        actual = report.actual
        liquidity = report.liquidity
        protection = report.protection_management or {}
        
        goals_text = "\n".join([f"- {g.get('icon','')} {g.get('name','')}：{g.get('desc','')}（{g.get('horizon','')}，预估成本 ¥{g.get('estimated_cost', 0):,.0f}）" for g in goals])
        
        system = """你是VE5财务规划助手。用户已设定了一些生活目标，你需要结合用户当前的真实财务状况，为每个目标分析：
1. 当前财务基础是否足以支撑
2. 需要做什么（具体、可执行的行动步骤）
3. 预计达成时间和所需月度投入
4. 可能的风险和应对策略

要求：
- 结合用户的真实资产数据给出分析
- 语气温暖专业，像朋友一样提供建议
- 如果某个目标当前财务难以支撑，诚实说明并提出替代方案
- 每个目标的分析控制在3-5句话"""

        prompt = f"""用户当前财务情况：
- 总资产：¥{actual.total_all:,.0f}
- 流动类（可用现金）：¥{actual.liquid_value:,.0f}
- 进取类（投资）：¥{actual.aggressive_value:,.0f}
- 稳健类（固收）：¥{actual.stable_value:,.0f}
- 保障类：¥{actual.protection_value:,.0f}
- 可分配盈余：¥{protection.get('surplus_amount', 0):,.0f}
- 应急覆盖率：{liquidity.coverage_ratio*100:.0f}%

用户已捕获的目标：
{goals_text}

请为每个目标分析达成路径。"""
        
        result = ve4_ai_call(
            task_type="goal_analysis",
            system=system,
            prompt=prompt,
            format_type="text",
            contains_privacy_data=True,
            complexity="high",
            temperature=0.6,
            max_tokens=2048,
        )
        
        if result.success:
            return JSONResponse(content={
                "status": "success",
                "response": result.text,
                "provider": result.provider,
            })
        return JSONResponse(content={"error": result.error or "分析失败"}, status_code=500)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


# ─── 目标持久化（userdata/goals.json）───

GOALS_CONFIG_PATH = DATA_DIR / "goals.json"


@app.get("/api/v1/allocation/goals")
def ve4_api_get_goals():
    """读取已捕获目标（结构化 + 非结构化）"""
    try:
        if GOALS_CONFIG_PATH.exists():
            with open(GOALS_CONFIG_PATH, "r", encoding="utf-8") as f:
                return JSONResponse(content=json.load(f))
        return JSONResponse(content={"goals": [], "plan_history": []})
    except Exception as e:
        return JSONResponse(content={"error": str(e)})


@app.post("/api/v1/allocation/goals/save")
def ve4_api_save_goals(payload: dict):
    """保存已捕获目标到 userdata/goals.json

    结构化保存：goals 数组（含 estimated_cost, horizon, captured_at 等）
    非结构化保存：plan_history 数组（LLM 原始分析文本，供衍生功能使用）
    """
    try:
        existing = {}
        if GOALS_CONFIG_PATH.exists():
            with open(GOALS_CONFIG_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)

        goals = payload.get("goals", [])
        # 确保每个目标有完整字段
        for g in goals:
            g.setdefault("estimated_cost", 0)
            g.setdefault("plan_text", "")

        existing["goals"] = goals
        existing["saved_at"] = datetime.now().isoformat()

        GOALS_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(GOALS_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)

        logger.info(f"Goals saved: {len(goals)} goals")
        return JSONResponse(content={"status": "saved", "count": len(goals)})
    except Exception as e:
        return JSONResponse(content={"error": str(e)})


@app.post("/api/v1/allocation/goals/estimate")
def ve4_api_estimate_goals(payload: dict):
    """LLM 估算目标所需金额"""
    try:
        from core.ai_gateway import ve4_ai_call
        goals = payload.get("goals", [])
        if not goals:
            return JSONResponse(content={"error": "无目标"}, status_code=400)

        goals_text = "\n".join([
            f"- {g.get('icon','')} {g.get('name','')}：{g.get('desc','')}（{g.get('horizon','')}）"
            for g in goals
        ])

        system = """你是VE5财务助手。根据用户目标和当前中国一线城市消费水平，估算每个目标所需资金。
以 JSON 数组格式返回：[{"id":"目标id","estimated_cost":金额}]
金额单位为人民币元。只返回 JSON，不要其他文字。"""

        result = ve4_ai_call(
            task_type="goal_estimation",
            system=system,
            prompt=f"请估算以下目标所需资金：\n{goals_text}",
            format_type="json",
            contains_privacy_data=False,
            complexity="low",
            temperature=0.3,
            max_tokens=256,
        )

        if result.success and result.text:
            text = result.text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"): text = text[:-3]
            estimates = json.loads(text)
            return JSONResponse(content={"status": "success", "estimates": estimates})

        return JSONResponse(content={"error": result.error or "估算失败"}, status_code=500)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


def _build_allocation_context(context_type: str) -> dict:
    """构建资产配置对话的上下文数据"""
    result = {}
    
    try:
        from core.allocation_engine import ve4_alloc_generate_report
        from core.asset_classification_rules import ve4_alloc_rules_classify
        report = ve4_alloc_generate_report()
        actual = report.actual
        framework = report.framework
        protection = report.protection_management or {}
        
        # 通用数据
        result["total_assets"] = actual.total_all
        result["aggressive_value"] = actual.aggressive_value
        result["stable_value"] = actual.stable_value
        result["liquid_value"] = actual.liquid_value
        result["protection_value"] = actual.protection_value
        result["framework_liquid"] = framework.liquid_amount
        result["framework_stable"] = framework.stable_amount
        result["framework_aggressive"] = framework.aggressive_amount
        result["essential_liquid"] = protection.get("essential_liquid", 0)
        result["surplus_amount"] = protection.get("surplus_amount", 0)
        result["surplus_pct"] = protection.get("surplus_pct", 0)
        result["protection_types"] = protection.get("protection_types", {})
        result["protection_products"] = protection.get("protection_products", [])
        
        # 获取对应类别的持仓详情（使用名称分类规则，与引擎一致）
        if context_type in ["liquid", "stable", "aggressive", "protection"]:
            conn = ve4_api_get_db()
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT product_name, current_value, account_key, asset_class,
                       unrealized_pnl, holding_return_pct, annualized_return_pct
                FROM asset_holdings
                WHERE current_value > 0
                AND product_name NOT IN ('ETF', '证券市值', '总资产')
                ORDER BY current_value DESC
            """).fetchall()
            conn.close()
            
            # 按名称分类过滤
            filtered = []
            for r in rows:
                name = r["product_name"] or ""
                ac = r["asset_class"] or ""
                # 保障类：asset_class 直接匹配或名称关键词
                if context_type == "protection":
                    if ac in ("alternative", "protection"):
                        cat = "protection"
                    else:
                        cat = ve4_alloc_rules_classify(name)
                else:
                    cat = ve4_alloc_rules_classify(name)
                
                if cat == context_type:
                    filtered.append({
                        "name": r["product_name"],
                        "value": r["current_value"],
                        "account": r["account_key"],
                        "return_pct": r["holding_return_pct"],
                        "annualized_return": r["annualized_return_pct"],
                    })
            
            result[f"{context_type}_holdings"] = filtered
        
    except Exception as e:
        logger.warning(f"[ALLOC_CHAT] 构建上下文失败: {e}")
    
    return result


def _build_allocation_system_prompt(context_type: str, context: dict) -> str:
    """根据上下文类型构建系统提示词"""
    
    base = (
        "你是VE5资产配置助手，一个温暖、专业、有同理心的理财顾问。\n"
        "你的角色是解读和解释，而不是推销或推荐具体产品。\n"
        "重要原则：\n"
        "1. 只基于用户当前已有的数据进行解读，不假设用户应该配置什么\n"
        "2. 用通俗易懂的语言解释金融概念，避免生硬的术语\n"
        "3. 尊重用户的选择，不评判用户的配置是否正确\n"
        "4. 如果用户问'我应该买什么'或'我应该配置什么'，请回复：\n"
        "   '我可以帮你理解你当前持有的资产能带来什么价值和作用，\n"
        "    但具体的配置决策需要你根据自己的情况来做。\n"
        "    你想了解哪类资产的作用和价值吗？'\n"
    )
    
    if context_type == "protection":
        specific = f"""
【当前用户的保障配置数据】
- 总资产：¥{context.get('total_assets', 0):,.0f}
- 已配置进取类：¥{context.get('aggressive_value', 0):,.0f}
- 已配置稳健类：¥{context.get('stable_value', 0):,.0f}
- 已配置保障类：¥{context.get('protection_value', 0):,.0f}
- 生活必需流动资金（战略基准）：¥{context.get('essential_liquid', 0):,.0f}
- 可分配盈余（流动类超出生活必需的部分）：¥{context.get('surplus_amount', 0):,.0f}

【用户已有的保障产品类型】
"""
        protection_types = context.get("protection_types", {})
        if protection_types:
            for t, info in protection_types.items():
                specific += f"- {t}：{info['count']}个产品，合计 ¥{info['amount']:,.0f}\n"
                specific += f"  产品：{', '.join(info['products'][:3])}\n"
        else:
            specific += "- 暂无保障类资产\n"
        
        specific += """
【你的任务】
- 解释用户当前已配置的各类保障资产分别能提供什么价值和作用
- 解释"可分配盈余"的概念和用途
- 如果用户问"我还需要什么保障"，不要直接推荐，而是引导用户思考：
  "不同的人有不同的保障需求，这取决于你的家庭结构、职业风险、健康状况等因素。
   你想了解某类保障产品的具体作用吗？我可以帮你分析它能在什么场景下发挥价值。"
- 保持温暖、专业的语气
"""
        return base + specific
    
    elif context_type in ["liquid", "stable", "aggressive"]:
        type_names = {"liquid": "流动类", "stable": "稳健类", "aggressive": "进取类"}
        type_name = type_names.get(context_type, context_type)
        
        holdings = context.get(f"{context_type}_holdings", [])
        holdings_text = ""
        if holdings:
            for h in holdings:
                holdings_text += f"- {h['name']}：¥{h['value']:,.0f}（{h['account']}）"
                if h.get('return_pct'):
                    holdings_text += f" 收益率 {h['return_pct']:.2f}%"
                holdings_text += "\n"
        else:
            holdings_text = "- 暂无持仓\n"
        
        specific = f"""
【{type_name}资产数据】
- {type_name}总额：¥{context.get(f'{context_type}_value', 0):,.0f}
- 战略基准目标：¥{context.get(f'framework_{context_type}', 0):,.0f}

【{type_name}持仓明细】
{holdings_text}
【你的任务】
- 解读用户当前持有的{type_name}资产的特点和作用
- 解释这类资产在资产配置中的角色和意义
- 如果偏离了战略基准，解释这种偏离可能意味着什么（不评判好坏）
- 用通俗的语言，让用户理解自己的钱在做什么
- 不要推荐具体的买入或卖出操作
"""
        return base + specific
    
    elif context_type == "emergency":
        return base + """
【你的任务】
生成不少于8个常见的需要应急资金的生活场景，每个场景包含：
- 场景名称（简短有力，有共鸣感）
- 预估金额（人民币，合理范围）
- 一句话描述（为什么需要这笔钱，有温度）

场景类别应涵盖：
1. 健康医疗类（如突发疾病、意外受伤）
2. 工作职业类（如失业、职业转型）
3. 家庭生活类（如家电损坏、房屋维修）
4. 人情往来类（如亲友急需、红白喜事）
5. 个人成长类（如技能培训、Gap Month）
6. 意外事件类（如交通事故、被盗）

请以JSON数组格式返回，每个元素包含：name, amount, description, category
金额要符合中国一线城市的实际消费水平。
"""
    
    elif context_type == "goal_analysis":
        return base + """
【你的任务】
用户已捕获了一些人生/财务目标，你需要结合用户的财务数据，回答用户关于目标实现路径的追问。
- 基于上下文中的资产数据给出具体、可操作的建议
- 如果用户问如何实现某个目标，给出分步骤的行动方案
- 语气温暖专业，像朋友一样提供建议
"""
    
    return base


# ════════════════════════════════════════════════════════════════
# 资产配置战术模块 (Tactical) API
# ════════════════════════════════════════════════════════════════
# 注意：tactical/ 目录已迁移到 ve4/tactical/（与 VE4/ 平行）
# 通过 ve4_launcher.py 将 PROJECT_ROOT (ve4/) 加入 sys.path

# (tactical imports moved to lazy-loading inside ve4_api_get_tactical_orchestrator)

# ════════════════════════════════════════════════════════════════
# 资产配置战术模块 (Tactical) API — v2.0
# ════════════════════════════════════════════════════════════════
# 定位：面向未来的投资决策辅助 — 当前该买什么、卖什么
# 方法：基本面分析 + 量化分析（不做战略内容）
# 严禁：夏普比率/回撤/流动比例/进取稳健配比/模式识别/回测

# ─── 数据源状态 ───

@app.get("/api/v1/tactical/datasources")
def ve4_api_tactical_datasources():
    """返回数据源配置状态（从 data_sources.yaml 读取）"""
    try:
        from tactical.quantitative.tools.data_source import VE4DataSourceManager
        mgr = VE4DataSourceManager()
        sources = list(mgr.list_sources().values())
        return JSONResponse(content={"sources": sources})
    except ImportError:
        # fallback
        return JSONResponse(content={"sources": [
            {"key": "ahshare", "name": "AkShare", "enabled": True, "status": "开源免费"},
            {"key": "tushare", "name": "Tushare Pro", "enabled": False, "status": "未配置 Token"},
            {"key": "wind", "name": "万得数据", "enabled": True, "status": "公开数据可用"},
            {"key": "local_file", "name": "本地数据", "enabled": False, "status": "未指定路径"},
            {"key": "other", "name": "其它", "enabled": False, "status": "自定义数据源"},
        ]})
    except Exception as e:
        return JSONResponse(content={"error": str(e)})


@app.post("/api/v1/tactical/datasources/config")
async def ve4_api_tactical_datasources_config(payload: dict):
    """保存数据源配置到 data_sources.yaml"""
    key = payload.get("key", "")
    if not key:
        return JSONResponse(content={"success": False, "error": "缺少数据源 key"})
    try:
        from tactical.quantitative.tools.data_source import VE4DataSourceManager
        mgr = VE4DataSourceManager()
        # local_file 用 path，其他用 token
        token_or_path = payload.get("token", "")
        if key == "local_file":
            ok = mgr.update_source(
                key=key,
                name=payload.get("name"),
                path=token_or_path,
                enabled=payload.get("enabled"),
            )
        else:
            ok = mgr.update_source(
                key=key,
                name=payload.get("name"),
                token=token_or_path,
                enabled=payload.get("enabled"),
            )
        return JSONResponse(content={"success": ok, "error": "" if ok else "更新失败"})
    except ImportError:
        return JSONResponse(content={"success": False, "error": "DataSourceManager 未就绪"})
    except Exception as e:
        import traceback
        return JSONResponse(content={"success": False, "error": str(e), "detail": traceback.format_exc()})


# ─── 基本面分析端点 ───

@app.post("/api/v1/tactical/fundamental/parse-url")
async def ve4_api_tactical_parse_url(payload: dict):
    """解析研报 URL → 提取文本 → LLM 分析 → 存入知识库"""
    url = payload.get("url", "")
    if not url:
        return JSONResponse(content={"success": False, "error": "URL 不能为空"})
    try:
        from tactical.fundamental.agents.report_agent import VE4ReportAnalysisAgent
        agent = VE4ReportAnalysisAgent()
        if hasattr(agent, 'parse_url'):
            result = await agent.parse_url(url)
            return JSONResponse(content={"success": True, "report": result})
        else:
            raise AttributeError("parse_url not implemented")
    except (ImportError, AttributeError):
        # Agent 未就绪时返回占位结构
        return JSONResponse(content={"success": True, "report": {
            "title": f"URL 待解析: {url[:50]}...",
            "source": url.split("//")[-1].split("/")[0] if "//" in url else "--",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "confidence": 0.5,
            "summary": "ReportAnalysisAgent 待实现 — URL 已接收",
            "investment_thesis": "",
            "key_points": ["待 LLM 接入后提取"],
            "sentiment": "待分析",
        }})
    except Exception as e:
        import traceback
        return JSONResponse(content={"success": False, "error": str(e), "detail": traceback.format_exc()})


@app.post("/api/v1/tactical/fundamental/upload-pdf")
async def ve4_api_tactical_upload_pdf(request: Request):
    """上传 PDF 研报 → 提取文本 → LLM 分析 → 存入知识库"""
    try:
        # 读取上传的文件
        form = await request.form()
        file = form.get("file")
        if not file:
            return JSONResponse(content={"success": False, "error": "未提供文件"})

        # 保存到临时文件
        import tempfile
        suffix = Path(file.filename).suffix if file.filename else ".pdf"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = tmp.name

        try:
            # 解析 PDF
            from tactical.fundamental.tools.document_parser import VE4DocumentParser
            parser = VE4DocumentParser()
            parse_result = await parser.parse(tmp_path)

            if not parse_result["success"]:
                return JSONResponse(content={"success": False, "error": f"PDF解析失败: {parse_result.get('error', '未知错误')}"})

            # 使用 agent 分析
            from tactical.fundamental.agents.report_agent import VE4ReportAnalysisAgent
            agent = VE4ReportAnalysisAgent()
            result = await agent.parse_text(
                text=parse_result["text"],
                title=parse_result.get("title") or file.filename or "上传的PDF研报"
            )
            return JSONResponse(content={"success": True, "report": result})
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
    except Exception as e:
        import traceback
        return JSONResponse(content={"success": False, "error": str(e), "detail": traceback.format_exc()})


@app.post("/api/v1/tactical/fundamental/parse-text")
async def ve4_api_tactical_parse_text(payload: dict):
    """解析粘贴的研报/新闻文本 → LLM 分析 → 存入知识库"""
    text = payload.get("text", "")
    if not text:
        return JSONResponse(content={"success": False, "error": "文本不能为空"})
    try:
        from tactical.fundamental.agents.report_agent import VE4ReportAnalysisAgent
        agent = VE4ReportAnalysisAgent()
        if hasattr(agent, 'parse_text'):
            result = await agent.parse_text(text)
            return JSONResponse(content={"success": True, "report": result})
        else:
            raise AttributeError("parse_text not implemented")
    except (ImportError, AttributeError):
        return JSONResponse(content={"success": True, "report": {
            "title": "文本待分析",
            "source": "手动粘贴",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "confidence": 0.5,
            "summary": f"收到文本 {len(text)} 字 — ReportAnalysisAgent 待实现",
            "investment_thesis": "",
            "key_points": ["待 LLM 接入后提取"],
            "sentiment": "待分析",
        }})
    except Exception as e:
        import traceback
        return JSONResponse(content={"success": False, "error": str(e), "detail": traceback.format_exc()})


# ─── 持仓影响评估端点 ───

@app.post("/api/v1/tactical/fundamental/holdings-impact")
async def ve4_api_tactical_holdings_impact(payload: dict):
    """基于研报分析结果，评估对用户持仓的影响，生成买入/卖出/持有建议"""
    try:
        from tactical.fundamental.agents.holdings_impact_agent import VE4HoldingsImpactAgent
        from tactical.shared.models.tactical_models import VE4AgentTask
        agent = VE4HoldingsImpactAgent()
        from tactical.shared.models.tactical_models import VE4AgentTaskType
        task = VE4AgentTask(
            task_id=f"impact_{datetime.now().strftime('%Y%m%d%H%M%S')}",
            task_type=VE4AgentTaskType.ANALYZE_HOLDINGS,
            goal="评估研报对用户持仓的影响",
            params=payload,
        )
        result = await agent.execute(task)
        if result.success:
            return JSONResponse(content={
                "success": True,
                "recommendations": result.data.get("recommendations", []),
                "holdings_count": result.data.get("holdings_count", 0),
                "report_title": result.data.get("report_title", ""),
            })
        else:
            return JSONResponse(content={"success": False, "error": result.error})
    except ImportError:
        return JSONResponse(content={"success": False, "error": "HoldingsImpactAgent 未就绪"})
    except Exception as e:
        import traceback
        return JSONResponse(content={"success": False, "error": str(e), "detail": traceback.format_exc()})


# ─── RAG 知识库端点 ───

@app.get("/api/v1/tactical/fundamental/knowledge")
def ve4_api_tactical_knowledge_list(limit: int = 50):
    """列出 RAG 知识库中的所有研报"""
    try:
        from tactical.fundamental.knowledge.vector_store import ve4_kb_list
        reports = ve4_kb_list(limit=limit)
        return JSONResponse(content={"success": True, "reports": reports})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.post("/api/v1/tactical/fundamental/search")
async def ve4_api_tactical_knowledge_search(payload: dict):
    """语义检索研报知识库"""
    query = payload.get("query", "")
    top_k = payload.get("top_k", 5)
    report_id = payload.get("report_id", None)
    if not query:
        return JSONResponse(content={"success": False, "error": "查询词不能为空"})
    try:
        from tactical.fundamental.knowledge.vector_store import ve4_kb_search
        results = ve4_kb_search(query, top_k=top_k, report_id_filter=report_id)
        return JSONResponse(content={"success": True, "results": results})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.delete("/api/v1/tactical/fundamental/knowledge/{report_id}")
async def ve4_api_tactical_knowledge_delete(report_id: str):
    """从知识库删除指定研报"""
    try:
        from tactical.fundamental.knowledge.vector_store import ve4_kb_delete
        ok = ve4_kb_delete(report_id)
        return JSONResponse(content={"success": ok})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.get("/api/v1/tactical/fundamental/knowledge/{report_id}/detail")
async def ve4_api_tactical_knowledge_detail(report_id: str):
    """获取单篇研报的完整结构化数据（从 ChromaDB 读取）"""
    try:
        from tactical.fundamental.knowledge.vector_store import ve4_kb_search, ve4_kb_list, _get_store
        # 按 report_id 从 ChromaDB 获取所有 chunks
        store = _get_store()
        all_results = store.collection.get(
            where={"report_id": report_id},
            include=["documents", "metadatas"]
        )
        # 按 chunk_type 汇总
        detail = {"report_id": report_id, "title": "", "summary": "", "key_points": [], "thesis": "", "risks": [], "sentiment": ""}
        docs = all_results.get("documents", [])
        metas = all_results.get("metadatas", [])
        for i in range(len(docs)):
            meta = metas[i] if i < len(metas) else {}
            if not detail["title"]:
                detail["title"] = meta.get("title", "")
            content = docs[i] if i < len(docs) else ""
            ct = meta.get("chunk_type", "")
            if ct == "summary":
                lines = content.split("\n", 1)
                detail["summary"] = lines[1].strip() if len(lines) > 1 else content
            elif ct == "key_point":
                lines = content.split("\n", 1)
                detail["key_points"].append(lines[1].strip() if len(lines) > 1 else content)
            elif ct == "thesis":
                lines = content.split("\n", 1)
                detail["thesis"] = lines[1].strip() if len(lines) > 1 else content
            elif ct == "risk":
                lines = content.split("\n", 1)
                detail["risks"].append(lines[1].strip() if len(lines) > 1 else content)
        # 简易情绪判断（基于关键词）
        all_text = " ".join(detail["key_points"] + detail["risks"])
        bullish = sum(1 for w in ["买入", "看好", "增持", "超配", "推荐"] if w in all_text)
        bearish = sum(1 for w in ["卖出", "减持", "看空", "低配", "风险"] if w in all_text)
        if bullish > bearish:
            detail["sentiment"] = "看多"
        elif bearish > bullish:
            detail["sentiment"] = "看空"
        else:
            detail["sentiment"] = "中性"
        return JSONResponse(content={"success": True, "detail": detail})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.post("/api/v1/tactical/fundamental/chat")
async def ve4_api_tactical_fundamental_chat(payload: dict):
    """
    基本面分析 AI 对话：
    1. 从 RAG 检索相关研报片段
    2. 构造 prompt（含 RAG 上下文）
    3. 调用 LLM 生成回答（未配置 LLM 时返回 RAG 原始片段）
    """
    query = payload.get("query", "")
    history = payload.get("history", [])  # [{role, content}]
    if not query:
        return JSONResponse(content={"success": False, "error": "问题不能为空"})

    try:
        # Step 1: RAG 检索
        from tactical.fundamental.knowledge.vector_store import ve4_kb_search
        rag_results = ve4_kb_search(query, top_k=10)
        rag_context = "\n\n".join(
            f"[{r.get('chunk_type', '')}] {r.get('content', '')}"
            for r in rag_results[:10]
        ) if rag_results else "知识库中暂无相关研报。"

        # Step 2: 尝试调用 LLM
        ai_response = ""
        llm_used = False
        try:
            # 读取用户配置的 AI provider
            ai_settings = _load_ai_settings()
            api_key = ai_settings.get("api_key", "")
            api_base = ai_settings.get("api_base", "")
            model = ai_settings.get("model", "")

            if api_key and api_base and model:
                import httpx
                messages = [
                    {"role": "system", "content": (
                        "你是 VE4 基本面分析助手。基于用户提供的研报知识库内容回答问题。\n"
                        "要求：\n"
                        "1. 仅基于下面的【研报知识库】内容回答，不要编造信息\n"
                        "2. 如果知识库中没有相关信息，明确说明\n"
                        "3. 引用具体研报的观点时标注来源\n"
                        "4. 如果用户的问题暗示需要量化策略，在回答末尾用 ===STRATEGY_SUGGESTION=== 标记，"
                        "后面附上建议的策略描述（一段自然语言即可，后续可用于生成量化策略代码）"
                    )},
                    {"role": "system", "name": "knowledge_base", "content": f"【研报知识库】\n{rag_context}"},
                ]
                # 添加对话历史
                for h in history[-10:]:
                    messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
                messages.append({"role": "user", "content": query})

                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        f"{api_base}/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={"model": model, "messages": messages, "temperature": 0.3, "max_tokens": 2000}
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        ai_response = data["choices"][0]["message"]["content"]
                        llm_used = True
                    else:
                        ai_response = f"[LLM 调用失败: HTTP {resp.status_code}]"
        except Exception as e:
            ai_response = ""

        # Fallback: 无 LLM 时返回 RAG 原始片段
        if not llm_used:
            ai_response = "[当前未配置 AI 模型，以下为 RAG 检索到的相关研报片段]\n\n"
            for r in rag_results[:5]:
                title = r.get("title", "")
                ct = r.get("chunk_type", "")
                content = r.get("content", "")[:300]
                ai_response += f"**{title}** [{ct}]\n{content}\n\n"
            ai_response += "\n配置 AI API 后可获得基于研报的智能分析回答。"

        return JSONResponse(content={
            "success": True,
            "response": ai_response,
            "rag_count": len(rag_results),
            "llm_used": llm_used,
        })
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


def _ensure_ai_settings_table(conn):
    """确保 ai_settings 表存在"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            provider TEXT,
            api_key TEXT,
            api_base TEXT,
            model TEXT,
            updated_at TEXT
        )
    """)


def _load_ai_settings() -> dict:
    """加载用户 AI 配置"""
    try:
        import sqlite3
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        _ensure_ai_settings_table(conn)
        row = conn.execute("SELECT * FROM ai_settings LIMIT 1").fetchone()
        conn.close()
        if row:
            return dict(row)
    except Exception:
        pass
    return {}


@app.get("/api/v1/ai-settings")
def ve4_api_get_ai_settings():
    """获取 AI 配置状态（隐藏敏感字段）"""
    settings = _load_ai_settings()
    has_config = bool(settings.get("api_key") and settings.get("api_base"))
    raw_key = settings.get("api_key", "")
    masked = ""
    if raw_key and len(raw_key) > 8:
        masked = raw_key[:4] + "****" + raw_key[-4:]
    elif raw_key:
        masked = "****"
    return JSONResponse(content={
        "success": True,
        "configured": has_config,
        "provider": settings.get("provider", ""),
        "model": settings.get("model", ""),
        "api_base": settings.get("api_base", ""),
        "api_key_masked": masked,
    })


@app.post("/api/v1/ai-settings")
async def ve4_api_save_ai_settings(payload: dict):
    """保存 AI 配置"""
    try:
        import sqlite3
        provider = payload.get("provider", "")
        api_key = payload.get("api_key", "")
        api_base = payload.get("api_base", "").rstrip("/")
        model = payload.get("model", "")

        if not api_key or not api_base or not model:
            return JSONResponse(content={"success": False, "error": "API Key、API 地址和模型名称为必填项"})

        db_path = str(DB_PATH)
        conn = sqlite3.connect(db_path)
        _ensure_ai_settings_table(conn)
        # 先删除旧记录（单条记录表），再插入新记录
        conn.execute("DELETE FROM ai_settings")
        conn.execute("""
            INSERT INTO ai_settings (id, provider, api_key, api_base, model, updated_at)
            VALUES (1, ?, ?, ?, ?, datetime('now'))
        """, (provider, api_key, api_base, model))
        conn.commit()
        conn.close()
        return JSONResponse(content={"success": True})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.delete("/api/v1/ai-settings")
def ve4_api_delete_ai_settings():
    """删除 AI 配置（清空 api_settings 表）"""
    try:
        import sqlite3
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("DELETE FROM ai_settings")
        conn.commit()
        conn.close()
        return JSONResponse(content={"success": True})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


# ─── 量化分析端点 ───

@app.post("/api/v1/tactical/quantitative/load-data")
async def ve4_api_tactical_load_data(payload: dict):
    """加载本地数据文件（CSV/Excel）到量化分析引擎"""
    path = payload.get("path", "")
    if not path:
        return JSONResponse(content={"success": False, "error": "路径不能为空"})
    try:
        from tactical.quantitative.tools.data_source import VE4DataSourceManager
        mgr = VE4DataSourceManager()
        info = mgr.load_local_file(path)
        return JSONResponse(content={"success": True, "info": info})
    except ImportError:
        return JSONResponse(content={"success": True, "info": f"DataSourceManager 待实现 — 路径已接收: {path}"})
    except Exception as e:
        import traceback
        return JSONResponse(content={"success": False, "error": str(e), "detail": traceback.format_exc()})


@app.post("/api/v1/tactical/quantitative/generate-code")
async def ve4_api_tactical_generate_code(payload: dict):
    """根据自然语言描述生成量化策略 Python 代码"""
    prompt = payload.get("prompt", "")
    if not prompt:
        return JSONResponse(content={"success": False, "error": "策略描述不能为空"})
    try:
        from tactical.quantitative.agents.code_generator_agent import VE4CodeGeneratorAgent
        from tactical.shared.models.tactical_models import VE4AgentTask, VE4AgentTaskType
        agent = VE4CodeGeneratorAgent()
        task = VE4AgentTask(
            task_id=f"codegen_{datetime.now().strftime('%Y%m%d%H%M%S')}",
            task_type=VE4AgentTaskType.QUANT_ANALYSIS,
            goal="根据自然语言描述生成量化策略代码",
            params={"prompt": prompt},
        )
        result = await agent.execute(task)
        if result.success:
            return JSONResponse(content={
                "success": True,
                "code": result.data.get("code", ""),
                "prompt": result.data.get("prompt", ""),
                "validation": result.data.get("validation", {}),
            })
        else:
            return JSONResponse(content={"success": False, "error": result.error})
    except ImportError:
        return JSONResponse(content={"success": False, "error": "CodeGeneratorAgent 未就绪"})
    except Exception as e:
        import traceback
        return JSONResponse(content={"success": False, "error": str(e), "detail": traceback.format_exc()})


@app.post("/api/v1/tactical/quantitative/execute")
async def ve4_api_tactical_execute_strategy(payload: dict):
    """在 CodeSandbox 中执行策略代码 → 返回程序输出 + LLM 解读"""
    code = payload.get("code", "")
    if not code:
        return JSONResponse(content={"success": False, "error": "策略代码不能为空"})
    try:
        from tactical.quantitative.tools.code_sandbox import VE4CodeSandbox
        sandbox = VE4CodeSandbox()
        exec_result = await sandbox.execute(code, timeout=30)
        return JSONResponse(content={
            "success": exec_result.get("success", False),
            "stdout": exec_result.get("stdout", ""),
            "stderr": exec_result.get("stderr", ""),
            "output": exec_result.get("stdout", ""),
            "error": exec_result.get("error", ""),
            "interpretation": "LLM 解读待接入 — 策略已执行",
        })
    except ImportError:
        return JSONResponse(content={"success": False, "error": "CodeSandbox 未就绪"})
    except Exception as e:
        import traceback
        return JSONResponse(content={"success": False, "error": str(e), "detail": traceback.format_exc()})


# ─── AI Coding 应用管理端点 ───

@app.get("/api/v1/tactical/apps")
def ve4_api_tactical_apps():
    """获取 AI Coding 应用列表（含自动检测结果）"""
    try:
        from tactical.shared.workspace_manager import VE4WorkspaceManager
        mgr = VE4WorkspaceManager()
        return JSONResponse(content={"apps": mgr.ve4_ws_get_apps()})
    except ImportError:
        return JSONResponse(content={"apps": [], "error": "WorkspaceManager 未就绪"})
    except Exception as e:
        return JSONResponse(content={"error": str(e)})


@app.post("/api/v1/tactical/apps/set-default")
async def ve4_api_tactical_apps_set_default(payload: dict):
    """设置默认 AI Coding 应用"""
    key = payload.get("key", "")
    if not key:
        return JSONResponse(content={"success": False, "error": "缺少 key"})
    try:
        from tactical.shared.workspace_manager import VE4WorkspaceManager
        mgr = VE4WorkspaceManager()
        ok = mgr.ve4_ws_set_default_app(key)
        return JSONResponse(content={"success": ok})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.post("/api/v1/tactical/apps/config")
async def ve4_api_tactical_apps_config(payload: dict):
    """更新应用配置（路径等）"""
    key = payload.get("key", "")
    if not key:
        return JSONResponse(content={"success": False, "error": "缺少 key"})
    try:
        from tactical.shared.workspace_manager import VE4WorkspaceManager
        mgr = VE4WorkspaceManager()
        ok = mgr.ve4_ws_update_app(key, path=payload.get("path"))
        return JSONResponse(content={"success": ok})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.get("/api/v1/tactical/workspace-dir")
def ve4_api_tactical_workspace_dir():
    """获取当前工作目录配置"""
    try:
        from tactical.shared.workspace_manager import VE4WorkspaceManager
        mgr = VE4WorkspaceManager()
        return JSONResponse(content={"dir": str(mgr.ve4_ws_get_workspace_dir())})
    except Exception as e:
        return JSONResponse(content={"error": str(e)})


@app.post("/api/v1/tactical/workspace-dir")
async def ve4_api_tactical_set_workspace_dir(payload: dict):
    """设置工作目录"""
    dir_path = payload.get("dir", "")
    try:
        from tactical.shared.workspace_manager import VE4WorkspaceManager
        mgr = VE4WorkspaceManager()
        ok = mgr.ve4_ws_set_workspace_dir(dir_path)
        return JSONResponse(content={"success": ok})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


# ─── 策略任务端点 ───

@app.post("/api/v1/tactical/expand-strategy")
async def ve4_api_tactical_expand_strategy(payload: dict):
    """AI 扩写策略描述：将大白话转化为结构化策略文本"""
    strategy = payload.get("strategy", "")
    if not strategy:
        return JSONResponse(content={"success": False, "error": "策略描述不能为空"})
    try:
        from tactical.quantitative.agents.code_generator_agent import VE4CodeGeneratorAgent
        from tactical.shared.models.tactical_models import VE4AgentTask, VE4AgentTaskType
        agent = VE4CodeGeneratorAgent()
        task = VE4AgentTask(
            task_id=f"expand_{datetime.now().strftime('%Y%m%d%H%M%S')}",
            task_type=VE4AgentTaskType.QUANT_ANALYSIS,
            goal="将用户大白话策略描述扩写为结构化策略文本",
            params={"prompt": strategy},
        )
        # 使用规则引擎扩写（TODO: 接入 LLM）
        expanded = agent._rule_based_expand(strategy)
        return JSONResponse(content={
            "success": True,
            "original": strategy,
            "expanded": expanded,
            "note": "已扩写为结构化策略，可复制粘贴到 AI Coding 应用",
        })
    except Exception as e:
        import traceback
        return JSONResponse(content={"success": False, "error": str(e), "detail": traceback.format_exc()})


@app.post("/api/v1/tactical/launch-task")
async def ve4_api_tactical_launch_task(payload: dict):
    """创建策略任务文件 + 启动 AI Coding 应用"""
    strategy = payload.get("strategy", "")
    if not strategy:
        return JSONResponse(content={"success": False, "error": "策略描述不能为空"})
    try:
        from tactical.shared.workspace_manager import VE4WorkspaceManager
        mgr = VE4WorkspaceManager()

        # 获取当前默认应用
        apps = mgr.ve4_ws_get_apps()
        default_app = None
        for a in apps:
            if a.get("enabled"):
                default_app = a["key"]
                break

        if not default_app:
            return JSONResponse(content={"success": False, "error": "未配置 AI 编程应用，请先在「应用设置」中配置并选择一个应用"})

        result = mgr.ve4_ws_launch_app(default_app, strategy)
        return JSONResponse(content=result)
    except ImportError:
        return JSONResponse(content={"success": False, "error": "WorkspaceManager 未就绪"})
    except Exception as e:
        import traceback
        return JSONResponse(content={"success": False, "error": str(e), "detail": traceback.format_exc()})


@app.get("/api/v1/tactical/read-results")
def ve4_api_tactical_read_results():
    """读取工作区输出结果"""
    try:
        from tactical.shared.workspace_manager import VE4WorkspaceManager
        mgr = VE4WorkspaceManager()
        results = mgr.ve4_ws_read_results()
        return JSONResponse(content=results)
    except ImportError:
        return JSONResponse(content={"exists": False, "error": "WorkspaceManager 未就绪"})
    except Exception as e:
        return JSONResponse(content={"exists": False, "error": str(e)})


@app.post("/api/v1/tactical/clear-results")
async def ve4_api_tactical_clear_results(payload: dict = None):
    """清空当前策略的工作区输出和数据库结果记录"""
    payload = payload or {}
    session_id = payload.get("session_id", "")
    try:
        from tactical.shared.workspace_manager import VE4WorkspaceManager
        mgr = VE4WorkspaceManager()
        ok = mgr.ve4_ws_clear_results()
        # 如果有 session_id，同时清理数据库中该会话的结果
        if session_id:
            conn = ve4_api_get_db()
            try:
                conn.execute("DELETE FROM strategy_results WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM strategy_chat WHERE session_id = ?", (session_id,))
                conn.execute("UPDATE strategy_sessions SET status = 'pending' WHERE session_id = ?", (session_id,))
                conn.commit()
            finally:
                conn.close()
        return JSONResponse(content={"success": ok})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


# ─── 策略会话管理 ───

def ve4_api_create_strategy_session(strategy_text: str, strategy_params: dict = None) -> str:
    """创建策略会话，返回 session_id"""
    session_id = f"ss_{datetime.now().strftime('%Y%m%d%H%M%S')}_{os.urandom(2).hex()}"
    conn = ve4_api_get_db()
    try:
        conn.execute("""
            INSERT INTO strategy_sessions (session_id, strategy_text, strategy_params, status)
            VALUES (?, ?, ?, ?)
        """, (session_id, strategy_text, json.dumps(strategy_params or {}), 'pending'))
        conn.commit()
        return session_id
    finally:
        conn.close()

def ve4_api_get_strategy_session(session_id: str) -> dict:
    """获取策略会话详情"""
    conn = ve4_api_get_db()
    try:
        row = ve4_api_safe_query(conn,
            "SELECT * FROM strategy_sessions WHERE session_id = ?", (session_id,))
        if not row:
            return None
        session = dict(row[0])
        # 获取结果
        results = ve4_api_safe_query(conn,
            "SELECT * FROM strategy_results WHERE session_id = ? ORDER BY created_at DESC LIMIT 1", (session_id,))
        if results:
            session['result'] = dict(results[0])
            if session['result'].get('images_json'):
                session['result']['images'] = json.loads(session['result']['images_json'])
        # 获取对话
        chats = ve4_api_safe_query(conn,
            "SELECT role, content, created_at FROM strategy_chat WHERE session_id = ? ORDER BY created_at ASC", (session_id,))
        session['chat_history'] = [dict(c) for c in chats]
        return session
    finally:
        conn.close()

def ve4_api_list_strategy_sessions(limit: int = 20) -> List[dict]:
    """获取策略会话列表"""
    conn = ve4_api_get_db()
    try:
        rows = ve4_api_safe_query(conn,
            "SELECT session_id, strategy_text, status, created_at FROM strategy_sessions ORDER BY created_at DESC LIMIT ?",
            (limit,))
        return [dict(r) for r in rows]
    finally:
        conn.close()

def ve4_api_save_strategy_result(session_id: str, text_result: str = None, csv_result: str = None, images: list = None, llm_analysis: str = None):
    """保存策略结果（UPSERT：先删旧记录再插入）"""
    conn = ve4_api_get_db()
    try:
        # 先删除该 session 的旧结果（避免重复累积）
        conn.execute("DELETE FROM strategy_results WHERE session_id = ?", (session_id,))
        conn.execute("""
            INSERT INTO strategy_results (session_id, text_result, csv_result, images_json, llm_analysis)
            VALUES (?, ?, ?, ?, ?)
        """, (session_id, text_result or '', csv_result or '', json.dumps(images or []), llm_analysis or ''))
        conn.execute("UPDATE strategy_sessions SET status = 'completed', updated_at = ? WHERE session_id = ?",
                     (datetime.now().isoformat(), session_id))
        conn.commit()
    finally:
        conn.close()

def ve4_api_add_chat_message(session_id: str, role: str, content: str):
    """添加对话消息"""
    conn = ve4_api_get_db()
    try:
        conn.execute("""
            INSERT INTO strategy_chat (session_id, role, content)
            VALUES (?, ?, ?)
        """, (session_id, role, content))
        conn.commit()
    finally:
        conn.close()


@app.post("/api/v1/tactical/strategy-sessions")
async def ve4_api_tactical_create_session(payload: dict):
    """创建策略会话"""
    strategy = payload.get("strategy", "")
    if not strategy:
        return JSONResponse(content={"success": False, "error": "策略描述不能为空"})
    try:
        session_id = ve4_api_create_strategy_session(strategy, payload.get("params"))
        return JSONResponse(content={"success": True, "session_id": session_id})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.get("/api/v1/tactical/strategy-sessions")
def ve4_api_tactical_list_sessions(limit: int = 20):
    """获取策略会话列表"""
    try:
        sessions = ve4_api_list_strategy_sessions(limit)
        return JSONResponse(content={"success": True, "sessions": sessions})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.get("/api/v1/tactical/strategy-sessions/{session_id}")
def ve4_api_tactical_get_session(session_id: str):
    """获取策略会话详情（含结果、对话历史）"""
    try:
        session = ve4_api_get_strategy_session(session_id)
        if not session:
            return JSONResponse(content={"success": False, "error": "会话不存在"})
        return JSONResponse(content={"success": True, "session": session})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.post("/api/v1/tactical/strategy-sessions/{session_id}/chat")
async def ve4_api_tactical_chat(session_id: str, payload: dict):
    """与策略会话进行对话"""
    message = payload.get("message", "")
    if not message:
        return JSONResponse(content={"success": False, "error": "消息不能为空"})
    try:
        # 保存用户消息
        ve4_api_add_chat_message(session_id, "user", message)
        # TODO: 接入 LLM 获取回复
        reply = f"[LLM 回复待接入]\n\n你说了：{message[:100]}"
        ve4_api_add_chat_message(session_id, "assistant", reply)
        return JSONResponse(content={"success": True, "reply": reply})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.post("/api/v1/tactical/strategy-sessions/{session_id}/save-result")
async def ve4_api_tactical_save_result(session_id: str, payload: dict):
    """保存策略结果到会话"""
    try:
        ve4_api_save_strategy_result(
            session_id,
            text_result=payload.get("text_result", ""),
            csv_result=payload.get("csv_result", ""),
            images=payload.get("images", []),
        )
        return JSONResponse(content={"success": True})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.post("/api/v1/tactical/analyze-results")
async def ve4_api_tactical_analyze_results(payload: dict):
    """VE4 LLM 分析策略结果的有效性"""
    text_result = payload.get("text_result", "")
    csv_preview = payload.get("csv_preview", "")
    session_id = payload.get("session_id", "")
    if not text_result and not csv_preview:
        return JSONResponse(content={"success": False, "error": "无可分析的结果"})

    # TODO: 接入 LLM（tactical_signal_interpret → cloud_beta）
    # 当前返回 fallback 分析
    combined = (text_result or "") + "\n\n" + (csv_preview or "")
    analysis = f"[LLM 分析待接入]\n\n以下为原始结果预览（前 500 字）：\n{combined[:500]}"
    return JSONResponse(content={
        "success": True,
        "analysis": analysis,
    })


@app.post("/api/v1/tactical/quantitative/assist-strategy")
async def ve4_api_tactical_assist_strategy(payload: dict):
    """AI 辅助完善策略描述"""
    idea = payload.get("idea", "")
    if not idea:
        return JSONResponse(content={"success": False, "error": "投资思路不能为空"})
    # TODO: 接入 LLM
    return JSONResponse(content={
        "success": True,
        "strategy": f"[AI 辅助待接入]\n\n基于你的思路，建议将策略完善为：\n\n策略名称：待定\n数据源：沪深300指数日K线（akshare）\n核心逻辑：{idea}\n持有期：30天\n止盈条件：收益率达到 X%\n止损条件：亏损达到 Y%\n回测区间：2019年至今\n输出要求：result.txt + result.csv + chart.png",
    })


# ════════════════════════════════════════════════════════════════
# VE管家 Chatbot API
# ════════════════════════════════════════════════════════════════

@app.post("/api/v1/chat")
def ve5_api_chat(payload: dict):
    """
    VE管家主聊天API。
    payload: {session_id, message, force_skill}
    """
    try:
        from core.ve5_chatbot import ve5_chatbot_process, ve5_chat_session_create
        from core.ve5_chatbot.session_manager import ve5_chat_session_get

        session_id = payload.get("session_id", "")
        message = payload.get("message", "").strip()
        force_skill = payload.get("force_skill", "")

        if not message:
            return JSONResponse(content={"success": False, "error": "消息不能为空"})

        # 如果没有session_id，创建新会话
        if not session_id:
            session_id = ve5_chat_session_create()

        result = ve5_chatbot_process(session_id, message, force_skill)
        return JSONResponse(content={
            "success": True,
            "session_id": session_id,
            "reply": result.get("reply", ""),
            "reasoning": result.get("reasoning", ""),
            "skill": result.get("skill", "general_chat"),
            "skill_result": result.get("skill_result", {}),
            "cards": result.get("cards", []),
            "actions": result.get("actions", []),
        })
    except Exception as e:
        import logging as _lg
        _lg.getLogger("ve5.chat").error("chat error: %s", e, exc_info=True)
        return JSONResponse(content={"success": False, "error": "服务暂时不可用，请检查AI配置后重试"})


@app.get("/api/v1/chat/latest-session")
def ve5_api_chat_latest_session():
    """获取最近活跃的session_id（用于恢复上次对话）"""
    try:
        from core.ve5_chatbot.session_manager import ve5_chat_get_latest_session_id
        sid = ve5_chat_get_latest_session_id()
        return JSONResponse(content={"success": True, "session_id": sid})
    except Exception as e:
        return JSONResponse(content={"success": False, "session_id": ""})


@app.get("/api/v1/chat/sessions")
def ve5_api_chat_sessions():
    """获取所有会话列表"""
    try:
        from core.ve5_chatbot.session_manager import ve5_chat_session_list
        sessions = ve5_chat_session_list()
        return JSONResponse(content={"success": True, "sessions": sessions})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": "服务暂时不可用"})


@app.post("/api/v1/chat/sessions")
def ve5_api_chat_session_create(payload: dict = None):
    """创建新会话"""
    try:
        from core.ve5_chatbot.session_manager import ve5_chat_session_create
        session_id = ve5_chat_session_create(payload.get("title", "新会话") if payload else "新会话")
        return JSONResponse(content={"success": True, "session_id": session_id})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": "服务暂时不可用"})


@app.delete("/api/v1/chat/sessions/{session_id}")
def ve5_api_chat_session_delete(session_id: str):
    """删除指定会话"""
    try:
        from core.ve5_chatbot.session_manager import ve5_chat_session_delete
        ve5_chat_session_delete(session_id)
        return JSONResponse(content={"success": True})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": "服务暂时不可用"})


@app.get("/api/v1/chat/sessions/{session_id}/messages")
def ve5_api_chat_messages(session_id: str):
    """获取指定会话的消息历史"""
    try:
        from core.ve5_chatbot.session_manager import ve5_chat_session_get
        messages = ve5_chat_session_get(session_id)
        return JSONResponse(content={"success": True, "messages": messages})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": "服务暂时不可用"})


@app.post("/api/v1/chat/sessions/{session_id}/append")
def ve5_api_chat_append_message(session_id: str, payload: dict = None):
    """追加消息到指定会话（用于经验执行等非对话消息的持久化）"""
    try:
        from core.ve5_chatbot.session_manager import ve5_chat_session_append
        p = payload or {}
        role = p.get("role", "user")
        content = p.get("content", "")
        skill_name = p.get("skill", "")
        metadata = p.get("metadata", {})
        ve5_chat_session_append(session_id, role, content, skill_name, metadata)
        return JSONResponse(content={"success": True})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


# ─── 用户位置管理 ───

@app.get("/api/v1/user/location")
def ve5_api_get_user_location():
    """获取用户设置的位置信息"""
    try:
        from core.regional_price import get_location_settings
        return JSONResponse(content={"success": True, "data": get_location_settings()})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": "服务暂时不可用"})


@app.post("/api/v1/user/location")
def ve5_api_set_user_location(payload: dict = None):
    """设置用户位置信息（省/市/区/县 + 高德Key）"""
    try:
        from core.regional_price import set_user_location
        p = payload or {}
        set_user_location(
            province=p.get("province", ""),
            city=p.get("city", ""),
            district=p.get("district", ""),
            county=p.get("county", ""),
            address=p.get("address", ""),
            amap_key=p.get("amap_key", ""),
        )
        return JSONResponse(content={"success": True})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": "服务暂时不可用"})


# ─── 消费价格 RAG ───

@app.post("/api/v1/consumer-price")
def ve5_api_consumer_price_store(payload: dict = None):
    """录入一条消费价格记录"""
    try:
        from core.consumer_price_rag import ve5_price_rag_store
        p = payload or {}
        ok = ve5_price_rag_store(
            item=p.get("item", ""),
            price=p.get("price", 0),
            spec=p.get("spec", ""),
            merchant=p.get("merchant", ""),
            location=p.get("location", ""),
            category=p.get("category", ""),
            source=p.get("source", "manual"),
            date=p.get("date"),
        )
        return JSONResponse(content={"success": ok})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": "服务暂时不可用"})


@app.get("/api/v1/consumer-price")
def ve5_api_consumer_price_search(
    q: str = "",
    location: str = None,
    category: str = None,
    n: int = 5,
):
    """搜索消费价格记录"""
    try:
        from core.consumer_price_rag import ve5_price_rag_search
        results = ve5_price_rag_search(
            query=q,
            n_results=n,
            location_filter=location,
            category_filter=category,
        )
        return JSONResponse(content={"success": True, "results": results})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": "服务暂时不可用"})


@app.get("/api/v1/consumer-price/stats")
def ve5_api_consumer_price_stats():
    """获取消费价格 RAG 统计"""
    try:
        from core.consumer_price_rag import ve5_price_rag_stats
        return JSONResponse(content={"success": True, **ve5_price_rag_stats()})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": "服务暂时不可用"})


# ─── RAG Hub 集中管理 ───

@app.get("/api/v1/rag-hub/modules")
def ve5_api_rag_hub_modules():
    """获取所有 RAG 模块列表"""
    try:
        from core.rag_hub import rag_hub_list_modules
        return JSONResponse(content={"success": True, "modules": rag_hub_list_modules()})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.get("/api/v1/rag-hub/modules/{module_id}")
def ve5_api_rag_hub_module_detail(module_id: str):
    """获取单个 RAG 模块详情（含业务逻辑描述）"""
    try:
        from core.rag_hub import rag_hub_get_module
        m = rag_hub_get_module(module_id)
        if not m:
            return JSONResponse(content={"success": False, "error": f"模块 {module_id} 不存在"}, status_code=404)
        return JSONResponse(content={"success": True, "module": m})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.get("/api/v1/rag-hub/modules/{module_id}/records")
def ve5_api_rag_hub_records(module_id: str, limit: int = 20, offset: int = 0):
    """获取 RAG 模块的记录列表"""
    try:
        from core.rag_hub import rag_hub_get_records
        result = rag_hub_get_records(module_id, limit, offset)
        return JSONResponse(content={"success": True, **result})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.get("/api/v1/rag-hub/modules/{module_id}/vectors")
def ve5_api_rag_hub_vectors(module_id: str, q: str = "", limit: int = 5):
    """获取 RAG 模块的典型向量（语义检索示例）"""
    try:
        from core.rag_hub import rag_hub_get_vectors
        vectors = rag_hub_get_vectors(module_id, q, limit)
        return JSONResponse(content={"success": True, "vectors": vectors})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.delete("/api/v1/rag-hub/modules/{module_id}/records/{record_id}")
def ve5_api_rag_hub_delete_record(module_id: str, record_id: str):
    """删除 RAG 记录"""
    try:
        from core.rag_hub import rag_hub_delete_record
        ok = rag_hub_delete_record(module_id, record_id)
        return JSONResponse(content={"success": ok})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.get("/api/v1/rag-hub/storage")
def ve5_api_rag_hub_storage():
    """获取所有 RAG 存储的磁盘占用"""
    try:
        from core.rag_hub import rag_hub_get_storage_info
        return JSONResponse(content={"success": True, **rag_hub_get_storage_info()})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.post("/api/v1/rag-hub/sync")
def ve5_api_rag_hub_sync(payload: dict = None):
    """触发 RAG 同步（全部或指定模块）"""
    try:
        from core.rag_sync_engine import rag_sync_all, rag_sync_module
        p = payload or {}
        module_id = p.get("module_id")
        if module_id:
            result = rag_sync_module(module_id)
            return JSONResponse(content={"success": True, "result": result})
        else:
            results = rag_sync_all()
            return JSONResponse(content={"success": True, "results": results})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.get("/api/v1/rag-hub/sync-status")
def ve5_api_rag_hub_sync_status():
    """获取所有模块的同步状态"""
    try:
        from core.rag_sync_engine import rag_get_sync_status
        return JSONResponse(content={"success": True, "status": rag_get_sync_status()})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.post("/api/v1/rag-hub/search")
def ve5_api_rag_hub_search(payload: dict = None):
    """LLM 关键词联想搜索"""
    try:
        from core.rag_sync_engine import rag_llm_expand_keywords
        from core.rag_sqlite_store import fin_search, price_search, report_search
        p = payload or {}
        query = p.get("query", "")
        module_id = p.get("module_id", "financial_rag")
        use_llm = p.get("use_llm", True)
        limit = p.get("limit", 5)

        # LLM 联想扩展关键词
        expanded = [query]
        if use_llm and query:
            expanded = rag_llm_expand_keywords(query, module_id)

        # 用扩展后的关键词搜索
        all_results = []
        for kw in expanded:
            if module_id == "financial_rag":
                all_results.extend(fin_search(kw, limit))
            elif module_id == "consumer_price_rag":
                all_results.extend(price_search(kw, limit))
            elif module_id == "report_rag":
                all_results.extend(report_search(kw, limit))

        # 去重
        seen_ids = set()
        unique = []
        for r in all_results:
            rid = r.get("id")
            if rid not in seen_ids:
                seen_ids.add(rid)
                unique.append(r)

        return JSONResponse(content={
            "success": True,
            "query": query,
            "expanded_keywords": expanded,
            "results": unique[:limit * 2],
        })
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


# ─── Cloud Bot 云端渠道管理 ───

@app.get("/api/v1/cloud-bots")
def ve5_api_cloud_bots():
    """获取所有 bot 配置"""
    try:
        from core.cloud_bot_manager import bot_list
        return JSONResponse(content={"success": True, "bots": bot_list()})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.get("/api/v1/cloud-bots/{bot_id}")
def ve5_api_cloud_bot_detail(bot_id: str):
    """获取单个 bot 配置"""
    try:
        from core.cloud_bot_manager import bot_get
        bot = bot_get(bot_id)
        if not bot:
            return JSONResponse(content={"success": False, "error": "Bot 不存在"}, status_code=404)
        return JSONResponse(content={"success": True, "bot": bot})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.post("/api/v1/cloud-bots")
def ve5_api_cloud_bot_create(payload: dict = None):
    """创建 bot 配置"""
    try:
        from core.cloud_bot_manager import bot_create
        p = payload or {}
        ok = bot_create(
            bot_id=p.get("bot_id", ""),
            name=p.get("name", ""),
            source_module=p.get("source_module", ""),
            channel_type=p.get("channel_type", "feishu"),
            access_token=p.get("access_token", ""),
            secret=p.get("secret", ""),
            webhook_url=p.get("webhook_url", ""),
            trigger_rules=p.get("trigger_rules", {}),
            message_recovery=p.get("message_recovery", False),
            recovery_config=p.get("recovery_config", {}),
        )
        return JSONResponse(content={"success": ok})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.put("/api/v1/cloud-bots/{bot_id}")
def ve5_api_cloud_bot_update(bot_id: str, payload: dict = None):
    """更新 bot 配置"""
    try:
        from core.cloud_bot_manager import bot_update
        p = payload or {}
        ok = bot_update(bot_id, **p)
        return JSONResponse(content={"success": ok})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.delete("/api/v1/cloud-bots/{bot_id}")
def ve5_api_cloud_bot_delete(bot_id: str):
    """删除 bot 配置"""
    try:
        from core.cloud_bot_manager import bot_delete
        return JSONResponse(content={"success": bot_delete(bot_id)})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.post("/api/v1/cloud-bots/{bot_id}/toggle")
def ve5_api_cloud_bot_toggle(bot_id: str):
    """开关 bot"""
    try:
        from core.cloud_bot_manager import bot_toggle
        bot = bot_toggle(bot_id)
        if not bot:
            return JSONResponse(content={"success": False, "error": "Bot 不存在"}, status_code=404)
        return JSONResponse(content={"success": True, "bot": bot})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.post("/api/v1/cloud-bots/{bot_id}/send")
def ve5_api_cloud_bot_send(bot_id: str, payload: dict = None):
    """手动触发 bot 发送消息"""
    try:
        from core.cloud_bot_manager import bot_send_message
        p = payload or {}
        result = bot_send_message(bot_id, p.get("content", ""), p.get("message_type", "manual"))
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.get("/api/v1/cloud-bots/{bot_id}/logs")
def ve5_api_cloud_bot_logs(bot_id: str, limit: int = 20):
    """获取 bot 发送日志"""
    try:
        from core.cloud_bot_manager import bot_get_logs
        return JSONResponse(content={"success": True, "logs": bot_get_logs(bot_id, limit)})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


# ─── Experience Layer 经验系统 ───

@app.get("/api/v1/experiences")
def ve5_api_experience_list(type: str = None, level: str = None):
    """获取经验列表"""
    try:
        from core.experience_engine import exp_engine_list
        exps = exp_engine_list(type, level)
        return JSONResponse(content={"success": True, "experiences": exps})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.get("/api/v1/experiences/runtime")
def ve5_api_experience_runtime():
    """获取 Experience Runtime 快照"""
    try:
        from core.experience_engine import exp_engine_get_runtime
        rt = exp_engine_get_runtime()
        return JSONResponse(content={"success": True, "runtime": rt})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.get("/api/v1/experiences/list")
def ve5_api_experience_list(exp_type: str = "", level: str = ""):
    """获取所有经验列表"""
    try:
        from core.experience_store import exp_list
        exps = exp_list(exp_type=exp_type or None, level=level or None)
        return JSONResponse(content={"success": True, "experiences": exps})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.get("/api/v1/experiences/{exp_id}")
def ve5_api_experience_detail(exp_id: str):
    """获取单个经验详情"""
    try:
        from core.experience_store import exp_get
        exp = exp_get(exp_id)
        if not exp:
            return JSONResponse(content={"success": False, "error": "经验不存在"}, status_code=404)
        return JSONResponse(content={"success": True, "experience": exp})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.post("/api/v1/experiences/save-from-report")
async def ve5_api_experience_save_from_report(payload: dict = None):
    """
    从已保存的报告中创建经验 / 预览 schema。
    preview_only=true 时不保存，只返回 compiled schema。
    """
    try:
        p = payload or {}
        report_id = p.get("report_id", "")
        report_type = p.get("type", "")
        preview_only = p.get("preview_only", False)
        overwrite = p.get("overwrite", {})

        if not report_id:
            return JSONResponse(content={"success": False, "error": "缺少 report_id"})

        from core.ve5_chatbot.report_store import ve5_report_get
        report = ve5_report_get(report_id)
        if not report:
            return JSONResponse(content={"success": False, "error": "报告不存在"}, status_code=404)

        data = report.get("data", {})

        # ── Preview 模式: 返回编译后的 schema 但不保存 ──
        if preview_only:
            preview = _build_experience_preview(report_id, report_type, data)
            if preview:
                return JSONResponse(content={"success": True, "preview": preview})
            return JSONResponse(content={"success": False, "error": "无法生成预览"})

        # ── 创建经验 ──
        # 统一走 LLM Compiler 路径（与 preview 一致）
        from core.experience_engine import exp_llm_compile_experience
        schema = exp_llm_compile_experience(data, report_type)

        if not schema or not schema.get("name"):
            return JSONResponse(content={"success": False, "error": "LLM 无法编译经验"})

        from core.experience_store import exp_create, exp_update
        # 类型归一化：life_plan → life_planner，避免 executor 无法识别
        from core.experience_engine import _normalize_exp_type
        exp_id = exp_create(
             source_report_id=report_id,
             exp_type=_normalize_exp_type(report_type),
             name=schema.get("name", "经验"),
             description=schema.get("description", ""),
             origin={
                 "type": "user_confirmed",
                 "episode_ids": [],
                 "created_reason": f"用户保存{report_type}为长期经验",
                 "confirmed_at": __import__('datetime').datetime.now().isoformat(),
                 "compiled_by": "LLM",
             },
             trigger_event=schema.get("trigger_event", "monthly_check"),
             trigger_frequency=schema.get("trigger_frequency", "recurring"),
             workflow=schema.get("workflow", []),
             template_json=schema.get("template_json", {}),
             decision_rules=schema.get("decision_rules", []),
             exception_rules=schema.get("exception_rules", []),
             context_variables=schema.get("context_variables", []),
             tags=schema.get("tags", []),
             llm_required=True,
         )
        # 追加 result_page 到 user_extensions
        if exp_id and schema.get("result_page"):
            exp_update(exp_id, user_extensions={"result_page": schema["result_page"]})

        # ── 用户手动编辑覆盖 ──
        if overwrite:
            from core.experience_store import exp_update
            allowed = ["name", "description", "workflow", "template_json",
                       "decision_rules", "exception_rules", "trigger_event"]
            update_kwargs = {k: v for k, v in overwrite.items() if k in allowed and v}
            if update_kwargs:
                # 标记为用户编辑
                update_kwargs["is_user_edited"] = True
                exp_update(exp_id, **update_kwargs)
                logger.info(f"[API] experience {exp_id} 用户手动编辑: {list(update_kwargs.keys())}")

        return JSONResponse(content={"success": True, "exp_id": exp_id, "message": "经验已创建"})

    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.put("/api/v1/experiences/{exp_id}")
async def ve5_api_experience_update(exp_id: str, payload: dict = None):
    """手动编辑经验 (workflow/template/rules)"""
    try:
        from core.experience_store import exp_update, exp_get
        exp = exp_get(exp_id)
        if not exp:
            return JSONResponse(content={"success": False, "error": "经验不存在"}, status_code=404)

        p = payload or {}
        allowed = [
            "name", "description", "trigger_event", "trigger_frequency",
            "workflow", "template_json", "decision_rules", "exception_rules",
            "context_variables", "tags", "is_user_edited",
        ]
        update_kwargs = {k: v for k, v in p.items() if k in allowed}
        if update_kwargs:
            ok = exp_update(exp_id, **update_kwargs)
            return JSONResponse(content={"success": ok})
        return JSONResponse(content={"success": False, "error": "无有效更新字段"})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.post("/api/v1/experiences/edit-assist")
async def ve5_api_experience_edit_assist(payload: dict = None):
    """
    经验编辑助手: 用户输入自然语言修改需求, LLM 生成对应的 experience schema 编辑建议。
    输入: { "report_id": str, "user_edit_request": str, "current_schema": dict }
    返回: { "edited_schema": dict, "explanation": str }
    """
    try:
        p = payload or {}
        report_id = p.get("report_id", "")
        user_request = p.get("user_edit_request", "")
        current = p.get("current_schema", {})

        if not report_id or not user_request:
            return JSONResponse(content={"success": False, "error": "缺少 report_id 或 user_edit_request"})

        from core.ve5_chatbot.report_store import ve5_report_get
        report = ve5_report_get(report_id)
        if not report:
            return JSONResponse(content={"success": False, "error": "报告不存在"}, status_code=404)

        from core.ai_gateway import ve4_ai_call

        data = report.get("data", {})
        system = """你是经验脚本编辑器。用户想修改一份即将创建的经验脚本。

经验是一段半程序、半知识的个人化代码,结构如下:
{
  "name": "经验名称",
  "description": "描述",
  "trigger_event": "触发事件 (如 monthly_check)",
  "trigger_frequency": "recurring | on_demand",
  "workflow": [{"step": N, "action":"xxx", "description":".."}],
  "template_json": {"key": "文本 (可用 {variable} 占位符)"},
  "decision_rules": [{"condition": "key op value", "action": "..."}],
  "exception_rules": [{"condition": "描述", "action": "call_llm"}],
  "context_variables": ["变量名列表"],
  "tags": ["标签"]
}

根据用户的修改需求,调整当前 schema 并输出调整后的完整 JSON。只输出 JSON,不要其他文字。"""

        prompt = f"""当前经验 schema:
{json.dumps(current, ensure_ascii=False, indent=2)}

原始 LLM 分析报告数据 (参考上下文):
{json.dumps(data, ensure_ascii=False, indent=2)[:2000]}

用户修改需求:
{user_request}

请输出调整后的完整经验 schema JSON。"""

        result = ve4_ai_call(
            task_type="experience_edit",
            system=system,
            prompt=prompt,
            format_type="json",
            complexity="high",
            max_tokens=2048,
        )

        if result.success and result.text:
            edited = json.loads(result.text)
            explanation = edited.pop("explanation", "") if isinstance(edited, dict) else ""
            return JSONResponse(content={
                "success": True,
                "edited_schema": edited,
                "explanation": explanation or "已根据你的需求调整经验脚本",
            })

        return JSONResponse(content={"success": False, "error": "LLM 编辑失败"})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


# ════════════════ Experience Preview Helper ════════════════

def _build_extra_context(output: dict) -> dict:
    """从 output 中计算额外变量，供 result_page 模板使用"""
    extra = {}
    # remaining_amount
    target = output.get("target_amount", 0)
    current = output.get("current_amount", 0)
    if target:
        extra["remaining_amount"] = int(target - current)
        extra["progress_pct"] = int(current / target * 100) if target else 0
    # in_progress_goals_count
    extra["in_progress_goals_count"] = output.get("goals_count", 1)
    return extra


_SECTION_LABEL_MAP = {
    "normal": "常规概览",
    "ahead": "进度超前",
    "behind": "进度落后",
    "achieved": "已达成",
    "warning": "预警提示",
    "hero": "概览",
    "details": "明细",
    "summary": "摘要",
    "budget": "预算",
    "progress": "进度",
    "goals": "目标",
    "assets": "资产",
    "savings": "储蓄",
    "income": "收入",
    "expense": "支出",
}

def _section_label(key: str) -> str:
    """将 section key 转为人类可读标签"""
    if key in _SECTION_LABEL_MAP:
        return _SECTION_LABEL_MAP[key]
    # 首字母大写
    return key.replace("_", " ").capitalize()


def _build_summary_sections_from_context(ctx: dict, exp: dict) -> list:
    """当 output 没有可用字符串时，从 exec_context 构建人类可读的摘要 sections"""
    sections = []

    # 目标概览
    if ctx.get("goal_name") or ctx.get("target_amount"):
        goal_name = ctx.get("goal_name", "目标")
        target = ctx.get("target_amount", 0)
        progress = ctx.get("progress", ctx.get("goal_progress", 0))
        current = ctx.get("current_amount", 0)
        sections.append({
            "id": "goal_overview",
            "label": "目标概览",
            "template": f"{goal_name}: 进度 {progress}% (¥{current:,.0f} / ¥{target:,.0f})",
            "filled": f"{goal_name}: 进度 {progress}% (¥{current:,.0f} / ¥{target:,.0f})",
        })

    # 财务概览
    if ctx.get("total_assets"):
        assets = ctx.get("total_assets", 0)
        income = ctx.get("monthly_income", 0)
        expense = ctx.get("monthly_expense", 0)
        savings = ctx.get("monthly_savings", 0)
        sections.append({
            "id": "financial_overview",
            "label": "财务概览",
            "template": f"总资产 ¥{assets:,.0f} | 月收入 ¥{income:,.0f} | 月支出 ¥{expense:,.0f} | 月结余 ¥{savings:,.0f}",
            "filled": f"总资产 ¥{assets:,.0f} | 月收入 ¥{income:,.0f} | 月支出 ¥{expense:,.0f} | 月结余 ¥{savings:,.0f}",
        })

    # 差距分析
    if ctx.get("gap") is not None or ctx.get("months_needed") is not None:
        gap = ctx.get("gap", 0)
        months = ctx.get("months_needed", 0)
        monthly_target = ctx.get("monthly_target", 0)
        sections.append({
            "id": "gap_analysis",
            "label": "差距分析",
            "template": f"距目标差 {gap}%, 预计 {months} 个月达成, 月目标 ¥{monthly_target:,.0f}",
            "filled": f"距目标差 {gap}%, 预计 {months} 个月达成, 月目标 ¥{monthly_target:,.0f}",
        })

    if not sections:
        sections.append({
            "id": "info",
            "label": "运行结果",
            "template": "经验已执行完成，暂无可用数据。",
            "filled": "经验已执行完成，暂无可用数据。",
        })

    return sections


def _build_life_plan_replay_sections(output: dict) -> list:
    """将生活规划回放数据转换为 result_page sections"""
    sections = []

    # 预算概览
    budget = output.get("weekly_budget", 0)
    breakdown = output.get("budget_breakdown", {})
    bd_lines = "\n".join(f"  {k}: ¥{v}" for k, v in breakdown.items()) if breakdown else ""
    sections.append({
        "id": "budget",
        "label": "预算概览",
        "template": f"周预算：¥{budget}\n{bd_lines}",
        "filled": f"周预算：¥{budget}\n{bd_lines}",
    })

    # 每日食谱
    recipes = output.get("recipes", [])
    if recipes:
        recipe_lines = []
        for r in recipes:
            day = r.get("day", "")
            bf = r.get("breakfast", "")
            ln = r.get("lunch", "")
            dn = r.get("dinner", "")
            cost = r.get("estimated_cost", 0)
            recipe_lines.append(f"{day}: 早餐({bf}) | 午餐({ln}) | 晚餐({dn}) ≈¥{cost}")
        recipe_text = "\n".join(recipe_lines)
        sections.append({
            "id": "recipes",
            "label": "每日食谱",
            "template": recipe_text,
            "filled": recipe_text,
        })

    # 购物清单
    shopping = output.get("shopping_list", [])
    if shopping:
        shop_lines = []
        for s in shopping:
            item = s.get("item", "")
            price = s.get("estimated_price", 0)
            prio = s.get("priority", "")
            cat = s.get("category", "")
            shop_lines.append(f"- {item} ¥{price} [{prio}优先] ({cat})")
        shop_text = "\n".join(shop_lines)
        sections.append({
            "id": "shopping",
            "label": "购物清单",
            "template": shop_text,
            "filled": shop_text,
        })

    # 娱乐安排
    entertainment = output.get("entertainment", [])
    if entertainment:
        ent_lines = []
        for e in entertainment:
            act = e.get("activity", "")
            day = e.get("day", "")
            budget_e = e.get("budget", 0)
            reason = e.get("reason", "")
            ent_lines.append(f"- {act} ({day}) 预算¥{budget_e}: {reason}")
        ent_text = "\n".join(ent_lines)
        sections.append({
            "id": "entertainment",
            "label": "娱乐安排",
            "template": ent_text,
            "filled": ent_text,
        })

    # 免责声明
    disclaimer = output.get("disclaimer", "")
    if disclaimer:
        sections.append({
            "id": "disclaimer",
            "label": "价格说明",
            "template": disclaimer,
            "filled": disclaimer,
        })

    return sections


def _build_experience_preview(report_id: str, report_type: str, data: dict) -> dict:
    """
    构建经验 schema 预览。优先走 LLM 编译器，失败时回退到规则编译。
    """
    from core.experience_engine import exp_llm_compile_experience
    schema = exp_llm_compile_experience(data, report_type)

    preview = {
        "_preview_note": "这是 LLM 根据你的对话分析结果编译的经验脚本。可选择「直接采纳」或「手动编辑」。",
    }
    preview.update(schema)
    return preview


@app.post("/api/v1/experiences/{exp_id}/execute")
def ve5_api_experience_execute(exp_id: str, payload: dict = None):
    """手动触发经验执行 — 通过 exp_execute 统一路径，自动更新 confidence"""
    try:
        from core.experience_engine import exp_engine_execute
        from core.experience_store import exp_get
        context = payload or {}
        # 确保 triggered_by 被标记为手动运行
        if not context.get("triggered_by"):
            context["triggered_by"] = "manual_run"
        result = exp_engine_execute(exp_id, context)
        # 包装: 确保前端能识别 success, 并附带 result_page
        if result.get("decision") or result.get("output"):
            result["success"] = True
        elif result.get("error"):
            result["success"] = False
        else:
            result["success"] = True
        # 附带回 exp metadata for result page
        exp = exp_get(exp_id)
        if exp:
            result["experience_name"] = exp.get("name","")
            result["experience_type"] = exp.get("type","")
            result["level"] = exp.get("level","")
            result["confidence"] = exp.get("confidence", 0)
            result["result_page"] = (exp.get("user_extensions") or {}).get("result_page", {})
        # exec_context 和 data_changes 已由 executor 返回在 result 中
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.get("/api/v1/experiences/{exp_id}/run-result")
def ve5_api_experience_run_result(exp_id: str):
    """获取经验执行结果（用于结果展示页）。所有级别都填充模板，不再显示原始 {{变量}}。"""
    try:
        from core.experience_engine import exp_engine_execute
        from core.experience_store import exp_get
        exp = exp_get(exp_id)
        if not exp:
            return JSONResponse(content={"success": False, "error": "经验不存在"}, status_code=404)

        result = exp_engine_execute(exp_id, {})
        output = result.get("output", {})
        exec_context = result.get("exec_context", {})
        data_changes = result.get("data_changes", [])
        user_ext = exp.get("user_extensions") or {}
        result_page = user_ext.get("result_page", {}) if isinstance(user_ext, dict) else {}

        # ── 生活规划回放模式：从 output 直接生成 result_page ──
        if output.get("_replay_mode") and output.get("weekly_budget"):
            sections = _build_life_plan_replay_sections(output)
            result_page = {
                "title": "本周生活规划（习惯回放）",
                "description": output.get("_replay_message", "基于历史习惯回放"),
                "sections": sections,
            }
        # ── 旧经验没有 result_page：从 template_json + output 自动生成 ──
        elif not result_page.get("sections"):
            sections = []
            for key, val in output.items():
                if key.startswith("_") or not isinstance(val, str) or len(val) < 5:
                    continue
                sections.append({"id": key, "label": _section_label(key), "template": val, "filled": val})
            if not sections:
                # 如果 output 中没有可用字符串字段，从 exec_context 构建摘要
                sections = _build_summary_sections_from_context(exec_context, exp)
            result_page = {
                "title": exp.get("name", "经验运行结果"),
                "description": exp.get("description", ""),
                "sections": sections,
            }

        # ── 统一填充 result_page 模板 ──
        # 使用 exec_context + output 双重来源填充
        fill_sources = {}
        # 1. exec_context 中的所有标量值
        for k, v in exec_context.items():
            if isinstance(v, (str, int, float)):
                fill_sources[k] = v
        # 2. output 中的值覆盖 exec_context（output 优先级更高）
        for k, v in output.items():
            if isinstance(v, (str, int, float)):
                fill_sources[k] = v
        # 3. 额外计算的变量
        extra_vars = _build_extra_context(fill_sources)
        fill_sources.update(extra_vars)

        filled_sections = []
        for sec in result_page.get("sections", []):
            tmpl = sec.get("template", "") or sec.get("filled", "")
            # 用 fill_sources 填充所有 {variable} 占位符
            for ctx_key, ctx_val in list(fill_sources.items()):
                if isinstance(ctx_val, (str, int, float)) and f'{{{ctx_key}}}' in tmpl:
                    tmpl = tmpl.replace(f'{{{ctx_key}}}', str(ctx_val))
            # 清理未填充的 {{variable}} 和 {variable} 占位符 → 显示 —
            import re as _re
            tmpl = _re.sub(r'\{\{(\w+)\}\}', '—', tmpl)
            tmpl = _re.sub(r'\{(\w+)(?::[^}]*)?\}', '—', tmpl)
            filled_sections.append({**sec, "filled": tmpl})

        # ── 填充 title 和 description 的模板变量 ──
        rp_title = result_page.get("title", exp.get("name", ""))
        rp_desc = result_page.get("description", "")
        for ctx_key, ctx_val in list(fill_sources.items()):
            if isinstance(ctx_val, (str, int, float)):
                if f'{{{ctx_key}}}' in rp_title:
                    rp_title = rp_title.replace(f'{{{ctx_key}}}', str(ctx_val))
                if f'{{{ctx_key}}}' in rp_desc:
                    rp_desc = rp_desc.replace(f'{{{ctx_key}}}', str(ctx_val))
        # 清理未填充的占位符
        rp_title = _re.sub(r'\{(\w+)(?::[^}]*)?\}', '', rp_title).strip()
        rp_desc = _re.sub(r'\{(\w+)(?::[^}]*)?\}', '', rp_desc).strip()

        return JSONResponse(content={
            "success": True,
            "exp_id": exp_id,
            "experience_name": exp.get("name", ""),
            "experience_description": exp.get("description", ""),
            "level": exp.get("level", ""),
            "confidence": exp.get("confidence", 0),
            "trigger_event": exp.get("trigger_event", ""),
            "decision": result.get("decision", "unknown"),
            "output": output,
            "exec_context": exec_context,
            "data_changes": data_changes,
            "result_page": {
                "title": result_page.get("title", exp.get("name", "")),
                "description": result_page.get("description", ""),
                "sections": filled_sections,
            },
            "decisions_applied": result.get("decisions_applied", []),
        })
    except Exception as e:
        import traceback
        return JSONResponse(content={"success": False, "error": str(e), "trace": traceback.format_exc()[-500:]})


@app.delete("/api/v1/experiences/{exp_id}")
def ve5_api_experience_delete(exp_id: str):
    """删除经验（遗忘）"""
    try:
        from core.experience_store import exp_delete
        # 同时删除代码文件
        try:
            from core.experience.code_generator import delete_code
            delete_code(exp_id)
        except Exception:
            pass
        return JSONResponse(content={"success": exp_delete(exp_id)})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


@app.get("/api/v1/experiences/{exp_id}/code")
def ve5_api_experience_code(exp_id: str):
    """获取经验生成的 Python 代码"""
    try:
        from core.experience_store import exp_get
        from pathlib import Path
        exp = exp_get(exp_id)
        if not exp:
            return JSONResponse(content={"success": False, "error": "经验不存在"})
        code_path = exp.get("code_path", "")
        if not code_path:
            return JSONResponse(content={"success": False, "error": "此经验未生成代码"})
        # 直接从存储的 code_path 读取（兼容 temp_id 命名的文件）
        code_file = Path(code_path)
        if not code_file.exists():
            # 回退：尝试从 exp_id 构造路径
            from core.experience.code_generator import read_code
            code = read_code(exp_id)
            if not code:
                return JSONResponse(content={"success": False, "error": "代码文件不存在"})
        else:
            code = code_file.read_text(encoding="utf-8")
        return JSONResponse(content={
            "success": True,
            "code": code,
            "name": exp.get("name", ""),
            "code_path": code_path,
        })
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})


# ─── 消费档次管理 ───

@app.get("/api/v1/consumption-tier")
def ve5_api_get_consumption_tier():
    """获取当前消费档次状态"""
    try:
        from core.consumption_tier import get_tier_status
        from core.regional_price import get_user_city
        city = get_user_city()
        status = get_tier_status(city)
        return JSONResponse(content={"success": True, **status})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": "服务暂时不可用"})


@app.post("/api/v1/consumption-tier")
def ve5_api_set_consumption_tier(payload: dict = None):
    """手动设置消费档次"""
    try:
        from core.consumption_tier import set_tier, reset_tier
        p = payload or {}
        action = p.get("action", "set")
        if action == "reset":
            reset_tier()
            return JSONResponse(content={"success": True, "message": "已恢复自动检测"})
        tier = p.get("tier", "")
        if set_tier(tier):
            return JSONResponse(content={"success": True})
        return JSONResponse(content={"success": False, "error": f"无效档次: {tier}"})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": "服务暂时不可用"})


# ─── 区域数据（省市县三级联动）───

@app.get("/api/v1/regions")
def ve5_api_get_regions():
    """获取中国行政区划数据（省市县三级）"""
    try:
        import json
        regions_file = Path(__file__).parent.parent / "assets" / "china_regions.json"
        if not regions_file.exists():
            return JSONResponse(content={"success": False, "error": "区域数据文件不存在"})
        data = json.loads(regions_file.read_text(encoding="utf-8"))
        return JSONResponse(content={"success": True, "data": data})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": "服务暂时不可用"})


# ─── Skill 数据 ───

@app.get("/api/v1/chatbot/skill-data/{skill_name}")
def ve5_api_chatbot_skill_data(skill_name: str):
    """读取指定skill的持久化数据（如life_plan.json）"""
    try:
        from app_paths import DATA_DIR
        import json
        skill_file = DATA_DIR / "chatbot" / "skills" / f"{skill_name}.json"
        if not skill_file.exists():
            return JSONResponse(content={"success": False, "error": "暂无数据，请先生成规划"})
        data = json.loads(skill_file.read_text(encoding="utf-8"))
        return JSONResponse(content={"success": True, "data": data})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": "服务暂时不可用"})


# ─── Life-plan / Report 管理 ───

@app.get("/api/v1/chatbot/plans")
def ve5_api_chatbot_plans_list():
    """列出所有生活规划（兼容旧API，实际查询report_store）"""
    try:
        from core.ve5_chatbot.report_store import ve5_report_list
        plans = ve5_report_list("life_plan")
        return JSONResponse(content={"success": True, "plans": plans})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": "服务暂时不可用", "plans": []})


@app.get("/api/v1/chatbot/plans/{plan_id}")
def ve5_api_chatbot_plans_get(plan_id: str):
    """获取指定生活规划详情（兼容旧API）"""
    try:
        from core.ve5_chatbot.report_store import ve5_report_get
        report = ve5_report_get(plan_id)
        if report is None:
            return JSONResponse(content={"success": False, "error": "规划不存在"})
        return JSONResponse(content={"success": True, "data": report.get("data", report)})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": "服务暂时不可用"})


@app.delete("/api/v1/chatbot/plans/{plan_id}")
def ve5_api_chatbot_plans_delete(plan_id: str):
    """删除指定生活规划（兼容旧API）"""
    try:
        from core.ve5_chatbot.report_store import ve5_report_delete
        ok = ve5_report_delete(plan_id)
        return JSONResponse(content={"success": ok, "error": "" if ok else "规划不存在"})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": "服务暂时不可用"})


# ─── 通用报告管理 API ───

@app.get("/api/v1/chatbot/reports")
def ve5_api_chatbot_reports_list(type: str = None):
    """列出所有报告。可指定 type 过滤（life_plan / spending / goal 等）"""
    try:
        from core.ve5_chatbot.report_store import ve5_report_list
        reports = ve5_report_list(type)
        return JSONResponse(content={"success": True, "reports": reports})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": "服务暂时不可用", "reports": []})


@app.get("/api/v1/chatbot/reports/{report_id}")
def ve5_api_chatbot_reports_get(report_id: str):
    """获取指定报告详情"""
    try:
        from core.ve5_chatbot.report_store import ve5_report_get
        report = ve5_report_get(report_id)
        if report is None:
            return JSONResponse(content={"success": False, "error": "报告不存在"})
        return JSONResponse(content={"success": True, "data": report})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": "服务暂时不可用"})


@app.delete("/api/v1/chatbot/reports/{report_id}")
def ve5_api_chatbot_reports_delete(report_id: str):
    """删除指定报告"""
    try:
        from core.ve5_chatbot.report_store import ve5_report_delete
        ok = ve5_report_delete(report_id)
        return JSONResponse(content={"success": ok, "error": "" if ok else "报告不存在"})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": "服务暂时不可用"})


# ─── 策略中心路由 ───
app.include_router(strategy_router)

@app.get("/health")
def ve4_api_health():
    return {"status": "ok", "version": "0.1.0", "timestamp": datetime.now().isoformat()}


# ─── 主入口 ───

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("VE5_PORT", "8765")))

