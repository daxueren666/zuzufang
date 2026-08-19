#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rent-assist: 视频本地转写（sherpa-onnx + SenseVoice-Small int8，触发式）。

视频类口碑帖（小红书/抖音视频笔记、MediaCrawler 下载的抖音视频，或本地
任意视频文件）没有正文可采，用本脚本把口播语音转成文字再进后续管线。
全程本地 CPU 推理（无 torch），不调外部 ASR API。仅在需要处理视频时触发。

用法（CLI 与退出码保持不变）:
    python asr.py --video <path/to/video.mp4>            # 输出同名 .txt
    python asr.py --video <path> --out <path/to/out.txt>
    python asr.py --video-dir <dir>                       # 批量转写目录下所有视频

管线: 视频 --ffmpeg 抽 16k 单声道 wav（缓存 data/media/wav/，已存在跳过）
      -- silero-vad 切语音段 -- SenseVoice int8 逐段识别 -- 拼段、去 <|zh|><|NEUTRAL|>
      等标记、压空白 -- 截断至 <=3000 字写 .txt（UTF-8）。
      stdout 每个视频一行摘要（字数+耗时）。

运行环境（懒加载，缺了不阻断 import，转写时才报）:
  - venv: <tools>/asr-venv（sherpa-onnx + soundfile + numpy，共约 60MB；工具目录
      解析见 auth_common.tools_dir：开发机 E:\\租房\\tools，其余机器 ~/.rent-assist/tools，
      RENT_ASSIST_TOOLS 可覆盖）装法见 INSTALL_HINT；跑本脚本要用该 venv 的 python
  - 模型: <tools>/models/sherpa-onnx-sense-voice-small/
      model_q8.onnx(239MB int8) + tokens.txt + silero-vad/model.onnx
      （env ASR_MODEL_DIR 覆盖模型目录）
  - ffmpeg: env ASR_FFMPEG > 开发机 E:\\ffmpeg\\...\\ffmpeg.exe > PATH 查找

退出码: 0 = 至少成功一个；2 = 无产出（文件/目录不存在、无视频文件、全部失败）；
            3 = 依赖缺失（sherpa-onnx 未安装 / 模型文件不齐 / ffmpeg 不存在）。
"""

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth_common as ac

TEXT_MAX = 3000
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".flv", ".webm", ".m4v", ".ts"}
SAMPLE_RATE = 16000
DEFAULT_MODEL_DIR = ac.tools_dir("models") / "sherpa-onnx-sense-voice-small"
DEFAULT_FFMPEG = Path(r"E:\ffmpeg\ffmpeg-9.0-essentials_build\bin\ffmpeg.exe")
VENV_PY = ac.venv_python(ac.tools_dir("asr-venv"))
# 模型权重候选文件名（ModelScope xiaowangge 包用 model_q8.onnx，
# sherpa-onnx 官方 release 包用 model.int8.onnx）
MODEL_FILE_CANDIDATES = ("model_q8.onnx", "model.int8.onnx", "model.onnx")
FFMPEG_TIMEOUT = 300          # 单视频抽 wav 超时（秒）

INSTALL_HINT = (
    "[asr] 缺少 sherpa-onnx，无法本地转写。安装（无 torch，约 60MB）:\n"
    "[asr]   python -m venv \"%s\"\n"
    "[asr]   PIP_CACHE_DIR=\"%s\" \"%s\" -m pip install sherpa-onnx soundfile numpy \\\n"
    "[asr]     --index-url https://pypi.tuna.tsinghua.edu.cn/simple\n"
    "[asr] 之后用该 venv 的 python 跑本脚本: \"%s\" asr.py --video <mp4>"
) % (VENV_PY.parent.parent, ac.tools_dir("pip-cache"), VENV_PY, VENV_PY)

MODEL_HINT = """\
[asr] 模型文件不齐（目录: %s），缺: %s
[asr] 下载（ModelScope 国内直连，共约 242MB，curl 在 bash 下）:
[asr]   mkdir -p <模型目录>/silero-vad && cd <模型目录>
[asr]   BASE=https://modelscope.cn/models/xiaowangge/sherpa-onnx-sense-voice-small/resolve/main
[asr]   curl -L -o model_q8.onnx "$BASE/model_q8.onnx"
[asr]   curl -L -o tokens.txt "$BASE/tokens.txt"
[asr]   curl -L -o silero-vad/model.onnx "$BASE/silero-vad/model.onnx"
[asr] （env ASR_MODEL_DIR 可覆盖模型目录）"""

FFMPEG_HINT = """\
[asr] ffmpeg 不存在: %s
[asr] 安装 ffmpeg（进 PATH 即可被自动找到）或设 env ASR_FFMPEG 指向可执行文件。
[asr] 开发机在 E:\\ffmpeg\\ffmpeg-9.0-essentials_build\\bin\\ffmpeg.exe（未进 PATH）。"""

_TAG_RE = re.compile(r"<\|[^|>]*\|>")   # SenseVoice 输出的 <|zh|><|NEUTRAL|> 等标记
_WS_RE = re.compile(r"\s+")


def model_dir():
    """模型目录（env ASR_MODEL_DIR 可覆盖）。"""
    return Path(os.environ.get("ASR_MODEL_DIR") or DEFAULT_MODEL_DIR)


def ffmpeg_exe():
    """ffmpeg 路径（env ASR_FFMPEG > 开发机 E 盘绝对路径 > PATH 查找；
    都没有时返回 E 盘默认值供报错提示用）。"""
    env = os.environ.get("ASR_FFMPEG")
    if env:
        return Path(env)
    if DEFAULT_FFMPEG.exists():
        return DEFAULT_FFMPEG
    w = shutil.which("ffmpeg")
    if w:
        return Path(w)
    return DEFAULT_FFMPEG


def find_model_file(md):
    """按候选名找 ASR 权重文件，找不到返回 None。"""
    for name in MODEL_FILE_CANDIDATES:
        p = Path(md) / name
        if p.is_file():
            return p
    return None


def wav_path_for(video):
    """视频对应的 16k wav 缓存路径: {data}/media/wav/<stem>_<hash8>.wav。

    hash 取视频绝对路径 → 同一视频幂等（跳过重抽）、不同目录同名不撞。
    """
    video = Path(video)
    h = hashlib.sha1(str(video.resolve()).encode("utf-8")).hexdigest()[:8]
    return ac.data_dir("media/wav") / ("%s_%s.wav" % (video.stem, h))


def extract_wav(video, wav=None, ffmpeg=None, timeout=FFMPEG_TIMEOUT):
    """ffmpeg 抽 16k 单声道 wav。wav 已存在直接复用。

    成功返回 wav Path；ffmpeg 缺失或执行失败抛 RuntimeError（含 stderr 末尾）。
    """
    video = Path(video)
    wav = Path(wav) if wav else wav_path_for(video)
    if wav.is_file() and wav.stat().st_size > 44:   # 空壳 wav(仅头)不算
        return wav
    exe = Path(ffmpeg) if ffmpeg else ffmpeg_exe()
    if not exe.is_file():
        raise RuntimeError("ffmpeg 不存在: %s" % exe)
    wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(exe), "-y", "-loglevel", "error", "-i", str(video),
           "-vn", "-ar", "16000", "-ac", "1", str(wav)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError("ffmpeg 执行失败: %s" % e)
    if r.returncode != 0 or not wav.is_file() or wav.stat().st_size <= 44:
        tail = " | ".join(r.stderr.decode("utf-8", errors="replace")
                          .strip().splitlines()[-5:])
        raise RuntimeError("ffmpeg 抽 wav 失败（退出码 %s）: %s" % (r.returncode, tail))
    return wav


def load_model():
    """懒加载 sherpa-onnx SenseVoice int8 + silero-vad（首次调用才 import）。

    返回 (recognizer, vad_config)。缺依赖/模型文件: stderr 打指引后 SystemExit(3)。
    """
    try:
        import sherpa_onnx
    except ImportError:
        print(INSTALL_HINT, file=sys.stderr)
        raise SystemExit(3)
    md = model_dir()
    model = find_model_file(md)
    tokens = md / "tokens.txt"
    vad_model = md / "silero-vad" / "model.onnx"
    missing = [str(p) for p in (model, tokens, vad_model) if p is None or not p.is_file()]
    if missing:
        print(MODEL_HINT % (md, ", ".join(missing)), file=sys.stderr)
        raise SystemExit(3)
    recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(model), tokens=str(tokens),
        num_threads=2, use_itn=True)
    vad_config = sherpa_onnx.VadModelConfig()
    vad_config.silero_vad.model = str(vad_model)
    vad_config.silero_vad.min_silence_duration = 0.25
    vad_config.sample_rate = SAMPLE_RATE
    return recognizer, vad_config


def clean_text(raw):
    """清理转写输出: 去模型标记、压空白。"""
    t = _TAG_RE.sub("", str(raw or ""))
    return _WS_RE.sub(" ", t).strip()


def transcribe(recognizer, vad_config, wav):
    """单 wav 转写: silero-vad 切语音段逐段识别后拼接，返回清理后的全文。

    纯静音/无人声 → 无语音段，返回空串（不报错，由调用方按 chars=0 汇报）。
    """
    import numpy as np
    import sherpa_onnx
    import soundfile as sf

    samples, sr = sf.read(str(wav), dtype="float32", always_2d=True)
    samples = samples[:, 0]                       # 单声道
    if len(samples) == 0:
        return ""
    vad = sherpa_onnx.VoiceActivityDetector(vad_config,
                                            buffer_size_in_seconds=100)
    window_size = int(vad_config.silero_vad.window_size)
    min_seg = int(0.1 * SAMPLE_RATE)              # <100ms 碎段跳过
    texts = []

    def drain():
        while not vad.empty():
            seg = np.asarray(vad.front.samples, dtype="float32")
            vad.pop()
            if len(seg) < min_seg:
                continue
            stream = recognizer.create_stream()
            stream.accept_waveform(sr, seg)
            recognizer.decode_stream(stream)
            t = clean_text(stream.result.text)
            if t:
                texts.append(t)

    i, n = 0, len(samples)
    while i + window_size <= n:
        vad.accept_waveform(samples[i:i + window_size])
        i += window_size
        drain()
    if i < n:                                     # 收尾不足一个窗口的样本
        vad.accept_waveform(samples[i:])
    vad.flush()                                   # 逼出最后一个未闭合语音段
    drain()
    return "".join(texts)


def process_one(bundle, video, out):
    """转写单视频并写 txt。返回 (字符数, 耗时秒, 是否截断)。"""
    video, out = Path(video), Path(out)
    t0 = time.time()
    wav = extract_wav(video)
    text = transcribe(bundle[0], bundle[1], wav)
    truncated = len(text) > TEXT_MAX
    if truncated:
        text = text[:TEXT_MAX]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    elapsed = time.time() - t0
    print("[asr] %s -> %s chars=%d elapsed=%.1fs%s" % (
        video.name, out.name, len(text), elapsed,
        " truncated=yes" if truncated else ""))
    return len(text), elapsed, truncated


def collect_videos(video_dir):
    """目录下全部视频文件（按文件名排序）。"""
    d = Path(video_dir)
    return sorted(p for p in d.iterdir()
                  if p.is_file() and p.suffix.lower() in VIDEO_EXTS)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="视频本地转写（sherpa-onnx SenseVoice-Small int8，输出 .txt）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--video", type=Path, help="单个视频文件路径")
    g.add_argument("--video-dir", type=Path,
                   help="批量：转写目录下所有视频（各自输出同目录 .txt）")
    ap.add_argument("--out", type=Path, default=None,
                    help="输出 txt 路径（仅配合 --video；默认同目录同名 .txt）")
    args = ap.parse_args(argv)

    if args.video is not None:
        if not args.video.is_file():
            print("[asr] 视频文件不存在: %s" % args.video, file=sys.stderr)
            return 2
        jobs = [(args.video, args.out or args.video.with_suffix(".txt"))]
    else:
        if not args.video_dir.is_dir():
            print("[asr] 目录不存在: %s" % args.video_dir, file=sys.stderr)
            return 2
        if args.out is not None:
            print("[asr] --out 仅支持配合 --video 单文件使用", file=sys.stderr)
            return 2
        jobs = [(v, v.with_suffix(".txt")) for v in collect_videos(args.video_dir)]
        if not jobs:
            print("[asr] 目录下没有视频文件: %s" % args.video_dir, file=sys.stderr)
            return 2

    if not ffmpeg_exe().is_file():   # 先验依赖再加载（慢），缺 ffmpeg 直接 exit 3
        print(FFMPEG_HINT % ffmpeg_exe(), file=sys.stderr)
        return 3
    bundle = load_model()            # 缺库/缺模型 SystemExit(3)

    done = 0
    for video, out in jobs:
        try:
            process_one(bundle, video, out)
            done += 1
        except SystemExit:
            raise
        except Exception as e:  # 单个失败不崩，跳过并继续
            print("[asr] %s 转写失败跳过: %s" % (video.name, e), file=sys.stderr)
    print("[asr] done=%d/%d" % (done, len(jobs)))
    return 0 if done else 2


if __name__ == "__main__":
    sys.exit(main())
