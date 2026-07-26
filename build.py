"""打包脚本 —— 产出带版本号的单文件版：

  dist/wsmc-v{version}.exe       # 默认（不含 AI；当前产品策略）
  dist/wsmc-ai-v{version}.exe    # python build.py --with-ai（仅维护/实验）

当前默认**不**主动提供 AI 发行版：``python build.py`` 始终打主线。
``--with-ai`` 仍保留在脚本里，便于以后按 ``assets/docs/ai-reenable.md`` 重新接入；
日常开发与发布不要依赖它。

依赖系统已安装的 WebView2 运行时；缺失时程序启动会弹窗引导安装。
打包成功后会删掉 build/ 与 *.spec 中间产物，只保留 dist/。

用法：
    python build.py
    # python build.py --with-ai   # 默认关闭；重新接入 AI 时再使用
"""

from __future__ import annotations

import argparse
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

DIST_DIR = os.path.join(ROOT, "dist")

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
    # 注意：3.12 下不要排除 distutils。
    # 标准库已移除 distutils，但 PyInstaller 的 hook-distutils 仍可能对
    # 该名字做 alias；若先 --exclude-module distutils，会在分析阶段炸掉。
    "setuptools",
    "pkg_resources",
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

# 主线包排除 AI 模块与仅 AI 使用的 httpx 栈（前端 js 仍可存在，靠 list_modules 门控）
_AI_EXCLUDES = [
    "modules.ai",
    "modules.ai.config",
    "modules.ai.client",
    "modules.ai.prompts",
    "modules.ai.service",
    "modules.ai.packing",
    "modules.ai.tools",
    "httpx",
    "httpcore",
    "h11",
    "anyio",
    "sniffio",
    "certifi",
]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build WhoShitsOnMyC single-file exe")
    p.add_argument(
        "--with-ai",
        action="store_true",
        help=(
            "Include experimental AI module (wsmc-ai-v*.exe). "
            "Disabled by product policy; see assets/docs/ai-reenable.md"
        ),
    )
    return p.parse_args(argv)


def _prepare_web_datas(*, with_ai: bool) -> tuple[str, list[str]]:
    """准备打进包的 web 资源。

    主线（无 AI）排除 AI 专用前端依赖（``web/js/ai-vendor``），避免带上
    marked / DOMPurify。含 AI 包原样打包整个 ``web/``。
    返回 ``(datas 源路径, 临时目录列表，构建后需清理)``。
    """
    web_src = os.path.join(ROOT, "web")
    if with_ai:
        return web_src, []

    import tempfile

    tmp = tempfile.mkdtemp(prefix="wsmc-web-main-")
    for root, dirs, files in os.walk(web_src):
        rel = os.path.relpath(root, web_src)
        rel_posix = os.path.normpath(rel).replace("\\", "/")
        if rel_posix == "js/ai-vendor" or rel_posix.startswith("js/ai-vendor/"):
            dirs[:] = []
            continue
        if "ai-vendor" in dirs and os.path.basename(root) == "js":
            dirs.remove("ai-vendor")
        dest_dir = tmp if rel == "." else os.path.join(tmp, rel)
        os.makedirs(dest_dir, exist_ok=True)
        for name in files:
            shutil.copy2(os.path.join(root, name), os.path.join(dest_dir, name))
    return tmp, [tmp]


def _build_common(excludes: list[str], *, web_src: str | None = None) -> list[str]:
    web_path = web_src or os.path.join(ROOT, "web")
    common = [
        "--noconfirm",
        "--windowed",
        "--onefile",
        "--name", APP_NAME,
        "--icon", "logo.ico",
        "--add-data", f"{web_path};web",
        "--add-data", "logo.ico;.",
    ]
    for mod in excludes:
        common.extend(("--exclude-module", mod))
    return common


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


def _rename_with_version(*, with_ai: bool = False) -> str:
    """把 dist/wsmc.exe 改成带版本后缀；返回最终文件名。"""
    raw_exe = os.path.join(DIST_DIR, f"{APP_NAME}.exe")
    basename = (
        f"{APP_NAME}-ai-v{APP_VERSION}" if with_ai else f"{APP_NAME}-v{APP_VERSION}"
    )
    versioned_exe = os.path.join(DIST_DIR, f"{basename}.exe")

    if not os.path.isfile(raw_exe):
        sys.exit(f"打包结果不存在：{raw_exe}")

    # 同版本已存在则覆盖，避免 rename 失败。
    if os.path.isfile(versioned_exe):
        try:
            os.remove(versioned_exe)
        except OSError as exc:
            sys.exit(f"无法覆盖旧文件 {versioned_exe}：{exc}")

    try:
        os.replace(raw_exe, versioned_exe)
    except OSError as exc:
        sys.exit(f"重命名失败：{exc}")

    return f"{basename}.exe"


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.exit("未找到 PyInstaller，请先执行：pip install pyinstaller")

    # 默认永远主线无 AI；--with-ai 仅供重新接入实验包（见 ai-reenable.md）。
    with_ai = bool(args.with_ai)
    if with_ai:
        print(
            "==> 注意：当前产品默认不发 AI 包；"
            "--with-ai 仅供维护/实验（见 assets/docs/ai-reenable.md）"
        )

    excludes = list(_EXCLUDES)
    if not with_ai:
        excludes.extend(_AI_EXCLUDES)
        print(
            f"==> 打包主线版 v{APP_VERSION}（不含 AI；依赖系统 WebView2）..."
        )
    else:
        print(
            f"==> 打包 AI 版 v{APP_VERSION}（含实验 AI；依赖系统 WebView2）..."
        )

    _ensure_conda_dll_path()
    web_src, tmp_dirs = _prepare_web_datas(with_ai=with_ai)
    if not with_ai:
        print("==> 主线：已排除 web/js/ai-vendor（Markdown 渲染库）")
    common = _build_common(excludes, web_src=web_src)
    cmd = [sys.executable, "-m", "PyInstaller", *common, "app.py"]
    try:
        # 继承已补齐 PATH 的环境，避免子进程丢 conda Library/bin。
        subprocess.run(cmd, cwd=ROOT, check=True, env=os.environ.copy())
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)
    finally:
        for d in tmp_dirs:
            shutil.rmtree(d, ignore_errors=True)

    _cleanup_intermediates()
    final_name = _rename_with_version(with_ai=with_ai)
    print(f"完成 -> dist/{final_name}")


if __name__ == "__main__":
    main()
