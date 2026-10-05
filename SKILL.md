---
name: z-video-classroom-notes
description: |
  当用户要求「视频转课堂笔记」「把视频做成课堂笔记网页」「课程视频学习笔记」「讲座/网课笔记网页」
  「录音转课堂笔记」「音频转笔记网页」「上传视频或音频帮我整理笔记」「把这个 B 站/YouTube 课程链接
  变成可复习的笔记」时必须使用。本 skill 输入视频网址、本地视频或本地音频，自动完成抽音轨转写、
  抽关键帧、Qwen 逐段笔记分析、全局合成，输出一份「课堂笔记式」单页 HTML：知识框架思维导图、
  逐节笔记（定义/原理/例子/公式/要点）、重点·易错·考点、自测题、作业延伸、术语表、
  可点击回看的时间戳与关键帧、全文转写。

  内容规范取自 JLearning 的 lecture-notes 心法（噪声过滤、逻辑重构、术语纠错、严禁编造、
  AI 补全必须标注），工程管线取自 z-skills 的 z-video-study-webpage-qwen / z-audio-study-webpage-qwen。
---

# 视频 / 音频 → 课堂笔记式学习网页

一句话定位：**给一段课，还一份能直接复习的笔记网页。**

与相邻 skill 的分工：

| Skill | 输入 | 产出侧重 |
|------|------|---------|
| `z-video-study-webpage-qwen` | 视频 | 观点型「学习总结」（总览/知识点/风险矩阵/行动清单） |
| `z-audio-study-webpage-qwen` | 音频 | 同上，绑定时间戳 |
| **`z-video-classroom-notes`（本 skill）** | 视频网址 / 视频 / 音频 | **课堂笔记**：知识框架 + 逐节讲义 + 考点 + 自测题 + 作业 |

---

## 适用场景

- 给一个 B 站 / YouTube / 视频号 / m3u8 / 直链课程地址，要一份可复习的笔记网页。
- 给一个本地录播课、会议录像、网课 MP4。
- 给一段课堂录音、讲座音频、播客（mp3/wav/m4a）。
- 已有字幕/转录文本，只想让 AI 整理成笔记（`--transcript`）。
- 老师要把自己的授课视频变成给学生发的复习资料（`--audience` 指定学段）。

## 安全约定

- API Key **只**从环境变量 `DASHSCOPE_API_KEY` 读取，禁止写入 SKILL.md、脚本、HTML、日志、最终回复。
- 默认 base URL：`https://dashscope.aliyuncs.com/compatible-mode/v1`，默认模型：`qwen3.7-plus`。
  模型不存在时用 `--model` 换成实际可用模型。
- 脚本产出前会跑 `assert_no_secret()` 扫描所有输出文件，命中 `sk-...` 会写进 `run-report.json` 的 issues。

## 输入输出

输入（四选一）：

1. 视频网址 → 优先调用已安装的 `z-video-downloader`，回退 `yt-dlp`
2. 本地视频：`mp4/mkv/webm/mov/m4v/avi/flv/ts/wmv`
3. 本地音频：`mp3/wav/m4a/aac/ogg/opus/flac/wma`
4. 已有转录文本 / JSON：`--transcript`（最稳，零 ASR 依赖）

输出：

```text
notes-<标题>/
├── classroom-notes.html        # 最终笔记网页（打开即用）
├── classroom-notes-single.html # 可选：--single-file，内联资源便于分享
├── notes-analysis.json         # 合成后的结构化笔记（框架/考点/自测/术语）
├── segments.json               # 逐段模型原始输出
├── transcript.txt / .json      # 转录（带时间戳）
├── assets/
│   ├── frame-001.jpg …         # 关键帧
│   └── audio-asr.wav           # 16k 单声道，给 ASR 用
└── run-report.json             # 步骤 / 警告 / 校验问题
```

## 环境依赖

- **Python 3.11+**，需 `requests`
- **ffmpeg / ffprobe**：脚本自动探测 PATH、`~/bin/ffmpeg/*/bin/`、`C:/ffmpeg/bin/`；缺失直接报错
- **ASR 后端**（四选一，见下）；首次跑 `scripts/setup.py` 可一键装好
- **DASHSCOPE_API_KEY**

## 一键准备环境

```bash
python "$HOME/.workbuddy/skills/z-video-classroom-notes/scripts/setup.py"
```

用清华镜像装 `requests / vosk / soundfile`，并预热中文小模型 `vosk-model-small-cn-0.22`（~42MB）到
`~/.cache/vosk-models/`。加 `--doctor` 只检查不安装。

## 推荐命令

### 最常用：给网址，直接出笔记

```bash
export DASHSCOPE_API_KEY="你的key，不要写进文件"

python "$HOME/.workbuddy/skills/z-video-classroom-notes/scripts/lecture_notes.py" \
  "https://www.bilibili.com/video/BVxxxxxx" \
  --title "微分几何第32讲" \
  --subject "数学" --audience "大二学生"
```

### 本地视频 / 本地音频

```bash
python "$HOME/.workbuddy/skills/z-video-classroom-notes/scripts/lecture_notes.py" \
  "C:/Users/me/Desktop/第3课.mp4" --subject "初中物理" --audience "八年级学生"
```

```bash
python "$HOME/.workbuddy/skills/z-video-classroom-notes/scripts/lecture_notes.py" \
  "C:/Users/me/Desktop/课堂录音.m4a" --title "孔子与老子"
```

### 已有字幕/转录（最快、最准）

```bash
python "$HOME/.workbuddy/skills/z-video-classroom-notes/scripts/lecture_notes.py" \
  "课程.mp4" --transcript "课程.txt"
```

### 只验版式，不调模型、不转写（零成本自检）

```bash
python "$HOME/.workbuddy/skills/z-video-classroom-notes/scripts/lecture_notes.py" \
  "课程.mp4" --mock-notes --skip-transcribe
```

### 改完内容重新出网页（不重复烧 token）

手改 `notes-analysis.json` 后，用同一输出目录重渲染：

```bash
python "$HOME/.workbuddy/skills/z-video-classroom-notes/scripts/lecture_notes.py" \
  "课程.mp4" --from-analysis "notes-<标题>/notes-analysis.json" \
  --out-dir "notes-<标题>"
```

关键帧从目录里的 `storyboard.json` 重建，媒体从原路径引用，**全程不调模型、不转写、不抽帧**。

### 分享给学生：单文件 HTML

```bash
python ... "课程.mp4" --single-file --max-inline-mb 40
```

## 关键参数

| 参数 | 默认 | 说明 |
|-----|-----|------|
| `--title` | 文件名 | 课程标题 |
| `--subject` | 空 | 学科背景，显著提升术语纠错与公式排版质量 |
| `--audience` | 空 | 目标读者（如「初三学生」），决定讲解粒度 |
| `--extra` | 空 | 追加个性化要求，如「每节都要配一道计算题」 |
| `--asr` | `auto` | `auto`(Vosk→Whisper→DashScope) / `vosk` / `whisper` / `dashscope` / `none` |
| `--max-segments` | 14 | 最多分几节 |
| `--min-segment-seconds` | 60 | 每节最短秒数，避免短视频被切碎 |
| `--frames-per-segment` | 2 | 每节抽几张关键帧（1–4） |
| `--no-frames` | 关 | 不抽帧，纯转写分析，更快 |
| `--no-math` / `--no-mermaid` | 关 | 不加载 KaTeX / Mermaid（离线环境可关） |
| `--single-file` | 关 | 额外产出内联资源的单文件 HTML |
| `--model` | `qwen3.7-plus` | 分析模型 |

## 执行流程

1. **取媒体**：网址 → `z-video-downloader`（回退 yt-dlp）；本地路径直接校验；后缀未知时用 ffprobe 判断有无画面。
2. **抽音轨 + 转写**：ffmpeg 转 16k 单声道 wav → Vosk（本机首选，纯 C++ Kaldi，不依赖 torch）→ Whisper → DashScope 兜底。
3. **抽关键帧**：每段均匀取帧，`-ss` 必须放在 `-i` **之前**（关键帧跳转）。
4. **切段**：按时长切，每段 ≥ `--min-segment-seconds`，最多 `--max-segments` 段。
5. **逐段笔记**：每段发送「该段转写 + 该段画面 + 学科/读者背景」，要求模型输出
   概念 / 原理 / 例子 / 公式 / 要点 / 考点 / 自测题 / 回看锚点 / 补充 / 待核验。
6. **全局合成**：再调一次模型，产出课程信息、一句话总览、知识框架、章节最终标题、
   重点·易错·考点、自测题、作业、术语表、待核验。章节正文仍取自分段结果，**不重写**，避免二次失真。
7. **渲染 HTML**：课堂笔记版式（见下）。
8. **收尾校验**：本地资源存在性、`notes-analysis.json` 可解析、无密钥泄漏，结果写进 `run-report.json`。

## 笔记内容规范（模型侧硬约束）

完整细则见 `references/note-craft.md`，摘要如下：

- **噪声过滤**：去口头禅、重复、跑题、课堂管理话术；保留学生提问中的关键概念与老师强调的易错点。
- **逻辑重构**：老师讲得跳跃时按学科内在逻辑重排，例子紧邻对应理论点。
- **术语纠错**：转录中希腊字母、专有名词、化学式识别错误按学科背景纠正；不确定标 `[原词? → 推测词]`。
- **严禁编造**：教师、日期、作业无法确定一律写 `[未提供]`；不确定写「待核验」。
- **补全受控**：只在「提到术语没给定义」「推导缺步导致断裂」时补全，且必须进 `supplements`
  并在网页上以「AI 补充（老师未讲）」紫色块单独显示，绝不混进正文。
- **语言风格**：书面语、无演讲腔，保留老师的关键比喻与口诀。

## 网页版式规范（渲染侧）

页面共九块，固定顺序：

| # | 区块 | 要点 |
|---|-----|------|
| 壹 | 知识框架 | 纯 CSS 思维导图，不依赖 Mermaid，离线也渲染 |
| 贰 | 讲课时间线 | 每节起止时间，点 ▶ 跳播放器 |
| 叁 | 逐节详细笔记 | 概念定义 → 原理推导 → 公式与代码 → 例子 → 要点 → 回看锚点 |
| 肆 | 重点·易错·考点 | 三色分栏（琥珀/朱红/青绿） |
| 伍 | 自测题 | `<details>` 点击展开答案，带提示 |
| 陆 | 作业与延伸 | 作业 / 推荐阅读 / 课后思考 |
| 柒 | 术语表 | 卡片网格 |
| 捌 | 待核验与补充说明 | 明确列出 AI 补充与录音不清处 |
| 玖 | 全文转写 | 折叠面板，每句时间戳可点击回听 |

交互：**左侧目录滚动高亮**、**章节关键词实时过滤**、**所有时间戳/关键帧点击跳播**、
**打印/存 PDF**（`@media print` 自动隐藏播放器与目录，转 A4 纸质笔记）。

### 内容书写约定（模型与手写 JSON 通用）

- 正文支持极简内联格式：`**粗体**` 强调关键结论，反引号 `` `代码或参数名` ``。
  渲染时**先转义再插入标签**，所以模型给出的 `<script>` 之类文本不会被当标签执行。
- `formulas` 字段同时承载两类内容，二者**二选一**：
  - `latex` → 走 KaTeX 行间公式渲染（数理化）
  - `code` → 走等宽代码块（**编程/工具类教程必须用它**，画面的命令、配置、代码片段照抄进来，
    不要改写成自然语言）
- 因此本 skill 同样适用于编程课与软件教程，不只是数理化。

视觉：米黄纸张底 + 白色卡片 + 墨蓝主色 + 朱红重点，亮色主题。移动端单列、目录自动隐藏。

## 已知限制与坑位（已实测）

- **Windows 路径**：传给脚本的 `--out-dir` / 媒体路径用 `C:/Users/...` 形式。写成 MSYS 的 `/c/Users/...`
  会被 Windows 版 Python 误解析成 `c:\c\Users\...`。
- **长视频请串行、前台、长超时**：一个 3 分钟视频 = 3 段 + 1 次合成 = 4 次模型调用；
  45 分钟课约 15 次调用，实测每次 ~30 秒，总耗时 7–10 分钟。配置 `timeout 600000`（10 分钟），
  不要后台跑（后台任务有墙钟上限会被静默杀掉，无报错、无 HTML），也不要并行（触发接口限流）。
- **抽帧必须快进**：`-ss` 在 `-i` 之前。放错位置会逐帧解码，10 分钟视频抽 12 帧约 2 分钟，
  正确写法约 0.5 秒/帧。
- **本机 Whisper 基本不可用**：`faster-whisper` 加载即 ctranslate2 段错误（EXIT 139），
  `openai-whisper` 的 torch DLL 被第三方安全软件拦截（`WinError 1114`）。**本机本地 ASR 走 Vosk**。
- **DashScope 云端 ASR 需「文件转写」服务权限**，纯对话 key 会 `AccessDenied`；
  `/compatible-mode/v1/audio/transcriptions` 未实现，不要走兼容模式转写。
- **pip 必须走清华镜像**：本机直连 PyPI 会长时间卡死无输出。
  `-i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn`
- **沙箱**：WorkBuddy 沙箱会拦截 dashscope 网络与部分目录写入，运行需 `dangerouslyDisableSandbox: true`。
  以 `import` 方式调用脚本时沙箱可能拦截往技能目录写 `__pycache__`，可先复制到可写目录再跑。
- **KaTeX / Mermaid 走 CDN**：离线环境用 `--no-math --no-mermaid`；此时公式以 LaTeX 源码等宽显示、
  Mermaid 以代码块显示，内容不丢。知识框架是纯 CSS，**不依赖任何 CDN**。
- **单文件模式**：默认只内联图片；媒体超过 `--max-inline-mb`（默认 25MB）不内联，
  页面会提示「请把 HTML 与媒体放在同一目录」。
- **Vosk 小模型**会在中文词间插空格（「欢迎 关注 公众 号」）并偶有错字，脚本已做词间空格压缩，
  Qwen 分析时通常能结合上下文纠正。但它对**专有名词几乎必错**（实测把 Three.js 识别成「随着而死」、
  Blender 识别成「不然的 / 懒得」、Remotion 识别成「某省」）。**务必让模型结合关键帧画面字幕交叉校正，
  拿不准的写进 uncertain，不要静默替换**。
- **小模型常不输出标点**，会让整段音频只切成 1 段，导致每段的转写窗口都返回全文、笔记内容整体错位。
  脚本已加「少于 2 段就按 20 秒时间窗重切」的兜底。
- **DashScope 账号欠费会静默降级**：接口返回 `Arrearage`，脚本每段都重试 3 次后写入空内容，
  最终仍会生成一份「空壳网页」。**跑完必须看 `run-report.json` 的 `warnings`，不要只看有没有 HTML。**
  欠费时可改用 `--from-analysis`：由 Agent 自己读转写文本 + 关键帧画面写出 `notes-analysis.json`，
  再交给脚本渲染，同样能出完整网页。

## 质量标准（交付前自查）

- [ ] 笔记不是「照着标题泛泛总结」，每节都有具体概念/原理/例子
- [ ] 每个回看锚点的时间戳落在该节时间范围内，点击能跳到对应画面
- [ ] 公式已用 LaTeX 渲染；结构图已生成
- [ ] 重点·易错·考点来自老师强调过的内容，非空泛套话
- [ ] AI 补充项都在紫色「AI 补充」块里，没混进正文
- [ ] 无法确定的是 `[未提供]` / 「待核验」，没有编造教师、日期、作业
- [ ] `run-report.json` 的 `issues` 为空

## 测试

```bash
python -m py_compile \
  "$HOME/.workbuddy/skills/z-video-classroom-notes/scripts/lecture_notes.py" \
  "$HOME/.workbuddy/skills/z-video-classroom-notes/tests/test_lecture_notes.py"

python "$HOME/.workbuddy/skills/z-video-classroom-notes/tests/test_lecture_notes.py"
```
