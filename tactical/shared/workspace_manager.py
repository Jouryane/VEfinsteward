"""
VE4 策略工作区管理器
========================
职责：
    1. 管理策略任务文件（strategy_task.md）的创建和读取
    2. 启动外部 AI Coding 应用并打开工作目录
    3. 扫描工作区输出结果（文本、CSV、图片）
    4. 配置 AI Coding 应用（路径检测、启用/禁用）

命名规范：
    ve4_ws_{function_name}
"""

import sys
import os
import json
import yaml
import shutil
import logging
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger("ve5.tactical.workspace")

# 配置文件路径
from app_paths import TACTICAL_DIR

CONFIG_PATH = TACTICAL_DIR / "config" / "ai_coding_apps.yaml"


class VE4WorkspaceManager:
    """策略工作区管理器"""

    def __init__(self):
        self.config_path = CONFIG_PATH
        self._config = self._load_config()

    # ── 配置管理 ──

    def _load_config(self) -> dict:
        """加载配置"""
        try:
            if self.config_path.exists():
                with open(self.config_path, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
        except Exception as e:
            logger.error(f"加载配置失败: {e}")
        return self._default_config()

    def _default_config(self) -> dict:
        """默认配置"""
        return {
            "apps": {
                "trae": {"name": "Trae Work", "path": "", "enabled": False, "description": "Trae 官方 IDE"},
                "cursor": {"name": "Cursor", "path": "", "enabled": False, "description": "Cursor IDE"},
                "vscode": {"name": "VS Code", "path": "", "enabled": False, "description": "VS Code + Copilot"},
                "claude": {"name": "Claude Desktop", "path": "", "enabled": False, "description": "Claude Desktop"},
                "other": {"name": "其它", "path": "", "enabled": False, "description": "自定义 AI 编程应用", "launch_args": ["{workspace_dir}"]},
            },
            "workspace": {
                "default_dir": str(TACTICAL_DIR),
                "task_filename": "strategy_task.md",
                "output_subdir": "output",
            }
        }

    def _save_config(self) -> bool:
        """保存配置"""
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                yaml.dump(self._config, f, allow_unicode=True, sort_keys=False)
            return True
        except Exception as e:
            logger.error(f"保存配置失败: {e}")
            return False

    def ve4_ws_get_apps(self) -> List[dict]:
        """获取所有 AI Coding 应用配置"""
        apps = self._config.get("apps", {})
        result = []
        for key, val in apps.items():
            path = val.get("path", "")
            detected = bool(path and Path(path).exists())
            # 如果没有手动配置路径，尝试自动检测
            if not detected:
                path = self._auto_detect(key)
                if path:
                    detected = True
            result.append({
                "key": key,
                "name": val.get("name", key),
                "path": path or "",
                "enabled": val.get("enabled", False),
                "detected": detected,
                "description": val.get("description", ""),
            })
        return result

    def ve4_ws_update_app(self, key: str, path: str = None, enabled: bool = None) -> bool:
        """更新应用配置"""
        apps = self._config.setdefault("apps", {})
        if key not in apps:
            return False
        if path is not None:
            apps[key]["path"] = path
        if enabled is not None:
            apps[key]["enabled"] = enabled
        return self._save_config()

    def ve4_ws_set_default_app(self, key: str) -> bool:
        """设置默认应用"""
        apps = self._config.setdefault("apps", {})
        if key not in apps:
            return False
        # 禁用所有，启用指定的
        for k in apps:
            apps[k]["enabled"] = (k == key)
        return self._save_config()

    def _auto_detect(self, app_key: str) -> Optional[str]:
        """自动检测应用安装路径"""
        apps = self._config.get("apps", {})
        app = apps.get(app_key, {})
        detect_list = app.get("auto_detect", [])
        username = os.getenv("USERNAME", os.getenv("USER", ""))
        for template in detect_list:
            candidate = template.replace("{username}", username)
            if Path(candidate).exists():
                return candidate
        return None

    # ── 工作区目录 ──

    def ve4_ws_get_workspace_dir(self) -> Path:
        """获取工作区目录"""
        ws_cfg = self._config.get("workspace", {})
        default_dir = ws_cfg.get("default_dir", "")
        if default_dir:
            return Path(default_dir)
        return TACTICAL_DIR / "workspace"

    def ve4_ws_set_workspace_dir(self, path: str) -> bool:
        """设置工作区目录"""
        ws = self._config.setdefault("workspace", {})
        ws["default_dir"] = path
        return self._save_config()

    # ── 任务文件 ──

    def ve4_ws_create_task(self, strategy_text: str, data_sources: dict = None,
                            task_id: str = None) -> dict:
        """
        创建策略任务文件。

        生成 strategy_task.md，包含：
            - 策略描述
            - 可用数据源配置
            - 期望输出格式
            - Python 依赖提示
        """
        ws_dir = self.ve4_ws_get_workspace_dir()
        ws_dir.mkdir(parents=True, exist_ok=True)
        output_dir = ws_dir / self._config.get("workspace", {}).get("output_subdir", "output")
        output_dir.mkdir(parents=True, exist_ok=True)

        task_filename = self._config.get("workspace", {}).get("task_filename", "strategy_task.md")
        task_file = ws_dir / task_filename
        task_id = task_id or datetime.now().strftime("%Y%m%d_%H%M%S")

        # 读取数据源配置
        ds_config = self._load_datasource_config()

        # 构建任务文件内容
        content = self._build_task_markdown(
            strategy_text=strategy_text,
            task_id=task_id,
            output_dir=str(output_dir),
            data_sources=data_sources or ds_config,
        )

        with open(task_file, "w", encoding="utf-8") as f:
            f.write(content)

        return {
            "task_id": task_id,
            "task_file": str(task_file),
            "workspace_dir": str(ws_dir),
            "output_dir": str(output_dir),
            "task_text": strategy_text,
        }

    def _load_datasource_config(self) -> dict:
        """加载数据源配置"""
        ds_path = TACTICAL_DIR / "config" / "data_sources.yaml"
        try:
            if ds_path.exists():
                with open(ds_path, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
        except Exception:
            pass
        return {}

    def _build_task_markdown(self, strategy_text: str, task_id: str,
                              output_dir: str, data_sources: dict) -> str:
        """构建策略任务 Markdown"""
        # 可用数据源列表
        ds_lines = []
        ds_cfg = data_sources.get("data_sources", {})
        for key, val in ds_cfg.items():
            if val.get("enabled"):
                name = val.get("name", key)
                token = val.get("token", "")
                if key == "local_file":
                    path = val.get("path", "")
                    ds_lines.append(f"- **{name}**: 本地文件 `{path}`")
                elif token:
                    ds_lines.append(f"- **{name}**: 已配置 Token（直接使用）")
                else:
                    ds_lines.append(f"- **{name}**: 免密钥，直接 `import akshare as ak` 使用")

        ds_text = "\n".join(ds_lines) if ds_lines else "暂无已配置的数据源，请在 VE4 战术模块中配置。"

        return f"""# 策略任务 #{task_id}

> 此文件由 VE4 资产配置战术模块自动生成。
> 请根据下方策略描述编写 Python 代码，执行后将结果保存到输出目录。

---

## 策略描述

{strategy_text}

---

## 可用数据源

{ds_text}

**依赖安装提示**（如尚未安装）：
```
pip install akshare pandas numpy matplotlib
```

如使用 Tushare Pro：
```
pip install tushare
```

---

## 输出要求

**结果保存到**: `{output_dir}/`

请将分析结果保存为以下格式（至少一种）：
- **文本结果**: `result.txt`（包含关键统计指标、结论）
- **数据表格**: `result.csv`（包含交易信号、收益率等详细数据）
- **图表**: `chart.png` 或 `chart.svg`（可视化结果）

建议输出格式（result.txt 示例）：
```
策略: [策略名称]
数据源: [使用的数据源]
时间范围: [起止日期]

核心指标:
- 总交易次数: X
- 胜率: XX.X%
- 平均收益率: XX.XX%
- 最大单笔收益: XX.XX%
- 最大单笔亏损: XX.XX%

结论: [一句话总结策略有效性]
```

---

## 注意事项

- 代码运行环境：用户本机 Python（非沙箱），可使用所有已安装的包
- 数据源 API 调用请使用 try-except 处理网络异常
- 图表请保存为 PNG 格式（方便 VE4 读取展示）
- 完成后请确保 result.txt 存在，VE4 将自动读取分析
"""

    # ── 启动外部应用 ──

    def ve4_ws_launch_app(self, app_key: str, strategy_text: str = None,
                           data_sources: dict = None) -> dict:
        """
        启动 AI Coding 应用。

        1. 如果提供了 strategy_text，先创建任务文件
        2. 检查应用路径
        3. 启动应用并打开工作目录
        """
        # 创建任务文件
        task_info = None
        if strategy_text:
            task_info = self.ve4_ws_create_task(strategy_text, data_sources)

        workspace_dir = str(self.ve4_ws_get_workspace_dir())

        # 获取应用路径
        apps = self._config.get("apps", {})
        app = apps.get(app_key, {})
        app_path = app.get("path", "") or self._auto_detect(app_key)

        if not app_path:
            return {
                "success": False,
                "error": f"未找到应用路径，请先在配置中设置 {app.get('name', app_key)} 的安装路径",
                "task_info": task_info,
            }

        if not Path(app_path).exists():
            return {
                "success": False,
                "error": f"应用路径不存在: {app_path}",
                "task_info": task_info,
            }

        try:
            # 启动应用
            launch_args = app.get("launch_args", [])
            args = [p.replace("{workspace_dir}", workspace_dir) for p in launch_args]

            if app_key == "claude":
                # Claude Desktop 无法通过命令行传递内容，复制到剪贴板
                self._copy_to_clipboard(strategy_text or "请查看 strategy_task.md")
                subprocess.Popen([app_path])
            else:
                # Trae / Cursor / VS Code：打开工作目录
                subprocess.Popen([app_path] + args)

            return {
                "success": True,
                "app_name": app.get("name", app_key),
                "app_path": app_path,
                "workspace_dir": workspace_dir,
                "task_info": task_info,
                "is_claude": app_key == "claude",
            }
        except Exception as e:
            logger.error(f"启动应用失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "task_info": task_info,
            }

    def _copy_to_clipboard(self, text: str):
        """复制文本到剪贴板（Windows）"""
        try:
            import ctypes
            CF_UNICODETEXT = 13
            kernel32 = ctypes.windll.kernel32
            user32 = ctypes.windll.user32

            kernel32.GlobalAlloc(0x0042, len(text.encode('utf-16-le')) + 2)
            h_mem = kernel32.GlobalAlloc(0x0042, len(text.encode('utf-16-le')) + 2)
            p_mem = kernel32.GlobalLock(h_mem)
            ctypes.memmove(p_mem, text.encode('utf-16-le'), len(text.encode('utf-16-le')))
            kernel32.GlobalUnlock(h_mem)
            user32.OpenClipboard(0)
            user32.EmptyClipboard()
            user32.SetClipboardData(CF_UNICODETEXT, h_mem)
            user32.CloseClipboard()
        except Exception as e:
            logger.warning(f"剪贴板操作失败: {e}")

    # ── 读取结果 ──

    def ve4_ws_read_results(self) -> dict:
        """
        扫描工作区输出目录，读取所有结果文件。

        返回：
            - result.txt 内容
            - result.csv 预览
            - 图片文件列表
            - 其他文件列表
        """
        ws_dir = self.ve4_ws_get_workspace_dir()
        output_dir = ws_dir / self._config.get("workspace", {}).get("output_subdir", "output")

        results = {
            "output_dir": str(output_dir),
            "exists": output_dir.exists(),
            "text_result": "",
            "csv_preview": "",
            "csv_rows": 0,
            "images": [],
            "files": [],
            "last_modified": "",
        }

        if not output_dir.exists():
            return results

        # 读取 result.txt
        result_txt = output_dir / "result.txt"
        if result_txt.exists():
            with open(result_txt, "r", encoding="utf-8") as f:
                results["text_result"] = f.read()
            results["last_modified"] = datetime.fromtimestamp(
                result_txt.stat().st_mtime
            ).strftime("%Y-%m-%d %H:%M:%S")

        # 读取 result.csv（前 20 行预览）
        result_csv = output_dir / "result.csv"
        if result_csv.exists():
            try:
                import pandas as pd
                df = pd.read_csv(result_csv)
                results["csv_preview"] = df.head(20).to_string(index=False)
                results["csv_rows"] = len(df)
            except Exception:
                # 回退：直接读文本
                with open(result_csv, "r", encoding="utf-8") as f:
                    lines = f.readlines()[:21]
                    results["csv_preview"] = "".join(lines)
                    results["csv_rows"] = max(0, len(lines) - 1)

        # 图片文件
        for ext in ('*.png', '*.jpg', '*.jpeg', '*.svg', '*.gif'):
            for img in output_dir.glob(ext):
                results["images"].append({
                    "name": img.name,
                    "path": f"/tactical-output/{img.name}",
                    "size": img.stat().st_size,
                    "modified": datetime.fromtimestamp(img.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                })

        # 其他文件
        for f in output_dir.iterdir():
            if f.is_file() and f.name not in ('result.txt', 'result.csv'):
                if not any(f.name.endswith(e) for e in ('.png', '.jpg', '.jpeg', '.svg', '.gif')):
                    results["files"].append(f.name)

        # 按修改时间排序图片
        results["images"].sort(key=lambda x: x["modified"], reverse=True)

        return results

    def ve4_ws_clear_results(self) -> bool:
        """清空输出目录"""
        output_dir = self.ve4_ws_get_workspace_dir() / self._config.get("workspace", {}).get("output_subdir", "output")
        if output_dir.exists():
            for f in output_dir.iterdir():
                try:
                    f.unlink()
                except Exception:
                    pass
        return True
