"""打包脚本 —— 产出单文件版：

  dist/WhoShitsOnMyC.exe

依赖系统已安装的 WebView2 运行时；缺失时程序启动会弹窗引导安装。
打包成功后会删掉 build/ 与 *.spec 中间产物，只保留 dist/。

用法：
    python build.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "WhoShitsOnMyC"

COMMON = [
    "--noconfirm",
    "--windowed",
    "--onefile",
    "--name", APP_NAME,
    "--icon", "logo.ico",
    "--add-data", "web;web",
    "--add-data", "logo.ico;.",
]


def _cleanup_intermediates() -> None:
    """删除 PyInstaller 中间产物，只保留 dist/。"""
    build_dir = os.path.join(ROOT, "build")
    if os.path.isdir(build_dir):
        shutil.rmtree(build_dir, ignore_errors=True)
        print("    已删除 build/")

    for name in (f"{APP_NAME}.spec", f"{APP_NAME}.spec.bak"):
        path = os.path.join(ROOT, name)
        if os.path.isfile(path):
            try:
                os.remove(path)
                print(f"    已删除 {name}")
            except OSError as exc:
                print(f"    删除 {name} 失败: {exc}", file=sys.stderr)


def main() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.exit("未找到 PyInstaller，请先执行：pip install pyinstaller")

    print("==> 打包单文件版（依赖系统 WebView2；缺失时启动会提示）...")
    cmd = [sys.executable, "-m", "PyInstaller", *COMMON, "app.py"]
    try:
        subprocess.run(cmd, cwd=ROOT, check=True)
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)

    _cleanup_intermediates()
    print(f"    完成 -> dist/{APP_NAME}.exe")


if __name__ == "__main__":
    main()
