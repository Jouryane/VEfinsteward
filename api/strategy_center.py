"""
AI 策略中心 API
=============
提供环境配置、数据源管理、策略执行、结果查看等接口。
"""
import os, json, subprocess, sys, tempfile, re, time
import yaml as _yaml
from pathlib import Path
from datetime import datetime
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, FileResponse
import logging
logger = logging.getLogger("ve5.strategy")

router = APIRouter()

# ─── 配置持久化 ───
def _get_settings_path():
    """获取设置文件路径（从 app_paths 导入）"""
    from app_paths import DATA_DIR
    return DATA_DIR / "strategy_settings.json"

def _load_settings() -> dict:
    p = _get_settings_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "ai_tool": {"name": "", "path": "", "workspace": ""},
        "data_source": {"provider": "akshare", "api_key": "", "data_dir": ""},
        "prompts": {},
    }

def _save_settings(data: dict):
    p = _get_settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.now().isoformat()
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ─── 数据目录 ───
def _get_data_dir() -> Path:
    from app_paths import DATA_DIR
    d = DATA_DIR / "market_data"
    d.mkdir(parents=True, exist_ok=True)
    return d

# ─── 策略结果目录 ───
def _get_results_dir() -> Path:
    from app_paths import DATA_DIR
    d = DATA_DIR / "strategy_results"
    d.mkdir(parents=True, exist_ok=True)
    return d

# ─── API 端点 ───

@router.get("/api/v1/strategy/settings")
def get_settings():
    return JSONResponse(content=_load_settings())

@router.post("/api/v1/strategy/settings")
async def save_settings(request: Request):
    try:
        data = await request.json()
        # 合并到已有配置，避免覆盖其他字段
        existing = _load_settings()
        existing.update(data)
        _save_settings(existing)
        return JSONResponse(content={"status": "saved"})
    except Exception as e:
        return JSONResponse(content={"error": str(e)})

@router.get("/api/v1/strategy/data-files")
def list_data_files():
    """列出已下载的数据文件"""
    data_dir = _get_data_dir()
    files = []
    for f in sorted(data_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.is_file() and f.suffix in ('.csv', '.parquet', '.json', '.feather', '.h5'):
            files.append({
                "name": f.name,
                "size_mb": round(f.stat().st_size / 1024 / 1024, 2),
                "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                "rows": _count_csv_rows(f) if f.suffix == '.csv' else None,
            })
    return JSONResponse(content={"files": files, "data_dir": str(data_dir)})

def _count_csv_rows(path: Path) -> int:
    try:
        count = 0
        with open(path, "r", encoding="utf-8") as f:
            for _ in f:
                count += 1
        return max(0, count - 1)  # 减去表头
    except Exception:
        return None

@router.post("/api/v1/strategy/download-data")
def download_data(request: dict):
    """下载市场数据（支持沪深300等）"""
    try:
        symbol = request.get("symbol", "sh000300")  # 默认沪深300
        name = request.get("name", "沪深300")
        start = request.get("start", "20200101")
        end = request.get("end", datetime.now().strftime("%Y%m%d"))
        provider = request.get("provider", "akshare")

        data_dir = _get_data_dir()
        out_path = data_dir / f"{name}_{start}_{end}.csv"

        if provider == "akshare":
            import akshare as ak
            df = ak.stock_zh_index_daily(symbol=symbol)
            # 筛选日期范围
            df = df[(df.index >= start) & (df.index <= end)]
            df.to_csv(out_path, encoding="utf-8-sig")
            rows = len(df)
        else:
            return JSONResponse(content={"error": f"不支持的数据源: {provider}"})

        return JSONResponse(content={
            "status": "ok",
            "file": out_path.name,
            "rows": rows,
            "path": str(out_path),
        })
    except ImportError:
        return JSONResponse(content={"error": "请先安装 akshare: pip install akshare"})
    except Exception as e:
        logger.error(f"[STRATEGY] 下载数据失败: {e}")
        return JSONResponse(content={"error": str(e)})

@router.get("/api/v1/strategy/preview-data/{filename}")
def preview_data(filename: str):
    """预览数据文件（前50行）"""
    from pathlib import PurePosixPath
    # 安全检查
    safe_name = Path(filename).name
    fpath = _get_data_dir() / safe_name
    if not fpath.exists():
        return JSONResponse(content={"error": "文件不存在"}, status_code=404)

    try:
        import csv
        rows = []
        with open(fpath, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            headers = next(reader)
            for i, row in enumerate(reader):
                if i >= 50:
                    break
                rows.append(row)
        return JSONResponse(content={"headers": headers, "rows": rows, "total_file": safe_name})
    except Exception as e:
        return JSONResponse(content={"error": str(e)})

@router.get("/api/v1/strategy/results")
def list_results():
    """列出策略执行结果"""
    results_dir = _get_results_dir()
    results = []
    for f in sorted(results_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.is_file() and f.suffix == '.json':
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                results.append({
                    "id": f.stem,
                    "name": data.get("name", f.stem),
                    "status": data.get("status", "unknown"),
                    "created": data.get("created_at", ""),
                    "script": data.get("script_type", ""),
                })
            except Exception:
                results.append({"id": f.stem, "name": f.stem, "status": "error", "created": "", "script": ""})
    return JSONResponse(content={"results": results})

@router.get("/api/v1/strategy/results/{result_id}")
def get_result(result_id: str):
    """获取策略结果详情"""
    safe_id = Path(result_id).stem
    fpath = _get_results_dir() / f"{safe_id}.json"
    if not fpath.exists():
        return JSONResponse(content={"error": "结果不存在"}, status_code=404)
    return JSONResponse(content=json.loads(fpath.read_text(encoding="utf-8")))

@router.post("/api/v1/strategy/launch-app")
def launch_app(request: dict):
    """启动外部 AI 编程应用"""
    try:
        app_path = request.get("path", "")
        workspace = request.get("workspace", "")
        if not app_path or not os.path.isfile(app_path):
            return JSONResponse(content={"error": f"应用不存在: {app_path}"})

        cmd = [app_path]
        if workspace and os.path.isdir(workspace):
            cmd.extend(["--workspace", workspace])

        subprocess.Popen(cmd, cwd=workspace if workspace and os.path.isdir(workspace) else None)
        return JSONResponse(content={"status": "launched", "app": app_path})
    except Exception as e:
        return JSONResponse(content={"error": str(e)})

@router.get("/api/v1/strategy/prompt-templates")
def get_prompt_templates():
    """获取预设的 prompt 模板"""
    templates = [
        {
            "id": "backtest_ma_cross",
            "name": "均线交叉回测",
            "description": "测试双均线交叉策略在沪深300上的表现",
            "prompt": "请使用 {data_file} 中的日线数据，编写一个双均线交叉回测策略（5日/20日均线）。要求：1. 使用pandas计算均线；2. 生成买卖信号；3. 计算年化收益率、最大回撤、夏普比率；4. 生成持仓变化时序图；5. 将结果保存到 {results_dir}。数据列名为：date,open,close,high,low,volume。",
            "script_type": "backtest",
        },
        {
            "id": "analyze_sector",
            "name": "板块轮动分析",
            "description": "分析用户持仓的板块轮动特征",
            "prompt": "分析以下持仓数据的板块配置和轮动特征。持仓：{holdings}。要求：1. 计算各板块的历史占比变化；2. 识别最近的调仓方向；3. 评估板块集中度风险；4. 给出分散化建议。",
            "script_type": "analysis",
        },
    ]
    return JSONResponse(content={"templates": templates})


# ─── LLM 检查 + 就地执行 ───

@router.post("/api/v1/strategy/llm-review")
def llm_review_script(request: dict):
    """LLM 检查并优化下载脚本"""
    try:
        script = request.get("script", "")
        if not script:
            return JSONResponse(content={"error": "没有脚本内容"})

        from core.ai_gateway import ve4_ai_call

        system = (
            "你是一个 Python 数据工程专家。用户会给你一个用于下载金融数据的 Python 脚本。\n"
            "请你:\n"
            "1. 检查脚本是否有语法错误或逻辑问题\n"
            "2. 检查依赖库是否正确使用（如 akshare/baostock/yfinance 的 API 调用）\n"
            "3. 如果有问题，直接输出修正后的完整脚本\n"
            "4. 如果没问题，原样输出即可\n"
            "5. 只输出 Python 代码，不要任何解释文字，不要 markdown 标记\n"
            "6. 确保脚本开头包含安装依赖的注释（如 pip install ...）"
        )
        prompt = f"请检查并优化以下脚本:\n\n```\n{script}\n```"

        result = ve4_ai_call(
            task_type="general",
            system=system,
            prompt=prompt,
            max_tokens=4000,
            temperature=0.1,
            format_type="text",
        )

        if not result.success:
            return JSONResponse(content={"error": result.error or "LLM 调用失败"})

        # 提取代码块（LLM 可能在代码外包裹 markdown）
        reviewed = result.text.strip()
        md_match = re.search(r'```(?:python)?\s*\n(.*?)```', reviewed, re.DOTALL)
        if md_match:
            reviewed = md_match.group(1).strip()

        # 验证: 基本语法检查
        try:
            compile(reviewed, '<script>', 'exec')
            syntax_ok = True
        except SyntaxError as e:
            syntax_ok = False
            reviewed = script  # 回退到原始脚本

        return JSONResponse(content={
            "status": "ok",
            "script": reviewed,
            "syntax_ok": syntax_ok,
            "provider": result.provider,
            "elapsed": round(result.elapsed_ms / 1000, 1),
        })
    except Exception as e:
        return JSONResponse(content={"error": str(e)})


@router.post("/api/v1/strategy/execute-script")
def execute_script(request: dict):
    """就地执行 Python 脚本，返回 stdout/stderr"""
    try:
        script = request.get("script", "")
        data_dir = request.get("data_dir", "")
        timeout = request.get("timeout", 120)

        if not script:
            return JSONResponse(content={"error": "没有脚本内容"})

        import io, contextlib

        # 切换工作目录
        old_cwd = os.getcwd()
        if data_dir and os.path.isdir(data_dir):
            os.chdir(data_dir)

        # 设置环境变量
        if data_dir:
            os.environ["VE5_DATA_DIR"] = data_dir

        captured_out = io.StringIO()
        captured_err = io.StringIO()
        t0 = time.time()
        success = True
        exc_info = None

        try:
            with contextlib.redirect_stdout(captured_out), contextlib.redirect_stderr(captured_err):
                exec(script, {"__builtins__": __builtins__})
        except Exception as e:
            success = False
            import traceback
            captured_err.write(traceback.format_exc())
        finally:
            os.chdir(old_cwd)

        elapsed = round(time.time() - t0, 1)
        stdout = captured_out.getvalue().strip()
        stderr = captured_err.getvalue().strip()

        # 从 stdout 中提取保存的文件路径
        saved_files = []
        for line in (stdout or "").split("\n"):
            for pattern in [r'数据已保存:\s*(\S+)', r'saved:\s*(\S+)', r'→\s*(\S+\.(?:csv|json|png|html))']:
                m = re.search(pattern, line)
                if m:
                    saved_files.append(m.group(1))
                    break

        return JSONResponse(content={
            "status": "ok" if success else "error",
            "returncode": 0 if success else 1,
            "stdout": stdout,
            "stderr": stderr if stderr else None,
            "elapsed": elapsed,
            "saved_files": saved_files,
        })
    except Exception as e:
        return JSONResponse(content={"error": str(e)})


@router.post("/api/v1/strategy/open-file")
def open_file_in_editor(request: dict):
    """在 AI 应用中打开指定文件"""
    try:
        from app_paths import CODE_DIR
        relative = request.get("path", "")
        filepath = CODE_DIR / relative
        if not filepath.exists():
            # 文件不存在时，用 AI 应用打开 CODE_DIR 目录，用户可新建文件
            settings = _load_settings()
            app_path = settings.get("ai_tool", {}).get("path", "")
            if app_path:
                subprocess.Popen([app_path, str(CODE_DIR)], cwd=str(CODE_DIR))
            return JSONResponse(content={"status": "file_not_found", "message": "文件尚未创建: " + relative + "\n将打开项目目录以便新建", "dir": str(CODE_DIR)})

        # 获取 AI 应用路径
        settings = _load_settings()
        app_path = settings.get("ai_tool", {}).get("path", "")
        if not app_path:
            return JSONResponse(content={"error": "未配置AI应用路径，请在战术规划中设置"})

        # 启动 AI 应用，workspace 设为文件所在目录
        subprocess.Popen([app_path, str(filepath)], cwd=str(filepath.parent))
        return JSONResponse(content={"status": "opened", "file": str(filepath)})
    except Exception as e:
        logger.error(f"[STRATEGY] 打开文件失败: {e}")
        return JSONResponse(content={"error": str(e)})


@router.get("/api/v1/strategy/holdings")
def get_holdings_for_strategy():
    """获取非流动类持仓，供战术屏幕使用"""
    try:
        from app_paths import DB_PATH
        import sqlite3
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT product_name, current_value, asset_class, daily_change_pct "
            "FROM asset_holdings WHERE asset_class != 'liquid' ORDER BY current_value DESC"
        ).fetchall()
        conn.close()

        holdings = []
        for r in rows:
            holdings.append({
                "name": r["product_name"],
                "value": r["current_value"] or 0,
                "asset_class": r["asset_class"],
                "change_pct": r["daily_change_pct"] or 0,
            })
        return JSONResponse(content={"holdings": holdings})
    except Exception as e:
        logger.error(f"[STRATEGY] 获取持仓失败: {e}")
        return JSONResponse(content={"error": str(e), "holdings": []})


# ─── 券商 API 配置（YAML） ───

def _get_broker_config_path():
    from app_paths import DATA_DIR
    return DATA_DIR / "broker_api.yaml"

@router.get("/api/v1/strategy/broker-config")
def get_broker_config():
    """读取券商 API 配置"""
    try:
        BROKER_CONFIG_PATH = _get_broker_config_path()
        if BROKER_CONFIG_PATH.exists():
            with open(BROKER_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = _yaml.safe_load(f) or {}
            return JSONResponse(content={"status": "ok", "data": cfg})
        return JSONResponse(content={"status": "ok", "data": {}})
    except Exception as e:
        return JSONResponse(content={"error": str(e)})

@router.post("/api/v1/strategy/broker-config")
def save_broker_config(request: dict):
    """保存券商 API 配置到 YAML"""
    try:
        BROKER_CONFIG_PATH = _get_broker_config_path()
        BROKER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(BROKER_CONFIG_PATH, "w", encoding="utf-8") as f:
            _yaml.dump(request, f, allow_unicode=True, sort_keys=False)
        return JSONResponse(content={"status": "saved"})
    except Exception as e:
        return JSONResponse(content={"error": str(e)})