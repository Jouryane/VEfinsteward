"""
VE4 数据源管理器
==================
统一管理外部数据源 API 接入。

命名规范：
    - 类名: VE4DataSourceManager
    - 函数名: ve4_tactical_ds_{action}
"""

import yaml
import logging
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger("ve5.tactical.data_source")

from app_paths import TACTICAL_DIR, DATA_DIR
# 只读配置（模板）在资源目录
_CONFIG_TEMPLATE_PATH = TACTICAL_DIR / "config" / "data_sources.yaml"
# 用户隐私数据（token等）在 userdata 目录
_USER_CONFIG_PATH = DATA_DIR / "tactical_data_sources.yaml"


class VE4DataSourceManager:
    """数据源管理器"""

    def __init__(self, config_path: Path = None):
        self.config_path = config_path or _USER_CONFIG_PATH
        self._sources: Dict[str, dict] = {}
        self._load_config()

    def _load_config(self):
        # 优先从 userdata 加载用户配置
        if self.config_path.exists():
            with open(self.config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
                self._sources = cfg.get("data_sources", {})
        elif _CONFIG_TEMPLATE_PATH.exists():
            # 首次：从模板复制到 userdata
            with open(_CONFIG_TEMPLATE_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
                self._sources = cfg.get("data_sources", {})
            self._save_config()
        else:
            logger.warning(f"数据源配置文件不存在: {self.config_path}")

    def list_sources(self) -> Dict[str, dict]:
        """列出所有数据源及其状态（用于前端展示）"""
        result = {}
        for k, v in self._sources.items():
            # 判断状态文本
            if v.get("enabled"):
                status = "已启用"
            elif k == "local_file":
                status = "未指定路径" if not v.get("path") else "已配置路径"
            else:
                has_token = bool(v.get("token"))
                status = "已配置 Token" if has_token else "未配置 Token"
            result[k] = {
                "key": k,
                "name": v.get("name", k),
                "enabled": v.get("enabled", False),
                "status": status,
                # 不返回完整 token，只返回是否有
                "has_token": bool(v.get("token")),
                "path": v.get("path", ""),
            }
        return result

    def get_source(self, name: str) -> Optional[dict]:
        src = self._sources.get(name)
        if src and src.get("enabled"):
            return src
        return None

    def is_available(self, name: str) -> bool:
        src = self.get_source(name)
        return src is not None and bool(src.get("token") or src.get("enabled"))

    def update_source(self, key: str, name: str = None, token: str = None,
                      enabled: bool = None, path: str = None) -> bool:
        """更新数据源配置并保存到 YAML"""
        if key not in self._sources:
            return False

        src = self._sources[key]
        if name is not None:
            src["name"] = name
        if token is not None:
            src["token"] = token
        if enabled is not None:
            src["enabled"] = enabled
        if path is not None:
            src["path"] = path

        return self._save_config()

    def _save_config(self) -> bool:
        """保存配置到 YAML 文件"""
        try:
            import yaml
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            # 如果文件已存在，先读取其他字段；否则创建新配置
            cfg = {}
            if self.config_path.exists():
                try:
                    with open(self.config_path, "r", encoding="utf-8") as f:
                        cfg = yaml.safe_load(f) or {}
                except Exception:
                    pass
            cfg["data_sources"] = self._sources
            with open(self.config_path, "w", encoding="utf-8") as f:
                yaml.dump(cfg, f, allow_unicode=True, sort_keys=False)
            return True
        except Exception as e:
            logger.error(f"保存数据源配置失败: {e}")
            return False

    def load_local_file(self, path: str) -> str:
        """加载本地数据文件"""
        import pandas as pd
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"文件不存在: {path}")
        if p.suffix.lower() in ('.csv',):
            df = pd.read_csv(path)
        elif p.suffix.lower() in ('.xlsx', '.xls'):
            df = pd.read_excel(path)
        else:
            raise ValueError(f"不支持的文件格式: {p.suffix}")
        return f"加载成功: {len(df)} 行 x {len(df.columns)} 列"
