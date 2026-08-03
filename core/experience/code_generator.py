"""
VE5 Experience Code Generator — AI Coding Agent
================================================
将 LLM 输出编译为受限 Python 代码。

流程:
1. 读取 API Surface 清单 + LLM 原始输出
2. LLM 生成 Python 函数代码
3. AST 安全验证（禁止危险导入/调用）
4. 存储到 DATA_DIR/experience_code/{exp_id}.py

生成的代码通过 SafeContext 访问数据，无法触碰文件系统/网络。
"""

import ast
import json
import logging
import re
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("ve5.experience.code_generator")


# ════════════════════════════════════════════════
# LLM 代码生成
# ════════════════════════════════════════════════

_CODE_GEN_SYSTEM = """你是 VE5 的 AI Coding Agent，负责将财务分析逻辑编译为可复用的 Python 经验脚本。

你生成的代码将被存储并在用户每次触发对应经验时自动执行，可能绕过 LLM 调用。

核心原则:
1. 代码必须定义 execute(ctx) 函数
2. 通过 ctx 对象的 API 获取实时数据，绝不硬编码任何数值
3. 逻辑要完整：加载数据 → 计算 → 格式化 → 返回结果
4. 返回格式: {"reply": str, "data": dict, "sections": list}
5. reply 使用 Markdown 格式，包含表格和表情增强可读性
6. sections 是前端展示分区，每个包含 id/label/content

可用模块（仅限这些）:
- json, math, datetime, re, typing

禁止:
- 导入 os/sys/subprocess/shutil/pathlib/socket/http 等系统模块
- 使用 open/exec/eval/__import__
- 访问文件系统或网络

输出要求（重要）:
- 不要输出任何思考过程、解释或分析文字
- 直接输出完整的 Python 代码，以 def execute(ctx): 开头
- 代码必须完整可运行，不能有截断或省略
- 只输出代码本身，不要用 ``` 代码块包裹"""


def generate_experience_code(
    exp_id: str,
    schema: Dict,
    llm_output: str,
) -> Optional[str]:
    """
    将 LLM 分析结果编译为受限 Python 代码。

    参数:
        exp_id: 经验 ID
        schema: experience schema (来自 LLM 编译器，包含 name/description/trigger 等)
        llm_output: 原始 LLM 输出（要被固化为 experience 的内容）

    返回:
        代码文件路径，失败返回 None
    """
    from core.ai_gateway import ve4_ai_call
    from core.experience.api_surface import get_api_surface_text

    api_surface = get_api_surface_text()

    # 构建 prompt
    prompt_parts = [
        api_surface,
        "",
        "## 任务",
        "将以下 LLM 分析结果编译为一个可复用的 Python 经验脚本。",
        "",
        f"### 经验信息",
        f"- 名称: {schema.get('name', '')}",
        f"- 描述: {schema.get('description', '')}",
        f"- 触发: {schema.get('trigger_event', '')}",
        f"- 类型: {schema.get('exp_type', schema.get('type', ''))}",
        "",
        "### LLM 分析结果（要被固化为经验的原始输出）",
        llm_output[:3000] if llm_output else "(无原始输出，基于 schema 生成)",
        "",
        "### 要求",
        "1. 理解 LLM 输出中的分析逻辑和决策模式",
        "2. 将这些逻辑固化为 Python 代码，使用 ctx API 获取实时数据",
        "3. 代码应该能独立运行，不依赖原始 LLM 输出",
        "4. 返回格式必须包含 reply, data, sections 三个字段",
        "5. reply 是给用户的回复，使用 Markdown 格式",
        "6. sections 是前端展示分区",
        "",
        "只输出 Python 代码，以 def execute(ctx): 开头。",
    ]

    try:
        result = ve4_ai_call(
            task_type="experience_code_gen",
            system=_CODE_GEN_SYSTEM,
            prompt="\n".join(prompt_parts),
            format_type="text",
            complexity="high",
            max_tokens=8192,
        )

        # ── 推理模型兼容：content 为空但 reasoning 里有代码 ──
        # deepseek-v4-flash 等推理模型可能把代码写在 reasoning_content，
        # 此时 result.text 为空、result.reasoning 有内容。
        raw_text = result.text or ""
        if not raw_text.strip() and result.reasoning:
            logger.info(f"[CODE_GEN] text为空，尝试从 reasoning 提取代码 ({len(result.reasoning)}字符)")
            reasoning_code = _extract_code(result.reasoning)
            if reasoning_code:
                raw_text = reasoning_code
                logger.info(f"[CODE_GEN] 从 reasoning 提取到代码 ({len(raw_text)}字符)")

        if not result.success or not raw_text.strip():
            logger.warning(f"[CODE_GEN] LLM 调用失败: {exp_id} (text空={not result.text}, reasoning={len(result.reasoning or '')})")
            return None

        code = _extract_code(raw_text)
        if not code:
            logger.warning(f"[CODE_GEN] 未能提取代码: {exp_id}")
            return None

        # AST 安全验证
        if not validate_code_safety(code):
            logger.warning(f"[CODE_GEN] 安全检查失败: {exp_id}")
            return None

        # 存储
        code_path = _save_code(exp_id, code)
        if code_path:
            logger.info(f"[CODE_GEN] 代码生成成功: {exp_id} → {code_path}")
            return code_path

    except Exception as e:
        logger.error(f"[CODE_GEN] 生成异常: {exp_id}: {e}")

    return None


# ════════════════════════════════════════════════
# 代码提取与清理
# ════════════════════════════════════════════════

def _extract_code(text: str) -> str:
    """从 LLM 输出中提取 Python 代码"""
    # 尝试从 ```python ... ``` 块中提取
    m = re.search(r'```python\s*\n(.*?)```', text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # 尝试从 ``` ... ``` 块中提取
    m = re.search(r'```\s*\n(.*?)```', text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # 尝试直接提取 def execute(ctx): 开头的代码
    m = re.search(r'(def execute\(ctx\):.*?)$', text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # 如果整个文本看起来就是代码（以 def 开头）
    stripped = text.strip()
    if stripped.startswith("def execute"):
        return stripped

    return ""


# ════════════════════════════════════════════════
# AST 安全验证
# ════════════════════════════════════════════════

_FORBIDDEN_MODULES = {
    "os", "sys", "subprocess", "shutil", "pathlib",
    "socket", "http", "urllib", "requests",
    "ctypes", "multiprocessing", "threading",
    "pickle", "marshal", "importlib",
    "builtins", "__builtin__", "glob",
    "tempfile", "shelve", "dbm", "sqlite3",
}

_FORBIDDEN_BUILTINS = {
    "open", "exec", "eval", "compile", "globals",
    "locals", "vars", "dir", "getattr", "setattr",
    "delattr", "__import__", "exit", "quit",
    "breakpoint", "memoryview", "classmethod",
}

_ALLOWED_MODULES = {
    "json", "math", "datetime", "re", "typing",
    "collections", "itertools", "functools",
    "decimal", "statistics",
}


def validate_code_safety(code: str) -> bool:
    """
    AST 安全验证：
    1. 检查无禁止的 import
    2. 检查无禁止的内置函数调用
    3. 检查必须定义 execute 函数
    4. 检查无危险属性访问
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        logger.error(f"[CODE_GEN] 语法错误: {e}")
        return False

    for node in ast.walk(tree):
        # 检查 import 语句
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name.split(".")[0]
                if mod in _FORBIDDEN_MODULES:
                    logger.warning(f"[CODE_GEN] 禁止导入: {alias.name}")
                    return False
                if mod not in _ALLOWED_MODULES:
                    logger.warning(f"[CODE_GEN] 非白名单导入: {alias.name}")
                    return False

        if isinstance(node, ast.ImportFrom):
            if node.module:
                mod = node.module.split(".")[0]
                if mod in _FORBIDDEN_MODULES:
                    logger.warning(f"[CODE_GEN] 禁止 from 导入: {node.module}")
                    return False
                if mod not in _ALLOWED_MODULES:
                    logger.warning(f"[CODE_GEN] 非白名单 from 导入: {node.module}")
                    return False

        # 检查函数调用
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id in _FORBIDDEN_BUILTINS:
                    logger.warning(f"[CODE_GEN] 禁止调用: {node.func.id}")
                    return False

        # 检查属性访问中的危险方法
        if isinstance(node, ast.Attribute):
            if node.attr in ("system", "popen", "fork", "spawn",
                             "execv", "execve", "kill", "terminate",
                             "__subclasses__", "__bases__", "__mro__"):
                logger.warning(f"[CODE_GEN] 禁止属性访问: {node.attr}")
                return False

    # 检查必须定义 execute 函数
    has_execute = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "execute":
            # 检查参数：应该接受一个参数
            if len(node.args.args) == 1:
                has_execute = True
                break

    if not has_execute:
        logger.warning("[CODE_GEN] 未找到有效的 execute(ctx) 函数")
        return False

    return True


# ════════════════════════════════════════════════
# 代码存储
# ════════════════════════════════════════════════

def _get_code_dir() -> Path:
    """获取代码存储目录"""
    try:
        from app_paths import DATA_DIR
        code_dir = DATA_DIR / "experience_code"
    except Exception:
        # fallback
        code_dir = Path("userdata") / "experience_code"

    code_dir.mkdir(parents=True, exist_ok=True)
    return code_dir


def _save_code(exp_id: str, code: str) -> Optional[str]:
    """保存生成的代码到文件"""
    try:
        code_dir = _get_code_dir()
        # 安全的文件名
        safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', exp_id)
        code_file = code_dir / f"{safe_id}.py"
        code_file.write_text(code, encoding="utf-8")
        return str(code_file)
    except Exception as e:
        logger.error(f"[CODE_GEN] 保存代码失败: {e}")
        return None


def read_code(exp_id: str) -> Optional[str]:
    """读取已存储的代码（供前端展示）"""
    try:
        code_dir = _get_code_dir()
        safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', exp_id)
        code_file = code_dir / f"{safe_id}.py"
        if code_file.exists():
            return code_file.read_text(encoding="utf-8")
    except Exception:
        pass
    return None


def delete_code(exp_id: str):
    """删除经验对应的代码文件"""
    try:
        code_dir = _get_code_dir()
        safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', exp_id)
        code_file = code_dir / f"{safe_id}.py"
        if code_file.exists():
            code_file.unlink()
    except Exception:
        pass


def rename_code_file(old_path: str, new_exp_id: str) -> Optional[str]:
    """
    将编译阶段生成的临时代码文件重命名为正式 exp_id 命名。

    参数:
        old_path: 编译阶段生成的代码路径（temp_id 命名）
        new_exp_id: 正式经验 ID

    返回:
        新的代码路径，失败返回原路径
    """
    try:
        old_file = Path(old_path)
        if not old_file.exists():
            return old_path

        code_dir = _get_code_dir()
        safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', new_exp_id)
        new_file = code_dir / f"{safe_id}.py"

        # 如果新路径与旧路径相同，无需重命名
        if old_file.resolve() == new_file.resolve():
            return old_path

        # 如果目标文件已存在，先删除
        if new_file.exists():
            new_file.unlink()

        old_file.rename(new_file)
        logger.info(f"[CODE_GEN] 代码文件重命名: {old_path} → {new_file}")
        return str(new_file)
    except Exception as e:
        logger.warning(f"[CODE_GEN] 重命名失败: {e}")
        return old_path
