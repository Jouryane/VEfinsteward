"""
VE5 receiver configuration.
"""

from pathlib import Path

from app_paths import (
    CODE_DIR,
    FAILED_DIR,
    INCOMING_DIR,
    LOG_DIR,
    PROCESSED_DIR,
    ensure_data_dirs,
)


PROJECT_ROOT = CODE_DIR

IMAGES_DIR = INCOMING_DIR / "images"
TEXTS_DIR = INCOMING_DIR / "texts"

DEFAULT_BLUETOOTH_DIR = Path.home() / "Documents" / "Bluetooth Received Files"

SUPPORTED_IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}
SUPPORTED_TEXT_TYPES = {".txt", ".csv", ".json", ".xml", ".md"}
SUPPORTED_DOCUMENT_TYPES = {".pdf", ".docx", ".xlsx", ".xls"}
ALL_SUPPORTED_TYPES = SUPPORTED_IMAGE_TYPES | SUPPORTED_TEXT_TYPES | SUPPORTED_DOCUMENT_TYPES

MAX_FILE_SIZE_MB = 50
DEDUP_ENABLED = True
DEDUP_WINDOW_SECONDS = 60
AUTO_PROCESS = True
PROCESS_DELAY_SECONDS = 3
LOG_LEVEL = "INFO"


def ensure_dirs():
    ensure_data_dirs()
    for path in [IMAGES_DIR, TEXTS_DIR, LOG_DIR, PROCESSED_DIR, FAILED_DIR]:
        path.mkdir(parents=True, exist_ok=True)
