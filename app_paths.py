"""
VE5 local path policy.

Portable mode (frozen / exe):
  CODE_DIR = exe 所在目录，业务代码 (api/, receiver/, core/, pwa/, ...) 就在旁边
  DATA_DIR = %APPDATA%/VE5/  （可通过 VE5_DATA_DIR 环境变量覆盖）

Dev mode (source):
  CODE_DIR = 项目根目录
  DATA_DIR = CODE_DIR/userdata/
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "VE5"

# ─── 代码根目录 ───
if getattr(sys, "frozen", False):
    _exe_dir = Path(sys.executable).parent.resolve()
    CODE_DIR = _exe_dir  # 默认值
    # 向上遍历祖先目录，查找项目源码根目录（同时包含 app_paths.py 和 pwa/ 目录）
    _candidate = _exe_dir
    for _ in range(6):  # 最多向上查找 6 级
        _candidate = _candidate.parent
        if _candidate == _candidate.parent:  # 已到根目录
            break
        if (_candidate / "app_paths.py").is_file() and (_candidate / "pwa").is_dir():
            CODE_DIR = _candidate
            break
else:
    CODE_DIR = Path(__file__).parent.resolve()


def ve5_is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


# ─── 资源目录：直接在 CODE_DIR 下 ───
PWA_DIR = CODE_DIR / "pwa"
CONFIG_DIR = CODE_DIR / "config"
TACTICAL_DIR = CODE_DIR / "tactical"
TACTICAL_OUTPUT_DIR = TACTICAL_DIR / "output"


def ve5_data_dir() -> Path:
    configured = os.environ.get("VE5_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if not ve5_is_frozen():
        return CODE_DIR / "userdata"
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / APP_NAME
    return Path.home() / f".{APP_NAME.lower()}"


DATA_DIR = ve5_data_dir()
DB_PATH = DATA_DIR / "ve5.db"
PROFILE_PATH = DATA_DIR / "allocation_profile.json"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
LOG_DIR = DATA_DIR / "logs"
INCOMING_DIR = DATA_DIR / "incoming"
PROCESSED_DIR = DATA_DIR / "processed"
FAILED_DIR = DATA_DIR / "failed"


def ensure_data_dirs() -> None:
    for path in [
        DATA_DIR,
        LOG_DIR,
        SNAPSHOTS_DIR,
        INCOMING_DIR,
        INCOMING_DIR / "images",
        INCOMING_DIR / "texts",
        PROCESSED_DIR,
        FAILED_DIR,
        TACTICAL_OUTPUT_DIR,
    ]:
        path.mkdir(parents=True, exist_ok=True)