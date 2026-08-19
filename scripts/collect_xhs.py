#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rent-assist: 小红书笔记采集脚本。

经 opencli（复用 Chrome 浏览器登录态）搜索小红书笔记，取排序最靠前的前几篇，
逐篇抓取正文与热门评论，追加写入 jsonl（UTF-8，每行一条记录）。

用法:
    python collect_xhs.py --query "天通苑 租房 避坑" --limit 20 --top-comments 10
    python collect_xhs.py --query "天通苑 口碑" --days 180        # 只留近 180 天
    python collect_xhs.py --query "天通苑" --days 7 --sort time   # 近一周最新发布

--days N: 时间窗过滤(默认 0=不过滤)。搜索结果解析后按 published_at 过滤:
    在窗口内才保留; 无时间的保留并计 time_unknown。
--sort discussion,hot,time 逗号分隔多值(默认 discussion,hot):
    discussion=评论最多(comments 降序); hot=最热(likes 降序); time=发布时间
    倒序。多键=多队列 union: 每键各取 top K(K=min(limit,20)) 合并去重后统一
    补正文/评论(讨论最重要, 两队列都爬, 去重后可能 5-9 篇)。旧单值 heat
    兼容=discussion,hot。与 --days 可组合(找房源: --days 7 --sort time;
    口碑/讨论类: --days 180 --sort discussion,hot)。

extra.note_type: 笔记类型标记 video|image|unknown——搜索结果带显式类型
    字段(type/note_type/media_type 含 video/normal 等)则映射；否则不猜
    ("无正文且点赞高"的疑似视频笔记也只标 unknown，宁缺勿错)。可靠判定
    由 fetch_media.py 下载阶段按实际落盘文件类型确认，视频再走 asr.py 转写。

补详情（note/comments）失败时随机等 2-4s 自动重试 1 次，重试成功同样
    计入产出（摘要行加 retries=N）；仍失败才标 extra.note_fetch_failed /
    comments_fetch_failed（exit 4 口径不变，见文末）。

前置条件（见 check_deps.py）:
    - opencli 可用（随 agent-reach install --system 安装，含 Chrome 扩展）
    - 小红书主站登录态有效（开跑前本脚本会做一次真实探测：limit=1 轻搜索；
      缓存态不可信，whoami 已弃用。未登录时退出码 3，按提示跑 ensure_auth.py）

退出码: 0 = 至少写出 1 条；2 = 未写出任何记录（搜索失败/无结果）；
        3 = 需登录（主站登录态失效，运行 python ensure_auth.py --platform xhs）；
        4 = 数据质量差（本次写出的记录里 note/comments 抓取失败占比 >= 50%，
            记录仍已落盘，但详情/评论大面积缺失，建议排查后重跑）。
"""

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth_common as ac
import lexicon

parse_pub_datetime = ac.parse_pub_datetime
apply_time_window = ac.apply_time_window
window_stat_seg = ac.window_stat_seg

PLATFORM = "xhs"
DEFAULT_OUT_DIR = ac.data_dir("raw")  # ~/.rent-assist/data/raw（RENT_ASSIST_DATA 可覆盖）
CONTENT_MAX = 2000            # content 字段最大字符数
SLEEP_RANGE = (2.0, 5.0)      # 平台请求间隔（秒，随机）
TOP_DETAIL_MAX = 20           # 逐篇抓正文+评论的笔记数上限（实际取 min(limit, 20)）
OP_TIMEOUT = 180              # 浏览器自动化操作较慢，放宽超时
COMMENTS_HARD_LIMIT = 50      # opencli comments --limit 上限
DETAIL_RETRY_SLEEP = (2.0, 4.0)  # note/comments 失败重试前的随机等待（秒）
NOTE_ID_RE = re.compile(
    r"xiaohongshu\.com/(?:explore|search_result|discovery/item)/([0-9A-Za-z]+)"
)


def pick(d, *keys, default=None):
    """从 dict 里取第一个非空键的值（防御字段命名变化）。"""
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def parse_likes(v):
    """把 '1.2万' / '3.5亿' / '1,234' / 123 / None 统一转为 int。"""
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


def detect_note_type(raw):
    """搜索结果行 -> 笔记类型标记 video|image|unknown。

    opencli search 若返回显式类型字段(type/note_type/media_type)则映射:
    含 "video"/"视频" -> video；normal/image/photo/图文 -> image。
    没有显式字段就不猜——"无正文且点赞高"的疑似视频笔记也只标 unknown
    (宁可 unknown 不猜错)，可靠判定留给 fetch_media.py 按下载到的实际
    文件类型确认。
    """
    v = str(pick(raw, "type", "note_type", "media_type",
                 default="") or "").strip().lower()
    if not v:
        return "unknown"
    if "video" in v or v == "视频":
        return "video"
    if v in ("normal", "normal_note", "image", "photo", "图文", "text"):
        return "image"
    return "unknown"


def sort_key_of(item, mode):
    """单条笔记在某排序键下的键（供 ac.select_union_top 多队列 union 使用）。

    discussion = comments_hint（搜索行无评论数时为 0）；hot = likes；
    time = published_at（无时间排最后）。
    """
    return ac.sort_key_for(mode, item.get("comments_hint") or 0,
                           item["likes"], item.get("published_at"))


def canonical_note_url(url):
    """把 opencli search 返回的 search_result/<id> URL 规范化为 explore 形。

    opencli note/comments 命令要求完整笔记 URL（含 xsec_token）；search 返回的
    search_result 形 URL 的 xsec_source 常为空值，直接传入会导致 note/comments
    全挂（20260815 天通苑数据 5/5 note_fetch_failed+comments_fetch_failed 的根因）。
    规范化为 https://www.xiaohongshu.com/explore/<id>?xsec_token=...&xsec_source=pc_search。
    提取不到 note_id 时原样返回。
    """
    m = NOTE_ID_RE.search(url or "")
    if not m:
        return url
    tok = re.search(r"[?&]xsec_token=([^&]+)", url or "")
    out = "https://www.xiaohongshu.com/explore/%s" % m.group(1)
    if tok:
        out += "?xsec_token=%s&xsec_source=pc_search" % tok.group(1)
    return out


def detail_failure_stats(records):
    """统计详情/评论抓取失败情况。返回 (failed_n, total_n)。

    failed_n = extra 里记了 note_fetch_failed 或 comments_fetch_failed 的记录数
    （一篇笔记任一环节失败即计入）。
    """
    failed = 0
    for r in records:
        extra = r.get("extra") or {}
        if extra.get("note_fetch_failed") or extra.get("comments_fetch_failed"):
            failed += 1
    return failed, len(records)


def safe_query_name(q):
    """文件名安全化：仅保留中文/字母/数字，截 20 字。"""
    s = re.sub(r"[\W_]+", "", q, flags=re.UNICODE)
    return s[:20] or "query"


def out_path(out_dir, query):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / ("%s_%s_%s.jsonl" % (
        PLATFORM, safe_query_name(query), datetime.now().strftime("%Y%m%d")))


def run_opencli(args, timeout=OP_TIMEOUT):
    """运行 opencli xiaohongshu 子命令，返回 (rc, stdout, stderr) 文本。

    注意：Windows 下 opencli.CMD shim 经 subprocess 调用时 -f json 不生效
    （输出 YAML 且 rc=1），因此优先用 node 直调包内主脚本。
    """
    exe = shutil.which("opencli")
    if exe is None:
        return 127, "", "opencli not found in PATH"
    mainjs = Path(exe).resolve().parent / "node_modules" / "@jackwener" / "opencli" / "dist" / "src" / "main.js"
    node = shutil.which("node")
    if node and mainjs.exists():
        r = subprocess.run([node, str(mainjs), "xiaohongshu"] + args,
                           capture_output=True, timeout=timeout)
        return (r.returncode,
                r.stdout.decode("utf-8", errors="replace"),
                r.stderr.decode("utf-8", errors="replace"))
    r = subprocess.run([exe, "xiaohongshu"] + args,
                       capture_output=True, timeout=timeout)
    return (r.returncode,
            r.stdout.decode("utf-8", errors="replace"),
            r.stderr.decode("utf-8", errors="replace"))


YAML_FIELD_ROW_RE = re.compile(
    r"^\s*-\s*field\s*:\s*(?P<field>.*?)\s*$\n\s*value\s*:\s?(?P<value>.*?)"
    r"\s*(?=^\s*-\s*field\s*:|\Z)",
    re.MULTILINE | re.DOTALL,
)


def strip_quotes(s):
    """去掉 YAML 标量外层成对的引号。"""
    s = (s or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        return s[1:-1].strip()
    return s


def parse_yaml_field_rows(text):
    """YAML 行式兜底解析，无 - field: 匹配返回 None。

    .CMD shim 退路下 -f json 不生效，note 输出为 "- field: x\\n  value: y"
    逐字段行式 YAML；逐对提取为 [{"field","value"}] 列表——与 note 命令
    JSON 输出同形状，rows_to_dict 可直接消费。
    """
    rows = []
    for m in YAML_FIELD_ROW_RE.finditer(text or ""):
        field = strip_quotes(m.group("field"))
        if field:
            rows.append({"field": field, "value": strip_quotes(m.group("value"))})
    return rows or None


def parse_json_output(text):
    """opencli 输出解析：优先 JSON，失败时兜底 YAML 行式输出。

    正常路径（node 直调，-f json 生效）返回 json.loads 结果；退回 .CMD
    shim 时输出行式 YAML（rc=1），由 parse_yaml_field_rows 兜底。
    两者都解析不出返回 None。
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    return parse_yaml_field_rows(text)


def rows_to_dict(rows):
    """opencli note 输出是行式 [{field, value}, ...]，转为普通 dict。"""
    d = {}
    for row in rows or []:
        if isinstance(row, dict) and "field" in row:
            d[str(row["field"])] = row.get("value")
    return d


def parse_note_output(rc, out):
    """note 命令 (rc, stdout) -> (detail dict, shim_fallback bool)。

    判定放宽：rc in (0,1) 且解析出有效字段即成功——rc=1 是 .CMD shim 的
    行式 YAML 输出（-f json 失效），有数据就不算 note_fetch_failed，
    此时 shim_fallback=True 便于追踪。
    """
    if rc not in (0, 1):
        return {}, False
    rows = parse_json_output(out)
    if not isinstance(rows, list):
        return {}, False
    detail = rows_to_dict(rows)
    return detail, rc == 1 and bool(detail)


def parse_comments_output(rc, out):
    """comments 命令 (rc, stdout) -> (rows, shim_fallback bool)，规则同 parse_note_output。"""
    if rc not in (0, 1):
        return None, False
    rows = parse_json_output(out)
    if isinstance(rows, list) and rows:
        return rows, rc == 1
    return None, False


def fetch_note_detail(note_url):
    """note 命令补正文，失败随机 2-4s 重试 1 次。

    返回 (detail, shim_fallback, retries)：retries = 实际重试次数(0/1)，
    重试后拿到正文即算成功（不标 note_fetch_failed），连败才由调用方标记。
    """
    detail, shim = {}, False
    for attempt in (0, 1):
        rc, out, _ = run_opencli(["note", note_url, "-f", "json"])
        detail, shim = parse_note_output(rc, out)
        if pick(detail, "content", "text"):
            return detail, shim, attempt
        if attempt == 0:
            time.sleep(random.uniform(*DETAIL_RETRY_SLEEP))
    return detail, shim, 1


def fetch_comments(note_url, limit):
    """comments 命令拉评论，失败随机 2-4s 重试 1 次。

    返回 (rc, rows, shim_fallback, retries)；rows 为空且 rc!=0 才算失败
    （口径与重试前的旧逻辑一致），重试成功计入 retries。
    """
    rc, rows, shim = 0, None, False
    for attempt in (0, 1):
        rc, out, _ = run_opencli(["comments", note_url, "-f", "json",
                                  "--limit", str(limit)])
        rows, shim = parse_comments_output(rc, out)
        if rows:
            return rc, rows, shim, attempt
        if attempt == 0:
            time.sleep(random.uniform(*DETAIL_RETRY_SLEEP))
    return rc, rows, shim, 1


def parse_yaml_ish(text):
    """解析 opencli 的简易 YAML 输出（顶层 key: value 行）。"""
    d = {}
    for line in (text or "").splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$", line)
        if m and m.group(2):
            d[m.group(1)] = m.group(2)
    return d


def norm_comments(rows, top_n):
    """评论行归一化为 {text, likes, author}，按点赞降序取前 top_n 条。"""
    items = []
    for c in rows or []:
        if not isinstance(c, dict):
            continue
        text = str(pick(c, "text", "content", default="") or "").strip()
        if not text:
            continue
        items.append({
            "text": text[:CONTENT_MAX],
            "likes": parse_likes(pick(c, "likes", "liked_count", default=0)),
            "author": str(pick(c, "author", "nickname", "userId", default="") or ""),
        })
    items.sort(key=lambda x: x["likes"], reverse=True)
    return items[:top_n]


def build_record(query, url, title, content, author, published_at,
                 likes, comments, extra):
    return {
        "platform": PLATFORM,
        "query": query,
        "collected_at": datetime.now().isoformat(timespec="seconds"),
        "url": (url or "").strip(),
        "title": str(title or "").strip()[:CONTENT_MAX],
        "content": str(content or "").strip()[:CONTENT_MAX],
        "author": str(author or "").strip(),
        "published_at": published_at if published_at not in ("", "N/A") else None,
        "likes": parse_likes(likes),
        "comments_count": len(comments),
        "comments": comments,
        "extra": extra or {},
    }


# 需登录标记：探测/搜索命中登录墙时置 True，main 据此 exit 3
NEEDS_LOGIN = [False]


def login_hint():
    print("[xhs] 请运行: python ensure_auth.py --platform xhs", file=sys.stderr)
    print("[xhs] 该命令会打开 Chrome 登录页并每 20s 轮询，等待扫码完成（不会一闪而过），"
          "成功后重试本脚本。", file=sys.stderr)


def check_login():
    """开跑前真实探测登录态（opencli 主站搜索 limit=1 轻探测）。

    缓存态不可信：whoami/login 查创作者中心缓存会 already_logged_in 秒退，
    但主站搜索仍 AUTH_REQUIRED，故已弃用，只认真实搜索通过。

    通过: 更新 data/auth_state.json 并返回 True。
    需登录: 打印 ensure_auth 指引、置 NEEDS_LOGIN，返回 False（main exit 3）。
    """
    status, rc, blob = ac.xhs_probe_status()
    if status == "ok":
        print("[xhs] 登录态有效（真实搜索探测通过）")
        ac.mark_auth_state("xhs", True, probe="opencli_search")
        return True
    if status == "auth_required":
        print("[xhs] 小红书未登录或登录态已失效（主站搜索被登录墙拦截）。", file=sys.stderr)
        login_hint()
        NEEDS_LOGIN[0] = True
        return False
    if status == "browser_missing":
        print("[xhs] opencli 未连接到 Chrome（退出码 69），请先打开 Chrome 再重试。",
              file=sys.stderr)
        login_hint()
        NEEDS_LOGIN[0] = True
        return False
    if status == "opencli_missing":
        print("[xhs] 未找到 opencli 命令，请先运行 check_deps.py 按指引安装 Agent-Reach/OpenCLI。",
              file=sys.stderr)
        return False
    print("[xhs] 登录探测异常（退出码 %s）: %s" % (rc, blob[:200]), file=sys.stderr)
    return False


def search_notes(query, limit):
    """搜索并归一化笔记列表，失败返回 None，无结果返回 []。"""
    rc, out, err = run_opencli(["search", query, "-f", "json", "--limit", str(limit)])
    results = parse_json_output(out) if rc == 0 else None
    if not isinstance(results, list):
        results = parse_json_output(out + "\n" + err)
    if results is None:
        blob = (err or out).strip()
        print("[xhs] 搜索失败（退出码 %s）: %s" % (rc, blob[:300]), file=sys.stderr)
        if rc == 77 or "AUTH_REQUIRED" in blob or "login wall" in blob.lower():
            print("[xhs] 搜索被小红书登录墙拦截，登录态已失效。", file=sys.stderr)
            login_hint()
            NEEDS_LOGIN[0] = True
        return None
    items = []
    for raw in results:
        if not isinstance(raw, dict):
            continue
        url = str(pick(raw, "url", "note_url", "link", default="") or "")
        title = str(pick(raw, "title", "desc", "name", default="") or "")
        if not url and not title:
            continue
        items.append({
            "rank": raw.get("rank"),
            "title": title,
            "author": str(pick(raw, "author", "nickname", "user", default="") or ""),
            "likes": parse_likes(pick(raw, "likes", "liked_count", default=0)),
            "comments_hint": parse_likes(pick(raw, "comments", "comments_count",
                                             "comment_count", default=0)),
            "url": url,
            "author_url": str(pick(raw, "author_url", "user_url", default="") or ""),
            "published_at": pick(raw, "published_at", "time", "publish_time"),
            "content": str(pick(raw, "content", "desc", default="") or ""),
            "note_type": detect_note_type(raw),
        })
    return items


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="采集小红书搜索结果笔记及热门评论（追加写 jsonl）")
    ap.add_argument("--query", required=True, help="搜索关键词（必填）")
    ap.add_argument("--limit", type=int, default=20,
                    help="搜索结果条数上限（默认 20）；逐篇补详情的篇数取 "
                         "min(limit, 20)，50-100 时单轮最多 20 篇带详情")
    ap.add_argument("--days", type=int, default=0,
                    help="时间窗过滤天数（默认 0=不过滤）：只保留最近 N 天内发布的"
                         "笔记；无时间的保留并计 time_unknown")
    ap.add_argument("--sort", type=ac.sort_arg_type,
                    default=ac.parse_sort_spec(ac.DEFAULT_SORT),
                    help="选 top K 补正文/评论的排序键，逗号分隔多值"
                         "（默认 discussion,hot=评论最多∪最热两队列各取 top K "
                         "合并去重，讨论队列在前）。discussion=评论数降序；"
                         "hot=点赞降序；time=发布时间倒序（找房源配 --days 7）。"
                         "旧值 heat 兼容=discussion,hot。可与 --days 组合")
    ap.add_argument("--top-comments", type=int, default=10,
                    help="每篇笔记保留的热门评论条数（默认 10）")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                    help="输出目录（默认 ~/.rent-assist/data/raw）")
    args = ap.parse_args()

    if not check_login():
        sys.exit(3 if NEEDS_LOGIN[0] else 2)

    items = search_notes(args.query, max(1, args.limit))
    if items is None:
        sys.exit(3 if NEEDS_LOGIN[0] else 2)
    # 0 命中自愈：组合词剥掉场景词（保城市前缀+标的）重搜一次，仅一次防死循环；
    # 记录仍挂原 query（复用闸门按调用方请求词匹配）
    if not items:
        dq = lexicon.strip_scenario_words(args.query)
        if dq:
            print("[xhs] 0 命中，剥场景词降级重搜: %s" % dq)
            items = search_notes(dq, max(1, args.limit))
            if items is None:
                sys.exit(3 if NEEDS_LOGIN[0] else 2)

    # --days 时间窗过滤：窗口内保留，无时间保留（计 time_unknown），窗口外丢弃
    items, (win_kept, win_dropped, win_unknown) = apply_time_window(
        items, args.days, "published_at")
    if not items:
        print("[xhs] 搜索无可用结果（%s），未写出任何记录。" % window_stat_seg(
            args.days, win_kept, win_dropped, win_unknown), file=sys.stderr)
        sys.exit(2)
    print("[xhs] 搜索得到 %d 条结果，%s" % (
        win_kept + win_dropped if args.days > 0 else len(items),
        window_stat_seg(args.days, win_kept, win_dropped, win_unknown)))

    # --sort 多队列 union：每个排序键各取 top K（K=min(limit,20)）合并去重后
    # 逐篇补详情（discussion 在前则讨论队列优先；旧 heat 单键已兼容映射）
    top_detail = max(1, min(args.limit, TOP_DETAIL_MAX))
    top = ac.select_union_top(items, args.sort, top_detail, sort_key_of,
                              item_key=lambda it: it.get("url") or id(it))
    print("[xhs] sort=%s 多队列合并去重后取 %d/%d 篇补详情" % (
        ",".join(args.sort), len(top), len(items)))

    records = []
    total_retries = 0
    for idx, it in enumerate(top):
        if idx > 0:
            time.sleep(random.uniform(*SLEEP_RANGE))
        try:
            # note/comments 命令要求 explore 形完整 URL（含 xsec_token），
            # search 返回的 search_result 形直接传会全挂，先规范化
            note_url = canonical_note_url(it["url"])
            extra = {}
            if it["rank"] is not None:
                extra["search_rank"] = it["rank"]
            if it["author_url"]:
                extra["author_url"] = it["author_url"]
            note_id = NOTE_ID_RE.search(note_url)
            if note_id:
                extra["note_id"] = note_id.group(1)
            extra["note_type"] = it.get("note_type") or "unknown"

            # 正文：搜索结果已含正文则跳过 note 调用
            detail = {}
            if it["content"]:
                detail = {"content": it["content"]}
            elif note_url.startswith("http"):
                detail, shim, retries = fetch_note_detail(note_url)
                total_retries += retries
                if retries:
                    extra["fetch_retries"] = retries
                if shim:
                    extra["shim_fallback"] = True  # rc=1 但 YAML 行式兜底拿到了数据
                if not pick(detail, "content", "text"):
                    extra["note_fetch_failed"] = True

            content = str(pick(detail, "content", "text", default="") or it["content"])
            title = str(pick(detail, "title", "name", default="") or it["title"])
            author = str(pick(detail, "author", "nickname", default="") or it["author"])
            likes = parse_likes(pick(detail, "likes", "liked_count",
                                    default=it["likes"]))
            published_at = pick(detail, "published_at", "time") or it["published_at"]
            if detail.get("collects") not in (None, ""):
                extra["collects"] = parse_likes(detail.get("collects"))
            if detail.get("tags") not in (None, ""):
                extra["tags"] = str(detail.get("tags"))
            note_comments_total = parse_likes(detail.get("comments"))
            if note_comments_total:
                extra["note_comments_total"] = note_comments_total

            # 评论：点赞 top N
            comments = []
            if note_url.startswith("http"):
                time.sleep(random.uniform(*SLEEP_RANGE))
                climit = min(max(args.top_comments, 1), COMMENTS_HARD_LIMIT)
                crc, crows, cshim, cretries = fetch_comments(note_url, climit)
                total_retries += cretries
                if cretries:
                    extra["fetch_retries"] = \
                        extra.get("fetch_retries", 0) + cretries
                if crows:
                    comments = norm_comments(crows, args.top_comments)
                    if cshim:
                        extra["shim_fallback"] = True  # rc=1 但兜底解析拿到了数据
                    if not published_at and isinstance(crows[0], dict):
                        t = pick(crows[0], "time", "date")
                        if t:
                            published_at = str(t)
                elif crc != 0:
                    extra["comments_fetch_failed"] = True

            records.append(build_record(
                args.query, note_url, title, content, author, published_at,
                likes, comments, extra))
        except Exception as e:  # 单篇失败不崩，跳过并继续
            print("[xhs] 第 %d 篇处理失败，已跳过: %s" % (idx + 1, e), file=sys.stderr)

    if not records:
        print("[xhs] 未写出任何记录（%d 篇全部失败）。" % len(top), file=sys.stderr)
        sys.exit(2)

    path = out_path(args.out_dir, args.query)
    with path.open("a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("platform=%s fetched=%d/%d retries=%d %s %s file=%s" % (
        PLATFORM, len(records), len(top), total_retries, ac.sort_seg(args.sort),
        window_stat_seg(args.days, win_kept, win_dropped, win_unknown),
        path.name))

    # 失败率守卫：note 或 comments 抓取失败的记录占比 >= 50% 时数据空心风险高，
    # 记录已落盘但以退出码 4 上报数据质量差
    failed_n, total_n = detail_failure_stats(records)
    if total_n and failed_n * 2 >= total_n:
        print("[xhs] 详情/评论抓取失败率过高(%d/%d)，数据可能空心" % (
            failed_n, total_n), file=sys.stderr)
        sys.exit(4)
    sys.exit(0)


if __name__ == "__main__":
    main()
