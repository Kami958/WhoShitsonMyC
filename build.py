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
# APP_NAME = "WhoShitsOnMyC"
#改为缩写
APP_NAME = "wsmc"

# 与 version.py 保持一致；真正进程序的版本由 app 直接 import version。
try:
    from version import __version__ as APP_VERSION
except ImportError:  # pragma: no cover
    print("Lost of version info")
    exit(1)

# PyInstaller 先用固定名产出，再改成带版本后缀的文件名，避免 --name 里夹版本号
# 导致 build/spec 目录名随版本乱飘。
EXE_BASENAME = f"{APP_NAME}-v{APP_VERSION}"
DIST_DIR = os.path.join(ROOT, "dist")
RAW_EXE = os.path.join(DIST_DIR, f"{APP_NAME}.exe")
VERSIONED_EXE = os.path.join(DIST_DIR, f"{EXE_BASENAME}.exe")

# 正式包用不到的依赖：PyInstaller 会因 pywebview/bottle/pythonnet 的可选 import
# 把 cryptography、gevent、Tcl/Tk、setuptools 等一并打进 onefile，体积膨胀约 10MB。
# 本程序：WebView2 + 本地文件 UI，无 SSL 自签、无 gevent 服务端、无 tk 对话框。
_EXCLUDES = [
    "dev",  # 开发期计时/探测；运行时 sys.frozen 也会禁用
    # SSL 自签证书（pywebview 可选）→ 连带 OpenSSL DLL / rust 扩展
    "cryptography",
    "cryptography.hazmat",
    "cryptography.x509",
    "bcrypt",
    # bottle 可选 server 适配器
    "gevent",
    "geventwebsocket",
    "greenlet",
    # GUI 无关（界面是 WebView2）
    "tkinter",
    "_tkinter",
    "turtle",
    # 打包/安装工具链被 pythonnet 等间接拉入
    "setuptools",
    "pkg_resources",
    "distutils",
    "wheel",
    "pip",
    # 标准库调试/测试/文档（Analysis 偶发扫入）
    "test",
    "unittest",
    "pydoc",
    "pydoc_data",
    "doctest",
    "pdb",
    "lib2to3",
    "xmlrpc",
    # pywebview 其它平台后端（Windows 只用 edgechromium/winforms）
    "webview.platforms.cocoa",
    "webview.platforms.gtk",
    "webview.platforms.qt",
    "webview.platforms.android",
    "webview.platforms.cef",
    "webview.platforms.mshtml",
]

COMMON = [
    "--noconfirm",
    "--windowed",
    "--onefile",
    "--name", APP_NAME,
    "--icon", "logo.ico",
    "--add-data", "web;web",
    "--add-data", "logo.ico;.",
]
for _mod in _EXCLUDES:
    COMMON.extend(("--exclude-module", _mod))


def _cleanup_intermediates() -> None:
    """删除 PyInstaller 中间产物，只保留 dist/。"""
    build_dir = os.path.join(ROOT, "build")
    if os.path.isdir(build_dir):
        shutil.rmtree(build_dir, ignore_errors=True)
        print("已删除 build/")

    for name in (f"{APP_NAME}.spec", f"{APP_NAME}.spec.bak"):
        path = os.path.join(ROOT, name)
        if os.path.isfile(path):
            try:
                os.remove(path)
                print(f"已删除 {name}")
            except OSError as exc:
                print(f"删除 {name} 失败: {exc}", file=sys.stderr)


def _ensure_conda_dll_path() -> None:
    """让 PyInstaller 能解析 conda 扩展依赖的原生 DLL。

    miniconda/anaconda 下 pyexpat.pyd、_sqlite3.pyd 等依赖
    ``<prefix>/Library/bin`` 里的 libexpat.dll、sqlite3.dll 等；
    该目录不在 PATH 时，Analysis 会报 Library not found，
    打出的 onefile 启动即 ``DLL load failed while importing pyexpat``。
    """
    candidates: list[str] = []
    for prefix in (sys.prefix, getattr(sys, "base_prefix", sys.prefix)):
        for rel in (("Library", "bin"), ("DLLs",), ("Library", "usr", "bin")):
            d = os.path.join(prefix, *rel)
            if os.path.isdir(d) and d not in candidates:
                candidates.append(d)

    if not candidates:
        return

    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    changed = False
    for d in reversed(candidates):
        if d not in parts:
            parts.insert(0, d)
            changed = True
    if changed:
        os.environ["PATH"] = os.pathsep.join(parts)
        print("已将 conda DLL 目录加入 PATH：")
        for d in candidates:
            print(f"  {d}")


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
                    print(f"已删除旧包 {name}")
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
    _ensure_conda_dll_path()
    cmd = [sys.executable, "-m", "PyInstaller", *COMMON, "app.py"]
    try:
        # 继承已补齐 PATH 的环境，避免子进程丢 conda Library/bin。
        subprocess.run(cmd, cwd=ROOT, check=True, env=os.environ.copy())
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)

    _cleanup_intermediates()
    final_name = _rename_with_version()
    print(f"完成 -> dist/{final_name}")


if __name__ == "__main__":
    main()
