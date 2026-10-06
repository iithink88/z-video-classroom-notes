# z-video-classroom-notes

给一段课，还一份能直接复习的**课堂笔记网页**。

丢进去一个视频网址、本地视频或一段录音，自动完成：抽音轨转写 → 抽关键帧 → AI 逐段整理笔记 →
全局合成 → 输出一个单页 HTML。打开就能用，点时间戳能跳回原片对应画面。

> 内容规范取自 [wendao-ai/jlearning](https://github.com/wendao-ai/jlearning) 的 `lecture-notes` 心法，
> 工程管线思路取自 [tjxj/z-skills](https://github.com/tjxj/z-skills) 的
> `z-video-study-webpage-qwen` / `z-audio-study-webpage-qwen`。

---

## 你需要先准备三样东西

| 东西 | 怎么弄 | 检查命令 |
|---|---|---|
| **Python 3.11 以上** | python.org 下载，安装时勾上 **Add Python to PATH** | `python --version` |
| **ffmpeg** | Windows：ffmpeg.org 下载解压，把 `bin` 目录加进 PATH | `ffmpeg -version` |
| **通义千问 API Key** | 阿里云百炼控制台创建，形如 `sk-xxxxxx` | 拿到一串 `sk-` 开头的字符串即可 |

> API Key 是唯一要花钱的东西。分析一个 5 分钟视频大约 5 次调用；45 分钟的课程大约 15 次调用。
> 转写走本机离线模型，**不花钱**。

---

## 三步上手

### 第 1 步：把文件夹放到技能目录

复制整个 `z-video-classroom-notes` 文件夹到你的技能目录：

| 你用的工具 | 放到哪 |
|---|---|
| WorkBuddy | `C:\Users\你的用户名\.workbuddy\skills\` |
| Claude Code | `~/.claude/skills\` 或项目的 `.claude/skills\` |
| Codex | `~/.agent/skills\` 或项目的 `.agent/skills\` |

放好之后，**新开一个会话**，直接对 AI 说「把这个视频转成课堂笔记网页」即可，它会自动匹配这个技能。
想手动跑就继续看第 2、3 步。

## 🖥️ 图形界面（GUI，不想敲命令就用它）

`gui/` 目录里是一个**暖色调、人性化**的本地网页界面，把上面的命令行完全包了起来：

- 点「⚙️ 设置」填 Key，**默认就是通义千问（qwen3.7-plus）**，也能换成任意兼容 OpenAI 协议的模型 / Base URL；
- 拖拽视频或音频、或粘贴 B站 / YouTube / 直链网址，点「生成课堂笔记」，实时看进度；
- 完成后直接在页面里预览笔记网页，点 ▶ 时间戳 / 画面可跳回原片，Ctrl+P 存 PDF。

启动方式：

| 系统 | 操作 |
|---|---|
| Windows | 双击 `gui/启动.bat` |
| macOS / Linux | 终端运行 `bash gui/启动.sh` |

首次使用请先看 `gui/GUI说明.md`（含 ffmpeg 与 Python 依赖安装）。密钥只存在你本机 `gui/gui_settings.json`，**不会上传、不会写进笔记网页**。

---

### 第 2 步：跑一次环境准备（只需一次）

```bash
python "<技能目录>/z-video-classroom-notes/scripts/setup.py"
```

它会自动装好依赖（走清华镜像）并下载中文转写模型（约 42MB）。想先看看缺什么：

```bash
python "<技能目录>/z-video-classroom-notes/scripts/setup.py" --doctor
```

### 第 3 步：做笔记

```bash
# Windows PowerShell（Key 只在当前窗口有效，不会写进任何文件）
$env:DASHSCOPE_API_KEY = "你的sk-开头的Key"

python "<技能目录>/z-video-classroom-notes/scripts/lecture_notes.py" `
  "https://www.bilibili.com/video/BVxxxxxx" `
  --title "八年级物理·第3课 浮力" `
  --subject "初中物理" --audience "八年级学生"
```

macOS / Linux 把第一行换成 `export DASHSCOPE_API_KEY="你的Key"`，续行符把 `` ` `` 换成 `\`。

跑完打开输出目录里的 **`classroom-notes.html`** 就完事了。

---

## 零成本先试试（不花钱、不调 API）

```bash
python scripts/lecture_notes.py "课程.mp4" --mock-notes --skip-transcribe
```

会用模板内容渲染一份完整网页，用来确认版式和 ffmpeg 没问题。

---

## 网页长什么样

九块固定结构，米黄纸张风、亮色主题、手机上自动变单列：

| # | 区块 | 说明 |
|---|------|------|
| 壹 | 知识框架 | 纯 CSS 思维导图，**不依赖任何 CDN，断网也渲染** |
| 贰 | 讲课时间线 | 每节起止时间，点 ▶ 跳播 |
| 叁 | 逐节详细笔记 | 概念定义 → 原理推导 → 公式与代码 → 例子 → 要点 → 回看锚点 |
| 肆 | 重点 · 易错 · 考点 | 三色分栏 |
| 伍 | 自测题 | 点击展开答案，带提示 |
| 陆 | 作业与延伸 | 作业 / 推荐阅读 / 课后思考 |
| 柒 | 术语表 | 卡片网格 |
| 捌 | 待核验与补充说明 | AI 补充项单独列出，不与正文混淆 |
| 玖 | 全文转写 | 折叠面板，每句时间戳可点击回听 |

交互：左侧目录滚动高亮 · 章节关键词实时过滤 · 所有时间戳/关键帧点击跳播 · 一键打印存 PDF
（打印时自动隐藏播放器与目录，直接变成 A4 纸质笔记）。

---

## 四种输入都能吃

```bash
# 1) 视频网址（B站 / YouTube / 视频号 / m3u8 / 直链）
python scripts/lecture_notes.py "https://www.bilibili.com/video/BVxxxxxx" --title "课程名"

# 2) 本地视频
python scripts/lecture_notes.py "C:/Users/me/Desktop/第3课.mp4" --subject "初中物理"

# 3) 本地录音 / 播客
python scripts/lecture_notes.py "C:/Users/me/Desktop/课堂录音.m4a" --title "孔子与老子"

# 4) 已有字幕或转写文本（最准、最快、最省）
python scripts/lecture_notes.py "课程.mp4" --transcript "课程.txt"
```

---

## 常用参数

| 参数 | 默认 | 说明 |
|-----|-----|------|
| `--subject` | 空 | 学科背景，**填了能显著提升术语纠错和公式质量** |
| `--audience` | 空 | 目标读者（如「初三学生」），决定讲解粒度 |
| `--extra` | 空 | 追加要求，如「每节配一道计算题」 |
| `--transcript` | 空 | 已有字幕/转写，跳过本地转写 |
| `--asr` | `auto` | `auto`(Vosk→Whisper→云端) / `vosk` / `whisper` / `dashscope` / `none` |
| `--max-segments` | 14 | 最多分几节 |
| `--min-segment-seconds` | 60 | 每节最短秒数 |
| `--frames-per-segment` | 2 | 每节抽几张关键帧 |
| `--no-frames` | 关 | 不抽帧，纯转写分析，更快 |
| `--single-file` | 关 | 额外产出内联资源的单文件 HTML，方便发给学生 |
| `--from-analysis` | 空 | 用已有 `notes-analysis.json` 重渲染，**改完内容不用再调 API** |
| `--no-math` / `--no-mermaid` | 关 | 断网环境可关，公式退化为 LaTeX 源码 |

---

## 常见报错对照表

| 报错 / 现象 | 原因 | 怎么办 |
|---|---|---|
| `Access denied ... overdue-payment` | **百炼账号欠费**（账户级，换模型也没用） | 去阿里云百炼充值。注意：欠费时脚本会重试 3 次后**静默产出一份空壳网页**，务必看 `run-report.json` 的 `warnings` |
| `ffmpeg-not-found` | ffmpeg 没装或没进 PATH | 装 ffmpeg 并把 `bin` 加进 PATH；或放到 `~/bin/ffmpeg/*/bin/` |
| `pip install` 长时间卡住无输出 | 直连 PyPI 慢/被墙 | `setup.py` 已走清华镜像；手动装也请加 `-i https://pypi.tuna.tsinghua.edu.cn/simple` |
| 网页生成了但内容是空的 | 多半是上面那个欠费 | 看 `run-report.json` |
| `media-not-found` | 路径写错了 | Windows 用 `C:/Users/...` 斜杠形式，别用 `/c/Users/...`（会被解析成 `c:\c\Users\...`） |
| 公式显示为 `$...$` 源码 | 断网，KaTeX CDN 没加载 | 联网打开；或接受源码形式，内容不丢 |
| 转写里的专有名词全错 | Vosk 中文小模型的固有缺陷 | 正常现象，AI 会结合画面字幕校正；注意核对「待核验」一栏 |
| 跑 45 分钟长视频被中断 | 后台任务有墙钟上限 | **前台串行跑**，配 10 分钟超时，不要并行（会触发接口限流） |

---

## 内容质量保证（AI 侧的硬约束）

- **噪声过滤**：去口头禅、重复、跑题、课堂管理话术
- **逻辑重构**：老师讲得跳跃时按学科逻辑重排，例子紧邻理论点
- **术语纠错**：希腊字母 / 专有名词 / 化学式识别错误按学科背景纠正，不确定标 `[原词? → 推测词]`
- **严禁编造**：教师、日期、作业无法确定一律 `[未提供]`，不确定写「待核验」
- **补全受控**：只在「提到术语没给定义」「推导缺步」时补全，且必须进紫色「AI 补充（老师未讲）」块
- **代码照抄**：编程/软件教程里画面出现的代码、命令、配置，原样进代码块，不许改写成自然语言

细则见 `references/note-craft.md`。

---

## 目录结构

```
z-video-classroom-notes/
├── SKILL.md                  # AI 读的：触发词、流程、规范、坑位
├── README.md                 # 你看的：这份文件
├── scripts/
│   ├── lecture_notes.py      # 主管线
│   └── setup.py              # 环境准备 / --doctor
├── references/
│   └── note-craft.md         # 笔记内容规范细则
├── tests/
│   └── test_lecture_notes.py # 31 项单元测试
└── evals/
    └── evals.json            # 评估用例
```

## 自检

```bash
python -m py_compile scripts/lecture_notes.py scripts/setup.py tests/test_lecture_notes.py
python tests/test_lecture_notes.py
```

## 输出长这样

```
notes-<标题>/
├── classroom-notes.html        # 就是这个，双击打开
├── classroom-notes-single.html # 可选：--single-file 时生成，方便分享
├── notes-analysis.json         # 结构化笔记，可手改后用 --from-analysis 重渲染
├── segments.json               # 逐段原始输出
├── transcript.txt / .json      # 带时间戳的转写
├── assets/frame-*.jpg          # 关键帧
└── run-report.json             # 步骤 / 警告 / 校验问题（出问题先看它）
```
