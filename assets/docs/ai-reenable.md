# 重新接入 AI（当前默认关闭）

产品策略：AI 定位与行为尚不清晰，**开发运行与默认打包都不加载 AI**。  
`modules/ai/`、`web/js/ai.js`、相关测试**代码保留**，只是启动发现与默认发行路径关掉了。

## 现状（关掉了什么）

| 位置 | 行为 |
| --- | --- |
| `modules/__init__.py` → `discover()` | `ENABLE_AI = False`，不 `import modules.ai`、不注册 |
| `app.py` → `_init_modules()` | 仍调用 `discover()`；默认得到空表，`list_modules()` 无 `ai` |
| `build.py` | `python build.py` 排除 AI；`--with-ai` 仍在脚本中，仅维护/实验用 |
| `requirements.txt` | `httpx` 默认注释；无 AI 时不必安装 |

前端 `web/js/ai.js` 等仍会随页面加载，但靠 `list_modules` / `data-module="ai"` 门控，**没有后端 ai 模块时入口保持隐藏**。

## 重新打开 AI 的步骤

按顺序做，做完再跑测试与打包。

### 1. 开发运行：允许 discover 注册 AI

编辑 `modules/__init__.py`：

```python
ENABLE_AI = True
```

### 2. 依赖

在 `requirements.txt` 取消注释：

```text
httpx>=0.27,<1
```

然后：

```bash
pip install -r requirements.txt
```

### 3. 确认启动加载

```bash
python app.py
```

日志应出现 `modules loaded: ai`（或设置页出现 AI 分节）。  
前端 `list_modules` 应含 `ai: true`。

### 4. 打包 AI 实验包（可选）

```bash
python build.py --with-ai
```

得到 `dist/wsmc-ai-v{version}.exe`。  
日常发布仍建议只发默认主线 `python build.py`。

### 5. 测试

```bash
python -m pytest tests/ -q
```

AI 单测在 `tests/test_ai_*.py`、`tests/test_modules.py`；  
`test_discover_includes_ai` / `test_list_modules_after_init` 在 `ENABLE_AI = True` 且依赖齐全时才应期望含 `ai`。

## 不要做的事

- 不要为了「默认关闭」删除 `modules/ai/` 或前端 AI 文件  
- 不要在未改 `ENABLE_AI` 时假设 `list_modules` 一定有 `ai`  
- 不要把 `--with-ai` 当作当前默认发布路径  

## 相关文件

- 模块发现：`modules/__init__.py`
- 启动注册：`app.py` → `Api._init_modules`
- 打包：`build.py`（`_AI_EXCLUDES`、`--with-ai`）
- 实施史：`assets/docs/ai-integration-plan.md`、`assets/docs/Designed.md`
