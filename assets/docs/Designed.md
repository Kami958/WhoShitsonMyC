# 开发者文档

快照扫描目录，再对比两份快照，定位空间变化。

栈：Python 3.10+ · pywebview (WebView2) · SQLite · `web/` 前端。

## 构建

```bash
pip install -r requirements.txt
python app.py
python -m pytest tests/ -q
pip install pyinstaller && python build.py   # dist/wsmc-v*.exe
```

## 目录

```text
app.py           窗口、JS API、扫描/设置后台线程、WebView2
titlebar.py      标题栏暗/浅色（Windows）
build.py         打包
version.py       版本号
core/            无 UI 核心
  models.py        Entry / SnapshotMeta / DiffNode
  scanner.py       多线程扫描；盘符根默认尝试 MFT
  mft/             NTFS MFT（读∥解析、紧凑建树；失败回退 scandir）
  snapshot.py      SQLite 快照（v3 邻接表；meta 含 note）
  differ.py        逐层懒对比
  compress.py      .db ↔ .dbz；会话内解压；备注写回
  store.py         快照路径、settings.yaml、目录迁移、备注
  applog.py        进程内日志（默认不落盘；可导出；路径脱敏）
  i18n.py          后端文案语言
  timing_probe.py  开发计时入口（生产空操作）
web/             index.html / app.js / style.css（设置：通用/日志、卸载、备注、迁移进度）
tests/
dev/             开发辅助（打包 exclude）
assets/docs/     本文与模块说明
```

运行时数据：`%LOCALAPPDATA%\WhoShitsOnMyC\`（默认 `snapshots/`、可选 `settings.yaml`）。  
默认不写配置；用户改过设置后才自动生成 `settings.yaml`（`common:` 分节；旧扁平仍可读）。  
快照目录可在设置里改；改完点「完成」会迁移原目录中的 `.db`/`.dbz`。  
设置 → 通用「恢复默认」：删配置并回内置默认（不删快照文件）。  
设置 → 通用「卸载」：弹窗确认是否删数据（默认勾选）；勾选时只清应用数据目录，不碰自定义快照路径、不删程序本体；完成后确认退出。  
进程内日志（`applog`，1024 条）可在设置 → 日志查看/导出；默认不落盘；导出文本已脱敏。  
`.dbz` 对比时解到系统临时目录，仅本进程存活期间复用，**不写** `cache/`。  
备注写在快照文件内（`.db` meta / `.dbz` meta.json），随文件移动。

## 模块说明

| 文档 | 内容 |
|------|------|
| [扫描链路](scan-pipeline.md) | UI → 写快照 → 可选压缩；设置 / 备注 / 迁移 |
