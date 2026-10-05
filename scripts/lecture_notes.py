#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""z-video-classroom-notes —— 视频 / 音频 → 课堂笔记式学习网页。

输入（四选一）：
  1. 视频网址（YouTube / B 站 / 视频号 / m3u8 / 直链）
  2. 本地视频文件（mp4 / mkv / webm / mov ...）
  3. 本地音频文件（mp3 / wav / m4a / flac ...）
  4. 已有转录文本（--transcript，配合上述任一媒体）

管线：
  取媒体 → 抽音频 → 转写（Vosk/Whisper/DashScope/现成文本）
        → 抽关键帧（仅视频）→ 按时长切段
        → Qwen 逐段生成「课堂笔记片段」
        → Qwen 全局合成（课程信息 / 知识框架 / 重点考点 / 自测题 / 作业）
        → 渲染单页 HTML 课堂笔记
        → 收尾校验（本地资源存在 / 无密钥泄漏）

输出：
  notes-<slug>/classroom-notes.html   最终笔记网页
  notes-<slug>/notes-analysis.json    合成后的结构化笔记
  notes-<slug>/segments.json          逐段原始输出
  notes-<slug>/transcript.txt|json    转录文本
  notes-<slug>/assets/frame-*.jpg     关键帧
  notes-<slug>/run-report.json        运行报告
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

# Windows 控制台默认 GBK，中文 print 会崩；入口强制 UTF-8（跨项目铁律）
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

warnings.filterwarnings("ignore", message=r"urllib3 .* doesn't match a supported version!")

try:
    import requests
except Exception:  # noqa: BLE001
    requests = None  # type: ignore[assignment]

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen3.7-plus"
DEFAULT_API_KEY_ENV = "DASHSCOPE_API_KEY"

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".ts", ".wmv", ".mpg", ".mpeg"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wma", ".aiff"}

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{12,}"),
    re.compile(r"Das[Aa]h[Ss]cope\s+[A-Za-z0-9._-]{12,}"),
]

# ---------------------------------------------------------------- ffmpeg 探测
if os.name == "nt":
    _which = shutil.which("ffmpeg")
    if _which:
        _ff = Path(_which)
    else:
        _ff = next(
            (
                p
                for p in [
                    Path.home() / "bin" / "ffmpeg" / "ffmpeg-8.1.2-essentials_build" / "bin" / "ffmpeg.exe",
                    Path.home() / "bin" / "ffmpeg" / "ffmpeg-7.1-essentials_build" / "bin" / "ffmpeg.exe",
                    Path("C:/ffmpeg/bin/ffmpeg.exe"),
                    Path("C:/Program Files/ffmpeg/bin/ffmpeg.exe"),
                ]
                if p.exists()
            ),
            None,
        )
    DEFAULT_FFMPEG = _ff or Path("ffmpeg")
    DEFAULT_FFPROBE = (_ff.parent / "ffprobe.exe") if _ff else Path("ffprobe")
else:
    DEFAULT_FFMPEG = Path("/opt/homebrew/bin/ffmpeg")
    DEFAULT_FFPROBE = Path("/opt/homebrew/bin/ffprobe")


@dataclass
class Frame:
    frame_id: str
    timestamp: float
    time_label: str
    path: Path


@dataclass
class RunLog:
    steps: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def step(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append({"step": name, "ok": ok, "detail": detail})
        print(f"[{'ok' if ok else 'warn'}] {name}" + (f" — {detail}" if detail else ""), file=sys.stderr)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        print(f"[warn] {msg}", file=sys.stderr)


# ---------------------------------------------------------------- 基础工具
def scrub_secret(text: str) -> str:
    value = text or ""
    for pattern in SECRET_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value


def ensure_tool(path: Path, fallback_name: str) -> Path:
    if path.exists() and os.access(str(path), os.X_OK):
        return path
    found = shutil.which(fallback_name)
    if found:
        return Path(found)
    raise RuntimeError(f"{fallback_name}-not-found")


def run_command(cmd: list[str], *, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def seconds_to_label(seconds: float) -> str:
    seconds = max(0, int(round(seconds or 0)))
    hour = seconds // 3600
    minute = (seconds % 3600) // 60
    second = seconds % 60
    if hour:
        return f"{hour:02d}:{minute:02d}:{second:02d}"
    return f"{minute:02d}:{second:02d}"


def label_to_seconds(label: str) -> float:
    parts = [p for p in re.split(r"[:：]", str(label or "")) if p.strip().isdigit()]
    if not parts:
        return 0.0
    value = 0.0
    for part in parts:
        value = value * 60 + int(part)
    return value


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def para(value: Any) -> str:
    return esc(value).replace("\n", "<br>")


# 极简内联格式：只支持 **粗体** 与 `行内代码`，够用且不会引入 XSS
_INLINE_STRONG = re.compile(r"\*\*([^*\n]+)\*\*")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")


def rich(value: Any) -> str:
    """先转义、再套一层极简 markdown 内联格式。

    顺序不能反：必须先把文本转义成安全 HTML，之后才插入 strong/code 标签，
    否则模型给出 `<script>` 之类的文本会被当成真实标签执行。
    """
    text = para(value)
    text = _INLINE_STRONG.sub(r"<strong>\1</strong>", text)
    text = _INLINE_CODE.sub(r"<code>\1</code>", text)
    return text


def slugify(text: str, fallback: str = "notes") -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", str(text or "").strip())
    cleaned = cleaned.strip("-")
    return cleaned[:48] or fallback


# ---------------------------------------------------------------- 媒体输入
def is_url(value: str) -> bool:
    return bool(re.match(r"^https?://", str(value or "").strip(), re.I))


def find_largest_media(root: Path) -> Path | None:
    best: Path | None = None
    best_size = -1
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in VIDEO_EXTS | AUDIO_EXTS:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > best_size:
            best, best_size = path, size
    return best


def download_media(url: str, out_root: Path, title: str, log: RunLog) -> Path:
    """优先复用 z-video-downloader，回退 yt-dlp。"""
    out_root.mkdir(parents=True, exist_ok=True)
    skill_script = Path.home() / ".workbuddy" / "skills" / "z-video-downloader" / "scripts" / "download_video.py"
    if skill_script.exists():
        cmd = [sys.executable, str(skill_script), "--out-root", str(out_root), "--title", title, url]
        proc = run_command(cmd, timeout=3600)
        if proc.returncode == 0:
            found = find_largest_media(out_root)
            if found:
                log.step("下载视频(z-video-downloader)", True, str(found))
                return found
            log.warn(f"下载脚本返回 0 但没找到媒体文件：{scrub_secret(proc.stdout[-300:])}")
        else:
            log.warn(f"z-video-downloader 失败：{scrub_secret((proc.stderr or proc.stdout)[-300:])}")
    if shutil.which("yt-dlp"):
        tmpl = str(out_root / "%(title).80s.%(ext)s")
        proc = run_command(["yt-dlp", "-o", tmpl, "--no-playlist", url], timeout=3600)
        if proc.returncode == 0:
            found = find_largest_media(out_root)
            if found:
                log.step("下载视频(yt-dlp)", True, str(found))
                return found
        log.warn(f"yt-dlp 失败：{scrub_secret((proc.stderr or proc.stdout)[-300:])}")
    raise RuntimeError("media-download-failed: 无法获取视频，可先手动下载后把本地路径传给 --media")


def resolve_media(source: str, out_dir: Path, title: str, log: RunLog) -> tuple[Path, str]:
    """返回 (媒体路径, 'video'|'audio')。"""
    if is_url(source):
        media = download_media(source.strip(), out_dir / "downloads", title, log)
    else:
        media = Path(source).expanduser()
        if not media.exists():
            raise RuntimeError(f"media-not-found:{media}")
    suffix = media.suffix.lower()
    if suffix in VIDEO_EXTS:
        kind = "video"
    elif suffix in AUDIO_EXTS:
        kind = "audio"
    else:
        # 后缀未知时用 ffprobe 判断有没有画面
        kind = "video" if probe_has_video(media) else "audio"
    log.step("解析输入", True, f"{media.name} ({kind})")
    return media, kind


def probe_has_video(media: Path) -> bool:
    try:
        info = ffprobe_info(media)
    except Exception:  # noqa: BLE001
        return False
    for stream in info.get("streams", []) or []:
        if stream.get("codec_type") == "video":
            return True
    return False


# ---------------------------------------------------------------- ffprobe / ffmpeg
def ffprobe_info(media: Path) -> dict[str, Any]:
    ffprobe = ensure_tool(DEFAULT_FFPROBE, "ffprobe")
    proc = run_command(
        [
            str(ffprobe),
            "-hide_banner",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size,format_name",
            "-show_entries",
            "stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            str(media),
        ],
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(scrub_secret(proc.stderr or proc.stdout))
    return json.loads(proc.stdout)


def media_duration(media: Path) -> float:
    try:
        return float(ffprobe_info(media).get("format", {}).get("duration") or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def prepare_audio(media: Path, out_dir: Path) -> Path:
    ffmpeg = ensure_tool(DEFAULT_FFMPEG, "ffmpeg")
    target = out_dir / "assets" / "audio-asr.wav"
    target.parent.mkdir(parents=True, exist_ok=True)
    proc = run_command(
        [
            str(ffmpeg),
            "-y",
            "-i",
            str(media),
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(target),
        ],
        timeout=1800,
    )
    if proc.returncode != 0 or not target.exists():
        raise RuntimeError(scrub_secret(proc.stderr or proc.stdout))
    return target


def extract_frames(
    media: Path,
    out_dir: Path,
    *,
    duration: float,
    per_segment: int,
    plan: list[tuple[float, float]],
) -> list[Frame]:
    """按段抽帧：每段均匀取 per_segment 张。"""
    ffmpeg = ensure_tool(DEFAULT_FFMPEG, "ffmpeg")
    assets = out_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    if duration <= 0:
        duration = 60.0
    frames: list[Frame] = []
    index = 0
    per_segment = max(1, min(per_segment, 4))
    for start, end in plan:
        span = max(end - start, 0.5)
        for j in range(per_segment):
            index += 1
            ts = max(0.0, min(duration - 0.3, start + span * (j + 0.5) / per_segment))
            target = assets / f"frame-{index:03d}.jpg"
            # -ss 必须放在 -i 之前：关键帧跳转，10 分钟视频从 ~10s/帧 降到 ~0.5s/帧
            proc = run_command(
                [
                    str(ffmpeg),
                    "-y",
                    "-ss",
                    f"{ts:.3f}",
                    "-i",
                    str(media),
                    "-frames:v",
                    "1",
                    "-pix_fmt",
                    "yuvj420p",
                    "-q:v",
                    "3",
                    str(target),
                ],
                timeout=180,
            )
            if proc.returncode != 0 or not target.exists():
                continue
            frames.append(Frame(f"frame-{index:03d}", ts, seconds_to_label(ts), target))
    frames.sort(key=lambda f: f.timestamp)
    (out_dir / "storyboard.json").write_text(
        json.dumps(
            {
                "frames": [
                    {
                        "frame_id": f.frame_id,
                        "timestamp": f.timestamp,
                        "time_label": f.time_label,
                        "path": str(f.path.relative_to(out_dir)),
                    }
                    for f in frames
                ]
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return frames


def load_storyboard(out_dir: Path) -> dict[str, Frame]:
    """从 storyboard.json 重建关键帧映射，供 --from-analysis 重渲染使用。"""
    path = out_dir / "storyboard.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    result: dict[str, Frame] = {}
    for item in data.get("frames", []) or []:
        target = out_dir / str(item.get("path", ""))
        if target.exists():
            result[str(item.get("frame_id"))] = Frame(
                str(item.get("frame_id")),
                float(item.get("timestamp", 0)),
                str(item.get("time_label") or seconds_to_label(float(item.get("timestamp", 0)))),
                target,
            )
    return result


# ---------------------------------------------------------------- 转写
def save_transcript(out_dir: Path, segments: list[dict[str, Any]], text: str, backend: str) -> dict[str, Any]:
    (out_dir / "transcript.txt").write_text((text or "") + "\n", encoding="utf-8")
    (out_dir / "transcript.json").write_text(
        json.dumps({"segments": segments, "text": text, "backend": backend}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"status": "ok", "segments": segments, "text": text, "backend": backend}


def load_transcript(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            return {
                "status": "ok",
                "segments": data.get("segments") or [],
                "text": data.get("text") or "",
                "backend": "provided-json",
            }
    return {"status": "ok", "segments": [], "text": text.strip(), "backend": "provided-text"}


def ensure_vosk_model(model_path: str = "") -> str:
    if model_path and Path(model_path).exists():
        return model_path
    cache_dir = Path.home() / ".cache" / "vosk-models"
    model_dir = cache_dir / "vosk-model-small-cn-0.22"
    if model_dir.exists() and any(model_dir.iterdir()):
        return str(model_dir)
    import urllib.request
    import zipfile

    url = "https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip"
    cache_dir.mkdir(parents=True, exist_ok=True)
    print(f"vosk-model: 自动下载中文小模型 {url}", file=sys.stderr)
    try:
        with urllib.request.urlopen(url, timeout=300) as resp:
            data = resp.read()
        (cache_dir / "vosk-model-small-cn-0.22.zip").write_bytes(data)
        with zipfile.ZipFile(cache_dir / "vosk-model-small-cn-0.22.zip") as zf:
            zf.extractall(cache_dir)
        if model_dir.exists():
            return str(model_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"vosk-model-download-failed:{scrub_secret(str(exc))}", file=sys.stderr)
    return ""


def transcribe_vosk(wav: Path, out_dir: Path, *, model_path: str, log: RunLog) -> dict[str, Any]:
    try:
        from vosk import Model, KaldiRecognizer, SetLogLevel  # type: ignore
        import soundfile as sf  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return {"status": "skipped", "reason": f"vosk-unavailable:{exc}", "segments": [], "text": ""}
    if not model_path or not Path(model_path).exists():
        model_path = ensure_vosk_model(model_path)
    if not model_path:
        return {"status": "skipped", "reason": "vosk-model-missing", "segments": [], "text": ""}
    try:
        SetLogLevel(-1)
        model = Model(model_path)
        data, samplerate = sf.read(str(wav), dtype="int16", always_2d=False)
        if data.ndim > 1:
            data = data[:, 0]
        rec = KaldiRecognizer(model, int(samplerate))
        rec.SetWords(True)
        rec.AcceptWaveform(data.tobytes())
        result = json.loads(rec.FinalResult())
        words = result.get("result", []) or []
        segments: list[dict[str, Any]] = []
        buf: list[str] = []
        start = float(words[0].get("start", 0)) if words else 0.0
        for word in words:
            token = str(word.get("word", ""))
            buf.append(token)
            if token in "。！？!?，,；;":
                seg_text = "".join(buf).strip()
                if seg_text:
                    segments.append(
                        {"start": round(start, 2), "end": round(float(word.get("end", 0)), 2), "text": seg_text}
                    )
                buf = []
                start = float(word.get("end", 0))
        if buf:
            seg_text = "".join(buf).strip()
            if seg_text:
                segments.append(
                    {
                        "start": round(start, 2),
                        "end": round(float(words[-1].get("end", 0)), 2),
                        "text": seg_text,
                    }
                )
        # 小模型常不输出标点，导致整段音频只切成 1 段；此时按固定时间窗兜底重切，
        # 否则 transcript_for_window 每段都返回全文，笔记内容会整体错位。
        if len(segments) < 2 and words:
            window = 20.0
            segments = []
            bucket: list[str] = []
            bucket_start = float(words[0].get("start", 0))
            last_end = bucket_start
            for word in words:
                end = float(word.get("end", 0))
                if end - bucket_start >= window and bucket:
                    seg_text = "".join(bucket).strip()
                    if seg_text:
                        segments.append({"start": round(bucket_start, 2), "end": round(last_end, 2), "text": seg_text})
                    bucket, bucket_start = [], end
                bucket.append(str(word.get("word", "")))
                last_end = end
            if bucket:
                seg_text = "".join(bucket).strip()
                if seg_text:
                    segments.append({"start": round(bucket_start, 2), "end": round(last_end, 2), "text": seg_text})
        # 中文小模型会在词间插空格，压掉以便阅读和后继分析
        text = re.sub(r"\s+", " ", str(result.get("text", ""))).strip()
        text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
        log.step("Vosk 转写", True, f"{len(segments)} 段 / {len(text)} 字")
        return save_transcript(out_dir, segments, text, "vosk")
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "reason": f"vosk-error:{scrub_secret(str(exc))}", "segments": [], "text": ""}


def transcribe_whisper(wav: Path, out_dir: Path, *, model_name: str, language: str, log: RunLog) -> dict[str, Any]:
    try:
        import whisper  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return {"status": "skipped", "reason": f"whisper-unavailable:{exc}", "segments": [], "text": ""}
    try:
        model = whisper.load_model(model_name)
        result = model.transcribe(str(wav), language=language or None)
        segments = [
            {
                "start": float(seg.get("start", 0)),
                "end": float(seg.get("end", 0)),
                "text": str(seg.get("text", "")).strip(),
            }
            for seg in result.get("segments", [])
        ]
        text = str(result.get("text", "")).strip()
        log.step("Whisper 转写", True, f"{len(segments)} 段 / {len(text)} 字")
        return save_transcript(out_dir, segments, text, "whisper")
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "reason": f"whisper-error:{scrub_secret(str(exc))}", "segments": [], "text": ""}


def transcribe_dashscope(wav: Path, api_key: str, log: RunLog) -> dict[str, Any]:
    """DashScope 文件转写（paraformer-v2）。需要该 key 开通「文件转写」服务权限。"""
    if requests is None:
        return {"status": "skipped", "reason": "requests-unavailable", "segments": [], "text": ""}
    base = "https://dashscope.aliyuncs.com/api/v1"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        with wav.open("rb") as fh:
            up = requests.post(
                f"{base}/files",
                headers=headers,
                files={"file": (wav.name, fh, "audio/wav")},
                data={"purpose": "audio-transcription"},
                timeout=300,
            )
        if up.status_code >= 400:
            return {
                "status": "failed",
                "reason": f"upload-{up.status_code}:{scrub_secret(up.text[:200])}",
                "segments": [],
                "text": "",
            }
        file_id = (up.json().get("data", {}).get("uploaded_files") or [{}])[0].get("file_id")
        if not file_id:
            return {"status": "failed", "reason": "no-file-id", "segments": [], "text": ""}
        task = requests.post(
            f"{base}/services/audio/asr/transcription",
            headers={**headers, "Content-Type": "application/json"},
            json={"model": "paraformer-v2", "input": {"file_urls": [file_id]}},
            timeout=300,
        )
        if task.status_code >= 400:
            return {
                "status": "failed",
                "reason": f"submit-{task.status_code}:{scrub_secret(task.text[:200])}",
                "segments": [],
                "text": "",
            }
        task_id = task.json().get("output", {}).get("task_id")
        if not task_id:
            return {"status": "failed", "reason": "no-task-id", "segments": [], "text": ""}
        for _ in range(120):
            poll = requests.get(f"{base}/tasks/{task_id}", headers=headers, timeout=120)
            data = poll.json().get("output", {})
            status = data.get("task_status")
            if status in ("SUCCEEDED", "FAILED"):
                break
            time.sleep(5)
        else:
            return {"status": "failed", "reason": "poll-timeout", "segments": [], "text": ""}
        if status != "SUCCEEDED":
            return {"status": "failed", "reason": f"task-{status}", "segments": [], "text": ""}
        results = data.get("results") or []
        segments = []
        for item in results:
            for sentence in item.get("transcripts", []) or []:
                for sent in sentence.get("sentences", []) or []:
                    segments.append(
                        {
                            "start": float(sent.get("begin_time", 0)) / 1000.0,
                            "end": float(sent.get("end_time", 0)) / 1000.0,
                            "text": str(sent.get("text", "")).strip(),
                        }
                    )
        text = "\n".join(seg["text"] for seg in segments)
        log.step("DashScope 转写", True, f"{len(segments)} 段 / {len(text)} 字")
        return save_transcript(Path(wav).parent.parent, segments, text, "dashscope")
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "reason": f"dashscope-error:{scrub_secret(str(exc))}", "segments": [], "text": ""}


def run_asr(
    wav: Path,
    out_dir: Path,
    *,
    backend: str,
    api_key: str,
    vosk_model: str,
    whisper_model: str,
    language: str,
    log: RunLog,
) -> dict[str, Any]:
    order = {
        "vosk": ["vosk"],
        "whisper": ["whisper"],
        "dashscope": ["dashscope"],
        "auto": ["vosk", "whisper", "dashscope"],
    }[backend]
    last: dict[str, Any] = {"status": "skipped", "reason": "no-backend", "segments": [], "text": ""}
    for name in order:
        if name == "vosk":
            last = transcribe_vosk(wav, out_dir, model_path=vosk_model, log=log)
        elif name == "whisper":
            last = transcribe_whisper(wav, out_dir, model_name=whisper_model, language=language, log=log)
        else:
            if not api_key:
                last = {"status": "skipped", "reason": "no-api-key", "segments": [], "text": ""}
            else:
                last = transcribe_dashscope(wav, api_key, log)
        if last.get("status") == "ok" and last.get("text"):
            return last
        log.warn(f"转写后端 {name} 不可用：{last.get('reason', 'unknown')}")
    return last


def transcript_for_window(
    transcript: dict[str, Any],
    start: float,
    end: float,
    total_duration: float = 0.0,
    limit: int = 5000,
) -> str:
    segments = transcript.get("segments") or []
    selected = [
        str(seg.get("text", "")).strip()
        for seg in segments
        if float(seg.get("end", 0) or 0) >= start and float(seg.get("start", 0) or 0) <= end
    ]
    joined = "\n".join(part for part in selected if part)
    if joined:
        return joined[:limit]
    text = str(transcript.get("text") or "")
    if not text:
        return ""
    # 纯文本兜底：按「段起点 / 总时长」定位，而不是「段起点 / 段终点」——
    # 后者会让第 2 段直接从文本 50% 处开始，整篇笔记内容错位。
    denom = max(total_duration or end, 1.0)
    ratio = max(0.0, min(1.0, start / denom))
    span = max((end - start) / denom, 0.05)
    cursor = int(len(text) * ratio)
    chunk = max(int(len(text) * span), 400)
    return text[cursor : cursor + chunk][:limit]


# ---------------------------------------------------------------- 分段
def build_plan(duration: float, max_segments: int, min_seconds: float) -> list[tuple[float, float]]:
    if duration <= 0:
        duration = 60.0
    count = max(1, min(int(duration // max(min_seconds, 5.0)), max(1, max_segments)))
    if count == 1:
        return [(0.0, duration)]
    step = duration / count
    return [(i * step, (i + 1) * step) for i in range(count)]


# ---------------------------------------------------------------- Qwen
def image_to_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def extract_json_block(text: str) -> Any:
    raw = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S | re.I)
    if fenced:
        raw = fenced.group(1).strip()

    def _try(value: str) -> Any | None:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None

    for candidate in (raw, re.sub(r",(\s*[}\]])", r"\1", raw)):
        result = _try(candidate)
        if result is not None:
            return result
        cleaned = re.sub(r"[\x00-\x1f]", " ", candidate)
        result = _try(cleaned)
        if result is not None:
            return result
    for candidate in (raw, re.sub(r"[\x00-\x1f]", " ", raw)):
        starts = [p for p in (candidate.find("{"), candidate.find("[")) if p >= 0]
        if not starts:
            continue
        start = min(starts)
        end = max(candidate.rfind("}"), candidate.rfind("]"))
        if end > start:
            result = _try(candidate[start : end + 1])
            if result is not None:
                return result
    raise json.JSONDecodeError("could-not-parse-json", raw, 0)


def chat_completion(
    *,
    base_url: str,
    model: str,
    api_key: str,
    messages: list[dict[str, Any]],
    temperature: float = 0.2,
    timeout: int = 240,
    retries: int = 3,
) -> str:
    if requests is None:
        raise RuntimeError("requests-not-installed: 请先 pip install requests")
    endpoint = base_url.rstrip("/") + "/chat/completions"
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = requests.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "messages": messages, "temperature": temperature},
                timeout=timeout,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"api-error-{response.status_code}:{scrub_secret(response.text[:300])}")
            return response.json()["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(str(last_error))


SECTION_SYSTEM = (
    "你是资深学科教师兼课堂速记员，负责把一段课程音视频整理成可直接复习的课堂笔记。"
    "铁律：只忠实还原老师讲过的内容；噪声过滤（口头禅、重复、课堂管理话术）要彻底；"
    "老师讲得跳跃时按学科内在逻辑重排；不确定的内容宁可标 uncertain 也不要编造；"
    "任何超出讲解范围的补充都必须放进 supplements 并说明原因。只输出 JSON。"
)

SECTION_TEMPLATE = """
请整理第 __INDEX__/__TOTAL__ 段课程内容（__START__ — __END__）。

课程主题：__TITLE__
学科背景：__SUBJECT__
目标读者：__AUDIENCE__
__EXTRA_LINE__
__FRAME_LINE__
该时间段的转写文本（可能含识别错误，请结合学科背景纠正术语）：
"""

SECTION_SCHEMA = """
严格输出如下 JSON（不要输出解释文字）：
{
  "section_title": "本节标题（动宾或名词短语，不超过 20 字）",
  "section_lead": "一句话说明本节要解决的问题",
  "start": "MM:SS",
  "end": "MM:SS",
  "terms": [{"term": "术语/概念", "detail": "定义或精确表述，公式用 LaTeX"}],
  "principles": ["原理/机制/推导逻辑，公式用 LaTeX"],
  "examples": [{"title": "例子或推导标题", "detail": "具体过程，公式用 LaTeX"}],
  "formulas": [{"name": "公式或代码块名", "latex": "数学公式填这里（LaTeX）", "code": "代码片段填这里（与 latex 二选一）", "note": "符号含义 / 代码说明"}],
  "takeaways": ["本节必须记住的要点（每条一句话）"],
  "exam_points": [{"type": "重要|易错|考点", "text": "内容"}],
  "quiz": [{"q": "自测问题", "a": "参考答案", "hint": "提示（可为空）"}],
  "diagram_mermaid": "若适合用图表达，给出 Mermaid 代码；不适合则空字符串",
  "anchors": [{"time": "MM:SS", "label": "这一刻在讲什么", "frame_id": "从可用画面帧中选一个"}],
  "supplements": [{"text": "[补充] ...", "reason": "为什么必须补"}],
  "uncertain": ["录音不清或无法确定的内容"]
}

约束：
- 老师没讲的一律不写入 terms/principles/examples/takeaways。
- 只有在「提到术语却没给定义」「推导缺步导致逻辑断裂」时才写 supplements，禁止锦上添花。
- anchors 的 time 必须落在 __START__ — __END__ 之间。
- 正文可用 `**粗体**` 强调关键结论、用反引号包裹行内代码或文件名，网页会正确渲染。
- 编程/工具类教程：画面上出现的代码、命令、配置片段一律放进 formulas 的 code 字段，不要改写成自然语言。
"""


def build_section_messages(
    *,
    title: str,
    subject: str,
    audience: str,
    extra: str,
    index: int,
    total: int,
    start: float,
    end: float,
    transcript_text: str,
    frames: list[Frame],
) -> list[dict[str, Any]]:
    frame_line = (
        "可用画面帧（frame_id: 时间点）：\n"
        + "\n".join(f"- {f.frame_id}: {f.time_label}" for f in frames)
        if frames
        else "本段没有画面帧，请只依据转写文本整理。"
    )
    replacements = {
        "__INDEX__": str(index + 1),
        "__TOTAL__": str(total),
        "__START__": seconds_to_label(start),
        "__END__": seconds_to_label(end),
        "__TITLE__": title,
        "__SUBJECT__": subject or "（未指定，请根据内容推断）",
        "__AUDIENCE__": audience or "（未指定，默认在校学生）",
        "__EXTRA_LINE__": f"补充要求：{extra}" if extra else "",
        "__FRAME_LINE__": frame_line,
    }
    body_parts = [SECTION_TEMPLATE, transcript_text or "（没有可用转写，请谨慎判断并把不确定内容写入 uncertain。）", "\n", SECTION_SCHEMA]
    body = "".join(body_parts)
    for key, value in replacements.items():
        body = body.replace(key, value)
    content: list[dict[str, Any]] = [{"type": "text", "text": body}]
    for frame in frames:
        try:
            content.append({"type": "image_url", "image_url": {"url": image_to_data_url(frame.path)}})
        except Exception:  # noqa: BLE001
            continue
    return [{"role": "system", "content": SECTION_SYSTEM}, {"role": "user", "content": content}]


GLOBAL_SYSTEM = (
    "你是课程教研组长，负责把分段笔记合成一份完整的课堂笔记。"
    "要求：框架必须能一眼看出整堂课脉络；重点考点只收老师强调过的内容；"
    "自测题要能检验真实理解，不要出记忆性填空。只输出 JSON。"
)

GLOBAL_TEMPLATE = """
课程主题：__TITLE__
总时长：__DURATION__
学科背景：__SUBJECT__
目标读者：__AUDIENCE__
__EXTRA_LINE__
以下是按时间顺序整理的分段笔记 JSON：
__SECTIONS__

请输出 JSON：
{
  "course": {"name": "课程名", "topic": "本节主题", "teacher": "教师名或[未提供]", "date": "YYYY-MM-DD 或 [未提供]", "subject": "学科"},
  "headline": "一句话概括这堂课（不超过 30 字）",
  "abstract": "3-5 句总览，说明这堂课讲清楚了一件什么事",
  "framework": [{"title": "一级主题", "children": [{"title": "二级要点", "note": "一句话"}]}],
  "section_order": [{"segment_index": 0, "title": "最终使用的节标题", "lead": "一句话导语"}],
  "exam_focus": [{"type": "重要|易错|考点", "text": "内容"}],
  "quiz": [{"q": "问题", "a": "参考答案", "hint": "提示"}],
  "homework": ["老师布置的作业，未布置则空数组"],
  "further_reading": ["推荐阅读，未提及则空数组"],
  "thinking": ["课后思考题，未提及则空数组"],
  "glossary": [{"term": "术语", "definition": "简明定义"}],
  "caveats": ["待核验 / 录音不清 / AI 补充说明"]
}

约束：
- section_order 必须覆盖全部 __COUNT__ 段，segment_index 从 0 开始，不要增删段。
- 无法从内容中确定的字段一律写 [未提供]，禁止编造教师、日期、作业。
"""


def build_global_messages(
    *,
    title: str,
    duration: float,
    subject: str,
    audience: str,
    extra: str,
    sections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    compact = [
        {
            "segment_index": i,
            "section_title": s.get("section_title"),
            "section_lead": s.get("section_lead"),
            "terms": s.get("terms") or [],
            "takeaways": s.get("takeaways") or [],
            "exam_points": s.get("exam_points") or [],
        }
        for i, s in enumerate(sections)
    ]
    body = GLOBAL_TEMPLATE
    for key, value in {
        "__TITLE__": title,
        "__DURATION__": seconds_to_label(duration),
        "__SUBJECT__": subject or "（未指定）",
        "__AUDIENCE__": audience or "（未指定）",
        "__EXTRA_LINE__": f"补充要求：{extra}" if extra else "",
        "__SECTIONS__": json.dumps(compact, ensure_ascii=False),
        "__COUNT__": str(len(sections)),
    }.items():
        body = body.replace(key, value)
    return [
        {"role": "system", "content": GLOBAL_SYSTEM},
        {"role": "user", "content": body},
    ]


# ---------------------------------------------------------------- 归一化
def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def normalize_section(raw: dict[str, Any], index: int, start: float, end: float, frame_ids: list[str]) -> dict[str, Any]:
    def clean_anchor(item: dict[str, Any]) -> dict[str, Any] | None:
        label = str(item.get("label") or item.get("title") or "").strip()
        seconds = label_to_seconds(str(item.get("time") or ""))
        if seconds <= 0:
            seconds = float(item.get("timestamp") or 0) or start
        seconds = max(start, min(end, seconds))
        frame_id = str(item.get("frame_id") or "")
        if frame_id not in frame_ids:
            frame_id = ""
        if not label and not frame_id:
            return None
        return {"time": seconds_to_label(seconds), "seconds": seconds, "label": label, "frame_id": frame_id}

    anchors = [a for a in (clean_anchor(x) for x in _as_list(raw.get("anchors")) if isinstance(x, dict)) if a]
    return {
        "segment_index": index,
        "section_title": str(raw.get("section_title") or f"第 {index + 1} 节").strip(),
        "section_lead": str(raw.get("section_lead") or "").strip(),
        "start": seconds_to_label(start),
        "end": seconds_to_label(end),
        "start_seconds": start,
        "end_seconds": end,
        "terms": [x for x in _as_list(raw.get("terms")) if isinstance(x, dict)],
        "principles": [str(x) for x in _as_list(raw.get("principles")) if str(x).strip()],
        "examples": [x for x in _as_list(raw.get("examples")) if isinstance(x, dict)],
        "formulas": [x for x in _as_list(raw.get("formulas")) if isinstance(x, dict)],
        "takeaways": [str(x) for x in _as_list(raw.get("takeaways")) if str(x).strip()],
        "exam_points": [x for x in _as_list(raw.get("exam_points")) if isinstance(x, dict)],
        "quiz": [x for x in _as_list(raw.get("quiz")) if isinstance(x, dict)],
        "diagram_mermaid": str(raw.get("diagram_mermaid") or "").strip(),
        "anchors": anchors,
        "supplements": [x for x in _as_list(raw.get("supplements")) if isinstance(x, dict)],
        "uncertain": [str(x) for x in _as_list(raw.get("uncertain")) if str(x).strip()],
    }


def apply_section_order(sections: list[dict[str, Any]], order: Any) -> list[dict[str, Any]]:
    if not isinstance(order, list) or not order:
        return sections
    by_index = {s["segment_index"]: s for s in sections}
    result: list[dict[str, Any]] = []
    for item in order:
        if not isinstance(item, dict):
            continue
        idx = item.get("segment_index")
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            continue
        section = by_index.get(idx)
        if not section:
            continue
        title = str(item.get("title") or "").strip()
        lead = str(item.get("lead") or "").strip()
        if title:
            section["section_title"] = title
        if lead:
            section["section_lead"] = lead
        result.append(section)
    return result or sections


def merge_exam_points(sections: list[dict[str, Any]], extra: Any) -> list[dict[str, Any]]:
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for source in list(_as_list(extra)) + [p for s in sections for p in s.get("exam_points", [])]:
        if not isinstance(source, dict):
            continue
        text = str(source.get("text") or "").strip()
        if not text:
            continue
        kind = str(source.get("type") or "重要").strip()
        if kind not in ("重要", "易错", "考点"):
            kind = "重要"
        key = f"{kind}|{text}"
        if key in seen:
            continue
        seen.add(key)
        merged.append({"type": kind, "text": text})
    return merged


# ---------------------------------------------------------------- Mock 数据
def mock_notes(title: str, duration: float, kind: str, plan: list[tuple[float, float]]) -> dict[str, Any]:
    sections = []
    for i, (start, end) in enumerate(plan):
        sections.append(
            {
                "segment_index": i,
                "section_title": f"第 {i + 1} 节：示例主题",
                "section_lead": "这一节要解决的中心问题（示例）",
                "start": seconds_to_label(start),
                "end": seconds_to_label(end),
                "start_seconds": start,
                "end_seconds": end,
                "terms": [{"term": "示例概念", "detail": "这里是概念的精确定义，公式如 $E=mc^2$。"}],
                "principles": ["原理一：说明机制与因果。", "原理二：说明适用条件。"],
                "examples": [{"title": "示例 1", "detail": "老师举的例子与推导过程。"}],
                "formulas": [{"name": "示例公式", "latex": "a^2+b^2=c^2", "note": "$c$ 为斜边"}],
                "takeaways": ["要点一", "要点二"],
                "exam_points": [{"type": "考点", "text": "示例考点说明"}],
                "quiz": [{"q": "示例自测问题？", "a": "示例参考答案。", "hint": "提示"}],
                "diagram_mermaid": "",
                "anchors": [{"time": seconds_to_label(start), "seconds": start, "label": "本节起点", "frame_id": ""}],
                "supplements": [],
                "uncertain": ["这是模板预览，真实运行时由 Qwen 生成。"],
            }
        )
    return {
        "course": {
            "name": title,
            "topic": "示例章节",
            "teacher": "[未提供]",
            "date": dt.date.today().isoformat(),
            "subject": "[未提供]",
        },
        "headline": "这是一份课堂笔记网页模板预览",
        "abstract": "用于验证版式、框架、时间轴与媒体引用是否正常。真实运行时，全部内容由 Qwen 结合转写文本与画面生成。",
        "framework": [
            {"title": "一级主题 A", "children": [{"title": "要点 A1", "note": "一句话说明"}]},
            {"title": "一级主题 B", "children": [{"title": "要点 B1", "note": "一句话说明"}]},
        ],
        "exam_focus": [{"type": "重要", "text": "示例重点"}, {"type": "易错", "text": "示例易错点"}],
        "quiz": [{"q": "示例问题？", "a": "示例答案。", "hint": "提示"}],
        "homework": [],
        "further_reading": [],
        "thinking": [],
        "glossary": [{"term": "示例术语", "definition": "示例定义"}],
        "caveats": [f"模式：mock-notes（{kind}）"],
        "sections": sections,
    }


# ---------------------------------------------------------------- HTML 渲染
CSS = """
:root{
  --paper:#FBF7EF; --card:#FFFFFF; --ink:#243447; --ink-soft:#5C6B7E;
  --line:#E7E0CF; --accent:#1F5FA8; --accent-soft:#EDF3FB;
  --red:#B5382A; --red-soft:#FCECEA; --green:#1E7A5A; --green-soft:#E9F5EF;
  --amber:#9A6000; --amber-soft:#FCF3E1; --radius:14px;
  --shadow:0 1px 2px rgba(36,52,71,.05), 0 10px 28px rgba(36,52,71,.06);
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  margin:0; background:var(--paper); color:var(--ink);
  font-family:"PingFang SC","Microsoft YaHei","Hiragino Sans GB","Source Han Sans SC",system-ui,sans-serif;
  font-size:15px; line-height:1.75;
  background-image:linear-gradient(rgba(36,52,71,.022) 1px,transparent 1px);
  background-size:100% 30px;
}
code,kbd,pre,.mono{font-family:"JetBrains Mono",Consolas,"Courier New",monospace}
a{color:var(--accent);text-decoration:none}
strong{color:#16222F;font-weight:700}
.note-body code,dd code,p code,li code,.takeaway code,.fnote code{
  background:#F2EFE6;border:1px solid #E6DFCE;border-radius:5px;padding:1px 5px;font-size:89%;color:#8A4B1F;
}
pre.codeblock{
  background:#F7FAFD;border:1px solid #DFE9F4;border-radius:9px;padding:11px 13px;
  overflow:auto;font-size:12.5px;line-height:1.65;margin:6px 0 0;color:#243447;white-space:pre;
}

.hero{background:linear-gradient(180deg,#FFFDF7,#F7F1E4);border-bottom:1px solid var(--line)}
.hero-inner{max-width:1180px;margin:0 auto;padding:38px 20px 30px}
.kicker{font-size:12px;letter-spacing:.18em;color:var(--amber);font-weight:700}
.hero h1{margin:10px 0 8px;font-size:31px;line-height:1.35;letter-spacing:-.01em}
.headline{font-size:17px;color:var(--accent);font-weight:600;margin:0 0 10px}
.abstract{margin:0 0 16px;color:var(--ink-soft);max-width:74ch}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{background:var(--card);border:1px solid var(--line);border-radius:999px;padding:5px 13px;font-size:13px;color:var(--ink-soft)}
.chip b{color:var(--ink);font-weight:600}

.wrap{max-width:1180px;margin:0 auto;padding:26px 20px 90px;display:grid;grid-template-columns:236px 1fr;gap:30px;align-items:start}
aside.toc{position:sticky;top:18px}
.toc-box{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:14px;box-shadow:var(--shadow)}
.toc-box h4{margin:0 0 10px;font-size:12px;letter-spacing:.14em;color:var(--ink-soft)}
#search-box{width:100%;padding:7px 10px;border:1px solid var(--line);border-radius:9px;font-size:13px;margin-bottom:10px;font-family:inherit}
#search-box:focus{outline:2px solid var(--accent-soft);border-color:var(--accent)}
.toc-box ul{list-style:none;margin:0;padding:0;font-size:13.5px}
.toc-box li{margin:1px 0}
.toc-box a{display:block;padding:5px 9px;border-radius:8px;color:var(--ink-soft)}
.toc-box a:hover{background:var(--accent-soft);color:var(--accent)}
.toc-box a.active{background:var(--accent-soft);color:var(--accent);font-weight:600}
.toc-actions{margin-top:12px;display:flex;gap:8px}
.btn{flex:1;text-align:center;border:1px solid var(--line);background:var(--card);border-radius:9px;padding:6px 8px;font-size:12.5px;cursor:pointer;color:var(--ink-soft);font-family:inherit}
.btn:hover{border-color:var(--accent);color:var(--accent)}

main{min-width:0}
section.block{margin-bottom:26px}
.block-title{display:flex;align-items:center;gap:9px;font-size:19px;margin:0 0 13px}
.block-title .num{width:26px;height:26px;flex:none;border-radius:8px;background:var(--accent);color:#fff;font-size:13px;display:grid;place-items:center}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:18px 20px;box-shadow:var(--shadow)}

.player{position:sticky;top:0;z-index:30;background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:12px;box-shadow:var(--shadow);margin-bottom:20px}
.player video,.player audio{width:100%;display:block;border-radius:9px;background:#000}
.player video{max-height:340px}
.player-tip{font-size:12px;color:var(--ink-soft);margin-top:7px}

.mindmap{list-style:none;margin:0;padding:0;overflow-x:auto}
.mindmap ul{list-style:none;margin:0;padding-left:20px;position:relative}
.mindmap>li{padding-left:0}
.mindmap ul::before{content:"";position:absolute;left:6px;top:2px;bottom:14px;border-left:2px solid var(--line)}
.mindmap li{position:relative;padding:4px 0 4px 16px}
.mindmap li>ul{margin-top:2px}
.mindmap li::before{content:"";position:absolute;left:-14px;top:17px;width:14px;border-top:2px solid var(--line)}
.node{display:inline-block;background:var(--accent-soft);border:1px solid #D6E4F5;color:var(--accent);border-radius:9px;padding:4px 12px;font-weight:600;font-size:14px}
.mindmap>li>.node{background:var(--accent);color:#fff;border-color:var(--accent)}
.node-note{font-size:12.5px;color:var(--ink-soft);margin-left:8px}

.timeline{list-style:none;margin:0;padding:0}
.timeline li{position:relative;padding:0 0 16px 26px;border-left:2px solid var(--line)}
.timeline li:last-child{border-left-color:transparent;padding-bottom:0}
.timeline li::before{content:"";position:absolute;left:-7px;top:6px;width:12px;height:12px;border-radius:50%;background:var(--accent);border:2px solid var(--card)}
.tl-time{font-size:12px;color:var(--amber);font-weight:700}
.tl-title{font-weight:600}
.tl-body{color:var(--ink-soft);font-size:14px}

.note-card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:0;box-shadow:var(--shadow);margin-bottom:16px;overflow:hidden}
.note-head{display:flex;align-items:baseline;gap:10px;padding:15px 20px;background:#FDFBF5;border-bottom:1px solid var(--line);flex-wrap:wrap}
.note-idx{flex:none;width:27px;height:27px;border-radius:50%;background:var(--ink);color:#fff;font-size:13px;display:grid;place-items:center}
.note-head h3{margin:0;font-size:17px}
.time-range{margin-left:auto;font-size:12.5px;color:var(--ink-soft)}
.note-body{padding:16px 20px}
.note-lead{color:var(--accent);font-size:14.5px;margin:0 0 12px;padding-left:11px;border-left:3px solid var(--accent-soft)}
h5.field{margin:16px 0 7px;font-size:13px;letter-spacing:.06em;color:var(--ink-soft);display:flex;align-items:center;gap:6px}
h5.field:first-child{margin-top:0}
ul.tight{margin:0;padding-left:19px}
ul.tight li{margin:3px 0}
dl.terms{margin:0}
dl.terms dt{font-weight:700;color:var(--accent);margin-top:8px}
dl.terms dd{margin:2px 0 0 0;color:var(--ink)}
.example{border-left:3px solid var(--green-soft);padding:8px 0 8px 12px;margin:0 0 10px}
.example b{color:var(--green)}
.formula{background:#F7FAFD;border:1px solid #DFE9F4;border-radius:11px;padding:11px 14px;margin:0 0 10px}
.formula .fname{font-size:12.5px;color:var(--ink-soft);font-weight:700}
.formula .fnote{font-size:12.5px;color:var(--ink-soft);margin-top:5px}
.takeaway{background:var(--amber-soft);border-radius:10px;padding:9px 13px;margin:0 0 7px}
.supplement{background:#F4F1FF;border-left:3px solid #7A5AF8;border-radius:8px;padding:8px 12px;margin:0 0 8px;font-size:14px}
.uncertain{background:#FFF8F6;border-left:3px solid var(--red);border-radius:8px;padding:8px 12px;margin:0 0 8px;font-size:14px;color:#8C3B2E}
.anchors{display:flex;flex-wrap:wrap;gap:10px;margin-top:13px}
.anchor{border:1px solid var(--line);background:var(--card);border-radius:11px;overflow:hidden;cursor:pointer;padding:0;text-align:left;font-family:inherit;font-size:12px;color:var(--ink-soft);max-width:190px}
.anchor:hover{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.anchor img{display:block;width:100%;height:96px;object-fit:cover;background:#eee}
.anchor .cap{padding:6px 9px;line-height:1.4}
.ts{display:inline-flex;align-items:center;gap:5px;border:1px solid #D6E4F5;background:var(--accent-soft);color:var(--accent);border-radius:999px;padding:3px 11px;font-size:12.5px;cursor:pointer;font-family:inherit;font-weight:600}
.ts:hover{background:var(--accent);color:#fff}
pre.mermaid{background:#FBFDF8;border:1px dashed var(--line);border-radius:10px;padding:12px;overflow:auto;font-size:12.5px}

.exam-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:13px}
.exam-col{border-radius:var(--radius);padding:13px 15px;border:1px solid}
.exam-col h4{margin:0 0 8px;font-size:14px}
.exam-col ul{margin:0;padding-left:18px;font-size:14px}
.exam-col.重要{background:var(--amber-soft);border-color:#EBD9AE}.exam-col.重要 h4{color:var(--amber)}
.exam-col.易错{background:var(--red-soft);border-color:#F0CFC8}.exam-col.易错 h4{color:var(--red)}
.exam-col.考点{background:var(--green-soft);border-color:#CBE6D8}.exam-col.考点 h4{color:var(--green)}

.quiz{border:1px solid var(--line);border-radius:11px;margin-bottom:9px;background:var(--card);overflow:hidden}
.quiz summary{padding:11px 15px;cursor:pointer;font-weight:600;list-style:none}
.quiz summary::-webkit-details-marker{display:none}
.quiz summary::before{content:"Q  ";color:var(--accent);font-weight:800}
.quiz[open] summary{background:#FDFBF5;border-bottom:1px solid var(--line)}
.quiz .ans{padding:11px 15px;font-size:14px;color:var(--ink)}
.quiz .hint{font-size:12.5px;color:var(--ink-soft);margin-top:5px}

.glossary{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:11px}
.glossary .g{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:11px 14px}
.glossary .g b{color:var(--accent)}
.glossary .g p{margin:4px 0 0;font-size:14px;color:var(--ink)}

.transcript details{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:12px 16px}
.transcript summary{cursor:pointer;font-weight:600}
.transcript .lines{margin-top:11px;max-height:420px;overflow:auto;font-size:13.5px;color:var(--ink-soft)}
.transcript .line{display:flex;gap:9px;padding:2px 0;border-bottom:1px dashed #F0EAE0}
.transcript .line button{flex:none}

.empty{color:var(--ink-soft);font-size:14px}
footer.foot{max-width:1180px;margin:0 auto;padding:0 20px 40px;color:var(--ink-soft);font-size:12.5px}

@media (max-width:900px){
  .wrap{grid-template-columns:1fr}
  aside.toc{display:none}
  .hero h1{font-size:24px}
}
@media print{
  body{background:#fff}
  aside.toc,.player,.toc-actions,#search-box,.btn{display:none!important}
  .wrap{display:block;padding:0}
  .card,.note-card{box-shadow:none;break-inside:avoid}
  .quiz[open] .ans{display:block}
  details{open:all}
}
"""

JS = """
(function(){
  var media = document.getElementById('src-media');
  function seek(sec){
    if(media){
      try{ media.currentTime = Math.max(0, parseFloat(sec)||0); media.play(); }catch(e){}
    }
    var p = document.getElementById('player-block');
    if(p){ p.scrollIntoView({behavior:'smooth', block:'center'}); }
  }
  document.querySelectorAll('[data-seek]').forEach(function(el){
    el.addEventListener('click', function(){ seek(el.getAttribute('data-seek')); });
  });

  // 目录高亮
  var links = Array.prototype.slice.call(document.querySelectorAll('.toc-box a[href^="#"]'));
  var targets = links.map(function(a){ return document.querySelector(a.getAttribute('href')); });
  function onScroll(){
    var best = null, bestTop = -1e9;
    for(var i=0;i<targets.length;i++){
      var t = targets[i];
      if(!t) continue;
      var top = t.getBoundingClientRect().top;
      if(top < 160 && top > bestTop){ bestTop = top; best = links[i]; }
    }
    links.forEach(function(a){ a.classList.remove('active'); });
    if(best){ best.classList.add('active'); }
  }
  window.addEventListener('scroll', onScroll, {passive:true});
  onScroll();

  // 搜索：过滤章节
  var box = document.getElementById('search-box');
  var counter = document.getElementById('search-count');
  if(box){
    box.addEventListener('input', function(){
      var q = box.value.trim().toLowerCase();
      var n = 0;
      document.querySelectorAll('.note-card').forEach(function(card){
        var hit = !q || card.textContent.toLowerCase().indexOf(q) >= 0;
        card.style.display = hit ? '' : 'none';
        if(hit) n++;
      });
      if(counter){ counter.textContent = q ? ('匹配 ' + n + ' 节') : ''; }
    });
  }
})();
"""

KATEX_HEAD = """<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css" crossorigin="anonymous">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js" crossorigin="anonymous"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js" crossorigin="anonymous"></script>
<script>
window.addEventListener('load', function(){
  if(!window.renderMathInElement){ document.body.classList.add('no-katex'); return; }
  try{
    window.renderMathInElement(document.body, {
      delimiters:[
        {left:'$$', right:'$$', display:true},
        {left:'$', right:'$', display:false},
        {left:'\\\\(', right:'\\\\)', display:false},
        {left:'\\\\[', right:'\\\\]', display:true}
      ],
      throwOnError:false
    });
  }catch(e){ document.body.classList.add('no-katex'); }
});
</script>
"""

MERMAID_HEAD = """<script type="module">
try{
  const mermaid = (await import('https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs')).default;
  mermaid.initialize({startOnLoad:false, securityLevel:'loose', theme:'default'});
  await mermaid.run({querySelector:'pre.mermaid'});
}catch(e){
  document.body.classList.add('no-mermaid');
}
</script>
"""


def media_src(media: Path, out_dir: Path) -> str:
    """网页里引用本地媒体：优先相对路径（允许 ../），跨盘符时退回 file:// URI。"""
    try:
        rel = os.path.relpath(str(media.resolve()), str(out_dir.resolve()))
    except ValueError:
        rel = ""
    if rel:
        return quote(rel.replace("\\", "/"), safe="/")
    return media.resolve().as_uri()


def render_framework(framework: Any) -> str:
    items = [x for x in _as_list(framework) if isinstance(x, dict)]
    if not items:
        return '<p class="empty">[未提供]</p>'

    def children_of(node: dict[str, Any]) -> str:
        kids = [x for x in _as_list(node.get("children")) if isinstance(x, dict)]
        if not kids:
            return ""
        lis = []
        for kid in kids:
            note = str(kid.get("note") or "").strip()
            lis.append(
                "<li><span class='node'>"
                + esc(kid.get("title"))
                + "</span>"
                + (f"<span class='node-note'>{esc(note)}</span>" if note else "")
                + children_of(kid)
                + "</li>"
            )
        return "<ul>" + "".join(lis) + "</ul>"

    top = []
    for item in items:
        top.append(
            "<li><span class='node'>" + esc(item.get("title")) + "</span>" + children_of(item) + "</li>"
        )
    return '<ul class="mindmap">' + "".join(top) + "</ul>"


def render_sections(sections: list[dict[str, Any]], frames: dict[str, Frame], out_dir: Path, kind: str) -> str:
    blocks: list[str] = []
    for section in sections:
        idx = int(section.get("segment_index", 0)) + 1
        start_s = float(section.get("start_seconds") or 0)
        parts: list[str] = []
        parts.append('<div class="note-card" id="sec-' + str(idx) + '">')
        parts.append(
            '<div class="note-head"><span class="note-idx">'
            + str(idx)
            + "</span><h3>"
            + esc(section.get("section_title"))
            + '</h3><span class="time-range">'
            + esc(section.get("start"))
            + " – "
            + esc(section.get("end"))
            + "</span></div>"
        )
        parts.append('<div class="note-body">')
        if section.get("section_lead"):
            parts.append('<p class="note-lead">' + rich(section.get("section_lead")) + "</p>")

        terms = [x for x in section.get("terms", []) if isinstance(x, dict)]
        if terms:
            parts.append("<h5 class='field'>📌 概念与定义</h5><dl class='terms'>")
            for term in terms:
                parts.append("<dt>" + esc(term.get("term")) + "</dt><dd>" + rich(term.get("detail")) + "</dd>")
            parts.append("</dl>")

        principles = [str(x) for x in section.get("principles", []) if str(x).strip()]
        if principles:
            parts.append("<h5 class='field'>⚙️ 原理与推导</h5><ul class='tight'>")
            parts.extend("<li>" + rich(p) + "</li>" for p in principles)
            parts.append("</ul>")

        # formulas 同时承载「数学公式」与「代码片段」：
        # latex 走 KaTeX 行间渲染，code 走等宽代码块，编程类教程靠后者。
        formulas = [
            x
            for x in section.get("formulas", [])
            if isinstance(x, dict) and (str(x.get("latex") or "").strip() or str(x.get("code") or "").strip())
        ]
        if formulas:
            parts.append("<h5 class='field'>🧮 公式与代码</h5>")
            for formula in formulas:
                parts.append("<div class='formula'>")
                if formula.get("name"):
                    parts.append("<div class='fname'>" + esc(formula.get("name")) + "</div>")
                if str(formula.get("code") or "").strip():
                    parts.append("<pre class='codeblock'>" + esc(formula.get("code")) + "</pre>")
                else:
                    parts.append("<div>$$" + esc(formula.get("latex")) + "$$</div>")
                if formula.get("note"):
                    parts.append("<div class='fnote'>" + rich(formula.get("note")) + "</div>")
                parts.append("</div>")

        examples = [x for x in section.get("examples", []) if isinstance(x, dict)]
        if examples:
            parts.append("<h5 class='field'>🔍 例子与推导</h5>")
            for example in examples:
                parts.append("<div class='example'>")
                if example.get("title"):
                    parts.append("<b>" + esc(example.get("title")) + "</b><br>")
                parts.append(rich(example.get("detail")))
                parts.append("</div>")

        takeaways = [str(x) for x in section.get("takeaways", []) if str(x).strip()]
        if takeaways:
            parts.append("<h5 class='field'>✅ 本节要点</h5>")
            parts.extend("<div class='takeaway'>" + rich(t) + "</div>" for t in takeaways)

        if section.get("diagram_mermaid"):
            parts.append("<h5 class='field'>🗺️ 结构图</h5><pre class='mermaid'>" + esc(section.get("diagram_mermaid")) + "</pre>")

        # 画面 / 时间戳锚点
        anchors = [x for x in section.get("anchors", []) if isinstance(x, dict)]
        auto_frames = []
        if kind == "video":
            for frame in frames.values():
                if start_s <= frame.timestamp < float(section.get("end_seconds") or start_s + 1) + 0.001:
                    auto_frames.append(frame)
        seen_ids: set[str] = set()
        chips: list[str] = []
        for anchor in anchors:
            fid = str(anchor.get("frame_id") or "")
            seconds = float(anchor.get("seconds") or label_to_seconds(str(anchor.get("time") or "")))
            label = str(anchor.get("label") or "").strip() or "回看此处"
            if fid and fid in frames and fid not in seen_ids:
                seen_ids.add(fid)
                frame = frames[fid]
                chips.append(
                    "<button class='anchor' data-seek='"
                    + f"{frame.timestamp:.2f}"
                    + "'><img src='"
                    + quote(str(frame.path.relative_to(out_dir)).replace("\\", "/"), safe="/")
                    + "' alt='frame'><span class='cap'>▶ "
                    + esc(frame.time_label)
                    + " "
                    + esc(label)
                    + "</span></button>"
                )
            else:
                chips.append(
                    "<button class='ts' data-seek='" + f"{seconds:.2f}" + "'>▶ " + esc(anchor.get("time")) + " " + esc(label) + "</button>"
                )
        if kind == "video":
            for frame in auto_frames:
                if frame.frame_id in seen_ids:
                    continue
                seen_ids.add(frame.frame_id)
                chips.append(
                    "<button class='anchor' data-seek='"
                    + f"{frame.timestamp:.2f}"
                    + "'><img src='"
                    + quote(str(frame.path.relative_to(out_dir)).replace("\\", "/"), safe="/")
                    + "' alt='frame'><span class='cap'>▶ "
                    + esc(frame.time_label)
                    + "</span></button>"
                )
        if chips:
            parts.append("<h5 class='field'>🎬 回看锚点</h5><div class='anchors'>" + "".join(chips) + "</div>")

        supplements = [x for x in section.get("supplements", []) if isinstance(x, dict)]
        if supplements:
            parts.append("<h5 class='field'>➕ AI 补充（老师未讲，仅供衔接）</h5>")
            for item in supplements:
                reason = str(item.get("reason") or "").strip()
                parts.append(
                    "<div class='supplement'>" + rich(item.get("text")) + (f"<br><i>原因：{esc(reason)}</i>" if reason else "") + "</div>"
                )

        uncertain = [str(x) for x in section.get("uncertain", []) if str(x).strip()]
        if uncertain:
            parts.append("<h5 class='field'>⚠️ 待核验</h5>")
            parts.extend("<div class='uncertain'>" + rich(u) + "</div>" for u in uncertain)

        parts.append("</div></div>")
        blocks.append("".join(parts))
    return "".join(blocks) or '<p class="empty">[未提供]</p>'


def render_exam(exam: list[dict[str, Any]]) -> str:
    if not exam:
        return '<p class="empty">[未提供]</p>'
    buckets: dict[str, list[str]] = {"重要": [], "易错": [], "考点": []}
    for item in exam:
        kind = str(item.get("type") or "重要")
        buckets.setdefault(kind, []).append(str(item.get("text") or ""))
    cols = []
    for kind in ("重要", "易错", "考点"):
        if not buckets.get(kind):
            continue
        cols.append(
            "<div class='exam-col "
            + kind
            + "'><h4>"
            + kind
            + "</h4><ul>"
            + "".join("<li>" + rich(t) + "</li>" for t in buckets[kind])
            + "</ul></div>"
        )
    return '<div class="exam-grid">' + "".join(cols) + "</div>"


def render_quiz(quiz: list[dict[str, Any]]) -> str:
    if not quiz:
        return '<p class="empty">[未提供]</p>'
    out = []
    for item in quiz:
        hint = str(item.get("hint") or "").strip()
        out.append(
            "<details class='quiz'><summary>"
            + esc(item.get("q"))
            + "</summary><div class='ans'>"
            + rich(item.get("a"))
            + (f"<div class='hint'>提示：{esc(hint)}</div>" if hint else "")
            + "</div></details>"
        )
    return "".join(out)


def render_transcript(transcript: dict[str, Any]) -> str:
    segments = transcript.get("segments") or []
    if not segments:
        text = str(transcript.get("text") or "").strip()
        if not text:
            return '<p class="empty">[未提供]</p>'
        return "<details><summary>展开全文转写</summary><div class='lines'>" + para(text) + "</div></details>"
    lines = []
    for seg in segments:
        start = float(seg.get("start", 0) or 0)
        lines.append(
            "<div class='line'><button class='ts' data-seek='"
            + f"{start:.2f}"
            + "'>▶ "
            + seconds_to_label(start)
            + "</button><span>"
            + esc(seg.get("text"))
            + "</span></div>"
        )
    return (
        "<details><summary>展开全文转写（"
        + str(len(segments))
        + " 句，时间戳可点击回听）</summary><div class='lines'>"
        + "".join(lines)
        + "</div></details>"
    )


def render_toc(sections: list[dict[str, Any]]) -> str:
    items = ['<li><a href="#framework">知识框架</a></li>', '<li><a href="#timeline">时间线</a></li>']
    for section in sections:
        idx = int(section.get("segment_index", 0)) + 1
        items.append(
            '<li><a href="#sec-' + str(idx) + '">' + str(idx) + ". " + esc(section.get("section_title")) + "</a></li>"
        )
    items.append('<li><a href="#exam">重点与考点</a></li>')
    items.append('<li><a href="#quiz">自测题</a></li>')
    items.append('<li><a href="#homework">作业与延伸</a></li>')
    items.append('<li><a href="#glossary">术语表</a></li>')
    items.append('<li><a href="#caveats">待核验</a></li>')
    items.append('<li><a href="#transcript">全文转写</a></li>')
    return "<ul>" + "".join(items) + "</ul>"


def render_html(
    *,
    analysis: dict[str, Any],
    title: str,
    kind: str,
    media: Path | None,
    out_dir: Path,
    frames: dict[str, Frame],
    transcript: dict[str, Any],
    math_enabled: bool,
    mermaid_enabled: bool,
) -> str:
    course = analysis.get("course") or {}
    sections = analysis.get("sections") or []
    if media is not None:
        src = media_src(media, out_dir)
        media_tag = (
            f"<video id='src-media' controls preload='metadata' src='{esc(src)}'></video>"
            if kind == "video"
            else f"<audio id='src-media' controls preload='metadata' src='{esc(src)}'></audio>"
        )
        player = (
            "<div class='player' id='player-block'>"
            + media_tag
            + "<div class='player-tip'>点击笔记中的 ▶ 时间戳或画面，可直接跳到原片对应位置。</div></div>"
        )
    else:
        player = ""

    chips = []
    for label, key in (("课程", "name"), ("主题", "topic"), ("教师", "teacher"), ("日期", "date"), ("学科", "subject")):
        value = str(course.get(key) or "").strip()
        if value:
            chips.append(f"<span class='chip'>{label}：<b>{esc(value)}</b></span>")
    chips.append(f"<span class='chip'>时长：<b>{esc(seconds_to_label(transcript.get('_duration') or 0))}</b></span>")
    chips.append(f"<span class='chip'>章节：<b>{len(sections)}</b></span>")
    if media is not None:
        chips.append(f"<span class='chip'>来源：<b>{esc(media.name)}</b></span>")

    timeline_items = []
    for section in sections:
        timeline_items.append(
            "<li><div class='tl-time'>"
            + esc(section.get("start"))
            + " – "
            + esc(section.get("end"))
            + "</div><div class='tl-title'>"
            + esc(section.get("section_title"))
            + "</div><div class='tl-body'>"
            + rich(section.get("section_lead"))
            + "</div></li>"
        )

    homework = [str(x) for x in _as_list(analysis.get("homework")) if str(x).strip()]
    reading = [str(x) for x in _as_list(analysis.get("further_reading")) if str(x).strip()]
    thinking = [str(x) for x in _as_list(analysis.get("thinking")) if str(x).strip()]
    glossary = [x for x in _as_list(analysis.get("glossary")) if isinstance(x, dict)]
    caveats = [str(x) for x in _as_list(analysis.get("caveats")) if str(x).strip()]

    extra_blocks = []
    if homework:
        extra_blocks.append("<h5 class='field'>📝 作业</h5><ul class='tight'>" + "".join(f"<li>{rich(x)}</li>" for x in homework) + "</ul>")
    if reading:
        extra_blocks.append("<h5 class='field'>📚 推荐阅读</h5><ul class='tight'>" + "".join(f"<li>{rich(x)}</li>" for x in reading) + "</ul>")
    if thinking:
        extra_blocks.append("<h5 class='field'>🤔 课后思考</h5><ul class='tight'>" + "".join(f"<li>{rich(x)}</li>" for x in thinking) + "</ul>")
    if not extra_blocks:
        extra_blocks.append('<p class="empty">老师未布置作业或延伸内容。</p>')

    glossary_html = (
        '<div class="glossary">'
        + "".join(
            "<div class='g'><b>" + esc(g.get("term")) + "</b><p>" + rich(g.get("definition")) + "</p></div>"
            for g in glossary
        )
        + "</div>"
        if glossary
        else '<p class="empty">[未提供]</p>'
    )

    caveats_html = (
        "".join(f"<div class='uncertain'>{rich(c)}</div>" for c in caveats)
        if caveats
        else '<p class="empty">无。</p>'
    )

    head_extra = ""
    if math_enabled:
        head_extra += KATEX_HEAD
    if mermaid_enabled:
        head_extra += MERMAID_HEAD

    body = f"""
<header class="hero"><div class="hero-inner">
  <div class="kicker">课堂笔记 · AI 整理</div>
  <h1>{esc(title)}</h1>
  <p class="headline">{esc(analysis.get("headline") or "")}</p>
  <p class="abstract">{rich(analysis.get("abstract") or "")}</p>
  <div class="chips">{''.join(chips)}</div>
</div></header>

<div class="wrap">
  <aside class="toc"><div class="toc-box">
    <h4>目录</h4>
    <input id="search-box" type="search" placeholder="搜索章节关键词…">
    <div id="search-count" style="font-size:12px;color:var(--ink-soft);margin-bottom:8px"></div>
    {render_toc(sections)}
    <div class="toc-actions">
      <button class="btn" onclick="window.print()">🖨 打印 / 存 PDF</button>
      <button class="btn" onclick="window.scrollTo(0,0)">↑ 回顶部</button>
    </div>
  </div></aside>

  <main>
    {player}

    <section class="block" id="framework">
      <h2 class="block-title"><span class="num">壹</span>本堂课知识框架</h2>
      <div class="card">{render_framework(analysis.get("framework"))}</div>
    </section>

    <section class="block" id="timeline">
      <h2 class="block-title"><span class="num">贰</span>讲课时间线</h2>
      <div class="card"><ul class="timeline">{''.join(timeline_items) or '<li class="empty">[未提供]</li>'}</ul></div>
    </section>

    <section class="block" id="notes">
      <h2 class="block-title"><span class="num">叁</span>逐节详细笔记</h2>
      {render_sections(sections, frames, out_dir, kind)}
    </section>

    <section class="block" id="exam">
      <h2 class="block-title"><span class="num">肆</span>重点 · 易错 · 考点</h2>
      <div class="card">{render_exam(analysis.get("exam_focus") or [])}</div>
    </section>

    <section class="block" id="quiz">
      <h2 class="block-title"><span class="num">伍</span>自测题（点击展开答案）</h2>
      <div class="card">{render_quiz(analysis.get("quiz") or [])}</div>
    </section>

    <section class="block" id="homework">
      <h2 class="block-title"><span class="num">陆</span>作业与延伸</h2>
      <div class="card">{''.join(extra_blocks)}</div>
    </section>

    <section class="block" id="glossary">
      <h2 class="block-title"><span class="num">柒</span>术语表</h2>
      <div class="card">{glossary_html}</div>
    </section>

    <section class="block" id="caveats">
      <h2 class="block-title"><span class="num">捌</span>待核验与补充说明</h2>
      <div class="card">{caveats_html}</div>
    </section>

    <section class="block transcript" id="transcript">
      <h2 class="block-title"><span class="num">玖</span>全文转写</h2>
      {render_transcript(transcript)}
    </section>
  </main>
</div>

<footer class="foot">
  由 z-video-classroom-notes 生成 · {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} ·
  内容来自音视频转写与画面分析，AI 补充项已单独标注，请以原片为准。
</footer>
"""

    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{esc(title)} · 课堂笔记</title>"
        + head_extra
        + "<style>"
        + CSS
        + "</style></head><body>"
        + body
        + "<script>"
        + JS
        + "</script></body></html>"
    )


# ---------------------------------------------------------------- 单文件化
def make_single_file(html_path: Path, media: Path, out_dir: Path, max_media_mb: float) -> tuple[bool, str]:
    text = html_path.read_text(encoding="utf-8")

    def inline_images(src: str) -> str:
        for match in list(re.finditer(r"""(?:src|href)='([^']+)'""", src)):
            ref = match.group(1)
            if ref.startswith(("http", "data:", "#", "file:")):
                continue
            target = (out_dir / unquote(ref)).resolve()
            if not target.exists() or target.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
                continue
            mime = mimetypes.guess_type(target.name)[0] or "image/jpeg"
            data = base64.b64encode(target.read_bytes()).decode("ascii")
            src = src.replace(f"'{ref}'", f"'data:{mime};base64,{data}'")
        return src

    text = inline_images(text)
    size_mb = media.stat().st_size / 1024 / 1024 if media.exists() else 0
    if size_mb and size_mb <= max_media_mb:
        mime = mimetypes.guess_type(media.name)[0] or ("video/mp4" if media.suffix.lower() in VIDEO_EXTS else "audio/mpeg")
        data = base64.b64encode(media.read_bytes()).decode("ascii")
        text = re.sub(r"""(?:id='src-media'[^>]*src=')[^']+(')""", f"id='src-media' controls src='data:{mime};base64,{data}'", text)
        note = f"媒体已内联（{size_mb:.1f} MB）"
    else:
        text = text.replace(
            "<div class='player-tip'>",
            "<div class='player-tip'>⚠ 单文件模式下媒体未内联（超过 "
            f"{max_media_mb:.0f} MB），请把 HTML 与媒体文件放在同一目录。",
        )
        note = f"媒体未内联（{size_mb:.1f} MB > {max_media_mb:.0f} MB）"
    target = html_path.with_name(html_path.stem + "-single.html")
    target.write_text(text, encoding="utf-8")
    return True, f"{target.name}（{note}）"


# ---------------------------------------------------------------- 校验
def validate_html(html_path: Path, out_dir: Path) -> list[str]:
    issues: list[str] = []
    text = html_path.read_text(encoding="utf-8")
    for match in re.finditer(r"""(?:src|href)='([^']+)'""", text):
        ref = match.group(1)
        if ref.startswith(("http", "data:", "#", "file:")):
            continue
        # HTML 里的路径是 quote() 编码过的（中文会变 %E5...），校验前必须解码
        decoded = unquote(ref)
        if not (out_dir / decoded).exists():
            issues.append(f"missing-local-asset:{ref}")
    return issues


def assert_no_secret(out_dir: Path) -> list[str]:
    issues: list[str] = []
    for path in out_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".html", ".json", ".txt", ".md"}:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(content):
                issues.append(f"secret-leak:{path.name}")
                break
    return issues


# ---------------------------------------------------------------- 主流程
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="视频/音频 → 课堂笔记式学习网页",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("source", nargs="?", default="", help="视频网址 / 本地视频 / 本地音频文件路径（--from-analysis 时可省略）")
    parser.add_argument("--from-analysis", default="", help="用已有 notes-analysis.json 直接重渲染，不调模型、不转写")
    parser.add_argument("--title", default="", help="课程标题，默认取文件名")
    parser.add_argument("--out-dir", default="", help="输出目录，默认与媒体同级 notes-<标题>")
    parser.add_argument("--subject", default="", help="学科背景，如「高中物理」「机器学习」")
    parser.add_argument("--audience", default="", help="目标读者，如「初三学生」「考研党」")
    parser.add_argument("--extra", default="", help="追加给模型的个性化要求")
    parser.add_argument("--transcript", default="", help="已有转录文本/JSON，跳过 ASR")
    parser.add_argument("--asr", default="auto", choices=["auto", "vosk", "whisper", "dashscope", "none"])
    parser.add_argument("--vosk-model", default="", help="Vosk 模型目录，默认自动下载中文小模型")
    parser.add_argument("--whisper-model", default="base", help="Whisper 模型名")
    parser.add_argument("--language", default="zh", help="转写语言")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="分析用 Qwen 模型")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--max-segments", type=int, default=14, help="最多分几段")
    parser.add_argument("--min-segment-seconds", type=float, default=60.0, help="每段最短秒数")
    parser.add_argument("--frames-per-segment", type=int, default=2, help="每段抽几张关键帧")
    parser.add_argument("--no-frames", action="store_true", help="不抽帧（纯转写分析，更快）")
    parser.add_argument("--no-math", action="store_true", help="不加载 KaTeX")
    parser.add_argument("--no-mermaid", action="store_true", help="不加载 Mermaid")
    parser.add_argument("--skip-transcribe", action="store_true", help="跳过转写，只靠画面与标题")
    parser.add_argument("--mock-notes", action="store_true", help="不调用模型，用模板数据验证版式")
    parser.add_argument("--single-file", action="store_true", help="额外产出内联资源的单文件 HTML")
    parser.add_argument("--max-inline-mb", type=float, default=25.0, help="单文件模式下媒体内联上限 MB")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    log = RunLog()
    api_key = os.environ.get(DEFAULT_API_KEY_ENV, "").strip()

    title = args.title.strip() or (
        Path(args.source).stem if args.source and not is_url(args.source) else "课程笔记"
    )
    if args.out_dir:
        out_dir = Path(args.out_dir).expanduser()
    elif args.source and not is_url(args.source) and Path(args.source).expanduser().exists():
        base = Path(args.source).expanduser().resolve()
        out_dir = (base.parent if base.is_file() else base) / f"notes-{slugify(title)}"
    else:
        out_dir = Path.cwd() / f"notes-{slugify(title)}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "assets").mkdir(parents=True, exist_ok=True)
    print(f"输出目录：{out_dir}", file=sys.stderr)

    # 复用已有分析结果重渲染：不调模型、不转写、不抽帧
    if args.from_analysis:
        analysis = json.loads(Path(args.from_analysis).expanduser().read_text(encoding="utf-8"))
        frame_map = load_storyboard(out_dir)
        transcript: dict[str, Any] = {"status": "ok", "segments": [], "text": "", "backend": "reuse"}
        transcript_json = out_dir / "transcript.json"
        if transcript_json.exists():
            transcript = load_transcript(transcript_json)
        media: Path | None = None
        if args.source and Path(args.source).expanduser().exists():
            media = Path(args.source).expanduser()
            kind = "video" if media.suffix.lower() in VIDEO_EXTS else "audio"
        else:
            kind = "video"
        duration = media_duration(media) if media else 0.0
        transcript["_duration"] = duration
        log.step("载入已有分析结果", True, f"{len(analysis.get('sections') or [])} 节 / {len(frame_map)} 帧")
        html_text = render_html(
            analysis=analysis,
            title=title,
            kind=kind,
            media=media,
            out_dir=out_dir,
            frames=frame_map,
            transcript=transcript,
            math_enabled=not args.no_math,
            mermaid_enabled=not args.no_mermaid,
        )
        html_path = out_dir / "classroom-notes.html"
        html_path.write_text(html_text, encoding="utf-8")
        log.step("重渲染网页", True, html_path.name)
        single_note = ""
        if args.single_file and media is not None:
            try:
                _, single_note = make_single_file(html_path, media, out_dir, args.max_inline_mb)
                log.step("单文件化", True, single_note)
            except Exception as exc:  # noqa: BLE001
                log.warn(f"单文件化失败：{scrub_secret(str(exc))}")
        issues = validate_html(html_path, out_dir) + assert_no_secret(out_dir)
        (out_dir / "run-report.json").write_text(
            json.dumps(
                {
                    "title": title,
                    "mode": "from-analysis",
                    "segments": len(analysis.get("sections") or []),
                    "frames": len(frame_map),
                    "single_file": single_note,
                    "issues": issues,
                    "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print("\n=== 完成（重渲染）===", file=sys.stderr)
        print(f"笔记网页：{html_path}", file=sys.stderr)
        if single_note:
            print(f"单文件版：{out_dir / (html_path.stem + '-single.html')}", file=sys.stderr)
        if issues:
            print(f"校验问题：{issues}", file=sys.stderr)
        return 0

    try:
        media, kind = resolve_media(args.source, out_dir, title, log)
    except Exception as exc:  # noqa: BLE001
        print(f"失败：{scrub_secret(str(exc))}", file=sys.stderr)
        return 2

    duration = media_duration(media)
    if duration <= 0:
        duration = 60.0
        log.warn("无法探测时长，按 60 秒处理")
    log.step("探测时长", True, seconds_to_label(duration))

    # 1) 转写
    transcript: dict[str, Any] = {"status": "skipped", "segments": [], "text": "", "backend": "none"}
    if args.transcript:
        transcript = load_transcript(Path(args.transcript).expanduser())
        log.step("载入现成转录", True, f"{len(transcript.get('text', ''))} 字")
    elif args.skip_transcribe or args.asr == "none":
        log.warn("已跳过转写，笔记将更依赖画面与标题信息")
    else:
        try:
            wav = prepare_audio(media, out_dir)
            log.step("抽取音轨", True, wav.name)
        except Exception as exc:  # noqa: BLE001
            log.warn(f"抽音轨失败：{scrub_secret(str(exc))}")
            wav = None  # type: ignore[assignment]
        if wav:
            transcript = run_asr(
                wav,
                out_dir,
                backend=args.asr,
                api_key=api_key,
                vosk_model=args.vosk_model,
                whisper_model=args.whisper_model,
                language=args.language,
                log=log,
            )
            if transcript.get("status") != "ok":
                log.warn(f"转写不可用（{transcript.get('reason', 'unknown')}）：将只依据画面与标题分析")
    transcript["_duration"] = duration

    # 2) 分段 + 抽帧
    plan = build_plan(duration, args.max_segments, args.min_segment_seconds)
    log.step("切分章节", True, f"{len(plan)} 段")
    frames: list[Frame] = []
    if kind == "video" and not args.no_frames:
        frames = extract_frames(media, out_dir, duration=duration, per_segment=args.frames_per_segment, plan=plan)
        log.step("抽取关键帧", True, f"{len(frames)} 张")
    frame_map = {f.frame_id: f for f in frames}
    frame_ids = list(frame_map)

    # 3) 逐段分析
    sections: list[dict[str, Any]] = []
    if args.mock_notes:
        analysis = mock_notes(title, duration, kind, plan)
        sections = analysis.get("sections") or []
        log.step("生成笔记(mock)", True, f"{len(sections)} 节")
    else:
        if not api_key:
            print(f"失败：未设置环境变量 {DEFAULT_API_KEY_ENV}", file=sys.stderr)
            return 3
        total = len(plan)
        for index, (start, end) in enumerate(plan):
            seg_frames: list[Frame] = []
            if frames:
                seg_frames = [f for f in frames if start <= f.timestamp < end] or frames[:1]
            messages = build_section_messages(
                title=title,
                subject=args.subject,
                audience=args.audience,
                extra=args.extra,
                index=index,
                total=total,
                start=start,
                end=end,
                transcript_text=transcript_for_window(transcript, start, end, total_duration=duration),
                frames=seg_frames if kind == "video" else [],
            )
            print(f"  · 分析第 {index + 1}/{total} 段…", file=sys.stderr)
            try:
                raw_text = chat_completion(
                    base_url=args.base_url, model=args.model, api_key=api_key, messages=messages
                )
                raw = extract_json_block(raw_text)
            except Exception as exc:  # noqa: BLE001
                log.warn(f"第 {index + 1} 段分析失败：{scrub_secret(str(exc))}")
                raw = {}
            sections.append(normalize_section(raw if isinstance(raw, dict) else {}, index, start, end, frame_ids))
        log.step("逐段生成笔记", True, f"{len(sections)} 节")

        (out_dir / "segments.json").write_text(
            json.dumps(sections, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        # 4) 全局合成
        print("  · 合成全局结构…", file=sys.stderr)
        try:
            global_raw = chat_completion(
                base_url=args.base_url,
                model=args.model,
                api_key=api_key,
                messages=build_global_messages(
                    title=title,
                    duration=duration,
                    subject=args.subject,
                    audience=args.audience,
                    extra=args.extra,
                    sections=sections,
                ),
            )
            analysis = extract_json_block(global_raw)
            if not isinstance(analysis, dict):
                raise ValueError("global-not-object")
        except Exception as exc:  # noqa: BLE001
            log.warn(f"全局合成失败，降级为分段直出：{scrub_secret(str(exc))}")
            analysis = {
                "course": {"name": title, "topic": "", "teacher": "[未提供]", "date": "[未提供]", "subject": args.subject or "[未提供]"},
                "headline": "课堂笔记",
                "abstract": "（全局合成失败，以下为分段笔记直出）",
                "framework": [],
                "exam_focus": [],
                "quiz": [],
                "homework": [],
                "further_reading": [],
                "thinking": [],
                "glossary": [],
                "caveats": ["全局合成失败，框架与重点考点未生成，请以逐节笔记为准。"],
            }
        analysis["sections"] = apply_section_order(sections, analysis.get("section_order"))
        analysis["exam_focus"] = merge_exam_points(sections, analysis.get("exam_focus"))
        log.step("全局合成", True, str(len(analysis.get("sections") or [])) + " 节")

    if not analysis.get("exam_focus"):
        analysis["exam_focus"] = merge_exam_points(sections, [])
    analysis.setdefault("course", {})
    (out_dir / "notes-analysis.json").write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # 5) 渲染
    html_text = render_html(
        analysis=analysis,
        title=title,
        kind=kind,
        media=media,
        out_dir=out_dir,
        frames=frame_map,
        transcript=transcript,
        math_enabled=not args.no_math,
        mermaid_enabled=not args.no_mermaid,
    )
    html_path = out_dir / "classroom-notes.html"
    html_path.write_text(html_text, encoding="utf-8")
    log.step("渲染网页", True, html_path.name)

    # 6) 收尾
    issues = validate_html(html_path, out_dir)
    issues += assert_no_secret(out_dir)
    single_note = ""
    if args.single_file:
        try:
            _, single_note = make_single_file(html_path, media, out_dir, args.max_inline_mb)
            log.step("单文件化", True, single_note)
        except Exception as exc:  # noqa: BLE001
            log.warn(f"单文件化失败：{scrub_secret(str(exc))}")

    report = {
        "title": title,
        "media": str(media),
        "kind": kind,
        "duration": duration,
        "duration_label": seconds_to_label(duration),
        "segments": len(analysis.get("sections") or []),
        "frames": len(frames),
        "transcript_backend": transcript.get("backend", "none"),
        "transcript_status": transcript.get("status", "skipped"),
        "model": args.model,
        "single_file": single_note,
        "steps": log.steps,
        "warnings": log.warnings,
        "issues": issues,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    (out_dir / "run-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print("\n=== 完成 ===", file=sys.stderr)
    print(f"笔记网页：{html_path}", file=sys.stderr)
    if single_note:
        print(f"单文件版：{out_dir / html_path.stem}-single.html", file=sys.stderr)
    if issues:
        print(f"校验问题：{issues}", file=sys.stderr)
    print(f"运行报告：{out_dir / 'run-report.json'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
