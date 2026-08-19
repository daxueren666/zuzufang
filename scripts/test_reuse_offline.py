#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自测: run_collect 缓存强制复用 + 跨天去重修复（WS4）。
全程 mock：伪造 collect_<平台>.py 脚本（不触网，只写 jsonl 并打印摘要行，
调用痕迹落 FAKE_COLLECT_LOG）；raw/progress/伪造脚本全在临时目录，不碰
E:\\租房\\data 既有产出。

覆盖:
  - scan_recent_urls: 近 7 天窗口（文件名 _YYYYMMDD 日期优先，无日期回退 mtime）、
    文件名日期老但 mtime 新→排除 / 文件名今日但 mtime 老→纳入、平台过滤、
    按 (platform, query) 分组计数
  - 复用闸门（串行）: 7 天内伪造 raw ≥ --reuse-min → 组合跳过、采集子进程
    0 次调用、打印 [复用] 明细；--refresh 后正常执行且账本 done 补时间戳；
    阈值调高（--reuse-min 20）不触发复用
  - 8 天前文件不触发复用（文件名日期优先于新 mtime）
  - ledger done 时间戳: 超 7 天失效可重跑；旧格式无时间戳视为有效（打印提示，
    保存后自动补时间戳）
  - 账本损坏: stderr 警告"账本损坏，已忽略并重建"，降级全跑
  - --parallel 路径: build_worker_cmd 透传 --refresh/--reuse-min；并行集成
    （worker 复用跳过 0 调用；--refresh 后 worker 正常采集）

运行: python scripts/test_reuse_offline.py
退出码: 0 全部通过; 1 存在失败。
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# 数据根钉到临时目录（须在 import run_collect 前设置，避免碰到真实 data）
_TD = tempfile.TemporaryDirectory(prefix="rent_reuse_test_")
os.environ["RENT_ASSIST_DATA"] = _TD.name

import run_collect as rc    # noqa: E402

PASS, FAIL = 0, 0

Q = "天通苑租房"

# 伪造采集脚本：接受 run_collect 的全部传参（--query/--limit/--out-dir/
# --days/--sort/--intent），追加 N_REC 条记录并打印摘要行；调用痕迹落
# FAKE_COLLECT_LOG。
FAKE_COLLECT_TEMPLATE = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

PLATFORM = "__PLATFORM__"
N_REC = 3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--days", type=int, default=0)
    ap.add_argument("--sort", default="")
    ap.add_argument("--intent", default=None)
    ap.add_argument("--top-comments", dest="top_comments", type=int, default=10)
    a = ap.parse_args()
    log = os.environ.get("FAKE_COLLECT_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(PLATFORM + "|query=" + a.query
                     + "|limit=" + str(a.limit)
                     + "|top=" + str(a.top_comments) + "\\n")
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[\\W_]+", "", a.query)[:20] or "query"
    path = out / (PLATFORM + "_" + safe + "_"
                  + datetime.now().strftime("%Y%m%d") + ".jsonl")
    with path.open("a", encoding="utf-8") as fh:
        for i in range(N_REC):
            rec = {"platform": PLATFORM, "query": a.query,
                   "url": "https://fake.invalid/%s/%d" % (safe,
                                                         time.time_ns() + i),
                   "title": "t", "content": ""}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\\n")
    print("fetched=%d" % N_REC)
    print("platform=%s got=%d" % (PLATFORM, N_REC))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s" % name)


def safe_q(q):
    return rc.safe_name(q)


def seed_raw(out_dir, platform, query, n, date8=None, name=None,
             mtime_days_ago=None):
    """伪造 raw jsonl：命名 <platform>_<safe>_<date8>.jsonl，记录 query 精确匹配。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = name or ("%s_%s_%s.jsonl" % (platform, safe_q(query),
                                         date8 or datetime.now().strftime("%Y%m%d")))
    path = out_dir / fname
    with path.open("a", encoding="utf-8") as fh:
        for i in range(n):
            fh.write(json.dumps(
                {"platform": platform, "query": query,
                 "url": "seed://%s/%s/%d" % (platform, safe_q(query), i),
                 "title": "t", "content": ""}, ensure_ascii=False) + "\n")
    if mtime_days_ago is not None:
        ts = time.time() - mtime_days_ago * 86400
        os.utime(path, (ts, ts))
    return path


def make_env(platforms=("xhs",)):
    """临时环境：out/progress/scripts 目录 + 伪造 collect 脚本 + 调用痕迹文件。"""
    td = Path(tempfile.mkdtemp(prefix="rent_reuse_env_"))
    out_dir = td / "raw"
    prog_dir = td / "progress"
    scripts_dir = td / "scripts"
    scripts_dir.mkdir(parents=True)
    prog_dir.mkdir(parents=True)
    for p in platforms:
        (scripts_dir / ("collect_%s.py" % p)).write_text(
            FAKE_COLLECT_TEMPLATE.replace("__PLATFORM__", p), encoding="utf-8")
    return td, out_dir, prog_dir, scripts_dir, td / "calls.log"


def run_cli(argv, calls_log, timeout=300):
    """跑真实 run_collect.py 子进程（RENT_ASSIST_DATA 钉临时目录，离线 mock）。"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["RENT_ASSIST_DATA"] = _TD.name
    env["FAKE_COLLECT_LOG"] = str(calls_log)
    return subprocess.run(
        [sys.executable, str(HERE / "run_collect.py")] + argv,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, env=env)


def n_calls(calls_log):
    if not Path(calls_log).is_file():
        return 0
    return len([l for l in Path(calls_log).read_text(encoding="utf-8").splitlines()
                if l.strip()])


def base_argv(out_dir, prog_dir, scripts_dir, extra=()):
    return ["--per-platform", "50", "--min-per-platform", "5",
            "--queries", Q, "--platforms", "xhs",
            "--out-dir", str(out_dir), "--progress-dir", str(prog_dir),
            "--scripts-dir", str(scripts_dir), "--sleep-scale", "0"] + list(extra)


# ------------------------------------------------- scan_recent_urls 单元
def test_scan_recent_urls():
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        today = datetime.now()
        d8 = lambda days: (today - timedelta(days=days)).strftime("%Y%m%d")  # noqa: E731
        # 文件名今日 + mtime 30 天前 → 纳入（文件名日期优先）
        f_a = seed_raw(out, "xhs", "甲租房", 3, date8=d8(0), mtime_days_ago=30)
        # 文件名 8 天前 + mtime 现在 → 排除（不信新 mtime）
        seed_raw(out, "xhs", "乙租房", 2, date8=d8(8))
        # 无日期文件名 + mtime 现在 → 纳入（回退 mtime）
        f_c = seed_raw(out, "xhs", "丙租房", 2, name="xhs_丙租房.jsonl")
        # 无日期文件名 + mtime 9 天前 → 排除
        seed_raw(out, "xhs", "丁租房", 2, name="xhs_丁租房.jsonl",
                 mtime_days_ago=9)
        # 其他平台文件 → 平台过滤掉
        seed_raw(out, "web", "甲租房", 4, date8=d8(0))
        per, pairs, files = rc.scan_recent_urls(
            out, ["xhs"], ["甲租房", "乙租房", "丙租房", "丁租房"])
        ok = (len(per["xhs"]) == 5                                  # 甲3 + 丙2
              and len(pairs[("xhs", "甲租房")]) == 3
              and ("xhs", "乙租房") not in pairs
              and len(pairs[("xhs", "丙租房")]) == 2
              and ("xhs", "丁租房") not in pairs
              and set(files) == {f_a.name, f_c.name})
        check("scan_recent_urls 近7天窗口/文件名日期优先/无日期回退mtime/平台过滤/"
              "按(platform,query)分组", ok)


# ------------------------------------------------- 复用闸门（串行）
def test_reuse_gate_serial():
    # 1) 7 天内 12 条 ≥ 默认阈值 10 → 跳过组合，采集子进程 0 次调用
    td, out_dir, prog_dir, scripts_dir, calls = make_env()
    seed_raw(out_dir, "xhs", Q, 12)
    r = run_cli(base_argv(out_dir, prog_dir, scripts_dir), calls)
    ok1 = (r.returncode == 0 and n_calls(calls) == 0
           and "[复用] xhs|%s 已有 12 条(近7天)，跳过" % Q in r.stdout
           and "--refresh 可强制重采" in r.stdout)
    # 2) 阈值调到 20 → 12 条不触发复用，正常执行
    td2, out_dir2, prog_dir2, scripts_dir2, calls2 = make_env()
    seed_raw(out_dir2, "xhs", Q, 12)
    r2 = run_cli(base_argv(out_dir2, prog_dir2, scripts_dir2,
                           ["--reuse-min", "20"]), calls2)
    ok2 = (r2.returncode == 0 and n_calls(calls2) >= 1
           and "[复用]" not in r2.stdout)
    check("复用闸门: 近7天≥阈值跳过(0次调用)+打印明细 / 阈值调高不触发", ok1 and ok2)


def test_refresh_forces_rerun():
    td, out_dir, prog_dir, scripts_dir, calls = make_env()
    seed_raw(out_dir, "xhs", Q, 12)
    r = run_cli(base_argv(out_dir, prog_dir, scripts_dir, ["--refresh"]), calls)
    called = n_calls(calls) >= 1
    # 子进程确实收到 --top-comments 20（SKILL.md 正式口径，透传到 collect_*）
    top20 = False
    if Path(calls).is_file():
        top20 = any("|top=20" in l for l in
                    Path(calls).read_text(encoding="utf-8").splitlines())
    # 账本 done 已带时间戳（3 元素），且可解析
    led = json.loads((prog_dir / ("%s.json" % safe_q(Q))).read_text(encoding="utf-8"))
    ts_ok = False
    if led.get("done"):
        p, q, ts = led["done"][0]
        try:
            datetime.fromisoformat(ts)
            ts_ok = (p == "xhs" and q == Q)
        except ValueError:
            ts_ok = False
    check("--refresh 忽略复用强制重采 + --top-comments 20 透传 + 新账本 done 带时间戳",
          r.returncode == 0 and called and "[复用]" not in r.stdout and ts_ok
          and top20)


def test_eight_day_file_not_reused():
    td, out_dir, prog_dir, scripts_dir, calls = make_env()
    old8 = (datetime.now() - timedelta(days=8)).strftime("%Y%m%d")
    seed_raw(out_dir, "xhs", Q, 12, date8=old8)   # mtime=现在，文件名 8 天前
    r = run_cli(base_argv(out_dir, prog_dir, scripts_dir), calls)
    check("8 天前文件不触发复用（文件名日期优先于 mtime）",
          r.returncode == 0 and n_calls(calls) >= 1 and "[复用]" not in r.stdout)


# ------------------------------------------------- ledger 时间戳
def test_ledger_done_ttl():
    # 1) done 时间戳 14 天前 → 失效可重跑
    td, out_dir, prog_dir, scripts_dir, calls = make_env()
    stale = (datetime.now() - timedelta(days=14)).isoformat(timespec="seconds")
    (prog_dir / ("%s.json" % safe_q(Q))).write_text(json.dumps({
        "slug": safe_q(Q), "done": [["xhs", Q, stale]],
        "collected": {"xhs": 5}, "batches": {}, "degraded": {},
        "dedup_hashes": []}, ensure_ascii=False), encoding="utf-8")
    r = run_cli(base_argv(out_dir, prog_dir, scripts_dir), calls)
    ok1 = (r.returncode == 0 and n_calls(calls) >= 1
           and "已失效可重跑" in r.stdout)

    # 2) 旧格式无时间戳 → 视为有效（组合跳过 0 调用），保存后补时间戳
    td2, out_dir2, prog_dir2, scripts_dir2, calls2 = make_env()
    led_path = prog_dir2 / ("%s.json" % safe_q(Q))
    led_path.write_text(json.dumps({
        "slug": safe_q(Q), "done": [["xhs", Q]],
        "collected": {"xhs": 12}, "batches": {}, "degraded": {},
        "dedup_hashes": []}, ensure_ascii=False), encoding="utf-8")
    r2 = run_cli(base_argv(out_dir2, prog_dir2, scripts_dir2), calls2)
    led2 = json.loads(led_path.read_text(encoding="utf-8"))
    migrated = bool(led2.get("done")) and len(led2["done"][0]) == 3 \
        and isinstance(led2["done"][0][2], str)
    ok2 = (r2.returncode == 0 and n_calls(calls2) == 0
           and "无时间戳" in r2.stdout and migrated)
    check("ledger done: 超7天失效可重跑 / 旧格式无时间戳有效+提示+保存补时间戳",
          ok1 and ok2)


def test_ledger_corrupt_stderr():
    td, out_dir, prog_dir, scripts_dir, calls = make_env()
    (prog_dir / ("%s.json" % safe_q(Q))).write_text("不是json{{{",
                                                    encoding="utf-8")
    r = run_cli(base_argv(out_dir, prog_dir, scripts_dir), calls)
    check("账本损坏: stderr 警告并降级全跑",
          r.returncode == 0 and "账本损坏，已忽略并重建" in r.stderr
          and n_calls(calls) >= 1)


# ------------------------------------------------- --parallel 路径
def test_worker_cmd_passthrough():
    base = ["--per-platform", "30", "--queries", Q, "--platforms", "xhs",
            "--out-dir", "X:/tmp", "--progress-dir", "X:/tmp"]
    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td)
        args_on = rc.build_parser().parse_args(base + ["--refresh"])
        cmd_on = rc.build_worker_cmd(args_on, "xhs", 30, run_dir)
        args_off = rc.build_parser().parse_args(base)
        cmd_off = rc.build_worker_cmd(args_off, "xhs", 30, run_dir)
        top_val = (cmd_on[cmd_on.index("--top-comments") + 1]
                   if "--top-comments" in cmd_on else None)
        check("build_worker_cmd 透传 --refresh / 常规带 --reuse-min/--top-comments 20",
              "--refresh" in cmd_on and "--refresh" not in cmd_off
              and "--reuse-min" in cmd_on and "--reuse-min" in cmd_off
              and str(rc.DEFAULT_REUSE_MIN) in cmd_on
              and top_val == "20")


def test_parallel_integration():
    # 1) 复用闸门在 worker 生效：0 次调用，[复用] 明细经主进程回显
    td, out_dir, prog_dir, scripts_dir, calls = make_env()
    seed_raw(out_dir, "xhs", Q, 12)
    r = run_cli(base_argv(out_dir, prog_dir, scripts_dir, ["--parallel"]), calls)
    summ_path = prog_dir / ("%s.summary.json" % safe_q(Q))
    summ = json.loads(summ_path.read_text(encoding="utf-8")) \
        if summ_path.is_file() else {}
    ok1 = (r.returncode == 0 and n_calls(calls) == 0
           and "[复用]" in r.stdout and summ.get("mode") == "parallel")

    # 2) --refresh 透传到 worker：正常执行（伪造脚本被调用）
    td2, out_dir2, prog_dir2, scripts_dir2, calls2 = make_env()
    seed_raw(out_dir2, "xhs", Q, 12)
    r2 = run_cli(base_argv(out_dir2, prog_dir2, scripts_dir2,
                           ["--parallel", "--refresh"]), calls2)
    ok2 = r2.returncode == 0 and n_calls(calls2) >= 1
    check("--parallel 集成: worker 复用跳过 0 调用 / --refresh 透传后正常采集",
          ok1 and ok2)


def test_reuse_persisted():
    """复用明细落盘审计（P10）：summary.reused_pairs 非空且含该组合、
    批次日志文件含 [复用] 行；--parallel 下主摘要聚合 worker 的 reused_pairs。"""
    QT = "测试复用天通苑租房"
    # 1) 串行：summary.reused_pairs 含 [xhs, Q, 12]，log 文件含 [复用] 行
    td, out_dir, prog_dir, scripts_dir, calls = make_env()
    seed_raw(out_dir, "xhs", QT, 12)
    r = run_cli(base_argv(out_dir, prog_dir, scripts_dir)
                + ["--queries", QT], calls)
    summ = json.loads(
        (prog_dir / ("%s.summary.json" % safe_q(QT))).read_text(encoding="utf-8"))
    reused_ok = (summ.get("reused_pairs")
                 and ["xhs", QT, 12] in summ["reused_pairs"])
    log_text = (prog_dir / ("%s.log" % safe_q(QT))).read_text(encoding="utf-8")
    log_ok = ("[复用] xhs|%s 已有 12 条(近7天)，跳过" % QT) in log_text
    ok1 = (r.returncode == 0 and n_calls(calls) == 0
           and reused_ok and log_ok)

    # 2) --parallel：主摘要聚合 worker 的 reused_pairs，worker 摘要亦落盘
    td2, out_dir2, prog_dir2, scripts_dir2, calls2 = make_env()
    seed_raw(out_dir2, "xhs", QT, 12)
    r2 = run_cli(base_argv(out_dir2, prog_dir2, scripts_dir2)
                 + ["--queries", QT, "--parallel"], calls2)
    summ2 = json.loads(
        (prog_dir2 / ("%s.summary.json" % safe_q(QT))).read_text(encoding="utf-8"))
    agg_ok = summ2.get("reused_pairs") and ["xhs", QT, 12] in summ2["reused_pairs"]
    worker_summ = json.loads(
        (prog_dir2 / safe_q(QT) / "xhs.summary.json").read_text(encoding="utf-8"))
    worker_ok = (worker_summ.get("reused_pairs")
                 and ["xhs", QT, 12] in worker_summ["reused_pairs"])
    ok2 = (r2.returncode == 0 and n_calls(calls2) == 0 and agg_ok and worker_ok)
    check("复用明细落盘: 串行 summary+日志留痕 / --parallel 主摘要聚合 worker "
          "reused_pairs（结构 [平台,查询词,已有条数]）", ok1 and ok2)


def main():
    print("== scan_recent_urls 近7天窗口 #1 ==")
    test_scan_recent_urls()
    print("== 复用闸门（串行） #2 ==")
    test_reuse_gate_serial()
    test_refresh_forces_rerun()
    test_eight_day_file_not_reused()
    print("== ledger 时间戳/损坏 #3 ==")
    test_ledger_done_ttl()
    test_ledger_corrupt_stderr()
    print("== --parallel 路径 #4 ==")
    test_worker_cmd_passthrough()
    test_parallel_integration()
    test_reuse_persisted()
    print()
    print("离线自测: %d 通过 / %d 失败" % (PASS, FAIL))
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
