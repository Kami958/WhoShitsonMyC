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
import re

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


_DIRECT_DELETE_NAMES = frozenset({"delete_path", "delete_permanent", "delete_to_recycle"})


def test_delete_only_through_guard():
    """产品代码里只允许统一删除口调用底层删除。"""
    problems = []
    guard = REPO_ROOT / "core" / "fs_delete.py"
    for path in source_files():
        if path == guard or "tests" in path.relative_to(REPO_ROOT).parts:
            continue
        tree = parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            parts = _dotted(node.func)
            if not parts or parts[-1] not in _DIRECT_DELETE_NAMES:
                continue
            if len(parts) == 1 or (len(parts) >= 2 and parts[-2] == "fs_delete"):
                problems.append(
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno} 直接调用了 "
                    f"{'.'.join(parts)}，删除必须经过统一入口"
                )
    assert not problems, "存在绕过统一删除口的调用：\n" + "\n".join(problems)

# ---- 界面文案键：代码引用的键必须在两份语言里都存在 ----

_I18N_JS = REPO_ROOT / "web" / "js" / "i18n.js"
_INDEX_HTML = REPO_ROOT / "web" / "index.html"
_WEB_JS = REPO_ROOT / "web" / "js"


def _i18n_keys() -> tuple[set[str], set[str]]:
    """读出中英两段的键集合。

    文案表的结构是 ``const I18N = { zh: {...}, en: {...} }``，两段各占一半，
    因此按 ``en: {`` 的位置切开分别取键。
    """
    text = _I18N_JS.read_text(encoding="utf-8")
    split = text.index("  en: {")
    zh = set(re.findall(r"^    ([A-Za-z0-9_]+):", text[:split], re.M))
    en = set(re.findall(r"^    ([A-Za-z0-9_]+):", text[split:], re.M))
    return zh, en


def _used_i18n_keys() -> dict[str, set[str]]:
    """收集界面上用到的文案键，按来源分组，方便报错时指出是哪个文件。"""
    used: dict[str, set[str]] = {}
    html = _INDEX_HTML.read_text(encoding="utf-8")
    keys: set[str] = set()
    for attr in ("data-i18n", "data-i18n-title", "data-i18n-placeholder"):
        keys |= set(re.findall(r'%s="([^"]+)"' % attr, html))
    used["index.html"] = keys
    for path in sorted(_WEB_JS.glob("*.js")):
        text = path.read_text(encoding="utf-8", errors="replace")
        keys = set(re.findall(r't\(\s*"([A-Za-z0-9_]+)"', text))
        if keys:
            used[path.name] = keys
    return used


def test_language_tables_have_same_keys():
    """中文与英文必须一一对应，少一条那一侧就会把键名直接显示到界面上。"""
    zh, en = _i18n_keys()
    assert zh, "中文文案表没读到任何键"
    assert en, "英文文案表没读到任何键"
    assert not (zh - en), "只在中文里有：%s" % sorted(zh - en)[:10]
    assert not (en - zh), "只在英文里有：%s" % sorted(en - zh)[:10]


def test_used_i18n_keys_exist():
    """界面引用的每个键都要有译文。

    真实事故：一次批量删文案的脚本按行首键名匹配，把后面一整片键连带删掉，
    语法检查与全部测试都通过，但界面上那一片文字全变成了键名。
    """
    zh, _en = _i18n_keys()
    missing: list[str] = []
    for source, keys in _used_i18n_keys().items():
        for key in sorted(keys):
            if key not in zh:
                missing.append("%s 用到 %s" % (source, key))
    assert not missing, "文案表里没有这些键：" + "、".join(missing)
