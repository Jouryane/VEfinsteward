r"""
VE4 蓝牙接收目录配置脚本
========================
将 Windows 默认蓝牙接收目录映射到 ve4/incoming，实现"不经过中转"直接入库。

技术方案：
-----------
Windows 蓝牙文件接收的默认路径为：
    C:\Users\<用户名>\Documents\Bluetooth Received Files

本脚本提供两种方案：

方案 A（推荐）：符号链接（mklink /J）
    将系统默认蓝牙文件夹删除，创建符号链接指向 ve4/incoming。
    优点：文件直接落入 ve4，无中转，Watchdog 监听同一目录即可。
    前提：需管理员权限运行 PowerShell。

方案 B：Watchdog 桥接（无需管理员）
    保留默认蓝牙文件夹，用 Watchdog 监控默认目录，
    文件到达后自动移动到 ve4/incoming 并触发处理。
    优点：无需管理员权限，更安全。
    缺点：文件会先落入默认目录再移动，存在一个短暂中转。

用法：
    python receiver/setup_bluetooth.py --method junction
    python receiver/setup_bluetooth.py --method watchdog
    python receiver/setup_bluetooth.py --restore
"""

import os
import sys
import shutil
import argparse
from pathlib import Path
from datetime import datetime

from config import (
    INCOMING_DIR,
    DEFAULT_BLUETOOTH_DIR,
    ensure_dirs,
)


def setup_junction():
    """
    方案 A：创建符号链接（junction），将系统默认蓝牙接收目录指向 ve4/incoming。
    文件通过蓝牙接收后将直接落入 ve4/incoming，无中转。
    """
    ensure_dirs()

    if not DEFAULT_BLUETOOTH_DIR.exists():
        print(f"[INFO] 默认蓝牙接收目录不存在：{DEFAULT_BLUETOOTH_DIR}")
        print(f"[INFO] 直接创建符号链接指向：{INCOMING_DIR}")
        DEFAULT_BLUETOOTH_DIR.parent.mkdir(parents=True, exist_ok=True)
    else:
        # 备份现有文件
        backup_dir = Path.home() / "Documents" / f"BluetoothReceived_Backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        if any(DEFAULT_BLUETOOTH_DIR.iterdir()):
            print(f"[INFO] 备份现有蓝牙接收文件到：{backup_dir}")
            shutil.copytree(DEFAULT_BLUETOOTH_DIR, backup_dir, dirs_exist_ok=True)
        # 删除旧目录（必须是空目录或 junction 才能删除）
        try:
            DEFAULT_BLUETOOTH_DIR.rmdir()
        except OSError:
            # 目录非空，需要清空
            for item in DEFAULT_BLUETOOTH_DIR.iterdir():
                if item.is_file():
                    item.unlink()
                elif item.is_dir():
                    shutil.rmtree(item)
            DEFAULT_BLUETOOTH_DIR.rmdir()

    # 创建 junction（符号链接）
    import subprocess
    cmd = f'cmd /c mklink /J "{DEFAULT_BLUETOOTH_DIR}" "{INCOMING_DIR}"'
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)

    if result.returncode == 0:
        print(f"[SUCCESS] 符号链接创建成功")
        print(f"    系统蓝牙接收目录：{DEFAULT_BLUETOOTH_DIR}")
        print(f"    实际指向：{INCOMING_DIR}")
        print(f"    效果：蓝牙接收的文件将直接落入 ve4/incoming")
    else:
        print(f"[ERROR] 创建失败：{result.stderr}")
        print(f"[HINT] 请以管理员身份运行 PowerShell / CMD 后重试")
        return False

    return True


def setup_watchdog_bridge():
    """
    方案 B：保留默认蓝牙文件夹，生成 Watchdog 桥接配置。
    由 watcher.py 监控默认目录，文件到达后自动移动到 ve4/incoming。
    """
    ensure_dirs()

    # 生成桥接配置
    bridge_config = DEFAULT_BLUETOOTH_DIR.parent / "ve4_bluetooth_bridge.ini"
    with open(bridge_config, 'w', encoding='utf-8') as f:
        f.write(f"""; VE4 蓝牙接收桥接配置
[bridge]
source = {DEFAULT_BLUETOOTH_DIR}
target = {INCOMING_DIR}
method = move
auto_process = true
""")

    print(f"[SUCCESS] Watchdog 桥接配置已生成：{bridge_config}")
    print(f"    源目录（蓝牙默认）：{DEFAULT_BLUETOOTH_DIR}")
    print(f"    目标目录（ve4）：{INCOMING_DIR}")
    print(f"    效果：watcher.py 将监控源目录，文件到达后自动移动到 ve4/incoming")
    print(f"[NOTE] 无需管理员权限，请运行：python receiver/watcher.py --bridge")
    return True


def restore_default():
    """恢复系统默认蓝牙接收目录（删除符号链接，重建原始目录）"""
    if DEFAULT_BLUETOOTH_DIR.exists():
        # 判断是否是 junction
        import subprocess
        result = subprocess.run(
            f'fsutil reparsepoint query "{DEFAULT_BLUETOOTH_DIR}"',
            shell=True, capture_output=True, text=True
        )
        is_junction = "Junction" in result.stdout or result.returncode == 0

        if is_junction:
            # 删除 junction
            DEFAULT_BLUETOOTH_DIR.unlink()
            print(f"[INFO] 已删除符号链接：{DEFAULT_BLUETOOTH_DIR}")
        else:
            print(f"[WARN] {DEFAULT_BLUETOOTH_DIR} 不是符号链接，跳过删除")

    # 重建原始目录
    DEFAULT_BLUETOOTH_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[SUCCESS] 已恢复默认蓝牙接收目录：{DEFAULT_BLUETOOTH_DIR}")
    return True


def main():
    parser = argparse.ArgumentParser(description="VE4 蓝牙接收目录配置")
    parser.add_argument(
        "--method",
        choices=["junction", "watchdog", "restore"],
        default="watchdog",
        help="配置方案：junction（符号链接，需管理员）、watchdog（桥接，无需管理员）、restore（恢复默认）"
    )
    args = parser.parse_args()

    print("=" * 60)
    print("VE4 蓝牙接收目录配置工具")
    print("=" * 60)

    if args.method == "junction":
        print("\n[方案 A] 符号链接模式（文件直达，无中转）")
        print("-" * 60)
        success = setup_junction()
    elif args.method == "watchdog":
        print("\n[方案 B] Watchdog 桥接模式（无需管理员）")
        print("-" * 60)
        success = setup_watchdog_bridge()
    elif args.method == "restore":
        print("\n[恢复] 恢复系统默认蓝牙接收目录")
        print("-" * 60)
        success = restore_default()

    print("=" * 60)
    if success:
        print("配置完成。请查看上方的指引文件了解如何配对蓝牙并发送文件。")
    else:
        print("配置失败，请检查错误信息。")


if __name__ == "__main__":
    main()
