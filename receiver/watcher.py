"""
VE4 蓝牙文件接收监控器
=====================
使用 watchdog 监控接收目录，新文件到达后自动触发数据还原流程。

功能：
    1. 监听 ve4/incoming 目录（或蓝牙默认目录）的文件变化
    2. 文件到达后等待稳定（避免传输未完成时处理）
    3. 按文件类型分发到 images/ 或 texts/ 子目录
    4. 触发数据还原管道（pipeline.py）
    5. 处理完成后移动到 processed/ 或 failed/

用法：
    python receiver/watcher.py                          # 监听 ve4/incoming
    python receiver/watcher.py --bridge                 # 监听蓝牙默认目录并桥接
    python receiver/watcher.py --dir "C:\\custom"       # 监听自定义目录
"""

import os
import sys
import time
import hashlib
import shutil
import argparse
import threading
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from typing import Optional

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileModifiedEvent

from config import (
    INCOMING_DIR, IMAGES_DIR, TEXTS_DIR, PROCESSED_DIR, FAILED_DIR,
    DEFAULT_BLUETOOTH_DIR,
    ALL_SUPPORTED_TYPES, MAX_FILE_SIZE_MB, PROCESS_DELAY_SECONDS,
    ensure_dirs, LOG_DIR, LOG_LEVEL,
)

# 日志
import logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "receiver.log", encoding="utf-8", mode="a"),
    ]
)
logger = logging.getLogger("ve4.receiver")


class FileStabilizer:
    """
    文件稳定器：等待文件写入完成后再处理。
    蓝牙传输大文件时，文件会先出现在目录中但内容不完整，
    需要检测文件大小不再变化后再处理。
    """
    def __init__(self, stable_seconds: int = PROCESS_DELAY_SECONDS):
        self.stable_seconds = stable_seconds
        self._pending = {}  # path -> (last_size, last_time)
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None

    def add(self, file_path: Path):
        """添加文件到等待队列"""
        with self._lock:
            self._pending[str(file_path)] = (file_path.stat().st_size, time.time())
        logger.info(f"[STABILIZER] 文件加入等待队列：{file_path.name}")
        self._schedule_check()

    def _schedule_check(self):
        """调度检查"""
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(self.stable_seconds, self._check)
        self._timer.start()

    def _check(self):
        """检查文件是否稳定（大小不再变化）"""
        ready = []
        with self._lock:
            now = time.time()
            for path_str, (last_size, last_time) in list(self._pending.items()):
                path = Path(path_str)
                if not path.exists():
                    # 文件已被删除或移动
                    del self._pending[path_str]
                    continue

                current_size = path.stat().st_size
                if current_size == last_size and (now - last_time) >= self.stable_seconds:
                    # 文件已稳定
                    ready.append(path)
                    del self._pending[path_str]
                else:
                    # 文件仍在变化，更新记录
                    self._pending[path_str] = (current_size, now)

        # 处理已稳定的文件
        for path in ready:
            self._on_stable(path)

        # 如果还有等待中的文件，继续调度
        with self._lock:
            if self._pending:
                self._schedule_check()

    def _on_stable(self, file_path: Path):
        """文件稳定后的回调"""
        logger.info(f"[STABILIZER] 文件已稳定：{file_path.name}")
        # 触发分发
        dispatcher = FileDispatcher()
        dispatcher.dispatch(file_path)


class FileDispatcher:
    """文件分发器：按类型分发到子目录，并触发处理"""

    def dispatch(self, file_path: Path):
        """分发文件"""
        # 检查文件大小
        size_mb = file_path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_FILE_SIZE_MB:
            logger.warning(f"[DISPATCH] 文件过大 ({size_mb:.1f}MB)，跳过：{file_path.name}")
            self._move_to(file_path, FAILED_DIR / "oversize")
            return

        # 检查扩展名
        ext = file_path.suffix.lower()
        if ext not in ALL_SUPPORTED_TYPES:
            logger.warning(f"[DISPATCH] 不支持的文件类型 ({ext})，跳过：{file_path.name}")
            self._move_to(file_path, FAILED_DIR / "unsupported")
            return

        # 按类型分发
        if ext in {'.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp'}:
            target_dir = IMAGES_DIR
            file_type = "image"
        elif ext in {'.txt', '.csv', '.json', '.xml', '.md', '.pdf', '.docx', '.xlsx', '.xls'}:
            target_dir = TEXTS_DIR
            file_type = "text"
        else:
            target_dir = TEXTS_DIR
            file_type = "document"

        # 生成唯一文件名（避免覆盖）
        unique_name = self._unique_name(file_path, target_dir)
        target_path = target_dir / unique_name

        try:
            shutil.move(str(file_path), str(target_path))
            logger.info(f"[DISPATCH] {file_type} 文件已分发：{unique_name}")

            # 触发数据还原管道
            self._trigger_pipeline(target_path, file_type)
        except Exception as e:
            logger.error(f"[DISPATCH] 移动文件失败：{e}")
            self._move_to(file_path, FAILED_DIR / "move_error")

    def _unique_name(self, file_path: Path, target_dir: Path) -> str:
        """生成唯一文件名（时间戳 + 原文件名 + 哈希前4位）"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = file_path.stem[:30]  # 限制长度
        hash_prefix = hashlib.md5(file_path.name.encode()).hexdigest()[:4]
        ext = file_path.suffix
        unique = f"{timestamp}_{stem}_{hash_prefix}{ext}"

        # 如果已存在，追加序号
        counter = 1
        original = unique
        while (target_dir / unique).exists():
            unique = f"{timestamp}_{stem}_{hash_prefix}_{counter}{ext}"
            counter += 1
        return unique

    def _move_to(self, file_path: Path, target_dir: Path):
        """移动文件到指定目录"""
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(file_path), str(target_dir / file_path.name))
        except Exception as e:
            logger.error(f"[MOVE] 移动失败：{e}")

    def _trigger_pipeline(self, file_path: Path, file_type: str):
        """触发数据还原管道"""
        try:
            from pipeline import DataRestorePipeline
            pipeline = DataRestorePipeline()
            pipeline.process(file_path, file_type)
        except Exception as e:
            logger.error(f"[PIPELINE] 触发失败：{e}")
            self._move_to(file_path, FAILED_DIR / "pipeline_error")


class BluetoothFileHandler(FileSystemEventHandler):
    """Watchdog 事件处理器"""

    def __init__(self, stabilizer: FileStabilizer):
        self.stabilizer = stabilizer
        self._processed = set()  # 防止重复处理

    def on_created(self, event):
        if event.is_directory:
            return
        self._handle(event.src_path)

    def on_modified(self, event):
        if event.is_directory:
            return
        self._handle(event.src_path)

    def _handle(self, src_path: str):
        path = Path(src_path)

        # 忽略隐藏文件、临时文件、已处理的子目录
        if path.name.startswith('.') or path.name.startswith('~'):
            return
        if PROCESSED_DIR in path.parents or FAILED_DIR in path.parents:
            return
        if path.suffix.lower() == '.tmp':
            return

        # 去重：同一文件在短时间内不重复处理
        file_key = f"{path.name}_{path.stat().st_size}"
        if file_key in self._processed:
            return
        self._processed.add(file_key)

        logger.info(f"[WATCHER] 检测到新文件：{path.name}")
        self.stabilizer.add(path)


def start_watcher(watch_dir: Path, bridge_mode: bool = False):
    """启动文件监控"""
    ensure_dirs()

    # 如果是桥接模式，同时监控蓝牙默认目录
    if bridge_mode:
        source_dir = DEFAULT_BLUETOOTH_DIR
        logger.info(f"[WATCHER] 桥接模式：监控蓝牙默认目录 → 自动移动到 {INCOMING_DIR}")
    else:
        source_dir = watch_dir
        logger.info(f"[WATCHER] 监听目录：{watch_dir}")

    stabilizer = FileStabilizer()
    handler = BluetoothFileHandler(stabilizer)
    observer = Observer()
    observer.schedule(handler, str(source_dir), recursive=False)
    observer.start()

    logger.info("=" * 60)
    logger.info("VE4 蓝牙文件接收监控器已启动")
    logger.info("=" * 60)
    logger.info(f"监听目录：{source_dir}")
    logger.info(f"图片目录：{IMAGES_DIR}")
    logger.info(f"文本目录：{TEXTS_DIR}")
    logger.info(f"处理完成：{PROCESSED_DIR}")
    logger.info(f"处理失败：{FAILED_DIR}")
    logger.info("等待蓝牙文件传入...")
    logger.info("=" * 60)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("[WATCHER] 收到中断信号，正在停止...")
        observer.stop()
    observer.join()
    logger.info("[WATCHER] 已停止")


def main():
    parser = argparse.ArgumentParser(description="VE4 蓝牙文件接收监控器")
    parser.add_argument(
        "--dir",
        type=str,
        default=str(INCOMING_DIR),
        help=f"要监听的目录（默认：{INCOMING_DIR}）"
    )
    parser.add_argument(
        "--bridge",
        action="store_true",
        help="桥接模式：监控蓝牙默认目录并自动移动到 ve4/incoming"
    )
    args = parser.parse_args()

    watch_dir = Path(args.dir).resolve()
    start_watcher(watch_dir, bridge_mode=args.bridge)


if __name__ == "__main__":
    main()
