浮词 macOS 版首个公开发布。

## 安装

1. 下载下方 `WordFloat-macOS-v*.zip`，解压，把 **浮词.app** 拖进「应用程序」。
2. 第一次打开会被系统拦下（未经 Apple 公证）。双击一次后，到「系统设置 → 隐私与安全性」点 **仍要打开**。或在终端执行：
   ```bash
   xattr -dr com.apple.quarantine /Applications/浮词.app
   ```
3. 首次启动自动安装 Python 组件，约半分钟。需要 macOS 12+ 和 Python 3。

## 包含

- 悬浮球查词，本地 ECDICT 词典
- 间隔复习，题型逐级加难
- 雅思写作 / 口语 AI 批改，错误本，每周测评
- GMAT 熟词僻义
- 默认模型：智谱 GLM-4.7-Flash（免费）

详见 [README](https://github.com/yixuuuan/wordfloat#readme)。
