#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""课堂笔记智能体 · 图形界面启动器（零依赖，仅用 Python 标准库）。

把 z-video-classroom-notes 的命令行管线包成一个暖色调、人性化的本地网页：
  · 设置按钮：默认千问大模型(qwen3.7-plus)，支持自定义模型 / Base URL / API Key
  · 输入：拖拽视频或音频，或粘贴视频网址
  · 一键生成：实时滚动进度，完成后在页面内预览笔记网页

架构：本文件是一个极简 HTTP 服务（http.server），前端是同级 index.html。
所有处理都在你本机完成，API Key 仅保存在本机 gui_settings.json，不会外传。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ----------------------------------------------------------------------------
# 路径与目录
# ----------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent                # .../z-video-classroom-notes/gui
SKILL_DIR = HERE.parent                               # .../z-video-classroom-notes
SCRIPTS = SKILL_DIR / "scripts" / "lecture_notes.py"  # 核心管线
INDEX_HTML = HERE / "index.html"
SETTINGS_FILE = HERE / "gui_settings.json"
ROOT = HERE / "output"                                # 生成的笔记统一放这里，/view/ 对外提供
UPLOAD_DIR = ROOT / "uploads"                         # 上传的媒体临时存放

for d in (ROOT, UPLOAD_DIR):
    d.mkdir(parents=True, exist_ok=True)

DEFAULT_SETTINGS = {
    "api_key": "",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "model": "qwen3.7-plus",
    "subject": "",
    "audience": "",
    "extra": "",
    "single_file": True,   # 额外产出内联资源的单文件 HTML，便于分享
    "auto_open": True,     # 生成后自动用默认浏览器打开
}

# Windows 下隐藏子进程控制台窗口（输出仍走管道）
CREATE_NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0


# ----------------------------------------------------------------------------
# 设置读写
# ----------------------------------------------------------------------------
def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        if SETTINGS_FILE.exists():
            s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    # 只返回已知字段，避免脏数据
    return {k: s.get(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS}


def save_settings(s: dict) -> None:
    data = {k: s.get(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS}
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ----------------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------------
def safe_name(name: str) -> str:
    """保留中文与扩展名，去掉 Windows 非法字符。"""
    name = os.path.basename(name or "media")
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip()
    if not name:
        name = "media"
    return name


def slug_dir(title: str) -> str:
    """生成输出子目录名：去非法字符 + 时间戳，保留中文便于识别。"""
    base = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", (title or "课程笔记").strip()) or "课程笔记"
    base = base[:60]
    return f"{base}-{time.strftime('%Y%m%d-%H%M%S')}"


def find_ffmpeg() -> str:
    """提示用户 ffmpeg 位置（脚本自身会探测，这里仅做友好报错）。"""
    cand = (
        Path.home() / "bin" / "ffmpeg" ,
        Path("C:/ffmpeg/bin"),
    )
    return "（脚本会自动在 ~/bin/ffmpeg/*/bin 与 C:/ffmpeg/bin 中探测）"


# ----------------------------------------------------------------------------
# 请求处理
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "ClassroomNotesGUI/1.0"
    # HTTP/1.0：连接级关闭即表示响应结束，避免大请求体触发 curl 的
    # Expect: 100-continue 与 BaseHTTPRequestHandler 不回 100 造成的死锁。
    protocol_version = "HTTP/1.0"

    # 让日志安静一点
    def log_message(self, *args):  # noqa: ANN001, ANN002
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, code: int, text: str, ctype: str = "text/plain; charset=utf-8"):
        self._send(code, text.encode("utf-8", "replace"), ctype)

    # ---- GET ----
    def do_GET(self):
        # http.server 把请求行原始字节按 Latin-1 解码，中文路径会变乱码；
        # 先尝试还原成 UTF-8，兼容浏览器(百分号编码)与 curl(原始 UTF-8) 两种传法。
        try:
            self.path = self.path.encode("latin-1").decode("utf-8")
        except UnicodeDecodeError:
            self.path = self.path.encode("latin-1", "ignore").decode("utf-8", "ignore")
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            if INDEX_HTML.exists():
                self._send(200, INDEX_HTML.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send_text(500, "未找到 index.html，请确认 gui 目录完整。")
            return

        if path == "/api/settings":
            self._send(200, json.dumps(load_settings(), ensure_ascii=False).encode("utf-8"))
            return

        if path.startswith("/view/"):
            self._serve_view(path[len("/view/"):])
            return

        if path == "/favicon.ico":
            self._send(204, b"")
            return

        self._send_text(404, "Not Found")

    # ---- POST ----
    def do_POST(self):
        try:
            self.path = self.path.encode("latin-1").decode("utf-8")
        except UnicodeDecodeError:
            self.path = self.path.encode("latin-1", "ignore").decode("utf-8", "ignore")
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/settings":
            try:
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0)
                data = json.loads(raw.decode("utf-8", "replace"))
                save_settings(data)
                self._send(200, json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                self._send_text(400, f"保存失败：{exc}")
            return

        if path == "/api/run":
            self.handle_run(urllib.parse.parse_qs(parsed.query))
            return

        self._send_text(404, "Not Found")

    # ---- 提供生成的笔记与媒体 ----
    def _serve_view(self, rel: str):
        rel = urllib.parse.unquote(rel).lstrip("/")
        # 防目录穿越
        target = (ROOT / rel).resolve()
        if ROOT.resolve() not in target.parents and target != ROOT.resolve():
            self._send_text(403, "Forbidden")
            return
        if not target.exists() or target.is_dir():
            self._send_text(404, "未找到该文件")
            return
        ctype, _ = mimetypes.guess_type(target.name)
        ctype = ctype or "application/octet-stream"
        try:
            data = target.read_bytes()
        except Exception as exc:  # noqa: BLE001
            self._send_text(500, f"读取失败：{exc}")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---- 运行管线（流式） ----
    def handle_run(self, qs: dict):
        settings = load_settings()
        get = lambda k, d="": (qs.get(k, [d])[0] if qs.get(k) else d)  # noqa: E731

        url = get("url", "").strip()
        title = get("title", "").strip()
        subject = get("subject", "").strip() or settings["subject"]
        audience = get("audience", "").strip() or settings["audience"]
        extra = get("extra", "").strip() or settings["extra"]
        model = get("model", "").strip() or settings["model"] or DEFAULT_SETTINGS["model"]
        base_url = get("base_url", "").strip() or settings["base_url"] or DEFAULT_SETTINGS["base_url"]
        api_key = get("api_key", "").strip() or settings["api_key"]
        single_file = get("single_file", "1") not in ("0", "false", "off")
        mock = get("mock", "0") in ("1", "true", "on")

        # 准备源文件：网址 或 上传的文件
        length = int(self.headers.get("Content-Length", 0) or 0)
        session = slug_dir(title or (Path(url).stem if url else "课程笔记"))
        out_dir = ROOT / session / "notes"
        out_dir.mkdir(parents=True, exist_ok=True)

        source_arg = url
        if not url and length > 0:
            raw_name = get("name", "media")
            fname = safe_name(raw_name)
            if not os.path.splitext(fname)[1]:
                fname += ".mp4"
            up = UPLOAD_DIR / f"{session}__{fname}"
            try:
                with open(up, "wb") as fh:
                    remaining = length
                    while remaining > 0:
                        chunk = self.rfile.read(min(65536, remaining))
                        if not chunk:
                            break
                        fh.write(chunk)
                        remaining -= len(chunk)
                source_arg = str(up.resolve())
                if not title:
                    title = Path(fname).stem
            except Exception as exc:  # noqa: BLE001
                self._stream_error(f"读取上传文件失败：{exc}", out_dir)
                return
        elif not url and length <= 0:
            self._stream_error("请先选择视频/音频文件，或粘贴视频网址。", out_dir)
            return

        if not api_key and not mock:
            self._stream_error(
                "尚未配置 API Key。点击右上角「设置」填入通义千问( DashScope )的 Key，或选 mock 模式试跑。",
                out_dir,
            )
            return

        if not SCRIPTS.exists():
            self._stream_error(f"未找到核心脚本：{SCRIPTS}", out_dir)
            return

        # 组装命令
        cmd = [sys.executable, str(SCRIPTS), source_arg]
        if title:
            cmd += ["--title", title]
        if subject:
            cmd += ["--subject", subject]
        if audience:
            cmd += ["--audience", audience]
        if extra:
            cmd += ["--extra", extra]
        cmd += ["--model", model]
        cmd += ["--base-url", base_url]
        cmd += ["--out-dir", str(out_dir)]
        if single_file:
            cmd += ["--single-file", "--max-inline-mb", "40"]
        if mock:
            cmd += ["--mock-notes", "--skip-transcribe", "--no-frames", "--asr", "none"]

        env = dict(os.environ)
        env["DASHSCOPE_API_KEY"] = api_key
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        # 流式响应
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def emit(line: str):
            try:
                self.wfile.write(("L|" + line + "\n").encode("utf-8", "replace"))
                self.wfile.flush()
            except Exception:
                pass

        emit(f"▶ 准备生成：{title or '课程笔记'}")
        emit(f"· 模型：{model}　Base URL：{base_url}")
        if mock:
            emit("· 试跑模式(mock)：不调模型、不转写、不抽帧，仅验证版式")

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=str(SKILL_DIR),
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as exc:  # noqa: BLE001
            self._stream_error(f"启动管线失败：{exc}", out_dir)
            return

        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line.strip():
                emit(line)
        rc = proc.wait()

        notes_html = out_dir / "classroom-notes.html"
        single_html = out_dir / "classroom-notes-single.html"
        result_file = single_html if (single_file and single_html.exists()) else notes_html
        result_rel = result_file.relative_to(ROOT).as_posix().replace("\\", "/")
        result_url = "/view/" + urllib.parse.quote(result_rel, safe="/")

        report = {}
        report_path = out_dir / "run-report.json"
        if report_path.exists():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except Exception:
                report = {}

        issues = report.get("issues") or []
        ok = rc == 0 and notes_html.exists()

        final = {
            "__final__": True,
            "ok": ok,
            "returncode": rc,
            "url": result_url if ok else "",
            "file": str(result_file) if ok else "",
            "out_dir": str(out_dir),
            "issues": issues,
            "warnings": report.get("warnings", []),
            "session": session,
        }
        try:
            self.wfile.write(("F|" + json.dumps(final, ensure_ascii=False) + "\n").encode("utf-8"))
            self.wfile.flush()
        except Exception:
            pass

        # 自动打开
        if ok and settings.get("auto_open", True):
            try:
                if sys.platform.startswith("win"):
                    os.startfile(str(result_file))  # type: ignore[attr-defined]
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(result_file)])
                else:
                    subprocess.Popen(["xdg-open", str(result_file)])
            except Exception:
                pass

    def _stream_error(self, msg: str, out_dir: Path):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        final = {"__final__": True, "ok": False, "returncode": -1,
                 "url": "", "file": "", "out_dir": str(out_dir), "issues": [msg], "warnings": []}
        try:
            self.wfile.write(("L|❌ " + msg + "\n").encode("utf-8", "replace"))
            self.wfile.write(("F|" + json.dumps(final, ensure_ascii=False) + "\n").encode("utf-8"))
            self.wfile.flush()
        except Exception:
            pass


# ----------------------------------------------------------------------------
# 启动
# ----------------------------------------------------------------------------
def pick_port(start: int = 8765, limit: int = 20) -> int:
    import socket
    for p in range(start, start + limit):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


def main():
    port = pick_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print("=" * 56)
    print("  课堂笔记智能体 · 图形界面已启动")
    print("  在浏览器打开：", url)
    print("  关闭本窗口即可退出。")
    print("=" * 56)
    # 自动打开浏览器
    try:
        if sys.platform.startswith("win"):
            os.startfile(url)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", url])
        else:
            subprocess.Popen(["xdg-open", url])
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
