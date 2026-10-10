"""导入冒烟：把随产品发布的模块逐个导入一遍。

搬动符号、调整包结构之后最先炸的往往是导入：循环导入、指向老位置的
``from 老模块 import 名字``、模块顶层就写错的代码。逐个导入几秒钟跑完，
比点开每个功能去试快得多。dev/ 下的一次性脚本和打包产物不在范围内。
"""

from __future__ import annotations

import importlib
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGES = ("core", "app", "modules")


def module_names() -> list[str]:
    """包里除 ``__init__`` 之外的全部模块名。"""
    names = set()
    for package in PACKAGES:
        for path in sorted((REPO_ROOT / package).rglob("*.py")):
            if "__pycache__" in str(path):
                continue
            parts = [p for p in path.relative_to(REPO_ROOT).with_suffix("").parts if p != "__init__"]
            if parts:
                names.add(".".join(parts))
    return sorted(names)


def test_every_module_imports():
    """每个模块都能导入，失败信息里带模块名。"""
    failures = []
    for name in module_names():
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - 这里就是要抓住所有异常
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    assert not failures, "有模块导入失败：\n" + "\n".join(failures)


def test_legacy_import_paths_still_work():
    """拆分后老导入路径照旧可用，外部代码与前端桥接不受影响。"""
    import core.differ
    import core.store
    import modules.ai.service
    from app import Api
    from core import store_paths, store_values, store_yaml

    assert core.differ.Diff is not None
    assert core.differ.compare_snapshots is not None
    assert Api is not None
    assert modules.ai.service.AiService is not None

    # 从 store.py 搬出去的名字要能从老路径拿回来，否则老代码会 ImportError。
    missing = []
    for module in (store_paths, store_values, store_yaml):
        for name in dir(module):
            if name.startswith("_") or name in {"annotations"}:
                continue
            if not hasattr(core.store, name):
                missing.append(f"core.store 少了 {name}（定义在 {module.__name__}）")
    assert not missing, "\n".join(missing)


@pytest.mark.parametrize(
    ("package", "legacy"),
    [("core/differ_pkg", "core.differ"), ("modules/ai/service_pkg", "modules.ai.service")],
)
def test_implementation_does_not_import_legacy_shell(package, legacy):
    """实现包不反向导入兼容壳，否则会形成环。"""
    import ast

    offenders = []
    for path in sorted((REPO_ROOT / package).rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == legacy:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
            elif isinstance(node, ast.Import):
                offenders += [
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
                    for alias in node.names
                    if alias.name == legacy
                ]
    assert not offenders, f"{package} 反向导入了 {legacy}：\n" + "\n".join(offenders)
