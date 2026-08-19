#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自测: --get-video 链路（config 补丁/恢复、视频扫描、content 并入、参数校验）
+ asr.wav 路径生成与真实抽 wav（不触网、不跑 MediaCrawler、不装 sherpa-onnx）。

模拟点:
  - collect_douyin.patch_enable_get_meidas / restore_config / backup_config:
    临时 mock base_config.py——False→True 整行最小补丁（保留缩进、只动那一行）、
    文件级恢复后与原文逐字节一致、已是 True 时不改（n=0）、模拟 MediaCrawler
    运行抛错时 finally 也恢复
  - collect_douyin.scan_aweme_videos: videos/{aweme_id}/video.mp4 扫描、
    非 mp4/空目录忽略、目录不存在返回空
  - collect_douyin.merge_spoken_text: 【口播转写】前缀并入、总长截 2000、
    extra.asr / extra.video_file 登记
  - collect_douyin.check_get_video_args: 0-3 合法、4/负数拒绝、与 --parallel 互斥;
    真实子进程验证 ap.error 路径 exit 2 且不触碰 MediaCrawler（校验先于环境探测）
  - asr.wav_path_for: 同视频幂等、不同目录同名不撞、落在 data media/wav 下
  - asr.extract_wav（真实 ffmpeg）: 1 秒静音 mp4 → 16k 单声道 wav，二次调用复用

运行: python scripts/test_asr_offline.py
退出码: 0 全部通过; 1 存在失败。
"""

import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# wav 缓存目录钉到临时目录（须在 import asr/collect_douyin 前设置）
_TD = tempfile.TemporaryDirectory(prefix="rent_asr_test_")
os.environ["RENT_ASSIST_DATA"] = _TD.name

import asr                     # noqa: E402
import collect_douyin as cd    # noqa: E402

PASS, FAIL = 0, 0

FAKE_CONFIG = """# -*- coding: utf-8 -*-
PLATFORM = "xhs"
KEYWORDS = "test"

# Whether to enable crawling media mode
ENABLE_GET_MEIDAS = False

ENABLE_GET_COMMENTS = True
"""


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s" % name)


# ------------------------------------------------- config 补丁/恢复（mock 文件）
def test_config_patch_restore():
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "config" / "base_config.py"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(FAKE_CONFIG, encoding="utf-8")

        # 1) backup → patch: 只动 ENABLE_GET_MEIDAS 那一行，其余原样
        original = cd.backup_config(cfg)
        n = cd.patch_enable_get_meidas(cfg, original)
        patched = cfg.read_text(encoding="utf-8")
        ok_patch = (n == 1
                    and "ENABLE_GET_MEIDAS = True" in patched
                    and "ENABLE_GET_COMMENTS = True" in patched
                    and 'PLATFORM = "xhs"' in patched)

        # 2) restore: 文件级恢复，逐字节一致
        ok_restore = cd.restore_config(cfg, original) \
            and cfg.read_text(encoding="utf-8") == FAKE_CONFIG

        # 3) 模拟 MediaCrawler 运行抛错 → finally 恢复仍生效（手工复现 main 的 try/finally）
        cfg.write_text(FAKE_CONFIG, encoding="utf-8")
        original2 = cd.backup_config(cfg)
        cd.patch_enable_get_meidas(cfg, original2)
        try:
            raise RuntimeError("模拟 MediaCrawler 崩溃")
        except RuntimeError:
            pass
        finally:
            cd.restore_config(cfg, original2)
        ok_crash = cfg.read_text(encoding="utf-8") == FAKE_CONFIG

        # 4) 已是 True 的配置: patch 不改（n=0）
        already_true = FAKE_CONFIG.replace(
            "ENABLE_GET_MEIDAS = False", "ENABLE_GET_MEIDAS = True")
        cfg.write_text(already_true, encoding="utf-8")
        n2 = cd.patch_enable_get_meidas(cfg, cd.backup_config(cfg))
        ok_true = n2 == 0 and cfg.read_text(encoding="utf-8") == already_true

        # 5) 带缩进的 False 行也能补丁（防上游改排版）
        indented = "    ENABLE_GET_MEIDAS = False\n"
        cfg.write_text(indented, encoding="utf-8")
        n3 = cd.patch_enable_get_meidas(cfg, cd.backup_config(cfg))
        ok_indent = n3 == 1 and "    ENABLE_GET_MEIDAS = True" in \
            cfg.read_text(encoding="utf-8")

        # 6) backup 不存在的文件: 返回 None 不抛
        ok_missing = cd.backup_config(Path(td) / "nope.py") is None

        check("config 补丁只动 MEIDAS 行/文件级恢复/崩溃后恢复/已是True不动/缩进兼容/缺失返回None",
              ok_patch and ok_restore and ok_crash and ok_true and ok_indent
              and ok_missing)


# ------------------------------------------------- 视频目录扫描
def test_scan_aweme_videos():
    with tempfile.TemporaryDirectory() as td:
        vdir = Path(td) / "videos"
        for aid, files in {
            "111": ["video.mp4"],          # 正常
            "222": ["video.mp4", "video (1).mp4"],  # 多个取最新
            "333": ["cover.jpeg"],         # 非 mp4 忽略
        }.items():
            d = vdir / aid
            d.mkdir(parents=True)
            for i, f in enumerate(files):
                p = d / f
                p.write_bytes(b"x")
                import time
                os.utime(p, (1700000000 + i, 1700000000 + i))  # 越后越新
        (vdir / "444").mkdir()             # 空目录忽略
        got = cd.scan_aweme_videos(vdir)
        ok = (set(got) == {"111", "222"}
              and got["111"].name == "video.mp4"
              and got["222"].name == "video (1).mp4"     # mtime 最新的
              and cd.scan_aweme_videos(Path(td) / "nope") == {})
        check("scan_aweme_videos 按 aweme_id 扫 mp4/非mp4空目录忽略/取最新/不存在空",
              ok)


# ------------------------------------------------- content 并入
def test_merge_spoken_text():
    rec = {"content": "天通苑租房避坑指南", "extra": {"aweme_id": "123"}}
    cd.merge_spoken_text(rec, "大家好我是天通苑房东", r"E:\v\123\video.mp4")
    ok1 = (rec["content"] == "天通苑租房避坑指南\n【口播转写】大家好我是天通苑房东"
           and rec["extra"]["asr"] is True
           and rec["extra"]["video_file"] == r"E:\v\123\video.mp4")
    # 超长: 总长截 2000（原 content + 换行 + 前缀 + 文本）
    rec2 = {"content": "x" * 1500, "extra": {}}
    cd.merge_spoken_text(rec2, "y" * 900, "v.mp4")
    ok2 = len(rec2["content"]) == 2000 and rec2["content"].startswith("x" * 1500) \
        and "\n【口播转写】" in rec2["content"]
    # 空 content 的 record 也能并入
    rec3 = {"content": "", "extra": None}
    cd.merge_spoken_text(rec3, "口播内容", "v.mp4")
    ok3 = rec3["content"] == "\n【口播转写】口播内容" and rec3["extra"]["asr"] is True
    check("merge_spoken_text 前缀并入/总长截2000/extra登记/空content兼容",
          ok1 and ok2 and ok3)


# ------------------------------------------------- 参数校验
def test_check_get_video_args():
    ok1 = cd.check_get_video_args(0, False) is None \
        and cd.check_get_video_args(3, False) is None \
        and cd.check_get_video_args(1, True) is not None \
        and cd.check_get_video_args(4, False) is not None \
        and cd.check_get_video_args(-1, False) is not None \
        and cd.check_get_video_args(0, True) is None      # 不下载时 parallel 无碍
    check("check_get_video_args 0-3合法/超界拒绝/与--parallel互斥", ok1)


def test_cli_reject_subprocess():
    """真实子进程: 非法 --get-video / --parallel 互斥 → exit 2，且不触碰 MediaCrawler
    （校验先于 check_env 环境探测，stderr 含 usage 与原因）。"""
    cases = [
        ["--query", "t", "--get-video", "5"],
        ["--query", "t", "--get-video", "-1"],
        ["--query", "t", "--get-video", "1", "--parallel"],
    ]
    ok_all = True
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    for argv in cases:
        r = subprocess.run(
            [sys.executable, str(HERE / "collect_douyin.py")] + argv,
            capture_output=True, timeout=120, env=env)
        err = r.stderr.decode("utf-8", errors="replace")
        ok_all = ok_all and r.returncode == 2 and "usage:" in err \
            and "启动 MediaCrawler" not in err
    check("CLI 非法参数 exit 2（usage 报错，不启动 MediaCrawler）", ok_all)


# ------------------------------------------------- wav 路径生成
def test_wav_path_for():
    with tempfile.TemporaryDirectory() as td:
        a1 = Path(td) / "a" / "v.mp4"
        a2 = Path(td) / "b" / "v.mp4"     # 同名不同目录
        for p in (a1, a2):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
        w1a = asr.wav_path_for(a1)
        w1b = asr.wav_path_for(a1)
        w2 = asr.wav_path_for(a2)
        ok = (w1a == w1b                                        # 幂等
              and w1a != w2 and w1a.stem != w2.stem             # 同名不撞
              and w1a.parent == asr.ac.data_dir("media/wav")    # 落 data media/wav
              and w1a.suffix == ".wav")
        check("wav_path_for 幂等/同名不同目录不撞/落data media wav", ok)


# ------------------------------------------------- 真实抽 wav（ffmpeg）
def test_extract_wav_real():
    ffmpeg = asr.ffmpeg_exe()
    if not ffmpeg.is_file():
        print("  [SKIP] 本机无 ffmpeg（%s），跳过真实抽 wav" % ffmpeg)
        return
    with tempfile.TemporaryDirectory() as td:
        mp4 = Path(td) / "silent.mp4"
        r = subprocess.run(
            [str(ffmpeg), "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
             "-f", "lavfi", "-i", "color=c=black:s=320x240:r=10",
             "-t", "1", "-pix_fmt", "yuv420p", "-c:v", "libx264",
             "-c:a", "aac", "-shortest", str(mp4)],
            capture_output=True, timeout=120)
        if r.returncode != 0 or not mp4.is_file():
            print("  [SKIP] 造静音 mp4 失败: %s" % r.stderr.decode(
                "utf-8", errors="replace")[-200:])
            return
        wav = asr.extract_wav(mp4)
        ok_fmt = False
        try:
            with wave.open(str(wav), "rb") as w:
                ok_fmt = (w.getframerate() == 16000          # 16k
                          and w.getnchannels() == 1           # 单声道
                          and w.getnframes() >= 16000 * 0.9)  # 约 1 秒
        except (wave.Error, OSError):
            ok_fmt = False
        mtime1 = wav.stat().st_mtime
        wav2 = asr.extract_wav(mp4)                            # 二次调用复用
        ok_reuse = wav2 == wav and wav.stat().st_mtime == mtime1
        check("extract_wav 真实mp4→16k单声道wav + 二次调用复用缓存",
              ok_fmt and ok_reuse)


def main():
    print("== collect_douyin --get-video #1 ==")
    test_config_patch_restore()
    test_scan_aweme_videos()
    test_merge_spoken_text()
    test_check_get_video_args()
    test_cli_reject_subprocess()
    print("== asr wav #2 ==")
    test_wav_path_for()
    test_extract_wav_real()
    print()
    print("离线自测: %d 通过 / %d 失败" % (PASS, FAIL))
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
