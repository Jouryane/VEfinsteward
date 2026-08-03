"""
VE5 Experience Code Loader — 动态代码加载与执行
=================================================
负责加载生成的 Python 经验代码并执行。

流程:
1. 从文件加载 Python 模块
2. 构建 SafeContext 对象
3. 调用 execute(ctx) 函数
4. 验证返回格式
5. 失败时返回 None，由 executor 回退到模板模式
"""

import importlib.util
import logging
import sys
import traceback
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger("ve5.experience.code_loader")


def load_and_execute(
    code_path: str,
    state: Dict[str, Any],
    experience: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    加载并执行生成的 Python 经验代码。

    参数:
        code_path: 代码文件路径
        state: 当前状态对象
        experience: 经验元数据

    返回:
        标准化的执行结果 dict，失败返回 None
        格式: {
            "reply": str,
            "data": dict,
            "sections": list,
            "_code_executed": True,
        }
    """
    try:
        code_file = Path(code_path)
        if not code_file.exists():
            logger.warning(f"[CODE_LOADER] 代码文件不存在: {code_path}")
            return None

        # 动态加载模块
        module_name = f"_exp_generated_{code_file.stem}"
        spec = importlib.util.spec_from_file_location(module_name, str(code_file))
        if not spec or not spec.loader:
            logger.error(f"[CODE_LOADER] 无法创建模块 spec: {code_path}")
            return None

        module = importlib.util.module_from_spec(spec)

        # 注入允许的模块到模块命名空间（受限执行环境）
        import json as _json
        import math as _math
        import re as _re
        from datetime import datetime as _datetime
        from typing import Dict as _Dict, List as _List, Any as _Any, Optional as _Optional

        module.__dict__["json"] = _json
        module.__dict__["math"] = _math
        module.__dict__["re"] = _re
        module.__dict__["datetime"] = _datetime
        module.__dict__["typing"] = type("typing", (), {
            "Dict": _Dict, "List": _List, "Any": _Any, "Optional": _Optional,
        })

        # 执行模块代码
        spec.loader.exec_module(module)

        # 检查 execute 函数
        if not hasattr(module, "execute"):
            logger.error(f"[CODE_LOADER] 模块未定义 execute 函数: {code_path}")
            return None

        execute_fn = getattr(module, "execute")
        if not callable(execute_fn):
            logger.error(f"[CODE_LOADER] execute 不是可调用对象: {code_path}")
            return None

        # 构建 SafeContext
        from core.experience.api_surface import SafeContext
        ctx = SafeContext(state, experience)

        # 执行
        result = execute_fn(ctx)

        # 验证返回格式
        if not isinstance(result, dict):
            logger.error(f"[CODE_LOADER] execute 返回非 dict: {type(result)}")
            return None

        # 标准化返回格式
        normalized = {
            "reply": str(result.get("reply", "")),
            "data": result.get("data", {}) if isinstance(result.get("data"), dict) else {},
            "sections": result.get("sections", []) if isinstance(result.get("sections"), list) else [],
            "_code_executed": True,
            "_code_path": code_path,
        }

        # 合并额外的输出字段
        for k, v in result.items():
            if k not in ("reply", "data", "sections") and not k.startswith("_"):
                normalized[k] = v

        logger.info(f"[CODE_LOADER] 执行成功: {code_path}")
        return normalized

    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"[CODE_LOADER] 执行失败: {code_path}\n{tb}")
        return None

    finally:
        # 清理模块缓存
        if "module" in dir() and module:
            mod_name = getattr(module, "__name__", "")
            if mod_name in sys.modules:
                del sys.modules[mod_name]


def validate_code_path(code_path: str) -> bool:
    """检查代码路径是否有效且文件存在"""
    if not code_path:
        return False
    try:
        return Path(code_path).exists()
    except Exception:
        return False
