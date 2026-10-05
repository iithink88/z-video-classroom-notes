#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""z-video-classroom-notes 环境准备。

用法：
    python setup.py            # 装依赖 + 预热 Vosk 中文模型
    python setup.py --doctor   # 只检查不安装
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
TRUSTED = "pypi.tuna.tsinghua.edu.cn"
PACKAGES = ["requests", "vosk", "soundfile"]
VOSK_MODEL = "vosk-model-small-cn-0.22"
VOSK_URL = f"https://alphacephei.com/vosk/models/{VOSK_MODEL}.zip"


def ffmpeg_paths() -> list[Path]:
    found = shutil.which("ffmpeg")
    candidates = []
    if found:
        candidates.append(Path(found))
    candidates += [
        Path.home() / "bin" / "ffmpeg" / "ffmpeg-8.1.2-essentials_build" / "bin" / "ffmpeg.exe",
        Path.home() / "bin" / "ffmpeg" / "ffmpeg-7.1-essentials_build" / "bin" / "ffmpeg.exe",
        Path("C:/ffmpeg/bin/ffmpeg.exe"),
        Path("C:/Program Files/ffmpeg/bin/ffmpeg.exe"),
        Path("/opt/homebrew/bin/ffmpeg"),
        Path("/usr/local/bin/ffmpeg"),
    ]
    return [p for p in candidates if p.exists()]


def check(title: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'OK ' if ok else 'MISS'}] {title}" + (f" — {detail}" if detail else ""))
    return ok


def doctor() -> int:
    print("环境检查")
    print(f"  Python {sys.version.split()[0]}（{sys.executable}）")
    results = []
    ffmpeg = ffmpeg_paths()
    results.append(check("ffmpeg", bool(ffmpeg), str(ffmpeg[0]) if ffmpeg else "未找到，请从 https://ffmpeg.org 安装"))
    if ffmpeg:
        probe = ffmpeg[0].parent / ("ffprobe.exe" if os.name == "nt" else "ffprobe")
        results.append(check("ffprobe", probe.exists() or bool(shutil.which("ffprobe")), str(probe)))
    for pkg in PACKAGES:
        try:
            __import__("soundfile" if pkg == "soundfile" else pkg)
            results.append(check(f"python 包 {pkg}", True))
        except Exception:  # noqa: BLE001
            results.append(check(f"python 包 {pkg}", False, "未安装"))
    model_dir = Path.home() / ".cache" / "vosk-models" / VOSK_MODEL
    results.append(check("Vosk 中文模型", model_dir.exists(), str(model_dir)))
    key = os.environ.get("DASHSCOPE_API_KEY", "")
    results.append(check("环境变量 DASHSCOPE_API_KEY", bool(key), "已设置" if key else "未设置（分析步骤会失败）"))
    print("\n结论：" + ("全部就绪" if all(results) else "有缺失项，运行 `python setup.py` 自动补齐"))
    return 0 if all(results) else 1


def pip_install() -> None:
    print("安装 Python 依赖（清华镜像）")
    for pkg in PACKAGES:
        print(f"  · {pkg}")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-i", MIRROR, "--trusted-host", TRUSTED, pkg],
            check=False,
        )


def warm_vosk() -> None:
    cache_dir = Path.home() / ".cache" / "vosk-models"
    model_dir = cache_dir / VOSK_MODEL
    if model_dir.exists() and any(model_dir.iterdir()):
        print(f"Vosk 模型已存在：{model_dir}")
        return
    import urllib.request
    import zipfile

    cache_dir.mkdir(parents=True, exist_ok=True)
    print(f"下载 Vosk 中文小模型（~42MB）：{VOSK_URL}")
    try:
        with urllib.request.urlopen(VOSK_URL, timeout=600) as resp:
            data = resp.read()
        (cache_dir / f"{VOSK_MODEL}.zip").write_bytes(data)
        with zipfile.ZipFile(cache_dir / f"{VOSK_MODEL}.zip") as zf:
            zf.extractall(cache_dir)
        print(f"模型就绪：{model_dir}")
    except Exception as exc:  # noqa: BLE001
        print(f"模型下载失败（可稍后由主脚本自动重试）：{exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="z-video-classroom-notes 环境准备")
    parser.add_argument("--doctor", action="store_true", help="只检查不安装")
    args = parser.parse_args()
    if args.doctor:
        return doctor()
    doctor()
    pip_install()
    warm_vosk()
    print("\n完成。下一步：")
    print("  export DASHSCOPE_API_KEY='你的key'")
    print("  python scripts/lecture_notes.py <视频网址或本地文件> --title '课程名' --mock-notes --skip-transcribe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
