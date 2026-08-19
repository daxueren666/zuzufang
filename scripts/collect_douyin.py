#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rent-assist: 抖音采集脚本（经 MediaCrawler，本地部署于 E:\\租房\\tools\\MediaCrawler）。

subprocess 调 MediaCrawler（dy 平台 / search 类型 / 开评论 / jsonl 输出），
跑完读它的输出 jsonl，转成本 skill 统一 schema 追加写入 {out-dir}/douyin_*.jsonl。

query 完全由调用方传入，本脚本原样透传给 MediaCrawler 不改写（检索词组合如
"{query} 租房" 是调用方的职责）。结果侧不过滤（清洗层负责）。

用法:
    python collect_douyin.py --query "天通苑" --limit 20 --top-comments 10
    python collect_douyin.py --query "天通苑" --days 180  # 只留近 180 天
    python collect_douyin.py --query "天通苑" --days 7 --sort time  # 近一周最新发布
    python collect_douyin.py --query "天通苑" --get-video 1  # top1 视频口播转写

--days N: 时间窗过滤(默认 0=不过滤)。读 MediaCrawler 输出 jsonl 后按发布时间
    (search_contents 行的 create_time, unix 秒)过滤: 窗口外丢弃; 无时间的保留并计
    time_unknown。
--get-video N: 对讨论 top N(0-3, 默认 0)条视频: 临时开 MediaCrawler
    ENABLE_GET_MEIDAS 下载原片(落其 data/douyin/videos/{aweme_id}/) → 跑完
    try/finally 恢复原配置 → ffmpeg 抽 wav → asr-venv 跑 asr.py(sherpa-onnx
    SenseVoice int8) 转写 → 文本以"【口播转写】"前缀并入该条 record.content
    (总长截 2000), extra.asr=true + extra.video_file。下载/转写失败只警告不判
    采集失败。与 --parallel 互斥(config 补丁会竞争)。
--sort discussion,hot,time 逗号分隔多值(默认 discussion,hot):
    discussion=评论最多(comment_count 降序); hot=最热(liked_count 降序);
    time=发布时间倒序。多键=多队列 union: 每键各取 top K(抖音原逻辑=全部内容行
    都转记录, 即 K=条数)合并去重后按序写出, discussion 队列在前。旧单值 heat
    兼容=discussion,hot。与 --days 可组合(找房源: --days 7 --sort time)。

CDP 无代码防护: MediaCrawler 默认 ENABLE_CDP_MODE=True, 与本机签名管线
    (Node.js execjs)不兼容——重装/更新 MediaCrawler 后该配置回到 True 会让
    采集批次全灭。本脚本每次启动前读 config/base_config.py 检查该项: 为 True
    则备份原文→临时改 False→采集结束 finally 恢复原文件; 已是 False 则不动
    (与 --get-video 的 ENABLE_GET_MEIDAS 补丁共用同一套备份/恢复)。

前置条件:
    - E:\\租房\\tools\\MediaCrawler 已克隆且 .venv 可用（MediaCrawler 要求
      Python >= 3.11，依赖以其 requirements.txt 为准）
    - 抖音需 Node.js >= 16（MediaCrawler 用 pyexecjs 算签名）
    - 首次运行会打开浏览器，需用抖音 App 扫码登录一次（登录态缓存持久化在
      MediaCrawler/browser_data/dy_user_data_dir，之后免扫码；开跑前本脚本
      会探测该缓存并登记到 data/auth_state.json）

退出码: 0 = 至少写出 1 条；2 = 未写出任何记录（环境缺失/登录失败/无结果/超时）。
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import deque
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth_common as ac

parse_pub_datetime = ac.parse_pub_datetime
apply_time_window = ac.apply_time_window
window_stat_seg = ac.window_stat_seg

PLATFORM = "douyin"
DEFAULT_MC_DIR = ac.tools_dir("MediaCrawler")  # RENT_ASSIST_TOOLS 可覆盖，默认回退 ~/.rent-assist/tools
DEFAULT_OUT_DIR = ac.data_dir("raw")  # ~/.rent-assist/data/raw（RENT_ASSIST_DATA 可覆盖）
CONTENT_MAX = 2000          # content 字段最大字符数
MC_TIMEOUT = 1800           # MediaCrawler 单次运行超时（首跑含扫码登录，放宽到 30 分钟）
TAIL_LINES = 40             # 失败时回显的日志末尾行数
PER_POST_COMMENT_FETCH = 3  # MediaCrawler 每帖实际抓取评论数 = top_comments * 3（便于按点赞排序取 top）

# ---- 视频下载 + 口播转写（--get-video，sherpa-onnx 管线详见 asr.py） ----
GET_VIDEO_MAX = 3           # --get-video 上限（每视频几十 MB，控制下载量）
ASR_VENV_PY = ac.tools_dir("asr-venv") / "Scripts" / "python.exe"
ASR_SCRIPT = Path(__file__).resolve().parent / "asr.py"
ASR_TIMEOUT = 900           # 单视频转写超时（含模型加载，int8 CPU）
SPOKEN_PREFIX = "【口播转写】"
# MediaCrawler 原文拼写就是 MEIDAS（项目 typo）；只匹配整行 False，补丁最小化
_ENABLE_MEDIA_RE = re.compile(r"^(?P<indent>[ \t]*)ENABLE_GET_MEIDAS[ \t]*=[ \t]*False[ \t]*$",
                              re.M)
# CDP 模式与本机签名管线不兼容，只匹配整行 True（已是 False 则不动）
_ENABLE_CDP_RE = re.compile(r"^(?P<indent>[ \t]*)ENABLE_CDP_MODE[ \t]*=[ \t]*True[ \t]*$",
                            re.M)


def pick(d, *keys, default=None):
    """从 dict 里取第一个非空键的值（防御字段命名变化）。"""
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def parse_likes(v):
    """把 '1.2万' / '1,234' / 'None' / 123 统一转为 int。"""
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().replace(",", "")
    m = re.match(r"^([\d.]+)\s*([万亿]?)$", s)
    if m:
        mult = {"": 1, "万": 10000, "亿": 100000000}[m.group(2)]
        try:
            return int(float(m.group(1)) * mult)
        except ValueError:
            return 0
    digits = re.sub(r"\D", "", s)
    return int(digits) if digits else 0


def sort_key_of(content, mode):
    """单条内容在某排序键下的键（供 ac.select_union_top 多队列 union 使用）。

    discussion = comment_count；hot = liked_count；
    time = create_time（unix 秒）倒序，缺失排最后。
    """
    return ac.sort_key_for(mode, parse_likes(content.get("comment_count")),
                           parse_likes(content.get("liked_count")),
                           content.get("create_time"))


def safe_query_name(q):
    """文件名安全化：仅保留中文/字母/数字，截 20 字。"""
    s = re.sub(r"[\W_]+", "", q, flags=re.UNICODE)
    return s[:20] or "query"


def out_path(out_dir, query):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / ("%s_%s_%s.jsonl" % (
        PLATFORM, safe_query_name(query), datetime.now().strftime("%Y%m%d")))


# ---------------------------------------------------------------- 环境探测

def rebuild_venv_hint(mc_dir):
    print("[douyin] MediaCrawler venv 缺失或损坏，重建指引（它要求 Python >= 3.11，"
          "requirements.txt 基于 3.11，本机若只有 3.10 需先装 3.11+，勿用 3.10 硬装）:",
          file=sys.stderr)
    print("[douyin]   cd /d \"%s\"" % mc_dir, file=sys.stderr)
    print("[douyin]   py -3.11 -m venv .venv", file=sys.stderr)
    print("[douyin]   .venv\\Scripts\\python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple", file=sys.stderr)
    print("[douyin]   .venv\\Scripts\\playwright install chromium", file=sys.stderr)


def check_env(mc_dir):
    """探测 MediaCrawler 与 venv。返回 (venv_python 或 None)。不抛异常，缺什么报什么。"""
    if not mc_dir.is_dir():
        print("[douyin] 未找到 MediaCrawler 目录: %s" % mc_dir, file=sys.stderr)
        print("[douyin] 重建: git clone https://github.com/NanmiCoder/MediaCrawler \"%s\""
              % mc_dir, file=sys.stderr)
        rebuild_venv_hint(mc_dir)
        return None
    if not (mc_dir / "main.py").is_file():
        print("[douyin] 目录存在但缺少 main.py，疑似克隆不完整: %s" % mc_dir, file=sys.stderr)
        print("[douyin] 重建: 删除该目录后重新 git clone https://github.com/NanmiCoder/MediaCrawler",
              file=sys.stderr)
        return None
    vpy = mc_dir / ".venv" / "Scripts" / "python.exe"
    if not vpy.is_file():
        print("[douyin] 未找到 venv Python: %s" % vpy, file=sys.stderr)
        rebuild_venv_hint(mc_dir)
        return None
    try:  # venv 可用性抽查（缺 playwright/typer 等即视为损坏）
        r = subprocess.run([str(vpy), "-c", "import playwright, httpx, typer"],
                           capture_output=True, timeout=120)
        if r.returncode != 0:
            print("[douyin] MediaCrawler venv 依赖不完整（import 抽查失败）: %s" % vpy,
                  file=sys.stderr)
            rebuild_venv_hint(mc_dir)
            return None
    except (OSError, subprocess.SubprocessError) as e:
        print("[douyin] venv 抽查无法执行（%s），视为损坏: %s" % (e, vpy), file=sys.stderr)
        rebuild_venv_hint(mc_dir)
        return None
    return vpy


def node_hint():
    """抖音签名依赖 Node.js >= 16，缺失只警告不阻塞（交给 MediaCrawler 报错兜底）。"""
    from shutil import which
    if which("node") is None:
        print("[douyin] 警告: 未找到 node，抖音签名可能失败（MediaCrawler 要求 Node.js >= 16）。",
              file=sys.stderr)


def login_state_hint(mc_dir):
    """探测 MediaCrawler 的抖音登录缓存（browser_data/dy_user_data_dir）并登记状态。"""
    d = mc_dir / "browser_data" / "dy_user_data_dir"
    try:
        cached = d.is_dir() and any(d.iterdir())
    except OSError:
        cached = False
    if cached:
        print("[douyin] 复用登录态: 检测到 MediaCrawler 抖音登录缓存(%s)。" % d)
        ac.mark_auth_state("douyin", True, cache=True)
    else:
        print("[douyin] 首跑将弹扫码: 未检测到抖音登录缓存，本次运行 MediaCrawler 会打开"
              "可见浏览器，请用抖音 App 扫码登录；成功后登录态缓存到 browser_data/"
              "dy_user_data_dir（持久化，下次免扫）。", file=sys.stderr)
        ac.mark_auth_state("douyin", False, cache=False)


def refresh_login_state_after_run(mc_dir):
    """MediaCrawler 跑完后若生成了登录缓存，则登记为就绪。"""
    d = mc_dir / "browser_data" / "dy_user_data_dir"
    try:
        cached = d.is_dir() and any(d.iterdir())
    except OSError:
        cached = False
    if cached:
        ac.mark_auth_state("douyin", True, cache=True)
        print("[douyin] 登录缓存已就绪，已登记到 data/auth_state.json。")


# ---------------------------------------------------------------- 调 MediaCrawler

def mc_output_files(mc_dir):
    """MediaCrawler jsonl 输出（追加模式，按日期命名）：data/douyin/jsonl/search_*.jsonl。"""
    data_dir = mc_dir / "data" / "douyin" / "jsonl"
    today = datetime.now().strftime("%Y-%m-%d")
    return (data_dir / ("search_contents_%s.jsonl" % today),
            data_dir / ("search_comments_%s.jsonl" % today))


def count_lines(p):
    try:
        with p.open("rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def read_delta(p, offset):
    """读文件第 offset 行之后的内容（MediaCrawler 追加写，只取本次新增）。"""
    rows = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i < offset:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return rows


def run_mediacrawler(vpy, mc_dir, keywords, limit, per_post_comments):
    """跑 MediaCrawler 抖音搜索；stdout 实时转发到 stderr，返回 (returncode 或 None, 日志末尾)。"""
    cmd = [str(vpy), "main.py",
           "--platform", "dy",
           "--type", "search",
           "--lt", "qrcode",           # 扫码登录（首跑扫码，之后走缓存）
           "--keywords", ",".join(keywords),
           "--get_comment", "true",
           "--get_sub_comment", "false",
           "--crawler_max_notes_count", str(max(1, limit)),
           "--max_comments_count_singlenotes", str(per_post_comments),
           "--save_data_option", "jsonl",
           "--save_data_path", str(mc_dir / "data"),
           "--headless", "false",      # 扫码需要可见浏览器窗口
           ]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    # playwright 浏览器内核装在 E 盘（紧邻 MediaCrawler 的 playwright-browsers/，
    # 避免占 C 盘）；不注入该变量时 playwright 找不到 chromium
    pw_dir = mc_dir.parent / "playwright-browsers"
    if "PLAYWRIGHT_BROWSERS_PATH" not in env and pw_dir.is_dir():
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(pw_dir)
    tail = deque(maxlen=TAIL_LINES)
    try:
        proc = subprocess.Popen(cmd, cwd=str(mc_dir), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=env)
    except OSError as e:
        print("[douyin] 无法启动 MediaCrawler: %s" % e, file=sys.stderr)
        return None, ["启动失败: %s" % e]
    for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").rstrip()
        if line:
            print("[mc] %s" % line, file=sys.stderr)
            tail.append(line)
    try:
        rc = proc.wait(timeout=60)  # stdout 已读完，留缓冲收尾
    except subprocess.TimeoutExpired:
        proc.kill()
        return None, list(tail)
    return rc, list(tail)


# ---------------------------------------------------------------- 视频下载+口播转写

def base_config_path(mc_dir):
    """MediaCrawler 主配置文件路径。"""
    return Path(mc_dir) / "config" / "base_config.py"


def backup_config(path):
    """读配置原文（供 try/finally 文件级恢复）。失败返回 None 并警告。"""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        print("[douyin] 警告: 读 %s 失败（%s），本次无法做配置临时补丁"
              "（CDP 防护/视频下载开关均跳过）。" % (path, e), file=sys.stderr)
        return None


def patch_enable_get_meidas(path, original):
    """把 ENABLE_GET_MEIDAS = False 改 True（整行最小补丁，保留缩进）。

    返回替换处数；0 = 没找到 False 行（可能已 True/拼写变化），配置不动。
    """
    new, n = _ENABLE_MEDIA_RE.subn(
        lambda m: m.group("indent") + "ENABLE_GET_MEIDAS = True", original)
    if not n:
        return 0
    try:
        path.write_text(new, encoding="utf-8")
    except OSError as e:
        print("[douyin] 警告: 改 %s 失败（%s），MediaCrawler 可能不落视频。" % (
            path, e), file=sys.stderr)
        return 0
    return n


def patch_disable_cdp(path, original):
    """把 ENABLE_CDP_MODE = True 改 False（整行最小补丁，保留缩进）。

    MediaCrawler 默认开 CDP 模式，与本机签名管线（Node.js execjs）不兼容——
    重装/更新后配置回到 True 会让采集批次全灭，故每次启动前检查并临时关掉。
    返回 (改后全文, 替换处数)；0 = 没找到 True 行（多半已是 False，配置不动）。
    """
    new, n = _ENABLE_CDP_RE.subn(
        lambda m: m.group("indent") + "ENABLE_CDP_MODE = False", original)
    if not n:
        return original, 0
    try:
        path.write_text(new, encoding="utf-8")
    except OSError as e:
        print("[douyin] 警告: 改 %s 失败（%s），CDP 模式若开着采集可能全灭。"
              % (path, e), file=sys.stderr)
        return original, 0
    return new, n


def restore_config(path, original):
    """文件级恢复配置原文。失败大声警告（需手工还原本次临时改动的开关）。"""
    try:
        path.write_text(original, encoding="utf-8")
        return True
    except OSError as e:
        print("[douyin] 警告: 恢复 %s 失败（%s）！请手工检查并还原本次临时改动的"
              "配置（ENABLE_CDP_MODE / ENABLE_GET_MEIDAS）。" % (path, e), file=sys.stderr)
        return False


def videos_dir(mc_dir):
    """MediaCrawler 抖音视频落盘目录: {SAVE_DATA_PATH}/douyin/videos/{aweme_id}/。"""
    return Path(mc_dir) / "data" / "douyin" / "videos"


def scan_aweme_videos(vdir):
    """扫描 videos/ 下 {aweme_id}/ 目录里的 mp4 → {aweme_id: 最新 mp4 路径}。"""
    out = {}
    try:
        entries = sorted(Path(vdir).iterdir())
    except OSError:
        return out
    for d in entries:
        if not d.is_dir():
            continue
        try:
            vids = [p for p in d.iterdir()
                    if p.is_file() and p.suffix.lower() == ".mp4"]
        except OSError:
            continue
        if vids:
            out[d.name] = max(vids, key=lambda p: p.stat().st_mtime)
    return out


def run_asr(mp4, venv_py=None, timeout=ASR_TIMEOUT):
    """用 asr-venv 的 python 跑 asr.py 转写单视频。

    返回清理后的转写文本；venv/转写/超时失败只 stderr 警告并返回 None
    （不抛出：视频转写是增强项，失败不判采集失败）。txt 落视频旁（E 盘）。
    """
    py = Path(venv_py) if venv_py else ASR_VENV_PY
    if not py.is_file():
        print("[douyin] 警告: asr venv 缺失（%s），跳过转写。安装指引:"
              % py, file=sys.stderr)
        print("[douyin]   python -m venv E:\\租房\\tools\\asr-venv && "
              "PIP_CACHE_DIR=E:\\租房\\tools\\pip-cache "
              "E:\\租房\\tools\\asr-venv\\Scripts\\python -m pip install "
              "sherpa-onnx soundfile numpy", file=sys.stderr)
        return None
    txt = mp4.with_suffix(".txt")
    cmd = [str(py), str(ASR_SCRIPT), "--video", str(mp4), "--out", str(txt)]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        print("[douyin] 警告: 转写超时（>%ss）: %s" % (timeout, mp4.name),
              file=sys.stderr)
        return None
    except OSError as e:
        print("[douyin] 警告: 启动转写失败: %s" % e, file=sys.stderr)
        return None
    if r.returncode != 0 or not txt.is_file():
        err = r.stderr.decode("utf-8", errors="replace").strip()[-600:]
        print("[douyin] 警告: 转写失败（退出码 %s）: %s\n%s" % (
            r.returncode, mp4.name, err), file=sys.stderr)
        return None
    try:
        text = txt.read_text(encoding="utf-8").strip()
    except OSError as e:
        print("[douyin] 警告: 读转写结果失败: %s" % e, file=sys.stderr)
        return None
    if not text:
        print("[douyin] 警告: 转写结果为空（视频可能无人声/纯背景乐）: %s"
              % mp4.name, file=sys.stderr)
        return None
    return text


def merge_spoken_text(record, text, video_file):
    """【口播转写】前缀并入 record.content（总长截 CONTENT_MAX），登记 extra。"""
    record["content"] = (str(record.get("content") or "")
                         + "\n" + SPOKEN_PREFIX + text)[:CONTENT_MAX]
    if not isinstance(record.get("extra"), dict):
        record["extra"] = {}
    record["extra"]["asr"] = True
    record["extra"]["video_file"] = str(video_file)


def attach_spoken_text(records, top_n, mc_dir, before_videos):
    """对讨论 top N 条记录: 匹配本次落盘视频 → asr.py 转写 → 并入 content。

    视频按 aweme_id 匹配本批 record（含本次新增与之前已下载过的，幂等复用）。
    任何一步失败只警告。返回成功转写条数。
    """
    after = scan_aweme_videos(videos_dir(mc_dir))
    new_ids = [a for a in after if a not in before_videos]
    print("[douyin] 视频落盘: 本次新增 %d 个 aweme 目录（存量 %d）"
          % (len(new_ids), len(after) - len(new_ids)))
    if not after:
        print("[douyin] 警告: MediaCrawler 未落任何视频文件（下载失败或开关未生效），"
              "跳过转写。", file=sys.stderr)
        return 0
    done = 0
    for rec in records:
        if done >= top_n:
            break
        aid = str(rec.get("extra", {}).get("aweme_id") or "")
        mp4 = after.get(aid)
        if mp4 is None:
            continue
        print("[douyin] 转写视频 aweme %s（%s）..." % (aid, mp4.name))
        text = run_asr(mp4)
        if text is None:
            continue
        merge_spoken_text(rec, text, mp4)
        done += 1
        print("[douyin] 口播转写已并入 content（%d 字）: aweme %s" % (len(text), aid))
    if done < top_n:
        print("[douyin] 警告: 视频转写完成 %d/%d 条（其余无视频文件或转写失败），"
              "不影响采集结果。" % (done, top_n), file=sys.stderr)
    return done


def check_get_video_args(get_video, parallel):
    """--get-video 参数校验。返回错误消息（None=通过）。"""
    if get_video < 0 or get_video > GET_VIDEO_MAX:
        return "--get-video 取值 0-%d， got %d" % (GET_VIDEO_MAX, get_video)
    if get_video and parallel:
        return ("--get-video 与 --parallel 不能同用（MediaCrawler 配置补丁"
                "并行会竞争读写）")
    return None


# ---------------------------------------------------------------- 结果转换

def iso_from_unix(ts):
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts).isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return None


def group_comments(comment_rows, aweme_ids):
    """评论按 aweme_id 归组 → {text, likes, author} 列表，组内按点赞降序。"""
    by = {}
    for c in comment_rows:
        if not isinstance(c, dict):
            continue
        aid = str(c.get("aweme_id") or "")
        if aid not in aweme_ids:
            continue
        text = str(c.get("content") or "").strip()
        if not text:
            continue
        by.setdefault(aid, []).append({
            "text": text[:CONTENT_MAX],
            "likes": parse_likes(c.get("like_count")),
            "author": str(c.get("nickname") or ""),
        })
    for v in by.values():
        v.sort(key=lambda x: x["likes"], reverse=True)
    return by


def convert_rows(content_rows, comment_rows, keywords, top_comments, query,
                 days=0, sort_spec=None):
    """MediaCrawler douyin 行 → 本 skill 统一 schema。结果侧不做内容过滤（清洗层负责）。

    days>0 时按发布时间(create_time, unix 秒)过滤: 窗口外丢弃, 无时间的保留并计
    time_unknown。随后按 sort_spec 多队列 union 排序（discussion=comment_count；
    hot=liked_count；time=发布时间倒序；K=内容条数，首键队列在前）。
    返回 (records, (kept, dropped, unknown))。
    """
    if sort_spec is None:
        sort_spec = ac.parse_sort_spec(ac.DEFAULT_SORT)
    kwset = set(keywords)
    contents = [c for c in content_rows
                if isinstance(c, dict) and c.get("aweme_id")
                and str(c.get("source_keyword") or "") in kwset]
    if not contents:  # 兼容 source_keyword 缺失的情况：接受全部新增内容行
        contents = [c for c in content_rows if isinstance(c, dict) and c.get("aweme_id")]
    contents, win_stats = apply_time_window(contents, days, "create_time")
    contents = ac.select_union_top(contents, sort_spec, len(contents),
                                   sort_key_of,
                                   item_key=lambda c: str(c["aweme_id"]))
    ids = {str(c["aweme_id"]) for c in contents}
    comments_by = group_comments(comment_rows, ids)

    records, seen = [], set()
    for c in contents:
        aid = str(c["aweme_id"])
        if aid in seen:
            continue
        seen.add(aid)
        cmts = comments_by.get(aid, [])[:max(1, top_comments)]
        extra = {"aweme_id": aid}
        if c.get("aweme_type") not in (None, ""):
            extra["aweme_type"] = str(c.get("aweme_type"))
        if c.get("source_keyword"):
            extra["source_keyword"] = str(c.get("source_keyword"))
        total = parse_likes(c.get("comment_count"))
        if total:
            extra["comment_count_total"] = total
        for k in ("collected_count", "share_count"):
            v = parse_likes(c.get(k))
            if v:
                extra[k] = v
        title = str(pick(c, "title", "desc", default="") or "").strip()
        content = str(pick(c, "desc", "title", default="") or "").strip()
        records.append({
            "platform": PLATFORM,
            "query": query,
            "collected_at": datetime.now().isoformat(timespec="seconds"),
            "url": str(pick(c, "aweme_url", default="") or "").strip(),
            "title": title[:CONTENT_MAX],
            "content": content[:CONTENT_MAX],
            "author": str(pick(c, "nickname", default="") or ""),
            "published_at": iso_from_unix(c.get("create_time")),
            "likes": parse_likes(c.get("liked_count")),
            "comments_count": len(cmts),
            "comments": cmts,
            "extra": extra,
        })
    return records, win_stats


# ---------------------------------------------------------------- 主流程

def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="经 MediaCrawler 采集抖音搜索结果视频及热门评论（追加写 jsonl）")
    ap.add_argument("--query", required=True,
                    help="搜索关键词（必填，原样透传给 MediaCrawler，不做组合改写）")
    ap.add_argument("--limit", type=int, default=20,
                    help="采集视频条数上限（默认 20；联调测试请用 3-5）")
    ap.add_argument("--days", type=int, default=0,
                    help="时间窗过滤天数（默认 0=不过滤）：只保留最近 N 天发布的"
                         "视频（create_time 缺失的保留并计 time_unknown）")
    ap.add_argument("--sort", type=ac.sort_arg_type,
                    default=ac.parse_sort_spec(ac.DEFAULT_SORT),
                    help="记录排序键, 逗号分隔多值（默认 discussion,hot=评论最多∪"
                         "最热两队列合并去重, 讨论队列在前）。discussion=评论数降序；"
                         "hot=点赞降序；time=发布时间倒序（找房源配 --days 7）。"
                         "旧值 heat 兼容=discussion,hot。可与 --days 组合")
    ap.add_argument("--top-comments", type=int, default=10,
                    help="每条视频保留的热门评论条数（默认 10）")
    ap.add_argument("--get-video", type=int, default=0, metavar="N",
                    help="对讨论 top N（0-%d，默认 0=不下载）条视频下载原片并做口播"
                         "转写（sherpa-onnx，本地），文本以【口播转写】前缀并入该条"
                         " record.content（总长截 2000），extra.asr=true。实现: 临时把"
                         " MediaCrawler ENABLE_GET_MEIDAS 改 True，跑完 try/finally 必"
                         "恢复原配置。下载/转写失败只警告，不判采集失败" % GET_VIDEO_MAX)
    ap.add_argument("--parallel", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                    help="输出目录（默认 ~/.rent-assist/data/raw）")
    ap.add_argument("--mc-dir", type=Path, default=DEFAULT_MC_DIR,
                    help="MediaCrawler 目录（默认 %s）" % DEFAULT_MC_DIR)
    ap.add_argument("--timeout", type=int, default=MC_TIMEOUT,
                    help="MediaCrawler 单次运行超时秒数（默认 %d）" % MC_TIMEOUT)
    args = ap.parse_args()

    err = check_get_video_args(args.get_video, args.parallel)
    if err:
        ap.error(err)   # 打 usage 到 stderr 并 exit 2（在任何环境探测之前）

    vpy = check_env(args.mc_dir)
    if vpy is None:
        sys.exit(2)
    node_hint()
    login_state_hint(args.mc_dir)

    # query 原样透传给 MediaCrawler（检索词组合是调用方职责，本脚本不改写）
    keywords = [args.query.strip()]
    print("[douyin] 检索词: %s" % keywords[0])

    contents_p, comments_p = mc_output_files(args.mc_dir)
    # MediaCrawler 按天追加写：记录运行前行数，跑完只读新增部分（同日多次运行互不干扰）
    off_c, off_m = count_lines(contents_p), count_lines(comments_p)

    # 配置防护（共用同一份原文备份，跑完 finally 必恢复；铁律: 动过必还原）:
    # ① CDP 无代码防护: ENABLE_CDP_MODE=True 与本机签名管线不兼容（MediaCrawler
    #    重装/更新后配置回 True 会让采集批次全灭），为 True 时临时改 False；
    #    已是 False 则不动。② --get-video: ENABLE_GET_MEIDAS 临时开 True。
    config_path = base_config_path(args.mc_dir)
    config_backup = None
    before_videos = {}
    if args.get_video > 0:
        before_videos = scan_aweme_videos(videos_dir(args.mc_dir))
    cur = backup_config(config_path)   # None=读失败（已警告），两种补丁都不做
    if cur is not None:
        patched, n_cdp = patch_disable_cdp(config_path, cur)
        if n_cdp:
            config_backup = cur        # 备份必须是补丁前的原文，finally 整体还原
            cur = patched              # 后续 GET_MEIDAS 补丁在已关 CDP 的文本上叠
            print("[douyin] ENABLE_CDP_MODE 检测为 True，已临时改 False（跑完恢复）——"
                  "CDP 模式与本机签名管线不兼容，不关会采集全灭。")
        if args.get_video > 0:
            if config_backup is None:
                config_backup = cur    # CDP 未动过时，cur 即当前原文
            n = patch_enable_get_meidas(config_path, cur)
            print("[douyin] --get-video %d: ENABLE_GET_MEIDAS 已临时开 True"
                  "（补丁 %d 处，跑完恢复）" % (args.get_video, n))
            if n == 0:
                print("[douyin] 警告: base_config.py 未找到 'ENABLE_GET_MEIDAS = False'"
                      " 行（可能已 True 或项目改名），按现状跑。", file=sys.stderr)

    per_post = min(max(args.top_comments * PER_POST_COMMENT_FETCH, 20), 100)
    print("[douyin] 启动 MediaCrawler（limit=%d，每帖抓评 %d 条，超时 %ds）..."
          % (args.limit, per_post, args.timeout))
    try:
        rc, tail = run_mediacrawler(vpy, args.mc_dir, keywords,
                                    max(1, args.limit), per_post)
    finally:
        if config_backup is not None:
            if restore_config(config_path, config_backup):
                print("[douyin] MediaCrawler 配置已恢复原文（ENABLE_CDP_MODE/"
                      "ENABLE_GET_MEIDAS 均还原）")
    refresh_login_state_after_run(args.mc_dir)  # 扫码成功生成缓存后立即登记
    if rc != 0:
        print("[douyin] MediaCrawler 运行失败（%s），日志末尾:\n%s" % (
            "超时" if rc is None else "退出码 %s" % rc,
            "\n".join(tail[-15:]) or "(无输出)"), file=sys.stderr)
        sys.exit(2)

    new_contents = read_delta(contents_p, off_c)
    new_comments = read_delta(comments_p, off_m)
    records, (win_kept, win_dropped, win_unknown) = convert_rows(
        new_contents, new_comments, keywords,
        args.top_comments, args.query.strip(), args.days, args.sort)
    if not records:
        print("[douyin] 未写出任何记录（本次运行无新增结果或 %s；若刚跨零点属正常"
              "日期文件切换）。" % window_stat_seg(
                  args.days, win_kept, win_dropped, win_unknown), file=sys.stderr)
        sys.exit(2)

    asr_done = 0
    if args.get_video > 0:
        asr_done = attach_spoken_text(records, args.get_video, args.mc_dir,
                                      before_videos)

    path = out_path(args.out_dir, args.query)
    with path.open("a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("platform=%s fetched=%d %s %s%s file=%s" % (
        PLATFORM, len(records), ac.sort_seg(args.sort),
        window_stat_seg(args.days, win_kept, win_dropped, win_unknown),
        " asr=%d/%d" % (asr_done, args.get_video) if args.get_video else "",
        path.name))
    sys.exit(0)


if __name__ == "__main__":
    main()
