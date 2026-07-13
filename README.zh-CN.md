# WhoShitsOnMyC

[English](README.md) | [中文](README.zh-CN.md)

<div align="center">
  <img src="logo.png" width="72" alt="WhoShitsOnMyC" />
  <h1>WhoShitsOnMyC</h1>
  <p><strong>C 盘刚清完，过几天又莫名少了一大截？用它记录找出这段时间到底是谁在吃空间</strong></p>
</div>


<p align="center">
  <a href="https://github.com/Kami958/WhoShitsonMyC/releases"><img src="https://img.shields.io/github/v/release/Kami958/WhoShitsonMyC?style=flat-square&label=release&sort=semver" alt="Release" /></a>
  <a href="https://github.com/Kami958/WhoShitsonMyC/blob/master/LICENSE"><img src="https://img.shields.io/github/license/Kami958/WhoShitsonMyC?style=flat-square" alt="License" /></a>
  <img src="https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-0A7EA4?style=flat-square" alt="Platform" />
  <img src="https://img.shields.io/badge/UI-WebView2-1F6FEB?style=flat-square" alt="WebView2" />
</p>

---

## 它解决什么问题

用清理软件清完 C 盘，通常能安静一阵子。可某天空间突然少了一大截，新垃圾从哪来却完全没头绪。再打开清理软件，还是一堆「也许能删」的项目，只能继续瞎猜，**你永远不知道是谁在上次清理过后偷偷拉💩**

**WhoShitsOnMyC** 正是为此而生

> **和上次比，谁变了？**

与其每次都靠感觉找垃圾，不如在空间正常时先扫描得到一份旧快照，等空间增长后再扫一遍得到新快照
**通过对比快照，谁在涨、谁新冒出来，一眼就能看出来**

<p align="center">
  <img src="assets/screenshots/ui-zh.jpg" alt="WhoShitsOnMyC 中文界面" width="900" />
</p>
## 下载

到 [Releases](https://github.com/Kami958/WhoShitsonMyC/releases) 下载

| 项目依赖 | 说明 |
| --- | --- |
| 系统 | Windows 10 / 11 |
| WebView2 | 界面需要 [Microsoft Edge WebView2](https://developer.microsoft.com/microsoft-edge/webview2/)。**Windows 11 和多数 Windows 10 一般已经预装**；若缺失，启动时会提示并打开下载页，装好常青版再打开程序 |

## 快速上手（推荐以管理员身份运行）


1. **推荐以管理员身份运行**，不仅能在扫描根盘符的时候提升速度，还能扫描部分隐藏路径
2. 点 **＋ 新建扫描**，选择一个目录，例如 `C:\`。扫描完成后，你会得到一份快照
3. 当空间又被吃掉时，请对**同一个目录**再扫描一次
4. 把较早的那份设为 **基准**，较新的设为 **当前**，然后点 **对比**
5. 对比结果会显示在下方：红色表示增长，绿色表示缩小

侧栏下方还可以：

- **打开 / 刷新 / 导入快照**：打开当前快照目录、刷新列表，或从其它位置导入快照
- **设置**：调整扫描线程、是否压缩快照、是否尝试 MFT、快照存放目录等
- **语言 / 主题**：在中文与英文、暗色与浅色之间切换

## 数据与卸载

### 我们产生了什么数据文件

> 是的，我们也在你的 C 盘底下拉了一点 💩

软件在本机产生的配置文件与默认快照，默认都存放在：

`%LOCALAPPDATA%\WhoShitsOnMyC`

你可以把这条路径粘贴到资源管理器地址栏后回车打开

| 内容 | 位置 |
| --- | --- |
| 快照 | 默认在上述目录下的 `snapshots` 文件夹中；你也可以在设置里改成其它存放位置 |
| 设置 | 同样写在上述目录中的配置文件里；你在设置页改过选项并点「完成」后会保存 |

### 如何卸载WhoShitsOnMyC？

**打开 设置 → 通用，点红色「卸载」，在弹窗中确认是否删除数据后完成清理**

- 删除数据时，软件会清掉默认数据目录里的配置与快照
- **如果您迁移过快照存放位置，则该迁移位置仍需您手动删除！**
- 清理完成后，再自行删除exe程序即可

## 常见问题

**Q：扫描进度很久是怎么回事？**  
**A：扫描时间主要取决于你选择的路径下有多少文件和电脑配置**

> 以作者机器为例：约百万级文件的盘符根、M.2 固态上，开启 MFT 时整次扫描大约十数秒量级（视机器与冷热缓存而变）

- 如果扫描目录在机械硬盘上，请到设置页把扫描线程数设为 1  

- **扫描根盘符（如 `C:\`、`D:\`）时**，在设置项中**打开或关闭** [尝试MFT扫描] 或许会有帮助（需要管理员身份）
- 若仍然有问题，欢迎提交 issue

**Q：为什么我连续扫描两次，容量变化很大？**

**A：**可以考虑到这几种情况

1. 两次扫描是在不同运行模式下进行的（**非管理员、管理员**），部分路径需要管理员的权限才能读取
2. 的确在这两次扫描的间隔之间，某些其他软件生成了内容，具体可查看对比树

**Q：对比树上出现「不可比较」？**  
**A：**部分路径可能因为权限等原因没有扫描完整，所以无法参与对比

**Q：启动时提示缺少 WebView2？**  
**A：**请先安装 [WebView2 常青版](https://developer.microsoft.com/microsoft-edge/webview2/)，然后再打开程序



---

## 从源码构建

需要 **Python 3.10+**

```bash
pip install -r requirements.txt
python app.py
python -m pytest tests/ -q

pip install pyinstaller
python build.py
```

## 开发者

若想详细了解项目构成，请查看[开发者文档](assets/docs/Designed.md)

## 许可

[MIT](LICENSE)

## 友情链接

[LINUX DO](https://linux.do/)
