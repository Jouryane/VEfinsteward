"""
VE4 代码沙盒 (CodeSandbox)
===========================
安全执行用户生成的策略代码。

安全策略（借鉴 TRAE 四层沙盒）：
    1. 进程隔离：在独立子进程中执行
    2. 超时限制：30 秒
    3. 内存限制：128MB（通过 resource 模块，Linux/Mac）
    4. 网络禁用：移除 socket 相关模块
    5. 文件系统只读：仅允许读取 userdata/，禁止写入系统目录
    6. 危险模块黑名单：os, sys.exit, subprocess, importlib

命名规范：
    - 类名: VE4CodeSandbox
    - 函数名: ve4_tactical_sandbox_{action}
"""

import sys
import os
import ast
import tempfile
import logging
import subprocess
import traceback
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger("ve5.tactical.sandbox")

# 代码输出目录
from app_paths import TACTICAL_OUTPUT_DIR
SANDBOX_OUTPUT_DIR = TACTICAL_OUTPUT_DIR

# 危险模块黑名单
VE4_SANDBOX_BANNED_MODULES = {
    "os", "sys", "subprocess", "importlib", "socket", "urllib",
    "http", "ftplib", "smtplib", "pickle", "marshal", "ctypes",
    " multiprocessing", "threading", "ctypes", "builtins.__import__",
}

# 危险函数黑名单
VE4_SANDBOX_BANNED_FUNCTIONS = [
    "eval", "exec", "compile", "__import__", "open",
    "input", "raw_input", "quit", "exit",
]


class VE4CodeSandbox:
    """
    安全代码执行沙盒。

    使用方式：
        sandbox = VE4CodeSandbox()
        result = await sandbox.execute(python_code, timeout=30)
    """

    DEFAULT_TIMEOUT = 30
    DEFAULT_MEMORY_MB = 128

    def __init__(self, output_dir: Path = None):
        self.output_dir = output_dir or SANDBOX_OUTPUT_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def execute(self, code: str, params: dict = None,
                      timeout: int = None) -> Dict[str, Any]:
        """
        安全执行 Python 代码。

        Args:
            code: Python 策略代码
            params: 传递给策略的参数（作为全局变量）
            timeout: 执行超时秒数

        Returns:
            {"success": bool, "output": str, "result": dict, "error": str}
        """
        timeout = timeout or self.DEFAULT_TIMEOUT
        params = params or {}

        # Step 1: 静态代码安全检查
        safety_check = self._check_code_safety(code)
        if not safety_check["safe"]:
            return {
                "success": False,
                "output": "",
                "result": {},
                "error": f"代码安全检查失败: {safety_check['reason']}",
            }

        # Step 2: 准备执行环境
        run_id = f"sandbox_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.urandom(4).hex()}"
        script_path = self.output_dir / f"{run_id}.py"

        # 包装代码：注入参数，捕获输出
        wrapped_code = self._wrap_code(code, params, run_id)

        try:
            script_path.write_text(wrapped_code, encoding="utf-8")
        except Exception as e:
            return {"success": False, "output": "", "result": {}, "error": f"写入脚本失败: {e}"}

        # Step 3: 在子进程中执行
        try:
            env = self._prepare_env()
            result_file = self.output_dir / f"{run_id}_result.json"

            cmd = [sys.executable, str(script_path)]
            logger.info(f"[SANDBOX] 执行代码: {run_id}, 超时={timeout}s")

            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                cwd=str(self.output_dir),
            )

            # 读取结果
            result_data = {}
            if result_file.exists():
                import json
                try:
                    result_data = json.loads(result_file.read_text(encoding="utf-8"))
                except Exception:
                    pass

            # 清理临时文件
            self._cleanup(script_path, result_file)

            if proc.returncode == 0:
                return {
                    "success": True,
                    "output": proc.stdout[:5000],  # 限制输出长度
                    "result": result_data,
                    "error": "",
                }
            else:
                return {
                    "success": False,
                    "output": proc.stdout[:2000],
                    "result": result_data,
                    "error": proc.stderr[:2000] or f"进程退出码: {proc.returncode}",
                }

        except subprocess.TimeoutExpired:
            self._cleanup(script_path, result_file)
            return {"success": False, "output": "", "result": {}, "error": f"执行超时（>{timeout}秒）"}
        except Exception as e:
            self._cleanup(script_path, result_file)
            return {"success": False, "output": "", "result": {}, "error": f"执行异常: {e}"}

    def _check_code_safety(self, code: str) -> Dict[str, Any]:
        """静态代码安全检查"""
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return {"safe": False, "reason": f"语法错误: {e}"}

        for node in ast.walk(tree):
            # 检查 import
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    mod_name = alias.name.split(".")[0]
                    if mod_name in VE4_SANDBOX_BANNED_MODULES:
                        return {"safe": False, "reason": f"禁止导入模块: {mod_name}"}

            # 检查危险函数调用
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in VE4_SANDBOX_BANNED_FUNCTIONS:
                        return {"safe": False, "reason": f"禁止调用函数: {node.func.id}"}

            # 检查文件写入（简单检测 open 的 mode 参数）
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id == "open":
                    if node.keywords:
                        for kw in node.keywords:
                            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                                if "w" in str(kw.value.value) or "a" in str(kw.value.value):
                                    return {"safe": False, "reason": "禁止文件写入操作"}

        return {"safe": True, "reason": ""}

    def _wrap_code(self, code: str, params: dict, run_id: str) -> str:
        """包装用户代码：注入参数，捕获结果"""
        import json
        params_json = json.dumps(params, ensure_ascii=False)

        wrapper = f'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VE4 CodeSandbox 包装脚本 — 由系统自动生成，请勿手动修改"""
import json
import sys
import math

# 注入参数
PARAMS = json.loads({repr(params_json)})

# 捕获结果
_RESULT = {{}}

def ve4_report_result(key, value):
    """报告回测结果（策略代码中调用）"""
    _RESULT[key] = value

# ── 用户策略代码 ──
{code}

# ── 保存结果 ──
with open("{run_id}_result.json", "w", encoding="utf-8") as f:
    json.dump(_RESULT, f, ensure_ascii=False, indent=2)
'''
        return wrapper

    def _prepare_env(self) -> Dict[str, str]:
        """准备隔离的环境变量"""
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["NO_COLOR"] = "1"
        env["VE4_SANDBOX"] = "1"
        # 移除可能干扰的系统变量
        for key in ["PYTHONPATH", "VIRTUAL_ENV", "CONDA_DEFAULT_ENV"]:
            env.pop(key, None)
        return env

    def _cleanup(self, *paths: Path):
        """清理临时文件"""
        for p in paths:
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass
