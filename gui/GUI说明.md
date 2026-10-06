# 课堂笔记智能体 · 图形界面（GUI）

把一段**视频或音频**变成一份能直接复习的**课堂笔记网页**。
本图形界面封装了 `z-video-classroom-notes` 命令行工具，无需记命令，点点鼠标即可用。

---

## 一、第一次怎么用

1. **装好运行环境**（只需一次）
   - 安装 [Python 3.11+](https://www.python.org/)（Windows 安装时务必勾选 “Add Python to PATH”）。
   - 安装 [ffmpeg](https://ffmpeg.org/) 并放到以下任一位置，脚本会自动找到：
     - `C:/ffmpeg/bin/ffmpeg.exe`（Windows）
     - `~/bin/ffmpeg/<任意名>/bin/ffmpeg`（macOS / Linux，即用户目录下的 `bin/ffmpeg`）
     - 或直接加入系统 PATH
   - 在本目录打开终端，安装 Python 依赖：
     ```bash
     python scripts/setup.py        # 自动用清华镜像装 requests / vosk / soundfile，并预热中文模型
     ```

2. **启动图形界面**
   - Windows：双击 `gui/启动.bat`
   - macOS / Linux：终端里运行 `bash gui/启动.sh`
   - 会自动打开浏览器，地址一般是 `http://127.0.0.1:8765/`

3. **填设置**：点右上角「⚙️ 设置」
   - 默认就是**通义千问（qwen3.7-plus）**。
   - 填入你的 **DashScope API Key**（形如 `sk-...`）。
   - 想用别的模型？把“模型名称”和“Base URL”改成任意兼容 OpenAI 协议的即可（例如你自己的私有部署）。
   - 点「保存设置」。

4. **生成笔记**：
   - 拖拽一个视频/音频到框里，或粘贴 B站 / YouTube / 直链网址；
   - （可选）展开“高级选项”填学科、读者、补充要求；
   - 点「✨ 生成课堂笔记」，等进度跑完即可在页面内预览。

> 密钥只保存在你本机的 `gui/gui_settings.json`，不会上传，也不会写进笔记网页。

---

## 二、界面说明

| 区域 | 作用 |
|------|------|
| ⚙️ 设置 | 配置 API Key、模型、Base URL（默认千问）、默认学科/读者、是否生成单文件、是否自动打开 |
| ① 放入素材 | 拖拽或选择本地视频/音频，或粘贴视频网址；可填学科 / 读者 / 补充要求 |
| ② 生成进度 | 实时滚动显示管线每一步（抽音轨 → 转写 → 抽帧 → 逐段笔记 → 全局合成 → 渲染） |
| ③ 笔记预览 | 生成后内嵌显示笔记网页，可点 ▶ 时间戳 / 画面跳回原片；支持 Ctrl+P 存 PDF |

---

## 三、常见问题

- **没有 ffmpeg**：页面进度会报错，按上面把 ffmpeg 放好即可。
- **显示“未配置 API Key”**：点设置填入 DashScope Key；或勾“试跑”先验证版式（不调模型、不烧 token）。
- **长视频很慢**：3 分钟约 4 次模型调用，45 分钟课约 7–10 分钟，请耐心等待，期间别关窗口。
- **笔记内容空壳 / 有“运行提示”红字**：多为账号欠费导致模型静默失败。先去 DashScope 充值，再重跑；或看页面提示里的 `run-report.json`。
- **想分享给学生**：设置里保持“生成单文件 HTML”开启，产物是 `classroom-notes-single.html`，自带媒体与样式，发文件即可。

---

## 四、文件结构

```
z-video-classroom-notes/
├── gui/
│   ├── gui.py          # 图形界面启动器（Python 标准库，零额外依赖）
│   ├── index.html      # 暖色调界面
│   ├── 启动.bat        # Windows 双击启动
│   ├── 启动.sh         # macOS / Linux 启动
│   ├── GUI说明.md      # 本文件
│   └── output/         # 生成的笔记（每次一个子目录）
├── scripts/lecture_notes.py   # 核心管线
├── references/、tests/、README.md、SKILL.md
```
