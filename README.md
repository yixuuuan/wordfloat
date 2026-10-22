<p align="center"><img src="assets/AppIcon.png" width="128" alt="浮词"></p>

<h1 align="center">浮词 WordFloat</h1>

<p align="center">macOS 桌面悬浮球。查词、记词、复习，为 GMAT 和雅思准备。</p>

---

## 它做什么

- **悬浮球查词。** 鼠标停在球上展开输入框，回车即查即记。本地词典毫秒出结果，大模型在后台补记忆点和例句。
- **复制即记录。** 打开后，复制任意英文单词就自动入库。
- **间隔复习。** 题型逐级加难：回忆 → 填空 → 词块填空 → 回译 / 写作 / 口语。
- **写作与口语批改。** AI 给出改正句、升级版和点评，下次练习时再拿出来。
- **错误本。** 自动标注介词、冠词、时态等语法错误，每天出复习卡。
- **每周测评。** 按雅思官方评分维度打分，记录历次成绩。
- **GMAT 熟词僻义。** 约 120 个高频词，阅读题风格出题。
- **英式英语。** RP 音标、英式拼写、英音朗读与识别。

复习页和生词本在浏览器里打开，服务只监听本机 `127.0.0.1`。

## 安装

1. 到 [Releases](../../releases/latest) 下载 `WordFloat-macOS-v*.zip`，解压，把 **浮词.app** 拖进「应用程序」。
2. 第一次打开会被系统拦下（app 未经 Apple 公证）：
   - 双击一次，看到提示后关掉。
   - 打开「系统设置 → 隐私与安全性」，往下找到浮词，点 **仍要打开**。
   - 或者在终端执行一次：
     ```bash
     xattr -dr com.apple.quarantine /Applications/浮词.app
     ```
3. 首次启动会自动安装两个 Python 组件（`pyobjc-framework-Cocoa`、`certifi`），约半分钟。

### 环境要求

- macOS 12 或更高
- Python 3（[python.org 安装包](https://www.python.org/downloads/macos/) 或 Homebrew）。没有的话，浮词会打开下载页。

## 设置 API Key

默认使用 **智谱 GLM-4.7-Flash**，可免费调用。

1. 在 [open.bigmodel.cn](https://open.bigmodel.cn/) 注册，进入「API Keys」创建一个 Key。
2. 第一次打开浮词时粘贴进去。之后可以 右键悬浮球 → 设置 API Key。

也可在 右键 → 模型服务 中换成 DeepSeek、通义千问或 Kimi。

不填 Key 也能查本地词典收录的词，只是没有记忆点、例句和批改。

## 数据

全部存在本机 `~/WordFloat/`：

| 文件 | 内容 |
|---|---|
| `words.json` | 生词和复习记录 |
| `config.json` | 设置和 API Key |
| `backups/` | 每日自动备份，保留 7 天 |
| `dict.db` | 首次启动解压的本地词典 |

右键 → 导出 CSV，可导入 Anki。

默认开启「开机自动打开」，会在 `~/Library/LaunchAgents/` 写入 `com.wordfloat.app.plist`。右键菜单里可以关掉。

## 从源码构建

```bash
git clone https://github.com/yixuuuan/wordfloat.git
cd wordfloat
./build.sh        # 生成 dist/浮词.app 和 dist/WordFloat-macOS-v*.zip
```

| 路径 | 内容 |
|---|---|
| `src/wordfloat.py` | 程序主体（PyObjC） |
| `macos/WordFloat` | app 启动器：找 Python、装组件、启动 |
| `macos/Info.plist` | app 元数据，版本号在这里改 |
| `data/wordfloat_dict.db.gz` | 本地词典（ECDICT 精简版） |
| `assets/AppIcon.icns` | 图标，由 `tools/make_icon.py` 生成 |

## 许可

MIT。本地词典来自 [ECDICT](https://github.com/skywind3000/ECDICT)（MIT）。

## 发布新版本

改 `macos/Info.plist` 里的 `CFBundleShortVersionString`，提交并推送到 main。

GitHub Actions 发现这个版本还没有 Release，会自动构建、打标签、发布。版本号不变的提交不会触发发布。
