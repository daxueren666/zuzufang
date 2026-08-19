#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_collect.py — 批次采集编排器（把单轮 --limit 20 的上限攒到几百条量级）。

问题：collect_xhs / collect_douban / collect_douyin / collect_web 单次运行
上限 20 条，单轮总量上不去。
方案：外层遍历 queries、内层遍历 platforms，每个 (query, platform) 组合以
--limit 20 跑一次对应采集脚本；批次间随机休眠；每批后重扫近 7 天 jsonl 按 URL
精确去重统计新增；进度账本 data/progress/<slug>.json 支持中断续采。

缓存复用（跨天去重 + 强制复用闸门）：去重基线扫描近 REUSE_WINDOW_DAYS=7 天的
raw jsonl（文件名尾部 _YYYYMMDD 日期优先，无日期回退文件 mtime），昨天的
URL 今天不重复计数也不重复爬；启动闸门——某 (platform, query) 近 7 天已采
URL ≥ --reuse-min（默认 10）→ 跳过该组合并在启动摘要打印 [复用] 明细，
--refresh 忽略全部复用强制重采（--parallel 下透传给各 worker）。账本 done
记录带时间戳，超过 7 天视为失效可重跑（旧格式无时间戳视为有效，打印提示）。

目标量（两者至少给一个，可共存）：
  --per-platform N  每平台采集上限（语义明确，推荐）：某平台累计达到 N 即停
                    该平台；给了它，平台配额以它为准，不按 --target 均分。
  --target N        总量停止线：所有平台累计和达到 N 即全部收队；未配
                    --per-platform 时兼作配额来源（按平台数均分 ceil）。

停止规则（按平台）：达到配额 / 连续 2 批新增率 <10% / 连续 3 批失败 /
查询耗尽（未达 --min-per-platform 时依次启用 --extra-queries）。
子进程退出码：0 正常；3 需登录→整体中止（先跑 ensure_auth.py）；4 数据质量差
→记 degraded 继续；2 及其他→记失败继续（不记 done，重跑时会重试）。

--parallel 分平台并行：每平台一个 worker 子进程（本脚本的单平台串行模式），
各自独立账本 progress/<slug>/<platform>.jsonl 与 worker 日志（auth_state.json
亦按平台隔离，避免多进程并发写坏）；同平台内仍串行批次+随机间隔（频控不变），
并行只发生在不同平台间；抖音 worker 用 MediaCrawler venv 的 python。主进程等
全部 worker 后汇总各平台摘要与退出码，单平台失败不中断汇总。注意：--target
的总量停止线在并行模式下不跨平台联动（已折算进各平台配额，由 worker 独立判断）。

用法（queries 由 SKILL.md 工作流从用户问题生成后传入）：
  python scripts/run_collect.py --per-platform 100 --parallel \
      --queries "天通苑租房,天通苑小区推荐,天通苑二房东,天通苑房东直租" \
      --platforms "xhs,douban,web,douyin" --days 180 --sort "discussion,hot" \
      --min-per-platform 20
  python scripts/run_collect.py --target 300 ...   # 总量停止线（兼容旧用法）

本脚本退出码：0=正常结束（允许部分批次失败）；3=子脚本需登录（已中止）；
130=用户中断；1=参数/环境错误。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))
import auth_common as ac  # noqa: E402

DEFAULT_OUT_DIR = ac.data_dir("raw")            # ~/.rent-assist/data/raw
DEFAULT_PROGRESS_DIR = ac.data_dir("progress")  # ~/.rent-assist/data/progress

KNOWN_PLATFORMS = ("xhs", "douban", "web", "douyin")

BATCH_SLEEP_RANGE = (10.0, 30.0)   # 相邻批次间基础休眠秒数
SAME_PLATFORM_SLEEP = (5.0, 10.0)  # 同平台连续批次额外休眠秒数
LOW_GAIN_RATIO = 0.10              # 单批新增率阈值（新增/本批 fetched）
LOW_GAIN_STREAK = 2                # 连续低新增批数 → 停该平台
REPEATED_FAIL = 3                  # 同平台连续失败批数 → 停该平台
HASH_INLINE_MAX = 2000             # 账本内联 URL 哈希上限，超过则外置文件
REUSE_WINDOW_DAYS = 7              # 缓存复用/去重基线窗口（天），也是 done 失效期
DEFAULT_REUSE_MIN = 10             # 复用闸门：组合近 7 天已采 URL 数达到即跳过

RC_LOGIN = 3
RC_BAD_DATA = 4


# ---------------------------------------------------------------- 小工具

def split_list(s):
    """逗号（半角/全角）分隔列表，去空去重保序。"""
    out, seen = [], set()
    for x in re.split(r"[,，]", s or ""):
        x = x.strip()
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def safe_name(q):
    """标的 slug：与采集脚本 safe_query_name 同口径（汉字/字母/数字，截 20）。"""
    s = re.sub(r"[\W_]+", "", q or "", flags=re.U)
    return s[:20] or "query"


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def sha1_of(url):
    return hashlib.sha1(url.encode("utf-8", "ignore")).hexdigest()


def log_exc_detail(tag, where, e, summary=None):
    """宽泛异常兜底：完整 traceback 落 data/logs/run_collect_<tag>_<时间戳>.log，
    stderr 只留一行中文摘要（带日志路径）；写日志本身失败则降级只打 stderr。
    tag 用 slug（--parallel worker 的 slug=平台名，且 RENT_ASSIST_DATA 已按平台
    隔离，各 worker 日志天然分文件防互踩）。"""
    try:
        d = ac.data_dir("logs")  # data_dir 自带建目录
        path = d / ("run_collect_%s_%s.log"
                    % (safe_name(tag or "run"), datetime.now().strftime("%Y%m%d_%H%M%S")))
        with path.open("a", encoding="utf-8") as fh:
            fh.write("[%s] %s: %r\n" % (now_iso(), where, e))
            traceback.print_exc(file=fh)
        print("%s: %s（详情: %s）" % (summary or "[异常] " + where, e, path),
              file=sys.stderr)
    except Exception:
        print("%s: %s（写日志失败，仅此摘要）" % (summary or "[异常] " + where, e),
              file=sys.stderr)


def err_tail(err):
    lines = [l.strip() for l in (err or "").splitlines() if l.strip()]
    return (lines[-1] if lines else "")[:80]


def _file_within_days(f, today, days):
    """近 N 天判定：文件名尾部 _YYYYMMDD 里的日期优先，无日期（或解析失败）
    回退文件 mtime；未来日期按今天算（容忍时钟微小偏差）。"""
    d = None
    m = re.search(r"_(\d{8})\.jsonl$", f.name)
    if m:
        try:
            d = datetime.strptime(m.group(1), "%Y%m%d").date()
        except ValueError:
            d = None
    if d is None:
        try:
            d = datetime.fromtimestamp(f.stat().st_mtime).date()
        except OSError:
            return False
    return 0 <= (today - d).days < days


def scan_recent_urls(out_dir, platforms, queries, days=REUSE_WINDOW_DAYS):
    """重扫 out-dir 近 N 天 jsonl（<platform>_*_YYYYMMDD.jsonl，文件名日期优先、
    无日期用 mtime），按 URL 精确集合返回 {platform: set(url)}（只计 query 属于
    本次查询集的记录）、{(platform, query): set(url)} 与文件名列表。"""
    per = {p: set() for p in platforms}
    pairs = {}
    files = []
    if not isinstance(out_dir, Path) or not out_dir.is_dir():
        return per, pairs, files
    today = datetime.now().date()
    qset = set(queries)
    for f in sorted(out_dir.glob("*.jsonl")):
        plat = f.name.split("_", 1)[0]
        if plat not in per:
            continue
        if not _file_within_days(f, today, days):
            continue
        files.append(f.name)
        try:
            fh = f.open("r", encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                url = rec.get("url")
                q = rec.get("query")
                if url and q in qset:
                    per[plat].add(url)
                    pairs.setdefault((plat, q), set()).add(url)
    return per, pairs, files


# ---------------------------------------------------------------- 进度账本

class Ledger(object):
    """data/progress/<slug>.json：done 组合 + 各平台累计 + URL 哈希去重集合。

    done 记录带时间戳 [platform, query, iso]，超过 REUSE_WINDOW_DAYS 天视为
    失效可重跑；旧格式 [platform, query] 无时间戳，视为有效（打印一行提示，
    下次保存起自动补时间戳）。哈希集合超过 HASH_INLINE_MAX 时外置到
    <slug>.urls 文件，账本只存路径。账本损坏（非法 JSON/结构不对/哈希文件
    缺失）→ stderr 警告并降级为全跑（重建账本）。
    """

    def __init__(self, path, slug):
        self.path = path
        self.slug = slug
        self.done = set()        # {(platform, query)}
        self.done_at = {}        # {(platform, query): 完成时间 iso}，旧格式无
        self.collected = {}      # {platform: 累计去重条数}
        self.batches = {}        # {platform: 累计批次数}
        self.degraded = {}       # {platform: 累计降级批次数}
        self.hashes = set()
        self.hashes_file = None

    def mark_done(self, platform, query):
        """记一个组合完成（时间取首次完成时刻，重复调用不刷新）。"""
        pair = (platform, query)
        self.done_at.setdefault(pair, now_iso())
        self.done.add(pair)

    @classmethod
    def load(cls, path, slug):
        led = cls(path, slug)
        if not path.exists():
            return led
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("账本不是 JSON 对象")
            done = data["done"]
            collected = data["collected"]
            if not isinstance(done, list) or not isinstance(collected, dict):
                raise ValueError("done/collected 结构不对")
            cutoff = datetime.now() - timedelta(days=REUSE_WINDOW_DAYS)
            legacy = expired = 0
            for e in done:
                if not isinstance(e, (list, tuple)) or len(e) < 2:
                    raise ValueError("done 条目结构不对: %r" % (e,))
                pair = (str(e[0]), str(e[1]))
                ts = str(e[2]) if len(e) >= 3 else None
                if ts is not None:
                    try:
                        finished = datetime.fromisoformat(ts)
                    except ValueError:
                        ts = None               # 解析失败按旧格式处理（有效）
                    else:
                        if finished < cutoff:   # 超 7 天：失效可重跑
                            expired += 1
                            continue
                        led.done_at[pair] = ts
                if ts is None:
                    legacy += 1
                led.done.add(pair)
            if legacy:
                print("[提示] 账本 %d 条 done 记录无时间戳（旧格式，视为有效；"
                      "本次保存起补记）。" % legacy)
            if expired:
                print("[提示] 账本 %d 条 done 记录超过 %d 天，已失效可重跑。"
                      % (expired, REUSE_WINDOW_DAYS))
            led.collected = {str(k): int(v) for k, v in collected.items()}
            led.batches = {str(k): int(v) for k, v in (data.get("batches") or {}).items()}
            led.degraded = {str(k): int(v) for k, v in (data.get("degraded") or {}).items()}
            hf = data.get("dedup_hashes_file")
            if hf:
                hp = Path(hf)
                if hp.is_file():
                    led.hashes = {l.strip() for l in
                                  hp.read_text(encoding="utf-8").splitlines() if l.strip()}
                    led.hashes_file = str(hp)
                else:
                    print("[警告] 账本引用的哈希文件缺失(%s)，去重基线将重建自近%d天 jsonl。"
                          % (hf, REUSE_WINDOW_DAYS))
            else:
                hs = data.get("dedup_hashes")
                if isinstance(hs, list):
                    led.hashes = {str(x) for x in hs}
        except Exception as e:
            print("[警告] 进度账本损坏(%s: %s)，降级为全跑（已采集数据不丢，仅组合重试）。"
                  % (path.name, e))
            log_exc_detail(slug, "进度账本损坏", e,
                           summary="[警告] 账本损坏，已忽略并重建")
            return cls(path, slug)
        return led

    def save(self):
        if self.hashes_file is None and len(self.hashes) > HASH_INLINE_MAX:
            self.hashes_file = self.path.stem + ".urls"
        hashes_field = []
        if self.hashes_file:
            hp = self.path.parent / self.hashes_file
            tmp = self.path.parent / (hp.name + ".tmp")
            tmp.write_text("\n".join(sorted(self.hashes)) + "\n", encoding="utf-8")
            tmp.replace(hp)
        else:
            hashes_field = sorted(self.hashes)
        data = {
            "slug": self.slug,
            "updated_at": now_iso(),
            "done": sorted([p, q, self.done_at.get((p, q)) or now_iso()]
                           for (p, q) in self.done),
            "collected": {k: int(v) for k, v in sorted(self.collected.items())},
            "batches": {k: int(v) for k, v in sorted(self.batches.items())},
            "degraded": {k: int(v) for k, v in sorted(self.degraded.items())},
            "dedup_hashes": hashes_field,
            "dedup_hashes_file": self.hashes_file,
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)


# ---------------------------------------------------------------- 子进程

def run_one_batch(scripts_dir, platform, query, args):
    """跑一次 collect_<platform>.py，返回 (rc, stdout, stderr, 耗时秒, cmd)。"""
    script = Path(scripts_dir) / ("collect_%s.py" % platform)
    cmd = [sys.executable, str(script), "--query", query,
           "--limit", str(args.batch_limit), "--out-dir", str(args.out_dir)]
    if args.days > 0:
        cmd += ["--days", str(args.days)]
    if args.sort:
        cmd += ["--sort", args.sort]  # 透传（组合排序语义由采集脚本实现）
    # 四个 collect_* 脚本均有 --top-comments 且自身默认 10；透传保证 SKILL.md
    # 承诺的"评论 top20（按点赞排序取）"在编排器批量跑时同样生效
    cmd += ["--top-comments", str(args.top_comments)]
    if platform == "douban" and args.douban_intent:
        cmd += ["--intent", args.douban_intent]  # 豆瓣搜索意图透传(word/listting)
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=args.batch_timeout, env=env)
        return r.returncode, r.stdout or "", r.stderr or "", time.monotonic() - t0, cmd
    except subprocess.TimeoutExpired:
        return 124, "", "batch timeout after %ds" % args.batch_timeout, \
            time.monotonic() - t0, cmd


# ---------------------------------------------------------------- 并行模式

def worker_python(platform):
    """并行 worker 的解释器。

    抖音 worker 用 MediaCrawler venv 的 python（路径与 collect_douyin.check_env
    同源：<tools>/MediaCrawler/.venv，经 auth_common.venv_python 按平台解析，
    3.11+），其余平台用当前解释器。run_collect / collect_douyin 均纯标准库，
    3.11 可直接跑；venv 缺失时回退 sys.executable 并警告（采集脚本内部本就会
    再探测 venv）。
    """
    if platform != "douyin":
        return sys.executable
    try:
        import auth_common as ac
        import collect_douyin
        vpy = ac.venv_python(collect_douyin.DEFAULT_MC_DIR / ".venv")
    except ImportError:
        return sys.executable
    if vpy.is_file():
        return str(vpy)
    print("[警告] 未找到 MediaCrawler venv python（%s），抖音 worker 回退 %s。"
          % (vpy, sys.executable))
    return sys.executable


def build_worker_cmd(args, platform, quota, run_dir):
    """并行 worker 命令行：单平台串行 run_collect，账本/日志/摘要全落 run_dir。

    只传 --per-platform（该平台配额），不传 --target——总量停止线已由主进程折算
    进配额，worker 单平台独立判断即可。
    """
    cmd = [worker_python(platform), str(SCRIPTS_DIR / "run_collect.py"),
           "--platforms", platform,
           "--per-platform", str(quota),
           "--min-per-platform", str(min(args.min_per_platform, quota)),
           "--out-dir", str(args.out_dir),
           "--progress-dir", str(run_dir),
           "--slug", platform,
           "--ledger-name", platform + ".jsonl",
           "--batch-limit", str(args.batch_limit),
           "--batch-timeout", str(args.batch_timeout),
           "--sleep-scale", str(args.sleep_scale),
           "--scripts-dir", str(args.scripts_dir),
           "--reuse-min", str(args.reuse_min),
           "--top-comments", str(args.top_comments)]
    if args.refresh:
        cmd += ["--refresh"]
    if args.queries:
        cmd += ["--queries", args.queries]
    if args.query_file:
        cmd += ["--query-file", str(args.query_file)]
    if args.extra_queries:
        cmd += ["--extra-queries", args.extra_queries]
    if args.days > 0:
        cmd += ["--days", str(args.days)]
    if args.sort:
        cmd += ["--sort", args.sort]
    if args.douban_intent:
        cmd += ["--douban-intent", args.douban_intent]
    return cmd


def _pump_worker(proc, log_fh, platform):
    """worker stdout 逐行落日志并带平台前缀回显（worker 侧已加 PYTHONUNBUFFERED=1）。"""
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        log_fh.write(line + "\n")
        print("[%s] %s" % (platform, line))


def run_parallel(args, platforms, slug, out_dir, progress_dir, queries, extras):
    """--parallel 主流程：每平台一个 worker 子进程（本脚本的单平台串行模式）。

    隔离：账本/日志/摘要在 progress/<slug>/ 下按平台分文件（<平台>.jsonl/.log/
    .summary.json），raw 输出本就按 <平台>_ 前缀分文件（无同文件跨进程写）；
    worker 的 RENT_ASSIST_DATA 指到 run_dir/<平台>/，把 auth_state.json 也按平台
    隔离（save_auth_state 的 .tmp 是固定名，多进程并发写会互相踩；该文件仅展示
    用，采集脚本每次开跑仍真实探测）。停止规则由 worker 按平台独立判断。
    返回值口径与串行一致：0/3/130/1。
    """
    run_dir = progress_dir / slug
    run_dir.mkdir(parents=True, exist_ok=True)
    share = platform_shares(platforms, args.target, args.per_platform)
    print("[run_collect] 并行模式：每平台一个 worker（同平台内仍串行批次+随机间隔，"
          "频控不变；并行只发生在不同平台间）")
    print("[run_collect] 标的=%s 平台=%s 配额=%s worker目录=%s" % (
        slug, ",".join(platforms),
        "/".join(str(share[p]) for p in platforms), run_dir))
    started_at = now_iso()
    t0 = time.monotonic()
    procs, log_fhs = {}, {}
    interrupted = False
    try:
        for p in platforms:
            cmd = build_worker_cmd(args, p, share[p], run_dir)
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"   # 子进程 stdout 不缓冲，日志实时可见
            env.setdefault("PYTHONIOENCODING", "utf-8")
            env["RENT_ASSIST_DATA"] = str(run_dir / p)
            log_fhs[p] = (run_dir / (p + ".worker.log")).open(
                "w", encoding="utf-8")
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", env=env)
            except OSError as e:
                print("[失败] %s worker 启动失败: %s（不影响其他平台）" % (p, e))
                procs[p] = (None, cmd)
                continue
            procs[p] = (proc, cmd)
            print("[run_collect] 启动 worker %s（pid %d）：$ %s"
                  % (p, proc.pid, " ".join(cmd)))
            threading.Thread(target=_pump_worker,
                             args=(proc, log_fhs[p], p), daemon=True).start()
        for p in platforms:
            if procs[p][0] is not None:
                procs[p][0].wait()
    except KeyboardInterrupt:
        interrupted = True
        print("\n[中断] 用户中断；终止各平台 worker（各自保存账本，重跑本命令自动续采）...")
        for p, (proc, _) in procs.items():
            if proc is not None and proc.poll() is None:
                proc.terminate()
        for p, (proc, _) in procs.items():
            if proc is None:
                continue
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
    finally:
        for fh in log_fhs.values():
            fh.close()

    # ------------------------------------------------ 汇总（单平台失败不中断）
    rcs = {p: (procs[p][0].returncode if procs[p][0] is not None else 1)
           for p in platforms}
    per_platform = {}
    total_collected = 0
    reused_pairs = []
    for p in platforms:
        child = None
        spath = run_dir / (p + ".summary.json")
        if spath.is_file():
            try:
                summ = json.loads(spath.read_text(encoding="utf-8"))
                child = ((summ or {}).get("per_platform") or {}).get(p)
                # 复用决策留痕：汇总 worker 子进程 summary 的 reused_pairs
                # （结构 [平台, 查询词, 已有条数]，与串行口径一致）
                for e in (summ or {}).get("reused_pairs") or []:
                    if isinstance(e, (list, tuple)) and len(e) == 3:
                        reused_pairs.append([str(e[0]), str(e[1]), int(e[2])])
            except ValueError:
                child = None
        if not isinstance(child, dict):
            child = {"share": share[p], "batches": 0, "new_session": 0,
                     "collected": 0, "degraded": 0, "failed": [],
                     "stop_reason": "worker_failed(无摘要)"}
        child = dict(child)
        child["worker_rc"] = rcs[p]
        child["worker_log"] = p + ".worker.log"
        per_platform[p] = child
        total_collected += int(child.get("collected") or 0)

    print("%-9s %5s %6s %10s %5s %5s %4s  %s" % (
        "平台", "批次", "新增", "累计/配额", "降级", "失败", "rc", "停止原因"))
    for p in platforms:
        c = per_platform[p]
        print("%-9s %5d %6d %6d/%-6d %5d %5d %4d  %s%s" % (
            p, c.get("batches", 0), c.get("new_session", 0),
            c.get("collected", 0), share[p], c.get("degraded", 0),
            len(c.get("failed") or []), c["worker_rc"], c.get("stop_reason", ""),
            "  <-- 失败" if c["worker_rc"] != 0 else ""))
    for p in platforms:
        if per_platform[p]["worker_rc"] != 0:
            print("[失败] %s worker 退出码 %s（单平台失败不影响其他平台，重跑本命令"
                  "可续采）。日志: %s" % (
                      p, per_platform[p]["worker_rc"],
                      run_dir / per_platform[p]["worker_log"]))

    summary = {
        "slug": slug,
        "mode": "parallel",
        "target": args.target,
        "per_platform_arg": args.per_platform,
        "min_per_platform": args.min_per_platform,
        "days": args.days,
        "sort": args.sort,
        "platforms": platforms,
        "queries": queries,
        "extra_queries": extras,
        "out_dir": str(out_dir),
        "run_dir": str(run_dir),
        "started_at": started_at,
        "finished_at": now_iso(),
        "duration_sec": round(time.monotonic() - t0, 1),
        "aborted": interrupted,
        "abort_reason": "interrupted" if interrupted else None,
        "total_collected": total_collected,
        "reused_pairs": reused_pairs,
        "per_platform": per_platform,
    }
    summary_path = progress_dir / (slug + ".summary.json")
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print("[汇总] 并行 %d 平台共 %d 条；summary=%s 账本=%s/<平台>.jsonl "
          "worker日志=%s/<平台>.worker.log" % (
              len(platforms), total_collected, summary_path.name,
              run_dir.name, run_dir.name))
    if interrupted:
        return 130
    if RC_LOGIN in rcs.values():
        return RC_LOGIN
    if 130 in rcs.values():
        return 130
    if 1 in rcs.values():
        return 1
    return 0


# ---------------------------------------------------------------- CLI

def build_parser():
    ap = argparse.ArgumentParser(
        description="批次采集编排器：queries×platforms 批量跑 collect_* 脚本，攒几百条"
                    "（--per-platform 每平台上限 / --target 总量停止线 / --parallel 分平台并行）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--target", type=int, default=None,
                    help="总量停止线：所有平台累计去重条数达到即全部收队；"
                         "未配 --per-platform 时兼作各平台配额来源（按平台数均分 ceil）。"
                         "与 --per-platform 的区别：它管总量不管分布，per-platform 管每个平台各多少")
    ap.add_argument("--per-platform", dest="per_platform", type=int, default=None,
                    help="每平台采集上限（跨批次累计，含账本续采与当日已有 jsonl 基线）："
                         "某平台达到即停该平台。给了它平台配额以它为准，不按 --target 均分；"
                         "与 --target 可共存（各平台配额照旧，总量线仍生效）。"
                         "并行模式下各 worker 按此独立判断")
    ap.add_argument("--queries", default="", metavar="Q1,Q2",
                    help="逗号分隔查询词（SKILL.md 工作流从用户问题生成后传入）")
    ap.add_argument("--query-file", default=None, metavar="PATH",
                    help="查询词文件（每行一个，支持 # 注释；或 JSON 数组），与 --queries 可叠加")
    ap.add_argument("--extra-queries", default="", metavar="Q1,Q2",
                    help="备用查询词：平台未达 --min-per-platform 且主查询耗尽时依次补跑")
    ap.add_argument("--platforms", default="xhs,douban,web",
                    help="逗号分隔，取值 xhs/douban/web/douyin")
    ap.add_argument("--days", type=int, default=0,
                    help="时间窗天数，透传采集脚本（默认 0=不过滤）")
    ap.add_argument("--sort", default="",
                    help="排序键，原样透传采集脚本（如 discussion,hot；空=用子脚本默认）")
    ap.add_argument("--top-comments", dest="top_comments", type=int, default=20,
                    metavar="N",
                    help="每条内容保留的热门评论条数，透传各 collect_* 脚本"
                         "（默认 20，对齐 SKILL.md 正式采集口径；子脚本自身默认 10）；"
                         "测试联调可传 5；--parallel 下透传各 worker")
    ap.add_argument("--douban-intent", dest="douban_intent", default=None,
                    choices=("word", "listing"),
                    help="豆瓣搜索意图，透传 collect_douban --intent：word=口碑类"
                         "（不拼租赁泛词，防求租帖），listing=找房类（双拼供给侧"
                         "租赁词）；不传用子脚本默认（word）")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                    help="jsonl 输出目录（默认 <skill>/data/raw）")
    ap.add_argument("--progress-dir", type=Path, default=DEFAULT_PROGRESS_DIR,
                    help="账本/日志/汇总目录（默认 <skill>/data/progress）")
    ap.add_argument("--min-per-platform", dest="min_per_platform", type=int, default=20,
                    help="每平台最低去重条数，不达则动用备用查询")
    ap.add_argument("--slug", default=None,
                    help="标的 slug（账本/日志/汇总文件名，默认取首个查询词）")
    ap.add_argument("--batch-limit", dest="batch_limit", type=int, default=20,
                    help="每批传给采集脚本的 --limit（单轮上限）")
    ap.add_argument("--batch-timeout", dest="batch_timeout", type=int, default=1800,
                    help="单批子进程超时秒数（超时记失败继续）")
    ap.add_argument("--scripts-dir", dest="scripts_dir", type=Path, default=SCRIPTS_DIR,
                    help="collect_*.py 所在目录")
    ap.add_argument("--sleep-scale", dest="sleep_scale", type=float, default=1.0,
                    help="批次间休眠缩放系数（离线测试传 0=不休眠）")
    ap.add_argument("--parallel", action="store_true",
                    help="分平台并行：每平台一个 worker 子进程（本脚本的单平台串行"
                         "模式），各自独立账本 progress/<slug>/<平台>.jsonl 与日志，"
                         "主进程等全部 worker 后汇总；同平台内仍串行批次+随机间隔"
                         "（频控不变），并行只发生在不同平台间。不加则串行（行为不变）")
    ap.add_argument("--reuse-min", dest="reuse_min", type=int,
                    default=DEFAULT_REUSE_MIN, metavar="N",
                    help="缓存复用闸门：某 (platform, query) 近 %d 天已采 URL ≥ N "
                         "→ 跳过该组合（打印 [复用] 明细）。默认 %d；--parallel 下"
                         "透传各 worker" % (REUSE_WINDOW_DAYS, DEFAULT_REUSE_MIN))
    ap.add_argument("--refresh", action="store_true",
                    help="忽略缓存复用闸门强制重采（跨天去重基线仍生效，仅不做跳过）；"
                         "--parallel 下透传各 worker")
    ap.add_argument("--ledger-name", dest="ledger_name", default=None,
                    help="（内部）账本文件名覆盖，默认 <slug>.json；--parallel worker "
                         "用它把账本落到 progress/<slug>/<平台>.jsonl")
    return ap


def platform_shares(platforms, target, per_platform):
    """各平台配额：给了 per_platform 以它为准（每平台上限，语义无歧义）；
    否则按 target 均分（ceil）。"""
    if per_platform:
        return {p: int(per_platform) for p in platforms}
    return {p: max(1, math.ceil(target / len(platforms))) for p in platforms}


def load_queries(args):
    qs = split_list(args.queries)
    if args.query_file:
        text = Path(args.query_file).read_text(encoding="utf-8-sig").strip()
        if text.startswith("["):
            qs += [str(x) for x in json.loads(text)]
        else:
            qs += [ln.strip() for ln in text.splitlines()
                   if ln.strip() and not ln.lstrip().startswith("#")]
    out, seen = [], set()
    for q in qs:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out


# ---------------------------------------------------------------- 主流程

class _Abort(Exception):
    pass


def main():
    args = build_parser().parse_args()
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass  # 老 Python/非标准流无 reconfigure，静默跳过（无需追溯）

    if args.target is None and args.per_platform is None:
        print("[错误] --target 与 --per-platform 至少提供一个（--per-platform N=每平台"
              "上限；--target N=总量停止线，未配前者时兼作均分配额来源）。", file=sys.stderr)
        return 1
    if args.target is not None and args.target <= 0:
        print("[错误] --target 需为正整数（收到 %s）。" % args.target, file=sys.stderr)
        return 1
    if args.per_platform is not None and args.per_platform <= 0:
        print("[错误] --per-platform 需为正整数（收到 %s）。" % args.per_platform,
              file=sys.stderr)
        return 1
    if args.ledger_name and re.search(r"[\\/]", args.ledger_name):
        print("[错误] --ledger-name 只能是文件名，不能带路径分隔符。", file=sys.stderr)
        return 1
    if args.reuse_min < 1:
        print("[错误] --reuse-min 需为正整数（收到 %s）。" % args.reuse_min,
              file=sys.stderr)
        return 1
    if args.top_comments < 1:
        print("[错误] --top-comments 需为正整数（收到 %s）。" % args.top_comments,
              file=sys.stderr)
        return 1

    platforms = split_list(args.platforms)
    if not platforms:
        print("[错误] --platforms 为空。", file=sys.stderr)
        return 1
    bad = [p for p in platforms if p not in KNOWN_PLATFORMS]
    if bad:
        print("[错误] 未知平台: %s（可选 %s）" % (",".join(bad), "/".join(KNOWN_PLATFORMS)),
              file=sys.stderr)
        return 1
    for p in platforms:
        if not (args.scripts_dir / ("collect_%s.py" % p)).is_file():
            print("[错误] 缺少 %s（--scripts-dir=%s）"
                  % (args.scripts_dir / ("collect_%s.py" % p), args.scripts_dir),
                  file=sys.stderr)
            return 1
    try:
        queries = load_queries(args)
    except Exception as e:
        log_exc_detail("queries", "读取查询词失败", e)  # traceback 落日志，stderr 一行摘要
        return 1
    if not queries:
        print("[错误] 查询词为空：用 --queries 或 --query-file 提供。", file=sys.stderr)
        return 1
    extras = [q for q in split_list(args.extra_queries) if q not in queries]

    slug = args.slug or safe_name(queries[0])
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    progress_dir = Path(args.progress_dir)
    progress_dir.mkdir(parents=True, exist_ok=True)

    if args.parallel:
        return run_parallel(args, platforms, slug, out_dir, progress_dir,
                            queries, extras)

    ledger_path = progress_dir / (args.ledger_name or (slug + ".json"))
    ledger = Ledger.load(ledger_path, slug)
    log_path = progress_dir / (slug + ".log")
    summary_path = progress_dir / (slug + ".summary.json")

    share = platform_shares(platforms, args.target, args.per_platform)
    # --per-platform 模式下最低兜底不超过配额（否则会为凑 min 空跑备用查询）
    min_pp = args.min_per_platform
    if args.per_platform is not None:
        min_pp = min(min_pp, args.per_platform)

    # 去重基线：近 7 天已有 jsonl（仅本次查询集）∪ 账本哈希；取大者为各平台累计起点
    url_sets, pair_urls, files = scan_recent_urls(out_dir, platforms,
                                                  queries + extras)
    for p in platforms:
        ledger.collected[p] = max(int(ledger.collected.get(p, 0)), len(url_sets[p]))
    collected = {p: int(ledger.collected.get(p, 0)) for p in platforms}
    all_hashes = set(ledger.hashes)
    for s in url_sets.values():
        all_hashes |= {sha1_of(u) for u in s}

    # 7 天复用闸门：某 (platform, query) 近 7 天已采 URL ≥ --reuse-min → 跳过
    # （--refresh 全部忽略复用强制重采；账本 done 跳过与此独立、互不替代）
    reuse_detail = []
    reuse_skipped = set()
    if args.refresh:
        print("[run_collect] --refresh：忽略近 %d 天复用闸门，全部重采（去重基线"
              "仍生效）。" % REUSE_WINDOW_DAYS)
    else:
        for q in dict.fromkeys(queries + extras):
            for p in platforms:
                n = len(pair_urls.get((p, q), ()))
                if n >= args.reuse_min:
                    reuse_skipped.add((p, q))
                    reuse_detail.append([p, q, n])
                    print("[复用] %s|%s 已有 %d 条(近%d天)，跳过；--refresh 可强制重采"
                          % (p, q, n, REUSE_WINDOW_DAYS))

    stats = {}
    for p in platforms:
        stats[p] = {"batches": int(ledger.batches.get(p, 0)),
                    "new": 0,
                    "degraded": int(ledger.degraded.get(p, 0)),
                    "failed": [], "low_streak": 0, "fail_streak": 0}

    total_pairs = len(queries) * len(platforms)
    qset_main = set(queries)
    queue = deque((q, p) for q in queries for p in platforms
                  if (p, q) not in ledger.done and (p, q) not in reuse_skipped)
    queued = set(queue)
    main_reuse = sum(1 for (p, q) in reuse_skipped if q in qset_main)
    skipped = total_pairs - len(queue) - main_reuse

    print("[run_collect] 标的=%s %s 平台=%s 配额=%s 单批=%d days=%d sort=%s" % (
        slug,
        "每平台上限=%d" % args.per_platform if args.per_platform is not None
        else "目标=%d" % args.target,
        ",".join(platforms),
        "/".join(str(share[p]) for p in platforms),
        args.batch_limit, args.days, args.sort or "(子脚本默认)"))
    print("[run_collect] 组合 %d 个（%d 查询 × %d 平台），账本跳过 %d，复用跳过 %d，"
          "待跑 %d；备用查询 %d 个" % (
              total_pairs, len(queries), len(platforms), skipped,
              len(reuse_skipped), len(queue), len(extras)))

    stops = {}
    prev_p = None
    batch_no = 0
    aborted = False
    abort_reason = None
    target_hit = False
    started_at = now_iso()
    t0 = time.monotonic()
    log_fh = log_path.open("a", encoding="utf-8")

    # 复用决策留痕：启动闸门的 [复用] 明细同行写进批次日志（与批次日志同文件
    # 同风格追加），事后可审计（不只 stdout）
    for _p, _q, _n in reuse_detail:
        log_fh.write("[%s] [复用] %s|%s 已有 %d 条(近%d天)，跳过；--refresh 可强制重采\n"
                     % (now_iso(), _p, _q, _n, REUSE_WINDOW_DAYS))
    if reuse_detail:
        log_fh.flush()

    try:
        while True:
            while queue:
                q, p = queue.popleft()
                queued.discard((q, p))
                if p in stops:
                    continue
                if prev_p is not None and args.sleep_scale > 0:
                    s = random.uniform(*BATCH_SLEEP_RANGE)
                    if prev_p == p:
                        s += random.uniform(*SAME_PLATFORM_SLEEP)
                    time.sleep(s * args.sleep_scale)
                batch_no += 1
                rc, out, err, el, cmd = run_one_batch(args.scripts_dir, p, q, args)
                m = re.search(r"fetched=(\d+)", out)
                fetched = int(m.group(1)) if m else args.batch_limit
                log_fh.write(
                    "==== %s batch#%d %s | %s rc=%s %.1fs ====\n$ %s\n--stdout--\n%s\n--stderr--\n%s\n"
                    % (now_iso(), batch_no, p, q, rc, el, " ".join(cmd),
                       out.rstrip(), err.rstrip()))
                log_fh.flush()
                summary_line = next(
                    (l for l in out.splitlines() if l.startswith("platform=")), "")
                if rc == RC_LOGIN:
                    print("[中止] %s 需登录（退出码 3）。先跑: python scripts/ensure_auth.py"
                          " —— 登录后重跑本命令，账本已保存可续采。" % p)
                    if summary_line:
                        print("  " + summary_line)
                    elif err_tail(err):
                        print("  [%s] %s" % (p, err_tail(err)))
                    aborted = True
                    abort_reason = "need_login(exit3)"
                    raise _Abort()
                stats[p]["batches"] += 1
                if rc == RC_BAD_DATA:
                    ledger.mark_done(p, q)
                    stats[p]["degraded"] += 1
                elif rc == 0:
                    ledger.mark_done(p, q)
                else:
                    stats[p]["failed"].append(
                        {"query": q, "rc": rc, "err": err_tail(err)})
                    stats[p]["fail_streak"] += 1

                # URL 精确去重：重扫近 7 天 jsonl，统计该平台本批新增
                old_set = url_sets[p]
                url_sets, _pair_urls, files = scan_recent_urls(
                    out_dir, platforms, queries + extras)
                new_p = len(url_sets[p] - old_set)
                if new_p:
                    collected[p] += new_p
                    stats[p]["new"] += new_p
                    all_hashes |= {sha1_of(u) for u in url_sets[p] - old_set}
                ratio = new_p / float(max(fetched, 1))
                if rc in (0, RC_BAD_DATA):
                    stats[p]["low_streak"] = \
                        stats[p]["low_streak"] + 1 if ratio < LOW_GAIN_RATIO else 0
                print("[批 %d] %s | %s | rc=%s %.1fs 新增=%d/%d 累计=%d/%d%s" % (
                    batch_no, p, q, rc, el, new_p, fetched,
                    collected[p], share[p], " [降级]" if rc == RC_BAD_DATA else ""))
                if summary_line:
                    print("  " + summary_line)

                if collected[p] >= share[p]:
                    stops[p] = "quota"
                    print("[停] %s 达到配额 %d，后续组合跳过。" % (p, share[p]))
                elif stats[p]["low_streak"] >= LOW_GAIN_STREAK:
                    stops[p] = "low_gain"
                    print("[停] %s 连续 %d 批新增率<%d%%，该平台收益枯竭。"
                          % (p, LOW_GAIN_STREAK, int(LOW_GAIN_RATIO * 100)))
                elif stats[p]["fail_streak"] >= REPEATED_FAIL:
                    stops[p] = "repeated_fail"
                    print("[停] %s 连续 %d 批失败。" % (p, REPEATED_FAIL))
                prev_p = p

                ledger.collected[p] = collected[p]
                ledger.batches[p] = stats[p]["batches"]
                ledger.degraded[p] = stats[p]["degraded"]
                ledger.hashes = all_hashes
                ledger.save()

                if args.target is not None and sum(collected.values()) >= args.target:
                    target_hit = True
                    break
            if target_hit:
                for p2 in platforms:
                    stops.setdefault(p2, "target_reached")
                print("[停] 总量达到目标 %d，剩余组合跳过。" % args.target)
                break
            # 队列耗尽：最小量兜底 + 备用查询补位
            added = False
            for p in platforms:
                if p in stops:
                    continue
                if collected.get(p, 0) >= min_pp:
                    stops[p] = "queries_exhausted"
                    continue
                eq = next((e for e in extras
                           if (p, e) not in ledger.done
                           and (p, e) not in reuse_skipped
                           and (e, p) not in queued), None)
                if eq:
                    queue.append((eq, p))
                    queued.add((eq, p))
                    print("[补] %s 未达最低 %d 条，启用备用查询「%s」"
                          % (p, min_pp, eq))
                    added = True
                else:
                    stops[p] = "min_unmet"
            if not added:
                break
    except _Abort:
        pass
    except KeyboardInterrupt:
        aborted = True
        abort_reason = "interrupted"
        print("\n[中断] 用户中断；账本已保存，重跑本命令自动续采。")
    finally:
        for p in platforms:
            ledger.collected[p] = collected.get(p, 0)
            ledger.batches[p] = stats[p]["batches"]
            ledger.degraded[p] = stats[p]["degraded"]
        ledger.hashes = all_hashes
        ledger.save()
        log_fh.close()

    # ------------------------------------------------------------ 汇总
    duration = round(time.monotonic() - t0, 1)
    total_collected = sum(collected.get(p, 0) for p in platforms)
    distinct_today = len(set().union(*url_sets.values())) if url_sets else 0
    per_platform = {}
    for p in platforms:
        st = stats[p]
        per_platform[p] = {
            "share": share[p],
            "batches": st["batches"],
            "new_session": st["new"],
            "collected": collected.get(p, 0),
            "degraded": st["degraded"],
            "failed": st["failed"],
            "stop_reason": stops.get(p) or ("aborted" if aborted else "queries_exhausted"),
        }
    summary = {
        "slug": slug,
        "target": args.target,
        "per_platform_arg": args.per_platform,
        "min_per_platform": args.min_per_platform,
        "days": args.days,
        "sort": args.sort,
        "platforms": platforms,
        "queries": queries,
        "extra_queries": extras,
        "out_dir": str(out_dir),
        "files": files,
        "started_at": started_at,
        "finished_at": now_iso(),
        "duration_sec": duration,
        "aborted": aborted,
        "abort_reason": abort_reason,
        "total_collected": total_collected,
        "distinct_urls_today": distinct_today,
        "refresh": args.refresh,
        "reuse_min": args.reuse_min,
        "reused_pairs": reuse_detail,
        "done_pairs": sorted([list(x) for x in ledger.done]),
        "per_platform": per_platform,
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")

    print("%-9s %5s %6s %10s %5s %5s  %s" % (
        "平台", "批次", "新增", "累计/配额", "降级", "失败", "停止原因"))
    tn = tf = td = 0
    for p in platforms:
        st = stats[p]
        tn += st["new"]
        tf += len(st["failed"])
        td += st["degraded"]
        print("%-9s %5d %6d %6d/%-6d %5d %5d  %s" % (
            p, st["batches"], st["new"], collected.get(p, 0), share[p],
            st["degraded"], len(st["failed"]),
            per_platform[p]["stop_reason"]))
    print("%-9s %5d %6d %10d %5d %5d" % ("合计", batch_no, tn, total_collected, td, tf))
    for p in platforms:
        for f in stats[p]["failed"][:2]:
            print("  [失败] %s | %s | rc=%s | %s" % (p, f["query"], f["rc"], f["err"]))
    print("[汇总] 共 %d 条（近%d天去重 URL %d 个）；summary=%s 账本=%s 日志=%s（全文追加）" % (
        total_collected, REUSE_WINDOW_DAYS, distinct_today,
        summary_path.name, ledger_path.name, log_path.name))
    if aborted and abort_reason == "need_login(exit3)":
        return 3
    if aborted and abort_reason == "interrupted":
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
