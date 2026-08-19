#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""豆瓣小组帖采集(两级降级, 租房定向)。

级别1: requests 直抓列表页。按 --intent 分两种搜索意图(搜索层防求租帖混入):
       word(口碑类 A/B/C, 默认): 不拼租赁泛词, 只用 query 原词裸搜
       (cat=1013, 实测免登录可访问, 搜索级请求总数<=2*limit, 请求间随机 sleep 2-5s),
       标题须命中 query 全串或其每个分词(AND 语义), 不启用固定租赁词表
       (求租帖标题恰含"租房/合租", 词表会放行纯噪声)。
       listing(找房类 D, 旧行为): query 与默认供给侧租赁词(转租/直租/合租)拼接
       分次搜索, 标题命中 query 或租赁词(转租/直租/合租/主卧/次卧/整租/押付)之一
       即保留, 否则丢弃并计入 filtered_irrelevant。
       也可用 --group <group_id> 直接抓指定租房小组讨论列表页再按标题过滤。
       之后逐帖抓详情(免登录大概率 403/没有访问权限, 此时仍写入搜索级元数据,
       extra.blocked=true)。
级别2: 若正文被拦比例达到半数(或搜索本身被拦), 先随机 2-5s 重试 1 次仍 403 才
       触发 Playwright 浏览器兜底, 持久化 context(user_data_dir=
       <tools>/douban-profile, 自动创建):
       ① 有登录素材(档案非空/DOUBAN_COOKIE)先 headless 快速开 context 查档案
         cookie 有效性(context.cookies() 判 dbcl2, HttpOnly 页面 JS 看不到)并试
         一次列表页(15s 超时): 有 dbcl2 或列表匿名可采 → 全程无头静默采集, 不弹窗;
       ② 无登录态且列表被拦时, 进二级前已用 sys.stdin.isatty() 判定过: 非交互
         环境(无 stdin/管道/分离进程)绝不弹窗也绝不 input() 等人, 打印登录指引并
         记录原因后 exit 3(需登录, 与 collect_xhs 语义一致; 级别1结果已落盘);
       ③ 交互终端才弹可见窗口(学 ensure_auth: 每 5s 轮询 context.cookies() 等
         dbcl2, 最长 180s, 不用 input()), 人工登录一次后 cookie 落盘, 后续免登录。
       滑块人工介入仅保留在可见窗口路径(无头模式遇滑块记被拦)。
       未安装 playwright 时提示安装命令并 exit 2。
       cookie 判定/轮询等待/持久化 context 启动等公共函数在 auth_common.py
       (ensure_auth.py 共用)。

用法:
    python collect_douban.py --query 天通苑 --limit 20 --top-comments 10
    python collect_douban.py --query "天通苑 住过" --intent word   # 口碑: 裸搜防求租帖
    python collect_douban.py --query 天通苑 --intent listing     # 找房: 拼供给侧词
    python collect_douban.py --query 天通苑 --group beijingzufang
    DOUBAN_COOKIE='bid=xx; ...' python collect_douban.py --query 天通苑
    python collect_douban.py --query test --limit 1   # 前台跑一次完成人工登录

--days N: 时间窗过滤(默认 0=不过滤)。列表行(搜索页 td-time 的 title 属性为完整
    时间)按发表时间过滤后再抓详情: 窗口外丢弃; 无时间的保留并计 time_unknown。
--sort discussion,hot,time 逗号分隔多值(默认 discussion,hot):
    discussion=评论最多(回复数降序); hot=最热(点赞降序, 列表行无点赞数时同序);
    time=发表时间倒序。多键=多队列 union: 每键各取 top K(豆瓣原逻辑=全部保留
    帖都抓详情, 即 K=帖数)合并去重后按序抓详情, discussion 队列在前。
    旧单值 heat 兼容=discussion,hot。与 --days 可组合(找房源: --days 7
    --sort time)。
"""
import argparse
import html as html_lib
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from urllib.parse import quote

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lexicon  # noqa: E402  COMMON_CITY_WORDS（豆瓣降级跳城市词）
import auth_common as ac

parse_pub_datetime = ac.parse_pub_datetime
apply_time_window = ac.apply_time_window
window_stat_seg = ac.window_stat_seg

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT_DIR = str(ac.data_dir("raw"))  # ~/.rent-assist/data/raw（RENT_ASSIST_DATA 可覆盖）
# 浏览器内核路径(PLAYWRIGHT_BROWSERS_PATH)与轮询等待/cookie 判定等公共实现
# 均在 auth_common.py（ensure_auth.py 共用）。
# 级别2 持久化登录档案: 登录一次 cookie 落盘, 后续免登录(目录自动创建)。
DOUBAN_PROFILE_DIR = str(ac.DOUBAN_PROFILE_DIR)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
SEARCH_URL = "https://www.douban.com/group/search?cat=1013&q={q}&start={start}"
GROUP_URL = "https://www.douban.com/group/{gid}/discussion?start={start}"
DEFAULT_RENTAL_WORDS = "转租 直租 合租"  # listing 默认供给侧词; 泛词"租房"是求租噪声源, 不进默认
TITLE_RENTAL_WORDS = ("租房", "转租", "直租", "合租", "主卧", "次卧", "整租", "押付")
PAGE_SIZE = 25
MAX_PAGES = 10


class InteractiveAbort(Exception):
    """非交互环境无法等待人工介入(滑块等)。"""


class NeedsLogin(Exception):
    """需人工登录但当前为非交互环境: main 捕获后打印摘要并 exit 3(与 xhs 语义一致)。"""


def rand_sleep():
    time.sleep(random.uniform(2, 5))


def sanitize_query(query):
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", query).strip("_")
    return safe[:50] or "query"


def strip_tags(fragment):
    frag = re.sub(r"<(script|style)\b.*?</\1>", " ", fragment, flags=re.S | re.I)
    frag = re.sub(r"<br\s*/?>", "\n", frag, flags=re.I)
    frag = re.sub(r"</p\s*>", "\n", frag, flags=re.I)
    text = re.sub(r"<[^>]+>", "", frag)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t\r\f]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def split_words(raw):
    """把空格/逗号/顿号分隔的词拆成非空列表。"""
    return [w for w in re.split(r"[\s,，、]+", (raw or "").strip()) if w]


def title_relevant(title, query, rental_words=(), intent="word"):
    """标题相关性过滤, 按意图两套语义:

    word(口碑): 只认 query 主词——命中 query 全串, 或其每个分词(>=2字)都出现
    (AND 语义); 不启用租赁词表(求租帖标题恰含"租房/合租", 词表形同虚设会放行)。
    listing(找房, 旧行为): 命中 query 关键词或任一租赁词(固定词表+自定义)之一
    即保留。固定租赁词: 租房/转租/直租/合租/主卧/次卧/整租/押付。
    """
    if not title:
        return False
    q = (query or "").strip()
    if q and q in title:
        return True
    toks = [t for t in split_words(q) if len(t) >= 2]
    if intent == "word":
        return bool(toks) and all(t in title for t in toks)
    if any(t in title for t in toks):
        return True
    words = tuple(TITLE_RENTAL_WORDS) + tuple(rental_words or ())
    return any(w and w in title for w in words)


def sort_key_of(row, mode):
    """单帖在某排序键下的键（供 ac.select_union_top 多队列 union 使用）。

    discussion = reply_count（列表行即回复数）；hot = likes（列表行无点赞数，
    即同序）；time = published_at 倒序（无时间排最后）。
    """
    return ac.sort_key_for(mode, row.get("reply_count") or 0,
                           row.get("likes") or 0, row.get("published_at"))


# ---------------------------------------------------------------- 搜索页解析
def parse_search_rows(html_text):
    """解析列表行, 兼容两种页面:
    - 搜索结果页: td-subject 链接/标题, td-time title 属性完整时间, td-reply 回复数
    - 小组讨论页: td class="title" 链接, td class="time" 短时间, r-count 回复数
    """
    rows = []
    for tr in re.findall(r"<tr\b.*?</tr>", html_text, re.S):
        if "td-subject" not in tr and 'class="title"' not in tr:
            continue
        a = re.search(
            r'<a\b[^>]*href="(?:https://www\.douban\.com)?/group/topic/(\d+)/[^"]*"'
            r'[^>]*>', tr)
        if not a:
            continue
        topic_id = a.group(1)
        atag = a.group(0)
        tm = re.search(r'title="([^"]*)"', atag)
        title = tm.group(1).strip() if tm else ""
        if not title:
            tm2 = re.search(r">\s*([^<>]{2,120}?)\s*</a>", tr)
            title = tm2.group(1).strip() if tm2 else ""
        timem = re.search(r'td-time[^>]*\stitle="([^"]+)"', tr)
        if not timem:
            timem = re.search(r'class="time"[^>]*>\s*([^<]+?)\s*<', tr)
        published_at = timem.group(1).strip() if timem else ""
        rm = re.search(r"td-reply[^>]*>.*?(\d+)\s*回复", tr, re.S)
        if not rm:
            rm = re.search(r'r-count[^>]*>\s*(\d+)', tr)
        reply_count = int(rm.group(1)) if rm else None
        gm = re.search(
            r'href="https://www\.douban\.com/group/([^/"?]+)/?"[^>]*>\s*([^<]+?)\s*</a>', tr)
        rows.append({
            "topic_id": topic_id,
            "url": "https://www.douban.com/group/topic/%s/" % topic_id,
            "title": title,
            "published_at": published_at,
            "reply_count": reply_count,
            "group_id": gm.group(1) if gm else None,
            "group_name": gm.group(2).strip() if gm else None,
        })
    seen, out = set(), []
    for r in rows:
        if r["topic_id"] in seen:
            continue
        seen.add(r["topic_id"])
        out.append(r)
    return out


# ---------------------------------------------------------------- 帖子页解析
def _people_link(text):
    m = re.search(
        r'href="https://www\.douban\.com/people/([^/"?]+)/?"[^>]*>\s*([^<]{1,40}?)\s*</a>', text)
    if not m:
        return None
    return m.group(2) or m.group(1)


def parse_topic_html(html_text, top_comments):
    """宽容正则解析帖子详情: 标题/正文/作者/时间/点赞/前 top_comments 条评论。"""
    out = {"title": "", "content": "", "author": "", "published_at": "",
           "likes": None, "comments": []}
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html_text, re.S | re.I)
    if h1:
        out["title"] = strip_tags(h1.group(1))

    m = re.search(r'class="[^"]*topic-(?:richtext|content)[^"]*"[^>]*>(.*)',
                  html_text, re.S)
    if m:
        seg = m.group(1)
        end = re.search(
            r'<div[^>]*class="[^"]*(?:topic-opt|topic-fav|topic-extra|group-item|aside)[^"]*"',
            seg)
        if end:
            seg = seg[:end.start()]
        out["content"] = strip_tags(seg)[:5000]

    sec = html_text[: max(len(html_text) // 2, 1)]
    docm = re.search(r'class="topic-doc"(.*?)(?:class="topic-content"|class="topic-richtext")',
                     html_text, re.S)
    if docm:
        sec = docm.group(1)
    author = _people_link(sec)
    out["author"] = author or ""
    cm = re.search(r'class="[^"]*create-time[^"]*"[^>]*>\s*([^<]+?)\s*<', html_text)
    if cm:
        out["published_at"] = cm.group(1).strip()
    else:
        dm = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?)", sec)
        out["published_at"] = dm.group(1) if dm else ""
    lk = re.search(r'class="[^"]*(?:fav-num|like-num)[^"]*"[^>]*>\D{0,12}(\d+)', html_text)
    if lk:
        out["likes"] = int(lk.group(1))

    pieces = re.split(r'<li\b[^>]*class="[^"]*comment-item[^"]*"[^>]*>', html_text)[1:]
    for piece in pieces:
        if len(out["comments"]) >= top_comments:
            break
        ctm = re.search(r'class="[^"]*reply-content[^"]*"[^>]*>(.*?)</(?:p|div)>',
                        piece, re.S)
        content = strip_tags(ctm.group(1)) if ctm else strip_tags(piece)[:500]
        if not content:
            continue
        ca = _people_link(piece)
        cpt = re.search(r'class="[^"]*pubtime[^"]*"[^>]*>\s*([^<]+?)\s*<', piece)
        cv = re.search(r'class="[^"]*(?:votes|vote-count)[^"]*"[^>]*>\s*(?:<[^>]+>\s*)*(\d+)',
                       piece)
        out["comments"].append({
            "author": ca or "",
            "content": content,
            "published_at": cpt.group(1).strip() if cpt else "",
            "likes": int(cv.group(1)) if cv else 0,
        })
    return out


def blocked_reason(html_text, status=None, final_url=""):
    """返回被拦原因字符串, 空串表示未被拦。"""
    if status in (403, 405, 429):
        return "http_%s" % status
    if html_text is None:
        return "request_failed"
    if "没有访问权限" in html_text:
        return "no_access_permission"
    if "accounts/login" in (final_url or ""):
        return "login_redirect"
    if (len(html_text) < 40000
            and "topic-content" not in html_text
            and "topic-richtext" not in html_text
            and not re.search(r"<h1[^>]*>", html_text, re.S)):
        return "empty_or_verify_page"
    return ""


# ---------------------------------------------------------------- HTTP
def safe_get(session, url, timeout=15):
    try:
        return session.get(url, timeout=timeout, allow_redirects=True,
                           headers={"Referer": "https://www.douban.com/"})
    except requests.RequestException as exc:
        print("[请求失败] %s : %s" % (url, exc), file=sys.stderr)
        return None


def get_with_retry(session, url, timeout=15):
    """一级 HTTP 抓取: 403 时随机 2-5s 后重试 1 次(仍 403 才算被拦, 交给二级)。

    重试不占 RequestBudget 名额(每次 403 至多重试一次, 间隔即 rand_sleep)。
    """
    resp = safe_get(session, url, timeout)
    if resp is not None and resp.status_code == 403:
        print("  [403] %s — 随机等待后重试 1 次" % url)
        rand_sleep()
        resp = safe_get(session, url, timeout)
    return resp


# ---------------------------------------------------------------- jsonl
def append_jsonl(out_path, record):
    with open(out_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def merge_jsonl(out_path, new_records):
    """按 url 去重, 新记录补充/覆盖旧记录后整体重写。"""
    by_url, order = {}, []
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                u = rec.get("url", "")
                if u not in by_url:
                    order.append(u)
                by_url[u] = rec
    for rec in new_records:
        u = rec.get("url", "")
        if u not in by_url:
            order.append(u)
        by_url[u] = rec
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for u in order:
            fh.write(json.dumps(by_url[u], ensure_ascii=False) + "\n")
    os.replace(tmp, out_path)
    return len(order)


def build_record(query, row, parsed, reason):
    parsed = parsed or {}
    extra = {
        "topic_id": row["topic_id"],
        "group_id": row.get("group_id"),
        "group_name": row.get("group_name"),
        "blocked": bool(reason),
    }
    if reason:
        extra["block_reason"] = reason
    return {
        "platform": "douban",
        "query": query,
        "collected_at": datetime.now().isoformat(timespec="seconds"),
        "url": row["url"],
        "title": parsed.get("title") or row.get("title") or "",
        "content": parsed.get("content", ""),
        "author": parsed.get("author", ""),
        "published_at": parsed.get("published_at") or row.get("published_at") or "",
        "likes": parsed.get("likes"),
        "comments_count": row.get("reply_count"),
        "comments": parsed.get("comments", []),
        "extra": extra,
    }


# ---------------------------------------------------------------- 级别1
class RequestBudget:
    """列表级请求预算: 总请求数<=max_requests, 请求间随机 sleep 2-5s。"""

    def __init__(self, session, max_requests):
        self.session = session
        self.max_requests = max(1, max_requests)
        self.used = 0

    @property
    def exhausted(self):
        return self.used >= self.max_requests

    def get(self, url):
        if self.exhausted:
            return None
        if self.used > 0:
            rand_sleep()
        self.used += 1
        return get_with_retry(self.session, url)


def fetch_list_rows(budget, url_for_start, limit, query, rental_words, seen,
                    group_id=None, intent="word"):
    """翻页抓列表页(搜索结果页/小组讨论页), 解析行并按标题相关性过滤。

    返回 (kept, filtered_n, blocked): kept<=limit, filtered_n 为丢弃的无关标题数。
    """
    kept, filtered_n, blocked = [], 0, False
    start = 0
    while len(kept) < limit and start < PAGE_SIZE * MAX_PAGES \
            and not budget.exhausted:
        resp = budget.get(url_for_start(start))
        if resp is None or resp.status_code != 200:
            blocked = True
            break
        new_rows = [r for r in parse_search_rows(resp.text)
                    if r["topic_id"] not in seen]
        if not new_rows:
            break
        for row in new_rows:
            seen.add(row["topic_id"])
            if group_id and not row.get("group_id"):
                row["group_id"] = group_id
            if title_relevant(row["title"], query, rental_words, intent):
                kept.append(row)
                if len(kept) >= limit:
                    break
            else:
                filtered_n += 1
        start += PAGE_SIZE
    return kept, filtered_n, blocked


def level1_search(session, query, limit, rental_words, intent="word"):
    """组合租赁词分次搜索(cat=1013), 每个组合翻页补齐直到满 limit 或无新结果。

    列表级请求总数上限 2*limit; rental_words 为空(word 意图默认)时退回裸 query
    搜索。返回 (kept, filtered_n, blocked, used_requests)。
    """
    budget = RequestBudget(session, 2 * limit)
    combos = [("%s %s" % (query, w)).strip() for w in (rental_words or [])]
    if not combos:
        combos = [query]
    kept, filtered_n, blocked, seen = [], 0, False, set()
    for combo in combos:
        if blocked or len(kept) >= limit or budget.exhausted:
            break
        part, fn, bl = fetch_list_rows(
            budget,
            lambda start, c=combo: SEARCH_URL.format(q=quote(c), start=start),
            limit - len(kept), query, rental_words, seen, intent=intent)
        kept.extend(part)
        filtered_n += fn
        blocked = blocked or bl
    return kept[:limit], filtered_n, blocked, budget.used


def level1_search_with_fallback(session, query, limit, rental_words,
                                intent="word"):
    """level1_search 的 word 意图自愈封装。

    实测豆瓣 cat=1013 搜索对组合词会丢标的词(q="回龙观 租房"返回全站泛租房帖
    0 命中回龙观, q="回龙观"裸词正常)。若组合 query 搜索后 kept=0 且 query 含
    >=2 个 >=2 字的分词, 自动用首个分词(=标的词)降级重搜一次(仅一次, 防死循环;
    同 limit/翻页逻辑复用); 重试仍 0 才交回调用方报 rc=2。listing 意图不触发。
    """
    kept, filtered_n, blocked, used = level1_search(
        session, query, limit, rental_words, intent=intent)
    if intent == "word" and not kept and not blocked:
        toks = [t for t in split_words(query) if len(t) >= 2]
        toks = [t for t in toks if t not in lexicon.COMMON_CITY_WORDS]
        if len(toks) >= 2 or (toks and toks[0] != query.strip()):
            first = toks[0]
            print("[级别1] 组合词 0 命中，降级首词重试: %s" % first)
            kept2, fn2, bl2, used2 = level1_search(
                session, first, limit, rental_words, intent=intent)
            kept, blocked = kept2, bl2
            filtered_n += fn2
            used += used2
    return kept, filtered_n, blocked, used


def level1_group(session, group_id, query, limit, rental_words, intent="word"):
    """定向抓取指定小组讨论列表页(/group/<id>/discussion), 再按标题过滤。

    返回 (kept, filtered_n, blocked, used_requests)。
    """
    budget = RequestBudget(session, 2 * limit)
    kept, filtered_n, blocked = fetch_list_rows(
        budget,
        lambda start: GROUP_URL.format(gid=group_id, start=start),
        limit, query, rental_words, set(), group_id=group_id, intent=intent)
    return kept[:limit], filtered_n, blocked, budget.used


# ---------------------------------------------------------------- 级别2
def parse_cookies(raw):
    """支持 JSON 数组格式与 'k=v; k2=v2' 两种。"""
    raw = raw.strip()
    cookies = []
    if raw.startswith("["):
        data = json.loads(raw)
        for c in data:
            if isinstance(c, dict) and "name" in c and "value" in c:
                item = {"name": c["name"], "value": c["value"],
                        "domain": c.get("domain", ".douban.com"),
                        "path": c.get("path", "/")}
                cookies.append(item)
        return cookies
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if k:
                cookies.append({"name": k, "value": v,
                                "domain": ".douban.com", "path": "/"})
    return cookies


def non_interactive():
    """True = 无 stdin / 非终端(管道、分离进程): 无人可等 input()。
    （公共实现收敛到 auth_common，ensure_auth.py 共用。）"""
    return ac.non_interactive()


def needs_login_hint(reason):
    """豆瓣需登录的统一指引(stderr), 配合 main 的 exit 3(与 xhs 语义一致)。"""
    print("[级别2] %s" % reason, file=sys.stderr)
    print("[级别2] 豆瓣需登录：请在前台运行 "
          "python scripts/ensure_auth.py --platform douban "
          "（弹浏览器登录, 每 5s 轮询等待, 最长 180s, 绝不秒退）。",
          file=sys.stderr)


def profile_has_content():
    """douban-profile 持久化档案存在且非空(空目录视为无档案)。"""
    try:
        return os.path.isdir(DOUBAN_PROFILE_DIR) and bool(os.listdir(DOUBAN_PROFILE_DIR))
    except OSError:
        return False


def ask(prompt):
    """等人工回车; 非交互环境不碰 input() 直接按中止处理。"""
    if non_interactive():
        raise InteractiveAbort()
    try:
        return input(prompt)
    except EOFError:
        raise InteractiveAbort()


def _douban_logged_in(context):
    """dbcl2 cookie 存在且非空视为已登录（公共实现在 auth_common.py）。"""
    return ac.douban_context_logged_in(context)


def _level2_target(query, rental_words, group_id):
    """级别2 首跳列表页(静默试探与正式采集共用)。"""
    if group_id:
        return GROUP_URL.format(gid=group_id, start=0)
    combo = ("%s %s" % (query, rental_words[0])).strip() \
        if rental_words else query
    return SEARCH_URL.format(q=quote(combo), start=0)


def _headless_probe(p, cookie_raw, target):
    """headless 快速开持久化 context 查档案 cookie 有效性 + 列表可采性。

    用 context.cookies() 判 dbcl2（HttpOnly, document.cookie 看不到, 必须走
    context API）; 返回 (context, page, logged_in, list_ok): logged_in=登录态
    有效, list_ok=目标列表页能解析出帖子行(匿名也可采)。
    调用方判"logged_in 或 list_ok"都算试探通过, 复用 (context, page) 全程无头
    采集; 两者皆否返回 (None, None, False, False), 并确保试探 context 已关闭
    (同一 user_data_dir 不允许并存两个 context, 必须先释放档案锁)。
    """
    context = None
    try:
        context = ac.launch_douban_profile(p, headless=True,
                                           profile_dir=DOUBAN_PROFILE_DIR)
        if cookie_raw:
            context.add_cookies(parse_cookies(cookie_raw))
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(target, timeout=15000)
            page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass  # 页面被拦/超时也要判已落盘 cookie
        logged_in = _douban_logged_in(context)
        list_ok = bool(parse_search_rows(page.content()))
        return context, page, logged_in, list_ok
    except Exception as exc:
        print("[级别2] 静默(无头)试探失败: %s" % exc, file=sys.stderr)
    if context is not None:
        try:
            context.close()
        except Exception:
            pass
    return None, None, False, False


def level2_playwright(query, limit, top_comments, rental_words=(), group_id=None,
                      intent="word"):
    """Playwright 浏览器兜底（持久化 context, 登录一次长期有效）。

    顺序: ① 有登录素材(档案非空/DOUBAN_COOKIE)先 headless 快速开 context 查
    cookie 有效性(context.cookies() 判 dbcl2)并试一次列表页: 有 dbcl2 或列表
    匿名可采 → 全程无头采集不弹窗; ② 无登录态且列表被拦时, 进二级前已用
    sys.stdin.isatty() 判定: 非交互环境绝不弹窗也绝不 input() 等人, 打印指引
    并记录原因后抛 NeedsLogin(main exit 3, 与 xhs 语义一致); ③ 交互终端才
    headless=False 弹可见窗口, 学 ensure_auth 每 5s 轮询 context.cookies()
    等 dbcl2(最长 180s, 不用 input()), 登录完成自动继续。
    滑块人工介入仅保留在可见窗口路径(无头模式遇滑块记被拦)。

    返回 (row, parsed, reason) 列表; 需登录抛 NeedsLogin。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[级别2] 未安装 playwright, 无法兜底采集帖子正文。请先执行:\n"
              "    pip install playwright && playwright install chromium",
              file=sys.stderr)
        sys.exit(2)

    ac.ensure_playwright_browsers_path()

    cookie_raw = os.environ.get("DOUBAN_COOKIE", "").strip()
    has_profile = profile_has_content()
    target = _level2_target(query, rental_words, group_id)
    # 进二级前一次性判定交互性: 非交互(分离进程/管道)后面绝不弹窗、绝不 input()
    interactive = not non_interactive()

    results = []
    with sync_playwright() as p:
        context, page, headless = None, None, True
        try:
            logged_in = False
            if cookie_raw or has_profile:
                context, page, logged_in, list_ok = _headless_probe(
                    p, cookie_raw, target)
            if context is not None and (logged_in or list_ok):
                # 静默(无头)路径: 登录态有效或列表匿名可采, 全程无头不弹窗
                headless = True
                if logged_in:
                    print("[级别2] 复用持久化登录态(dbcl2 有效), 无头静默采集, 不弹窗。")
                    ac.mark_auth_state(
                        "douban", True,
                        mode="cookie_env" if cookie_raw else "browser_profile",
                        profile=None if cookie_raw else DOUBAN_PROFILE_DIR)
                else:
                    print("[级别2] 列表页匿名可访问, 以匿名态无头继续。")
                    ac.mark_auth_state("douban", True, mode="anonymous")
            else:
                # 无头不通(无登录素材/登录态失效且列表被拦): 先释放试探 context
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        pass
                    context, page = None, None
                if not interactive:
                    if cookie_raw or has_profile:
                        reason = ("静默试探未通过(登录态失效/被拦), 非交互环境"
                                  "无法等待人工登录, 跳过二级降级。")
                    else:
                        reason = ("帖子正文被拦需要登录态, 当前无持久化档案"
                                  "且未设 DOUBAN_COOKIE, 非交互环境跳过二级降级。")
                    needs_login_hint(reason)
                    ac.mark_auth_state("douban", False,
                                       reason="needs_login_non_interactive")
                    raise NeedsLogin()
                # 可见窗口路径(交互终端): 弹窗 + 轮询等待人工登录, 不用 input()
                try:
                    context = ac.launch_douban_profile(p, headless=False,
                                                       profile_dir=DOUBAN_PROFILE_DIR)
                except Exception as exc:
                    print("[级别2] Playwright 浏览器启动失败: %s\n"
                          "    请先执行: playwright install chromium" % exc,
                          file=sys.stderr)
                    return []
                page = context.pages[0] if context.pages else context.new_page()
                try:
                    page.goto(ac.DOUBAN_HOME_URL, timeout=60000)
                except Exception as exc:
                    print("[级别2] 打开豆瓣失败: %s" % exc, file=sys.stderr)
                    return []
                print("[级别2] 请在弹出的浏览器窗口内登录豆瓣(如遇滑块请完成验证), "
                      "最长等待 %d 秒, 每 %d 秒自动检测, 完成后自动继续。"
                      % (ac.DOUBAN_LOGIN_WAIT, ac.DOUBAN_POLL_INTERVAL),
                      file=sys.stderr)
                if ac.wait_douban_login(context, ac.DOUBAN_LOGIN_WAIT,
                                        ac.DOUBAN_POLL_INTERVAL):
                    headless = False
                    # 登录态确认有效才登记（cookie 已随持久化 context 落盘）
                    ac.mark_auth_state("douban", True, mode="browser_profile",
                                       profile=DOUBAN_PROFILE_DIR)
                    print("[级别2] 登录成功(检测到 dbcl2), 已持久化到 %s, 后续免登录。"
                          % DOUBAN_PROFILE_DIR)
                else:
                    needs_login_hint("等待登录超时(%d 秒)。"
                                     % ac.DOUBAN_LOGIN_WAIT)
                    raise NeedsLogin()
            page.goto(target, timeout=60000)
            page.wait_for_load_state("domcontentloaded")
            all_rows = parse_search_rows(page.content())
            rows = [r for r in all_rows
                    if title_relevant(r["title"], query, rental_words,
                                      intent)][:limit]
            # 组合词在豆瓣搜索会丢标的词(P4 实证)，浏览器路径同样降级首词重试一次
            _toks = [t for t in split_words(query) if len(t) >= 2
                     and t not in lexicon.COMMON_CITY_WORDS]
            if (not rows and intent == "word" and not group_id
                    and (len(_toks) >= 2
                         or (_toks and _toks[0] != query.strip()))):
                first = _toks[0]
                print("[级别2] 组合词 0 命中，降级首词重试: %s" % first)
                target = _level2_target(first, rental_words, group_id)
                page.goto(target, timeout=60000)
                page.wait_for_load_state("domcontentloaded")
                all_rows = parse_search_rows(page.content())
                rows = [r for r in all_rows
                        if title_relevant(r["title"], first, rental_words,
                                          intent)][:limit]
            print("[级别2] 浏览器内列表命中 %d 条 (过滤前 %d, 过滤掉 %d)"
                  % (len(rows), len(all_rows), len(all_rows) - len(rows)))
            fail_streak = 0
            for row in rows:
                try:
                    page.goto(row["url"], timeout=60000)
                    page.wait_for_load_state("domcontentloaded")
                    html_text = page.content()
                    lowered = html_text.lower()
                    slider = ("captcha" in lowered or "滑块" in html_text
                              or "异常" in html_text) \
                        and "topic-content" not in html_text
                    if slider and headless:
                        # 无头窗口人工看不到滑块: 记被拦, 连续失败交由 fail_streak 终止
                        print("[级别2] 无头模式遇滑块/验证码, 无法人工完成, 记为被拦: %s"
                              % row["url"], file=sys.stderr)
                        reason, parsed = "slider_in_headless", None
                    else:
                        if slider:
                            try:
                                ask("[级别2] 检测到滑块/验证码, 请在浏览器中人工完成后"
                                    "按回车: ")
                                html_text = page.content()
                            except InteractiveAbort:
                                print("[级别2] 非交互环境无法等待人工验证, 终止级别2。",
                                      file=sys.stderr)
                                break
                        reason = blocked_reason(html_text)
                        parsed = None if reason else parse_topic_html(html_text, top_comments)
                    results.append((row, parsed, reason))
                    if reason:
                        fail_streak += 1
                    else:
                        fail_streak = 0
                except Exception as exc:
                    fail_streak += 1
                    print("[级别2] 采集失败 %s: %s" % (row["url"], exc), file=sys.stderr)
                if fail_streak >= 2:
                    print("[级别2] 连续 2 次失败, 终止采集。", file=sys.stderr)
                    break
                rand_sleep()
        finally:
            if context is not None:
                # 持久化 context 必须 close, cookie 才会完整落盘
                try:
                    context.close()
                except Exception:
                    pass
    return results


def _written_stats(out_path, days):
    """读回 out_path 实际落盘记录, 返回 (条数, kept, dropped, time_unknown)。

    末尾汇总行用: level2 兜底写盘后, 时间窗统计必须基于真实写盘记录重算,
    不能沿用 level1 空列表的 kept/dropped(否则 fetched=3 却打 kept=0 被误读)。
    """
    recs = []
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except ValueError:
                    continue
    _, (kept, dropped, unknown) = apply_time_window(recs, days, "published_at")
    return len(recs), kept, dropped, unknown


# ---------------------------------------------------------------- 主流程
def build_parser():
    ap = argparse.ArgumentParser(description="豆瓣小组帖采集(两级降级, 租房定向)")
    ap.add_argument("--query", required=True, help="搜索关键词, 如小区名")
    ap.add_argument("--limit", type=int, default=20, help="采集帖子数, 默认 20")
    ap.add_argument("--intent", choices=("word", "listing"), default="word",
                    help="搜索意图: word=口碑类(A/B/C, 默认), 不拼租赁泛词只用"
                         "query 原词裸搜, 标题只按 query 主词判相关(防求租帖混入); "
                         "listing=找房类(D, 旧行为), query 与默认供给侧租赁词双拼"
                         "搜索+标题租赁词过滤")
    ap.add_argument("--days", type=int, default=0,
                    help="时间窗过滤天数(默认 0=不过滤): 只保留最近 N 天发布的帖子"
                         "(列表行无时间的保留并计 time_unknown)")
    ap.add_argument("--sort", type=ac.sort_arg_type,
                    default=ac.parse_sort_spec(ac.DEFAULT_SORT),
                    help="抓详情顺序的排序键, 逗号分隔多值(默认 discussion,hot="
                         "评论最多∪最热两队列合并去重, 讨论队列在前)。discussion="
                         "回复数降序; hot=点赞降序(列表行无点赞数时同序); time="
                         "发表时间倒序(找房源配 --days 7)。旧值 heat 兼容="
                         "discussion,hot。可与 --days 组合")
    ap.add_argument("--rental-words", dest="rental_words", default=None,
                    help="与 query 组合分次搜索的租赁词(空格分隔); 默认按 --intent: "
                         "listing='%s', word=空(裸 query); 显式传值优先, "
                         "传空串亦为裸 query 搜索" % DEFAULT_RENTAL_WORDS)
    ap.add_argument("--group", default=None, metavar="GROUP_ID",
                    help="定向小组 id/slug(如 beijingzufang): 直接抓该小组讨论"
                         "列表页再按标题过滤, 不做全站搜索")
    ap.add_argument("--top-comments", dest="top_comments", type=int, default=10,
                    help="每帖最多保留评论数, 默认 10")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                    help="jsonl 输出目录, 默认 ~/.rent-assist/data/raw")
    return ap


def main():
    args = build_parser().parse_args()
    # --rental-words 默认按意图取: word=空(裸 query 搜), listing=供给侧词表;
    # 显式传值(含空串)以调用方为准
    if args.rental_words is None:
        args.rental_words = DEFAULT_RENTAL_WORDS if args.intent == "listing" else ""
    rental_words = split_words(args.rental_words)

    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(
        args.out_dir, "douban_%s_%s.jsonl" % (sanitize_query(args.query),
                                              datetime.now().strftime("%Y%m%d")))

    session = requests.Session()
    session.headers.update(BASE_HEADERS)

    if args.group:
        print("[级别1] 定向抓取小组 %s 讨论列表(intent=%s): %s"
              % (args.group, args.intent, args.query))
        topics, filtered_n, search_blocked, used = level1_group(
            session, args.group, args.query, args.limit, rental_words,
            intent=args.intent)
    else:
        print("[级别1] 抓取豆瓣小组搜索(intent=%s): %s + 租赁词[%s]"
              % (args.intent, args.query, " ".join(rental_words) or "无(裸query)"))
        topics, filtered_n, search_blocked, used = level1_search_with_fallback(
            session, args.query, args.limit, rental_words, intent=args.intent)
    print("[级别1] 过滤后保留 %d 条, 丢弃无关标题 %d 条 (列表页请求 %d 次, limit=%d)"
          % (len(topics), filtered_n, used, args.limit))

    # --days 时间窗过滤: 搜索页 td-time 的 title 属性为完整时间; 小组页短时间
    # (MM-DD / HH:MM)按当年推断, 无法解析的保留并计 time_unknown
    topics, (win_kept, win_dropped, win_unknown) = apply_time_window(
        topics, args.days, "published_at")
    if args.days > 0:
        print("[级别1] 时间窗 %s" % window_stat_seg(
            args.days, win_kept, win_dropped, win_unknown))
    # --sort 多队列 union 决定抓详情顺序（discussion 在前则讨论队列优先；
    # K=保留帖数，即两键全量序合并去重）
    topics = ac.select_union_top(topics, args.sort, len(topics), sort_key_of,
                                 item_key=lambda r: r["topic_id"])

    if not topics and not search_blocked:
        print("[级别1] 标题过滤后相关帖子为 0 (丢弃无关 %d 条, %s)。"
              % (filtered_n, window_stat_seg(
                  args.days, win_kept, win_dropped, win_unknown)),
              file=sys.stderr)
        if not args.group:
            print("[提示] 可用 --group <group_id> 定向指定租房小组"
                  "(如北京租房: --group beijingzufang), 直接抓取其讨论列表页重试。",
                  file=sys.stderr)
            if args.intent == "word":
                print("[提示] 口碑类 0 命中多为查询词过窄, 可换评价视角词重试"
                      "(如 '{小区} 住过/避坑/怎么样')。", file=sys.stderr)
        print("platform=douban fetched=0/0 filtered_irrelevant=%d %s %s file=%s"
              % (filtered_n, ac.sort_seg(args.sort), window_stat_seg(
                  args.days, win_kept, win_dropped, win_unknown), out_path))
        sys.exit(2)

    fetched, blocked_n = 0, 0
    for row in topics:
        resp = get_with_retry(session, row["url"])
        reason = blocked_reason(resp.text if resp is not None else None,
                                status=resp.status_code if resp is not None else None,
                                final_url=resp.url if resp is not None else "")
        parsed = None if reason else parse_topic_html(resp.text, args.top_comments)
        append_jsonl(out_path, build_record(args.query, row, parsed, reason))
        fetched += 1
        if reason:
            blocked_n += 1
            print("  [拦] %s (%s)" % (row["title"][:40] or row["url"], reason))
        else:
            print("  [得] %s" % (row["title"][:40] or row["url"]))
        rand_sleep()

    trigger2 = (search_blocked and fetched == 0) or \
               (fetched > 0 and blocked_n * 2 >= fetched)
    l2_n = 0
    if trigger2:
        print("[级别2] 正文被拦 %d/%d, 触发 Playwright 浏览器兜底..." % (blocked_n, fetched))
        try:
            l2 = level2_playwright(args.query, args.limit, args.top_comments,
                                   rental_words, args.group, intent=args.intent)
        except NeedsLogin:
            print("[级别2] 需登录退出(exit 3), 级别1结果已保留: %s" % out_path)
            print("platform=douban fetched=%d/%d filtered_irrelevant=%d %s %s "
                  "needs_login=1 file=%s"
                  % (fetched, len(topics), filtered_n, ac.sort_seg(args.sort),
                     window_stat_seg(args.days, win_kept, win_dropped,
                                     win_unknown), out_path))
            sys.exit(3)
        except SystemExit:
            raise
        except Exception as exc:
            print("[级别2] 异常终止(级别1结果已保留): %s" % exc, file=sys.stderr)
            l2 = []
        l2_n = len(l2)
        if l2:
            records = [build_record(args.query, row, parsed, reason)
                       for (row, parsed, reason) in l2]
            fetched = merge_jsonl(out_path, records)
            blocked_n = sum(1 for (_, _, reason) in l2 if reason)
            print("[级别2] 合并后共 %d 条记录" % fetched)

    # 汇总行如实反映本次运行: fetched=实际写盘条数(读回 out_path), "/"后=列表
    # 候选数(level1 与 level2 命中数的较大者, level2 兜底时不再误报 0 候选),
    # 时间窗统计同样基于写盘记录重算(而非 level1 空列表的统计)。
    written, wk, wd, wu = _written_stats(out_path, args.days)
    candidates = max(len(topics), l2_n)
    print("platform=douban fetched=%d/%d filtered_irrelevant=%d %s %s file=%s"
          % (written, candidates, filtered_n, ac.sort_seg(args.sort),
             window_stat_seg(args.days, wk, wd, wu), out_path))
    sys.exit(0 if written > 0 else 2)


if __name__ == "__main__":
    main()
