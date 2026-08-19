#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rent-assist: 全网（web）租房内容采集脚本。

链路: mcporter 调 Exa 语义搜索（免费无 Key） -> 每个命中写一条记录，
再取前几个 URL 抓正文写独立记录。正文降级链（任一级失败顺延下一级）:
  1) Jina Reader (https://r.jina.ai/<url>)
  2) requests 直连抓 HTML（UA、15s、仅 http/https）+ stdlib html.parser 剥正文
  3) 仍失败 -> 用 Exa 摘要兜底（extra.content_source="summary_only"），不丢弃
Exa 调用失败（非零退出/offline/超时）随机 3-5s 重试 1 次；仍失败的 query 记入
stderr 失败清单（附可直接补跑的命令），继续其余 query；仅全部 query 失败才
非零退出。追加写 jsonl（UTF-8，每行一条记录）。

query 完全由调用方传入，本脚本原样使用不改写（检索词组合是调用方的职责）；
--query 可重复传多个（与 run_collect 的单 query 传参方式兼容）。

用法:
    python collect_web.py --query "天通苑 租房" --limit 5
    python collect_web.py --query "天通苑 租房" --days 180  # 只留近 180 天
    python collect_web.py --query "天通苑" --days 7 --sort time  # 近一周最新发布
    python collect_web.py --query "天通苑 租房" --query "回龙观 租房"  # 多 query

--days N: 时间窗过滤(默认 0=不过滤)。Exa 结果带 Published 字段的按发布时间过滤:
    窗口外丢弃; 无时间的保留并计 time_unknown。
--sort discussion,hot,time 逗号分隔多值(默认 discussion,hot):
    正文抓取目标 = 每个排序键各取 top K(K=JINA_FETCH_COUNT) 合并去重
    (多队列 union)。Exa 结果无评论/点赞数, discussion/hot 退化为原始相关度
    序; time=发布时间降序(无时间排后)。其余命中按首键序排在后面(全部命中
    仍各写一条记录)。旧单值 heat 兼容=discussion,hot。
    与 --days 可组合(如 --days 7 --sort time=最近一周最新发布的)。

前置条件（见 check_deps.py）: agent-reach 已装、mcporter 可用（Exa 后端 ok）。

退出码: 0 = 至少一个 query 写出记录（个别 query 失败见 stderr 失败清单）；
    2 = mcporter 缺失 / 全部 query 失败 / 未写出任何记录。
"""

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth_common as ac

parse_pub_datetime = ac.parse_pub_datetime
apply_time_window = ac.apply_time_window
window_stat_seg = ac.window_stat_seg

PLATFORM = "web"
DEFAULT_OUT_DIR = ac.data_dir("raw")  # ~/.rent-assist/data/raw（RENT_ASSIST_DATA 可覆盖）
CONTENT_MAX = 2000            # content 字段最大字符数
SLEEP_RANGE = (2.0, 5.0)      # 请求间隔（秒，随机）
EXA_RETRY_SLEEP = (3.0, 5.0)  # Exa 失败重试前随机等待（秒）
JINA_FETCH_COUNT = 3          # 经 Jina 抓正文的 URL 数上限
MCPORTER_TIMEOUT = 120
HTTP_TIMEOUT = 30
DIRECT_TIMEOUT = 15           # Jina 失败后直连兜底的超时（秒）
UA = "Mozilla/5.0 (compatible; rent-assist-collector/1.0)"
KEYS_ENV_PATH = Path(r"E:\租房\config\keys.env")  # 密钥文件（KEY=VALUE 纯文本）
JINA_RETRY_BACKOFF = (2, 4, 8)  # Jina 限流(429/503)指数退避秒数，共重试 3 次

# 正文记录 extra.source 取值（extra.content_source 统一用 jina/direct/summary_only）
SRC_NAMES = {"jina": "jina_reader", "direct": "direct_html",
             "summary_only": "exa_summary"}

BLOCK_SPLIT_RE = re.compile(r"^-{3,}\s*$", re.M)

# 正文发布时间线索模式（Published 字段缺失/无效时兜底）。每模式捕获
# Y/M/D 三组（时间部分与"北京"等非时间后缀自然被截断丢弃）；取全文首个命中。
CONTENT_DATE_PATTERNS = (
    re.compile(r"最近更新时间[：:]\s*(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})"),
    re.compile(r"发布于[：:]?\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})"),
    re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日"),
    re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})(?=\s+\d{1,2}:\d{2}|\D|$)"),
)
DATE_MIN_YEAR = 2015        # 早于此视为不合理，不填


def content_date_extract(head):
    """从 title+正文前 500 字提取发布日期，返回 ISO 串（YYYY-MM-DD）或 ""。

    多模式取**位置最早**的首个命中；只捕日期三组，"HH:MM:SS"/"北京"等
    后缀不进结果。日期经 parse_pub_datetime 校验：无法解析、晚于当前
    时间 1 天以上（未来）或早于 2015 年的视为无效返回 ""。
    """
    best = None
    for pat in CONTENT_DATE_PATTERNS:
        m = pat.search(head or "")
        if m and (best is None or m.start() < best.start()):
            best = m
    if not best:
        return ""
    iso = "%04d-%02d-%02d" % tuple(int(g) for g in best.groups())
    dt = parse_pub_datetime(iso)
    if dt is None or dt.year < DATE_MIN_YEAR or dt > datetime.now() + timedelta(days=1):
        return ""
    return iso


def fill_date_from_content(rec, counter):
    """published_at 缺失/无效时，从 title+正文前 500 字补发布日期。

    补到则填 published_at、extra 标 date_from_content=True，并给
    counter["date_from_content"] 计数（就地累加，供摘要行展示）。
    """
    cur = rec.get("published_at")
    if cur and parse_pub_datetime(cur) is not None:
        return
    head = (rec.get("title", "") + "\n" + (rec.get("content") or ""))[:500]
    iso = content_date_extract(head)
    if not iso:
        return
    rec["published_at"] = iso
    rec.setdefault("extra", {})["date_from_content"] = True
    counter["date_from_content"] = counter.get("date_from_content", 0) + 1

FIELD_SPLIT_RE = re.compile(r"^(Title|URL|Published|Author|Highlights?)\s*:\s*",
                            re.M)
NA_VALUES = ("", "N/A", "NA", "UNKNOWN")


def safe_query_name(q):
    """文件名安全化：仅保留中文/字母/数字，截 20 字。"""
    s = re.sub(r"[\W_]+", "", q, flags=re.UNICODE)
    return s[:20] or "query"


LINE_BREAK_LIKE = "  "  # json.dumps(ensure_ascii=False) 不转义、
#                                      # 但 str.splitlines() 会按其切行的字符
#                                      # （U+0085 NEL / U+2028 / U+2029）。网页
#                                      # 正文里出现会把一条记录在下游读成多行，
#                                      # 产生"Expecting value: char 0"坏行。


def write_records(path, records):
    """追加写 jsonl：空记录跳过；序列化后把 splitlines 会切行的字符
    （U+0085/U+2028/U+2029，json.dumps 不转义）替换为空格，并做非空单行
    校验，保证下游按行读取时每行都是一条完整 JSON。返回实际写出条数。"""
    written = 0
    with path.open("a", encoding="utf-8") as f:
        for rec in records:
            if not isinstance(rec, dict) or not rec:
                print("[web] 跳过空记录（测试加固：非空校验）。", file=sys.stderr)
                continue
            line = json.dumps(rec, ensure_ascii=False)
            for ch in LINE_BREAK_LIKE:
                line = line.replace(ch, " ")
            line = line.strip()
            if not line:
                print("[web] 跳过序列化后为空的记录（测试加固：非空校验）。",
                      file=sys.stderr)
                continue
            f.write(line + "\n")
            written += 1
    return written


def out_path(out_dir, query):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / ("%s_%s_%s.jsonl" % (
        PLATFORM, safe_query_name(query), datetime.now().strftime("%Y%m%d")))


def run_mcporter(exe, params, output=None):
    """运行 mcporter call exa.web_search_exa，返回 (rc, stdout, stderr) 文本。

    超时/无法启动也归一为非零 rc（124/1），由上层 exa_search 的重试统一处理。
    """
    cmd = [exe, "call", "exa.web_search_exa"]
    for k, v in params.items():
        cmd.append("%s=%s" % (k, v))
    if output:
        cmd += ["--output", output]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=MCPORTER_TIMEOUT)
    except subprocess.TimeoutExpired:
        return 124, "", "mcporter timeout after %ds" % MCPORTER_TIMEOUT
    except OSError as e:
        return 1, "", "mcporter failed to start: %s" % e
    return (r.returncode,
            r.stdout.decode("utf-8", errors="replace"),
            r.stderr.decode("utf-8", errors="replace"))


def extract_json(text):
    """尽力从文本中提取 JSON 对象/数组（容忍外围日志行）。"""
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
    return None


def unwrap_envelope(data):
    """剥离 MCP 结果信封 {"content":[{"type":"text","text":"..."}]}。

    返回 list/dict（JSON 结果）或 str（text 块状格式原文）。
    """
    for _ in range(4):
        if isinstance(data, dict):
            if isinstance(data.get("result"), (dict, list)):
                data = data["result"]
                continue
            if isinstance(data.get("content"), list):
                texts = [item.get("text") for item in data["content"]
                         if isinstance(item, dict)
                         and isinstance(item.get("text"), str)]
                joined = "\n".join(texts).strip()
                if not joined:
                    return data
                inner = extract_json(joined)
                if inner is None:
                    return joined
                data = inner
                continue
        return data
    return data


def parse_text_blocks(text):
    """解析 text 输出的块状格式: Title:/URL:/Published:/Author:/Highlights:，--- 分隔。"""
    keymap = {"Title": "title", "URL": "url", "Published": "published",
              "Author": "author", "Highlight": "highlights",
              "Highlights": "highlights"}
    hits = []
    for block in BLOCK_SPLIT_RE.split(text or ""):
        if "URL:" not in block and "Title:" not in block:
            continue
        parts = FIELD_SPLIT_RE.split(block)
        hit = {"title": "", "url": "", "published": "", "author": "",
               "highlights": ""}
        for i in range(1, len(parts) - 1, 2):
            key = keymap.get(parts[i])
            val = (parts[i + 1] or "").strip()
            if key and val:
                if key == "highlights" and hit["highlights"]:
                    hit["highlights"] += "\n" + val
                else:
                    hit[key] = val
        if hit["url"] or hit["title"]:
            hits.append(hit)
    return hits


def norm_hits(parsed):
    """把 Exa 结果（JSON 结构或 text 块 str）统一为 dict 列表。"""
    raw = []
    if isinstance(parsed, str):
        raw = parse_text_blocks(parsed)
    elif isinstance(parsed, list):
        raw = [h for h in parsed if isinstance(h, dict)]
    elif isinstance(parsed, dict):
        for key in ("results", "data", "hits"):
            if isinstance(parsed.get(key), list):
                raw = [h for h in parsed[key] if isinstance(h, dict)]
                break
        if not raw and (parsed.get("url") or parsed.get("URL")):
            raw = [parsed]

    def str_field(h, *keys):
        for k in keys:
            if k in h and h[k] not in (None, ""):
                return str(h[k]).strip()
        return ""

    hits = []
    for h in raw:
        url = str_field(h, "url", "URL")
        title = str_field(h, "title", "Title")
        if not url and not title:
            continue
        highlights = h.get("highlights", h.get("Highlights",
                    h.get("text", h.get("Text", ""))))
        if isinstance(highlights, list):
            highlights = "\n".join(str(x) for x in highlights if x)
        hits.append({
            "url": url,
            "title": title,
            "published": str_field(h, "published", "publishedDate", "Published"),
            "author": str_field(h, "author", "Author"),
            "highlights": str(highlights or "").strip(),
        })
    return hits


def exa_search_once(exe, query, limit):
    """单次 Exa 搜索: query 由调用方传入、原样使用不改写。
    优先 --output json，失败则不带 --output 按 text 块状解析。"""
    params = {"query": query, "numResults": str(limit)}

    rc, out, err = run_mcporter(exe, params, output="json")
    if rc == 0:
        data = extract_json(out)
        if data is not None:
            hits = norm_hits(unwrap_envelope(data))
            if hits:
                return hits
        else:
            hits = parse_text_blocks(out)
            if hits:
                return norm_hits(hits)
    print("[web] --output json 路径未取到结果（rc=%s），改用默认 text 输出兜底..."
          % rc, file=sys.stderr)

    rc2, out2, err2 = run_mcporter(exe, params)
    if rc2 == 0:
        data = extract_json(out2)
        parsed = unwrap_envelope(data) if data is not None else out2
        hits = norm_hits(parsed)
        if hits:
            return hits
    print("[web] Exa 单次搜索失败（mcporter rc=%s %s）" % (
        rc2, (err2 or out2).strip()[:300]), file=sys.stderr)
    return []


def exa_search(exe, query, limit):
    """Exa 搜索 + 失败重试 1 次：本轮未取到结果（非零退出/offline/超时/空）
    随机 3-5s 后重试一次；仍失败返回 []，由调用方记入失败清单继续。"""
    hits = exa_search_once(exe, query, limit)
    if hits:
        return hits
    wait = random.uniform(*EXA_RETRY_SLEEP)
    print("[web] Exa 本轮失败（offline/非零退出），%.1fs 后重试 1 次: %s"
          % (wait, query[:40]), file=sys.stderr)
    time.sleep(wait)
    return exa_search_once(exe, query, limit)


def sort_key_of(hit, mode):
    """单命中在某排序键下的键（供 ac.select_union_top 多队列 union 使用）。

    Exa 结果无评论/点赞数: discussion/hot 键全为 0, 降序稳定排序即保持
    相关度原序; time = 发布时间降序（无时间排最后）。
    """
    return ac.sort_key_for(mode, 0, 0, hit.get("published"))


def parse_keys_env(path, name):
    """从 KEY=VALUE 纯文本文件找 name（大小写不敏感），返回去引号值或 ""。

    容忍空行/注释行(#开头)/键值两侧空格/文件不存在或读失败。不打印任何内容。
    """
    try:
        lines = Path(path).read_text(encoding="utf-8",
                                     errors="replace").splitlines()
    except OSError:
        return ""
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip().casefold() == name.casefold():
            return v.strip().strip('"').strip("'").strip()
    return ""


_JINA_KEY = ""          # 只进内存，绝不打印到任何输出
_JINA_KEY_LOADED = False


def load_jina_key():
    """读 JINA_API_KEY：环境变量优先，否则 E:\\租房\\config\\keys.env；
    都没有返回 ""（无 key 模式，行为与未加固前完全一致）。结果缓存只读一次。
    """
    global _JINA_KEY, _JINA_KEY_LOADED
    if _JINA_KEY_LOADED:
        return _JINA_KEY
    _JINA_KEY_LOADED = True
    _JINA_KEY = ((os.environ.get("JINA_API_KEY") or "").strip()
                 or parse_keys_env(KEYS_ENV_PATH, "JINA_API_KEY"))
    return _JINA_KEY


def _jina_rate_limited(r):
    """429/503 或非 200 正文含明确限流字样（免费池/超额常见报文）。"""
    if r.status_code in (429, 503):
        return True
    if r.status_code != 200:
        text = (r.text or "").lower()
        return "rate limit" in text or "too many requests" in text
    return False


def jina_fetch(url):
    """经 https://r.jina.ai/<url> 抓网页正文，失败返回空串（走降级链）。

    有 JINA_API_KEY（环境变量 > keys.env）时请求带 Authorization: Bearer
    <key>（独立配额约 200RPM），遇限流按 2/4/8s 指数退避共重试 3 次，仍失败
    返回空串交由降级链兜底；超时/非限流 5xx 不重试。无 key 模式单次请求、
    不带鉴权头、不重试，行为与加固前完全一致。key 只进请求头，绝不打印。
    """
    key = load_jina_key()
    headers = {"User-Agent": UA}
    if key:
        headers["Authorization"] = "Bearer " + key
    tries = len(JINA_RETRY_BACKOFF) + 1 if key else 1
    for attempt in range(1, tries + 1):
        if attempt > 1:
            time.sleep(JINA_RETRY_BACKOFF[attempt - 2])
        try:
            r = requests.get("https://r.jina.ai/" + url, timeout=HTTP_TIMEOUT,
                             headers=headers)
        except requests.RequestException as e:
            print("[web] Jina 抓取失败(%s): %s" % (url[:80], e), file=sys.stderr)
            return ""
        if r.status_code == 200 and r.text.strip():
            return r.text.strip()
        if not key or not _jina_rate_limited(r):
            return ""
        if attempt < tries:
            print("[web] Jina 限流(HTTP %s)，%ds 后重试 %d/%d: %s" % (
                r.status_code, JINA_RETRY_BACKOFF[attempt - 1], attempt,
                len(JINA_RETRY_BACKOFF), url[:80]), file=sys.stderr)
    print("[web] Jina 限流重试 %d 次仍失败，走降级链: %s" % (
        len(JINA_RETRY_BACKOFF), url[:80]), file=sys.stderr)
    return ""


class _HTMLTextParser(HTMLParser):
    """剥正文文本：丢弃 script/style 等标签内的内容，收集其余文本节点。"""
    SKIP_TAGS = ("script", "style", "noscript", "template", "svg")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            data = data.strip()
            if data:
                self.parts.append(data)


def html_to_text(html_str):
    """HTML -> 纯文本（stdlib html.parser，容错畸形标签；失败返回 ""）。"""
    p = _HTMLTextParser()
    try:
        p.feed(html_str or "")
        p.close()
    except Exception:
        pass
    return "\n".join(p.parts)


def direct_fetch(url):
    """Jina 失败后的直连兜底：requests 抓 HTML（仅 http/https、UA、15s），
    stdlib 剥正文文本；失败返回空串。"""
    if not url.lower().startswith(("http://", "https://")):
        print("[web] 非 http(s) 地址，跳过直连: %s" % url[:80], file=sys.stderr)
        return ""
    try:
        r = requests.get(url, timeout=DIRECT_TIMEOUT, headers={"User-Agent": UA})
        if r.status_code != 200:
            print("[web] 直连 HTTP %s: %s" % (r.status_code, url[:80]),
                  file=sys.stderr)
            return ""
        if r.encoding is None:
            r.encoding = r.apparent_encoding
        return html_to_text(r.text)
    except requests.RequestException as e:
        print("[web] 直连抓取失败(%s): %s" % (url[:80], e), file=sys.stderr)
        return ""


def fetch_content(url):
    """正文降级链: Jina Reader -> requests 直连+stdlib 剥文本 -> 摘要兜底。

    返回 (body, tag): tag in {"jina","direct"} 时 body 为抓到的正文；
    tag="summary_only" 时 body 为空串（调用方用 Exa 摘要补 content，不丢弃）。
    """
    if url:
        body = jina_fetch(url)
        if body:
            return body, "jina"
        body = direct_fetch(url)
        if body:
            return body, "direct"
    return "", "summary_only"


def make_record(query, hit, content, collected_at, extra):
    """由 Exa 命中构造一条 jsonl 记录（搜索命中记录与正文记录共用）。"""
    return {
        "platform": PLATFORM,
        "query": query,
        "collected_at": collected_at,
        "url": hit["url"],
        "title": hit["title"][:CONTENT_MAX],
        "content": (content or "")[:CONTENT_MAX],
        "author": hit["author"] if hit["author"].upper() not in NA_VALUES else "",
        "published_at": (hit["published"] or None)
        if hit["published"].upper() not in NA_VALUES else None,
        "likes": 0,
        "comments_count": 0,
        "comments": [],
        "extra": extra,
    }


def src_seg(counts):
    """摘要行三来源统计段: content_src=jina:2,direct:1,summary_only:0；
    有 JINA key 时追加 jina_key=yes（只标有无，不泄露值）。"""
    seg = "content_src=" + ",".join(
        "%s:%d" % (k, counts.get(k, 0)) for k in ("jina", "direct", "summary_only"))
    if load_jina_key():
        seg += " jina_key=yes"
    return seg


def print_failed_list(failed, args):
    """stderr 列出失败 query 清单 + 可直接补跑的完整命令。

    run_collect 侧对退出码非 0 的 query 不记 done，重跑编排器会自动重试；
    这里再给出单脚本补跑命令，便于手动补。
    """
    print("[web] 失败 query %d 个（重试后仍不可用/过滤后为空，未写出记录，可补跑）:"
          % len(failed), file=sys.stderr)
    for q, reason in failed:
        print("[web-failed] %s (%s)" % (q, reason), file=sys.stderr)
    cmd = [sys.executable, str(Path(sys.argv[0]).resolve())]
    for q, _ in failed:
        cmd += ["--query", '"%s"' % q]
    cmd += ["--limit", str(args.limit)]
    if args.days > 0:
        cmd += ["--days", str(args.days)]
    cmd += ["--sort", ",".join(args.sort), "--out-dir", str(args.out_dir)]
    print("[web] 补跑: %s" % " ".join(cmd), file=sys.stderr)


def collect_query(exe, query, args, src_counts):
    """采单个 query：Exa 搜索（含重试）-> 时间窗 -> 正文降级链 -> 追加写
    当日 jsonl，并打印该 query 的 platform= 摘要行。

    返回 (写出条数, 尝试条数, (kept,dropped,unknown), 失败原因或 None)；
    src_counts 为三来源（jina/direct/summary_only）累计计数，就地累加。
    """
    hits = exa_search(exe, query, max(1, args.limit))
    if not hits:
        return 0, 0, (0, 0, 0), "exa_unavailable"
    print("[web] Exa 搜索得到 %d 条命中" % len(hits))

    # --days 时间窗过滤：Exa 结果带 Published 的按窗口过滤；无时间保留计 time_unknown
    hits, win3 = apply_time_window(hits, args.days, "published")
    if not hits:
        print("[web] 时间窗过滤后无剩余结果（%s）。" % window_stat_seg(
            args.days, *win3), file=sys.stderr)
        return 0, 0, win3, "window_empty"

    # --sort 多队列 union：正文抓取目标 = 每个排序键各取 top K
    # （K=JINA_FETCH_COUNT）合并去重（首键队列在前）；其余命中按首键序跟后
    # （全部命中仍各写一条记录）
    jina_k = min(JINA_FETCH_COUNT, len(hits))
    jina_targets = ac.select_union_top(
        hits, args.sort, jina_k, sort_key_of,
        item_key=lambda h: h["url"] or h["title"])
    target_ids = {id(h) for h in jina_targets}
    rest = [h for h in hits if id(h) not in target_ids]
    if rest:
        first_mode = args.sort[0]
        rest.sort(key=lambda h: sort_key_of(h, first_mode), reverse=True)
    hits = jina_targets + rest

    def now():
        return datetime.now().isoformat(timespec="seconds")

    # 1) 每个搜索命中一条记录（Published 缺失/无效时从正文补日期，计 meta）
    date_meta = {}
    records = [make_record(query, h, h["highlights"], now(), {"source": "exa"})
               for h in hits]
    for r in records:
        fill_date_from_content(r, date_meta)

    # 2) union 选出的目标 URL 走正文降级链（jina/direct/summary_only），
    #    各写一条独立记录——三级都失败也保留 Exa 摘要兜底，不丢弃
    local = {"jina": 0, "direct": 0, "summary_only": 0}
    attempted = len(records)
    for h in jina_targets:
        attempted += 1
        time.sleep(random.uniform(*SLEEP_RANGE))
        body, tag = fetch_content(h["url"])
        if tag == "summary_only":
            body = h["highlights"]
        local[tag] += 1
        src_counts[tag] += 1
        rec = make_record(
            query, h, body, now(),
            {"source": SRC_NAMES[tag], "content_source": tag})
        fill_date_from_content(rec, date_meta)
        records.append(rec)

    path = out_path(args.out_dir, query)
    written_recs = write_records(path, records)
    date_seg = (" date_from_content=%d" % date_meta["date_from_content"]
                if date_meta.get("date_from_content") else "")
    print("platform=%s fetched=%d/%d %s %s %s%s file=%s" % (
        PLATFORM, written_recs, attempted, ac.sort_seg(args.sort),
        window_stat_seg(args.days, *win3), src_seg(local), date_seg, path.name))
    return written_recs, attempted, win3, None


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="采集全网租房内容（Exa 搜索 + Jina/直连正文，追加写 jsonl）")
    ap.add_argument("--query", required=True, action="append", metavar="Q",
                    help="搜索关键词（必填，可重复传多个；原样传给 Exa，"
                         "不做改写）")
    ap.add_argument("--limit", type=int, default=20,
                    help="搜索结果条数上限（默认 20）")
    ap.add_argument("--days", type=int, default=0,
                    help="时间窗过滤天数（默认 0=不过滤）：只保留最近 N 天发布的"
                         "结果（无时间的保留并计 time_unknown）")
    ap.add_argument("--sort", type=ac.sort_arg_type,
                    default=ac.parse_sort_spec(ac.DEFAULT_SORT),
                    help="排序键, 逗号分隔多值（默认 discussion,hot）。决定 "
                         "正文抓取目标: 每键各取 top K 合并去重（多队列 "
                         "union）。Exa 无互动数, discussion/hot 保持相关度原序；"
                         "time=发布时间降序（找房源配 --days 7）。旧值 heat "
                         "兼容=discussion,hot。可与 --days 组合")
    ap.add_argument("--top-comments", type=int, default=10,
                    help="兼容参数，web 平台无评论概念，忽略")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                    help="输出目录（默认 ~/.rent-assist/data/raw）")
    args = ap.parse_args()

    queries, seen = [], set()
    for q in args.query:          # action="append" -> 至少 1 个，去重保序
        if q and q not in seen:
            seen.add(q)
            queries.append(q)
    if not queries:
        print("[web] --query 不能为空。", file=sys.stderr)
        sys.exit(2)

    exe = shutil.which("mcporter")
    if exe is None:
        print("[web] 未找到命令 mcporter，Exa 搜索不可用；Jina 无法独立搜索，退出。",
              file=sys.stderr)
        print("[web] 请先安装 Agent-Reach（github.com/Panniantong/Agent-Reach，"
              "agent-reach install --system）；注意 PyPI 同名 agent-reach 为冒名包，"
              "禁止 pip 安装。", file=sys.stderr)
        sys.exit(2)

    src_counts = {"jina": 0, "direct": 0, "summary_only": 0}
    failed = []                    # [(query, reason)]
    ok_queries = total_written = total_attempted = 0
    win_sum = (0, 0, 0)

    for q in queries:
        written, attempted, win3, reason = collect_query(exe, q, args, src_counts)
        win_sum = tuple(a + b for a, b in zip(win_sum, win3))
        if reason is not None:
            failed.append((q, reason))
            continue
        ok_queries += 1
        total_written += written
        total_attempted += attempted

    if failed:
        print_failed_list(failed, args)
    if total_written == 0:
        print("[web] 未写出任何记录。", file=sys.stderr)
        sys.exit(2)
    if len(queries) > 1:           # 多 query 时补一行汇总（单 query 已逐条打印）
        print("platform=%s queries=%d/%d fetched=%d/%d %s %s %s" % (
            PLATFORM, ok_queries, len(queries), total_written,
            total_attempted, ac.sort_seg(args.sort),
            window_stat_seg(args.days, *win_sum), src_seg(src_counts)))
    sys.exit(0)


if __name__ == "__main__":
    main()
