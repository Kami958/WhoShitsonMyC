"""静态引用检查：不执行代码，只读语法树。

把函数或符号从一个文件搬到另一个模块之后，引用方那一侧的导入清单最容易漏。
这类遗漏语法编译看不出来，单元测试也覆盖不到没被执行的路径，只有真的点开那个
功能才报 NameError。真实例子：拆分 app.py 时 ``_start_temp_sweep`` 的定义搬到了
``app/bootstrap.py``，启动代码漏了导入它，一启动就崩，而语法编译和 320 条测试全过。

三条扫描：

1. 未定义名字：文件里引用了，但整个文件都没绑定过这个名字。
2. 顶层重复定义：同一模块里同名 def/class/赋值出现两次，脚本改文件常留下这种垃圾。
3. 模块属性：``某模块.某名字`` 里那个模块没有这个属性，符号被搬走或改名了。

范围是随产品发布的代码加 tests/，dev/ 下的一次性脚本不算。扫描只收集整个文件的
绑定名，不区分作用域，所以「局部名当全局用」这类错误会漏报，报出来的一定是真问题。
新增顶层包时要把目录补进 ``ROOTS``。
"""

from __future__ import annotations

import ast
import builtins
import importlib
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ROOTS = ("core", "app", "modules", "tests")

# 语法里没有绑定语句、但运行时确实存在的名字。
IMPLICIT_NAMES = {
    "__builtins__",
    "__cached__",
    "__debug__",
    "__doc__",
    "__file__",
    "__loader__",
    "__name__",
    "__package__",
    "__path__",
    "__spec__",
}

# 属性扫描放行的动态情形，每条都写清理由，不放行就只能靠猜。
ATTRIBUTE_ALLOWLIST = {
    # POSIX 专有，Windows 上 os 模块没有这个属性，代码里按 os.name 分支调用。
    "os.sysconf",
    # pythonnet 的 .NET 命名空间对象，属性由 CLR 运行时解析，静态看不见。
    "WinForms",
}

KNOWN_NAMES = set(dir(builtins)) | IMPLICIT_NAMES


def source_files() -> list[pathlib.Path]:
    """随产品发布的 Python 文件与测试。"""
    files = [p for p in REPO_ROOT.glob("*.py")]
    for name in ROOTS:
        files += sorted((REPO_ROOT / name).rglob("*.py"))
    return sorted(p for p in files if "__pycache__" not in str(p))


def parse(path: pathlib.Path) -> ast.AST | None:
    """解析文件；解析不了由语法编译那关负责，这里跳过。"""
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return None


def bound_names(tree: ast.AST) -> set[str]:
    """收集文件里所有被绑定过的名字。

    只统计绑定，不看作用域，所以集合偏大，报出来的引用一定是真未定义。
    """
    names: set[str] = set()

    def add_args(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        a = node.args
        for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs]:
            names.add(arg.arg)
        if a.vararg:
            names.add(a.vararg.arg)
        if a.kwarg:
            names.add(a.kwarg.arg)

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            if not isinstance(node, ast.ClassDef):
                add_args(node)
            for type_param in getattr(node, "type_params", []):
                names.add(type_param.name)
        elif isinstance(node, ast.Lambda):
            add_args(node)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
        elif isinstance(node, ast.MatchAs) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchStar) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.add(node.rest)
        elif isinstance(node, ast.TypeAlias):
            names.add(node.name.id)
        elif isinstance(node, ast.TypeVar | ast.ParamSpec | ast.TypeVarTuple) if hasattr(ast, "TypeVar") else False:
            names.add(node.name)
    return names


def test_no_undefined_names():
    """每个被引用的名字都得在这个文件里绑定过。"""
    problems = []
    for path in source_files():
        tree = parse(path)
        if tree is None:
            continue
        if any(isinstance(n, ast.ImportFrom) and n.names[0].name == "*" for n in ast.walk(tree)):
            continue  # 星号导入看不见来源，跳过
        known = bound_names(tree) | KNOWN_NAMES
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in known:
                problems.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} 引用了没定义的 {node.id}")
    assert not problems, "存在未定义的名字：\n" + "\n".join(problems)


def test_no_duplicate_top_level_definitions():
    """同一模块顶层不许同名 def/class/赋值出现两次。"""
    problems = []
    for path in source_files():
        tree = parse(path)
        if tree is None:
            continue
        seen: dict[str, list[int]] = {}
        for node in tree.body:
            targets: list[str] = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                targets = [node.name]
            elif isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets = [node.target.id]
            for name in targets:
                seen.setdefault(name, []).append(node.lineno)
        for name, lines in seen.items():
            if len(lines) > 1:
                rows = "、".join(str(n) for n in lines)
                problems.append(f"{path.relative_to(REPO_ROOT)} 顶层 {name} 重复定义于第 {rows} 行")
    assert not problems, "存在重复定义：\n" + "\n".join(problems)


def _dotted(node: ast.AST) -> list[str] | None:
    """把 ``a.b.c`` 这种属性链拆成 ['a', 'b', 'c']，链上有非名字节点就返回 None。"""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    parts.reverse()
    return parts


def _module_aliases(tree: ast.AST) -> dict[str, str]:
    """收集 ``import a.b`` 绑定的模块名，返回 {本地名: 模块全名}。"""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    root = alias.name.split(".")[0]
                    aliases[root] = root
        elif isinstance(node, ast.ImportFrom) and node.level:
            continue  # 相对导入的模块对象引用少，交给导入冒烟那关
    return aliases


def test_module_attribute_references_exist():
    """``某模块.某名字`` 里那个模块真的有这个属性。"""
    cache: dict[str, object | None] = {}
    problems = []
    for path in source_files():
        tree = parse(path)
        if tree is None:
            continue
        aliases = _module_aliases(tree)
        if not aliases:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or not isinstance(node.ctx, ast.Load):
                continue
            parts = _dotted(node)
            if not parts or len(parts) < 2 or parts[0] not in aliases:
                continue
            module_name = aliases[parts[0]]
            dotted = ".".join(parts)
            if any(dotted.startswith(allow) for allow in ATTRIBUTE_ALLOWLIST):
                continue
            if module_name in cache:
                module = cache[module_name]
            else:
                try:
                    module = importlib.import_module(module_name)
                except Exception:  # noqa: BLE001 - 导入失败由导入冒烟那关报
                    module = None
                cache[module_name] = module
            if module is None:
                continue
            obj: object = module
            owner = module_name
            for part in parts[1:]:
                if hasattr(obj, part):
                    obj = getattr(obj, part)
                    owner = f"{owner}.{part}"
                    continue
                submodule = f"{owner}.{part}"
                try:
                    obj = importlib.import_module(submodule)
                except Exception:  # noqa: BLE001 - 确实不存在
                    problems.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno} 用了 {dotted}，"
                        f"但 {owner} 没有 {part}"
                    )
                    obj = None
                    break
                owner = submodule
    assert not problems, "存在指不到东西的模块属性：\n" + "\n".join(sorted(set(problems)))
