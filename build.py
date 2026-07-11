"""打包脚本 —— 产出带版本号的单文件版：

  dist/WhoShitsOnMyC-v{version}.exe

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

# 与 version.py 保持一致；真正进程序的版本由 app 直接 import version。
try:
    from version import __version__ as APP_VERSION
except ImportError:  # pragma: no cover
    APP_VERSION = "0.0.0"

# PyInstaller 先用固定名产出，再改成带版本后缀的文件名，避免 --name 里夹版本号
# 导致 build/spec 目录名随版本乱飘。
EXE_BASENAME = f"{APP_NAME}-v{APP_VERSION}"
DIST_DIR = os.path.join(ROOT, "dist")
RAW_EXE = os.path.join(DIST_DIR, f"{APP_NAME}.exe")
VERSIONED_EXE = os.path.join(DIST_DIR, f"{EXE_BASENAME}.exe")

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


def _rename_with_version() -> str:
    """把 dist/WhoShitsOnMyC.exe 改成带版本后缀；返回最终文件名。"""
    if not os.path.isfile(RAW_EXE):
        sys.exit(f"打包结果不存在：{RAW_EXE}")

    # 同版本已存在则覆盖，避免 rename 失败。
    if os.path.isfile(VERSIONED_EXE):
        try:
            os.remove(VERSIONED_EXE)
        except OSError as exc:
            sys.exit(f"无法覆盖旧文件 {VERSIONED_EXE}：{exc}")

    try:
        os.replace(RAW_EXE, VERSIONED_EXE)
    except OSError as exc:
        sys.exit(f"重命名失败：{exc}")

    # 顺手清掉 dist 里其它旧版 exe（同前缀、不同版本），只留本次产物。
    try:
        for name in os.listdir(DIST_DIR):
            if not name.lower().endswith(".exe"):
                continue
            path = os.path.join(DIST_DIR, name)
            if path == VERSIONED_EXE:
                continue
            if name.startswith(f"{APP_NAME}-v") or name == f"{APP_NAME}.exe":
                try:
                    os.remove(path)
                    print(f"    已删除旧包 {name}")
                except OSError:
                    pass
    except OSError:
        pass

    return f"{EXE_BASENAME}.exe"


def main() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.exit("未找到 PyInstaller，请先执行：pip install pyinstaller")

    print(f"==> 打包单文件版 v{APP_VERSION}（依赖系统 WebView2；缺失时启动会提示）...")
    cmd = [sys.executable, "-m", "PyInstaller", *COMMON, "app.py"]
    try:
        subprocess.run(cmd, cwd=ROOT, check=True)
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)

    _cleanup_intermediates()
    final_name = _rename_with_version()
    print(f"    完成 -> dist/{final_name}")


if __name__ == "__main__":
    main()
