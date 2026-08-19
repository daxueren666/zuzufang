#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rent-assist: 三平台登录态共享工具（ensure_auth.py / collect_*.py 共用）。

职责:
  - auth_state.json 读写（~/.rent-assist/data/auth_state.json，记录各平台最近一次
    "实测"登录态，只作展示/排障参考，采集脚本每次开跑仍做真实探测）
  - xhs 真实探测：opencli xiaohongshu search 轻搜索一次（limit=1）。
    缓存态不可信——whoami/login 查的是创作者中心缓存，会 already_logged_in
    秒退，但主站搜索仍 AUTH_REQUIRED。只有主站搜索通过才算登录有效。
  - douyin 登录缓存探测：MediaCrawler browser_data/dy_user_data_dir
    （持久化，扫码一次长期有效）
  - douban 匿名可用性探测：1 次 HTTP GET（cat=1019 搜 "test"）

opencli 真实退出码（实测）:
  0   = 成功（登录态有效）
  69  = BROWSER_CONNECT，opencli 未连接到 Chrome（先打开 Chrome 再试）
  77  = AUTH_REQUIRED，主站未登录（需扫码）
  127 = opencli 命令不存在（未安装）
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]


def data_dir(sub: str = "") -> Path:
    """个人数据根目录（隐私：与 skill 目录分离，skill 目录可整体分发）。

    优先环境变量 RENT_ASSIST_DATA，否则 ~/.rent-assist/data。返回前自动创建：
    sub 为空建根目录；sub 无扩展名（目录名，如 "raw"）连子目录一起建；
    sub 带扩展名（文件名，如 "auth_state.json"）只建到其父目录。
    """
    root = Path(os.environ.get("RENT_ASSIST_DATA")
                or (Path(r"E:\租房\data") if Path(r"E:\租房\data").exists()
                    else Path.home() / ".rent-assist" / "data"))
    p = root / sub if sub else root
    try:
        (p if not sub or p.suffix == "" else p.parent).mkdir(
            parents=True, exist_ok=True)
    except OSError:
        pass
    return p


AUTH_STATE_PATH = data_dir("auth_state.json")

DOUYIN_DATA_DIR = Path(r"E:\租房\tools\MediaCrawler\browser_data\dy_user_data_dir")
DOUBAN_PROFILE_DIR = Path(r"E:\租房\tools\douban-profile")

XHS_PROBE_QUERY = "租房"
XHS_PROBE_TIMEOUT = 150        # 浏览器自动化搜索较慢，放宽
DOUBAN_PROBE_URL = "https://www.douban.com/group/search?cat=1019&q=test"
DOUBAN_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

RC_OK = 0
RC_BROWSER_CONNECT = 69
RC_AUTH_REQUIRED = 77
RC_NOT_FOUND = 127


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------- 时间解析/时间窗（四采集脚本共用）
# collect_web/douyin/xhs/douban 原各自逐字重复一份，收敛到此处；脚本内以
# parse_pub_datetime = ac.parse_pub_datetime 等别名保持原调用面不变。
_REL_AGO_RE = re.compile(r"^(\d+)\s*(秒|分钟|分|小时|时|天|日|周|星期|月|年)前$")


def parse_pub_datetime(v):
    """尽力把各平台发布时间解析为本地时区 naive datetime；无法解析返回 None。

    支持: unix 时间戳(秒/毫秒)、ISO(2026-08-01 / 2026-08-01 12:30[:56] /
    带T/Z/小数秒/时区偏移)、2026/8/1、2026.8.1、RFC2822 英文日期、
    中文相对时间(3天前/1周前/昨天/刚刚)、无年份 MM-DD(跨年自动回退一年)、当天 HH:MM。
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, (int, float)):
        ts = float(v)
        if ts <= 0:
            return None
        if ts > 1e12:  # 毫秒时间戳
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts)
        except (OSError, OverflowError, ValueError):
            return None
    s = str(v).strip()
    if not s or s.upper() in ("N/A", "NA", "UNKNOWN"):
        return None
    now = datetime.now()
    if s in ("刚刚", "刚", "今天"):
        return now
    if s == "昨天":
        return now - timedelta(days=1)
    if s == "前天":
        return now - timedelta(days=2)
    m = _REL_AGO_RE.match(s)
    if m:
        n = int(m.group(1))
        try:
            return now - {
                "秒": timedelta(seconds=n), "分钟": timedelta(minutes=n),
                "分": timedelta(minutes=n), "小时": timedelta(hours=n),
                "时": timedelta(hours=n), "天": timedelta(days=n),
                "日": timedelta(days=n), "周": timedelta(weeks=n),
                "星期": timedelta(weeks=n), "月": timedelta(days=30 * n),
                "年": timedelta(days=365 * n),
            }[m.group(2)]
        except OverflowError:
            return None
    try:  # RFC2822, 如 "Mon, 26 Aug 2024 08:00:00 GMT"
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if isinstance(dt, datetime):
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
            return dt
    except (ValueError, TypeError, ImportError):
        pass
    iso = re.sub(r"(\d)[Tt](\d)", r"\1 \2", s)         # T 分隔 -> 空格
    iso = re.sub(r"\s*[Zz]$", "", iso)                 # 去 Z 后缀
    iso = re.sub(r"(:\d{2})\.[0-9]+", r"\1", iso)     # 去小数秒(仅秒位后)
    iso = re.sub(r"\s*[+-]\d{2}:?\d{2}$", "", iso)     # 去时区偏移(按本地时区近似)
    iso = iso.replace("/", "-").replace(".", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y%m%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(iso, fmt)
        except ValueError:
            continue
    m = re.match(r"^(\d{1,2})[-/.月](\d{1,2})日?$", s)
    if m:  # 无年份 MM-DD: 按今年算, 落在将来(跨年帖)则回退一年
        try:
            dt = datetime(now.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
        if dt > now + timedelta(days=2):
            dt = dt.replace(year=now.year - 1)
        return dt
    if re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", s):  # 当天时刻
        return now
    return None


def apply_time_window(items, days, time_key):
    """--days>0 时按 time_key 发布时间过滤: 解析成功且在窗口外丢弃;
    解析失败/缺失保留并计 time_unknown。返回 (kept_items, (kept, dropped, unknown))。"""
    if days <= 0:
        return items, (len(items), 0, 0)
    now = datetime.now()
    kept, dropped, unknown = [], 0, 0
    for it in items:
        dt = parse_pub_datetime(it.get(time_key))
        if dt is None:
            unknown += 1
            kept.append(it)
        elif dt >= now - timedelta(days=days):
            kept.append(it)
        else:
            dropped += 1
    return kept, (len(kept), dropped, unknown)


def window_stat_seg(days, kept, dropped, unknown):
    """摘要行时间窗统计段（--days 0 时显示 window=off）。"""
    if days <= 0:
        return "window=off"
    return "window=%dd kept=%d dropped=%d time_unknown=%d" % (
        days, kept, dropped, unknown)


# ---------------------------------------------------------------- 排序多键
# --sort 语义(v2, 四采集脚本统一): 逗号分隔多值, discussion/hot/time 任意组合。
#   discussion = comments_count 降序（"评论最多", 口碑/讨论类 A/B/C 意图主队列）
#   hot        = likes 降序（"最热"）
#   time       = published_at 降序（找房源 D 意图, 配 --days 7）
#   heat       = 旧单值兼容别名, 映射为 discussion,hot（原 heat 拆成这两个语义）
# 多键 = 多队列 union: 每个排序键各取 top K 合并去重后统一处理（讨论最重要,
# 两种都取; K=原逻辑的补详情数, 去重后可能 5-9 条）。
SORT_KEYS = ("discussion", "hot", "time", "heat")
SORT_MODES = SORT_KEYS            # 旧名兼容
DEFAULT_SORT = "discussion,hot"


def parse_sort_spec(spec):
    """解析 --sort 多值为规范化键元组: 逗号分隔、去重保序、heat→(discussion, hot)。

    非法/空值抛 ValueError（argparse 层经 sort_arg_type 转参数报错）。
    """
    out = []
    for m in str(spec or "").split(","):
        m = m.strip()
        if not m:
            continue
        if m not in SORT_KEYS:
            raise ValueError("未知排序键 '%s'（可选: %s）" % (m, ",".join(SORT_KEYS)))
        for e in (("discussion", "hot") if m == "heat" else (m,)):
            if e not in out:
                out.append(e)
    if not out:
        raise ValueError("排序键不能为空")
    return tuple(out)


def sort_arg_type(text):
    """argparse --sort 的 type= 入口: 非法值转 ArgumentTypeError。"""
    import argparse
    try:
        return parse_sort_spec(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def sort_key_for(mode, comments_count, likes, published_at):
    """单排序键（四采集脚本统一口径，配合 reverse=True 降序使用）:
    discussion = comments_count（评论最多）
    hot        = likes（最热）
    time       = published_at 倒序（经 parse_pub_datetime 解析，失败/缺失排最后）
    heat       = comments_count*2 + likes（旧口径, 仅供兼容直传单键的调用方）
    comments_count/likes 需为可 int() 的数值（各平台先用 parse_likes 归一）。
    """
    if mode == "time":
        return (parse_pub_datetime(published_at) or datetime.min,)
    if mode == "hot":
        return (max(int(likes or 0), 0),)
    if mode == "discussion":
        return (max(int(comments_count or 0), 0),)
    return (max(int(comments_count or 0), 0) * 2 + max(int(likes or 0), 0),)


def sort_keys_for(spec, comments_count, likes, published_at):
    """多键生成: 返回 {key_name: sort_key}。spec 接受已解析元组或原始字符串。"""
    modes = spec if isinstance(spec, tuple) else parse_sort_spec(spec)
    return {m: sort_key_for(m, comments_count, likes, published_at)
            for m in modes}


def select_union_top(items, modes, k, sort_key, item_key=None):
    """多队列 union 选帖（四采集脚本共用的"选哪些帖子补详情/评论"环节）。

    对每个排序键各取 top k（降序），按 modes 顺序合并去重（前面的队列优先,
    如 discussion 在前则讨论队列排前）。k >= len(items) 时退化为首键全量序。
      sort_key(item, mode) -> 可比较键（降序）
      item_key(item)       -> 去重标识, 默认 id()
    返回合并去重后的新列表, 不改动 items。
    """
    k = max(1, int(k or 1))
    out, seen = [], set()
    for mode in modes:
        ranked = sorted(items, key=lambda it, m=mode: sort_key(it, m),
                        reverse=True)
        for it in ranked[:k]:
            marker = item_key(it) if item_key is not None else id(it)
            if marker in seen:
                continue
            seen.add(marker)
            out.append(it)
    return out


def sort_seg(modes):
    """摘要行排序段: sort=discussion,hot（实际生效值）。"""
    return "sort=%s" % ",".join(modes)


# ---------------------------------------------------------------- auth_state
def load_auth_state():
    """读 data/auth_state.json，损坏/不存在返回 {}。"""
    try:
        with AUTH_STATE_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_auth_state(state):
    AUTH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_STATE_PATH.with_name(AUTH_STATE_PATH.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(str(tmp), str(AUTH_STATE_PATH))


def update_auth_state(platform, ok, **extra):
    """记录某平台最近一次实测结果，返回写入的条目。"""
    state = load_auth_state()
    entry = {"ok": bool(ok), "verified_at": now_iso()}
    entry.update(extra)
    state[platform] = entry
    save_auth_state(state)
    return entry


def mark_auth_state(platform, ok, **extra):
    """采集脚本用：更新登录态，失败只警告不抛（不能因状态文件问题中断采集）。"""
    try:
        return update_auth_state(platform, ok, **extra)
    except OSError as e:
        print("[auth] 警告: auth_state.json 更新失败: %s" % e, file=sys.stderr)
        return None


# ---------------------------------------------------------------- xhs 真实探测
def run_xhs_probe():
    """跑一次 opencli xiaohongshu search 轻搜索（limit=1）。

    返回 (rc, stdout, stderr)；opencli 缺失时 rc=127。
    """
    exe = shutil.which("opencli")
    if exe is None:
        return RC_NOT_FOUND, "", "opencli not found in PATH"
    try:
        r = subprocess.run(
            [exe, "xiaohongshu", "search", XHS_PROBE_QUERY,
             "-f", "json", "--limit", "1"],
            capture_output=True, timeout=XHS_PROBE_TIMEOUT)
        return (r.returncode,
                r.stdout.decode("utf-8", errors="replace"),
                r.stderr.decode("utf-8", errors="replace"))
    except subprocess.TimeoutExpired:
        return 1, "", "probe timeout after %ss" % XHS_PROBE_TIMEOUT
    except OSError as e:
        return 1, "", "probe failed: %s" % e


def xhs_probe_status():
    """探测并归类。返回 (status, rc, blob)。

    status: 'ok' | 'auth_required' | 'browser_missing' | 'opencli_missing'
            | 'unknown'
    """
    rc, out, err = run_xhs_probe()
    blob = ((err or "") + " " + (out or "")).strip()
    if rc == RC_OK:
        return "ok", rc, blob
    if rc == RC_AUTH_REQUIRED or "AUTH_REQUIRED" in blob:
        return "auth_required", rc, blob
    if rc == RC_BROWSER_CONNECT or "BROWSER_CONNECT" in blob:
        return "browser_missing", rc, blob
    if rc == RC_NOT_FOUND:
        return "opencli_missing", rc, blob
    return "unknown", rc, blob


# ---------------------------------------------------------------- douyin 缓存
def douyin_cache_exists(data_dir=None):
    """MediaCrawler 抖音登录缓存目录存在且非空。"""
    d = Path(data_dir) if data_dir is not None else DOUYIN_DATA_DIR
    try:
        return d.is_dir() and any(d.iterdir())
    except OSError:
        return False


# ---------------------------------------------------------------- douban 探测
def probe_douban_http(timeout=15, url=None):
    """豆瓣匿名可用性探测（1 次轻请求）。返回 HTTP 状态码；异常返回 None。"""
    try:
        import requests
    except ImportError:
        return None
    try:
        r = requests.get(url or DOUBAN_PROBE_URL,
                         headers={"User-Agent": DOUBAN_UA}, timeout=timeout)
        return r.status_code
    except Exception:
        return None


# ---------------------------------------------------------------- douban 登录态
# ensure_auth.py / collect_douban.py 共用：cookie 判定（纯函数）、持久化 context
# 启动、轮询等待人工登录。核心坑：豆瓣登录凭证是 HttpOnly cookie，页面
# document.cookie 看不到，必须用 playwright context.cookies() 的结果判定；
# 且现行凭证名为 dbcl2（实测 douban-profile 落盘），dbclv 为旧名，两者兼容。
DOUBAN_HOME_URL = "https://www.douban.com/"
DOUBAN_LOGIN_WAIT = 180        # 人工登录轮询等待上限（秒）
DOUBAN_POLL_INTERVAL = 5       # 轮询间隔（秒）
LOCAL_BROWSERS_PATH = Path(r"E:\租房\tools\playwright-browsers")


def ensure_playwright_browsers_path():
    """本机浏览器内核统一放 E 盘(磁盘纪律): 未显式设置且目录存在时注入
    PLAYWRIGHT_BROWSERS_PATH，否则 launch 报 Executable doesn't exist。"""
    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH") \
            and LOCAL_BROWSERS_PATH.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(LOCAL_BROWSERS_PATH)


def non_interactive():
    """True = 无 stdin / 非终端(管道、分离进程): 无人可等，绝不能 input()。"""
    try:
        return sys.stdin is None or not sys.stdin.isatty()
    except Exception:
        return True


def douban_logged_in(cookies):
    """dbcl2(现行)/dbclv(旧名) 登录凭证 cookie 存在且非空视为已登录（纯函数）。

    入参为 cookie 字典列表（playwright context.cookies() 的返回）。
    """
    for c in cookies or []:
        if isinstance(c, dict) and c.get("name") in ("dbclv", "dbcl2") \
                and c.get("value"):
            return True
    return False


def douban_context_logged_in(context):
    """用 playwright context.cookies() 判豆瓣登录态；异常按未登录处理。"""
    try:
        return douban_logged_in(context.cookies("https://www.douban.com/"))
    except Exception:
        return False


def launch_douban_profile(p, headless, profile_dir=None):
    """开豆瓣持久化 context（cookie/localStorage 落盘，登录一次后续免登录）。

    p 为 sync_playwright() 实例；profile_dir 缺省用 DOUBAN_PROFILE_DIR，
    目录不存在时 playwright 自动创建。
    """
    return p.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir or DOUBAN_PROFILE_DIR), headless=headless,
        user_agent=DOUBAN_UA, locale="zh-CN",
        viewport={"width": 1280, "height": 800})


def wait_douban_login(context, timeout=DOUBAN_LOGIN_WAIT,
                      interval=DOUBAN_POLL_INTERVAL):
    """轮询 context.cookies() 等 dbclv 出现（人工登录完成），不碰 input()。

    返回 True=检测到登录；False=超时。先查后睡，登录完成立即返回。
    """
    deadline = time.monotonic() + max(0, timeout)
    while True:
        if douban_context_logged_in(context):
            return True
        remain = deadline - time.monotonic()
        if remain <= 0:
            return False
        time.sleep(min(interval, remain))
