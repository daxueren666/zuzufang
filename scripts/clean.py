# -*- coding: utf-8 -*-
"""rent-assist 数据清洗：data/raw/*.jsonl -> data/cleaned/*.json（stdlib only）。

去重（同 URL 合并保优 + 正文前 100 字归一化 MD5）、广告标记、8 类风险粗分类、
房源帖识别（is_listing / price_hint+price_int / room_hint）、评论 top8（正文留 150 字）、
讨论热度优先排序：heat_score = comments_count*3 + likes 为主、关键词命中数为次，
meta.discussion_top 给出评论最多前 3 条供 LLM 优先分析。
实体兜底：标题+正文（去空格）不含 query/别名任一 → entity_hit=false 并默认剔除
（--aliases 补充别名，--no-entity-filter 关闭剔除）。
求租帖过滤：标题+正文命中词库两档信号 → 默认剔除并计 meta.seek_posts
（--keep-seek 保留并打 seek_post 标记）；买房/卖房帖过滤：标题命中买卖词库即剔除
并计 meta.buy_sell_posts；raw extra 的 note_id/note_type 透传。
时效处理：old 标记（发布距今 >24 个月）、time_distribution 分布、
old 条目 heat_score 轻微衰减（×0.8）、可选 --max-age-months 直接剔除超龄条目。

用法：
  python clean.py --query "天通苑" [--raw-dir data/raw] [--out data/cleaned/xxx.json]
                  [--max-age-months 0] [--aliases "天通苑西,tt苑"] [--no-entity-filter]
                  [--keep-seek]
"""
import argparse
import hashlib
import json
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth_common as ac  # noqa: E402  统一数据目录解析（data_dir）
from lexicon import (  # noqa: E402
    AD_KEYWORDS,
    LISTING_KEYWORDS,
    RISK_CATEGORIES,
    ROOM_KEYWORDS,
    detect_buy_sell_post,
    detect_city_mismatch,
    detect_listing_page,
    detect_seek_post,
    extract_price,
    has_city_signal,
    price_to_int,
)

SKILL_ROOT = Path(__file__).resolve().parent.parent
_UNSAFE_RE = re.compile(r'[\\/:*?"<>|\r\n\t ]+')

# ---- SEO 模板列表页识别（#19，保守：宁可漏杀不可误杀正常口碑帖） ----
# 标题模板特征："租房-价格筛选"类页面名 / "整租·""合租·"挂牌头 + 多区间价格
_SEO_TEMPLATE_TITLE_RE = re.compile(r"租房\s*[-–—_ ]*\s*价格|价格筛选")
_SEO_RANGE_PRICE_RE = re.compile(r"\d{4}\s*[-~至]\s*\d{4}")
# URL 列表/筛选页模式（路径型，不用裸关键词，避免误伤正文页）
_SEO_LIST_URL_RE = re.compile(
    r"/zufang/[a-z0-9_]*?(?:$|[/?#])|/rent/(?:list|search)|[?&](?:price|filter|list)=",
    re.I)
# 正文挂牌价格堆叠："2500元/月" / "3500/月" 等
_SEO_PRICE_LINE_RE = re.compile(r"\d{3,6}\s*元?\s*/\s*月|\d{3,6}\s*元/月")
_SEO_PRICE_STACK_MIN = 5  # 堆叠阈值：正文价格/月表述少于该数不判（防误杀口碑帖）

# 抖音实体放宽（#18）：query 剥掉泛词后剩下的地标词作为放宽匹配词
_QUERY_NOISE_RE = re.compile(r"租房|小区|推荐|怎么样|怎样|附近|房子|出租|找房|避坑")


def douyin_relaxed_terms(entity_terms, query):
    """放宽词条 = 实体闸门词条 + query 剥泛词后的地标词（归一化，长度>=2）。"""
    terms = list(entity_terms)
    stripped = norm_entity_text(_QUERY_NOISE_RE.sub("", str(query or "")))
    if len(stripped) >= 2 and stripped not in terms:
        terms.append(stripped)
    return terms


def detect_seo_listing_page(title, url, content) -> bool:
    """SEO 模板列表页判定（#19，仅 web 源）。

    特征保守组合：标题命中挂牌模板特征（"租房-价格"页名，或"整租·/合租·"
    + 多区间价格）或 URL 为列表/筛选页模式，且正文为多条挂牌价格堆叠
    （>= SEO_PRICE_STACK_MIN 处"价格/月"表述）。任一特征单独出现不判。
    """
    t, u, c = str(title or ""), str(url or ""), str(content or "")
    title_hint = bool(_SEO_TEMPLATE_TITLE_RE.search(t)) or (
        ("整租·" in t or "合租·" in t)
        and len(_SEO_RANGE_PRICE_RE.findall(t + c)) >= 2)
    url_hint = bool(_SEO_LIST_URL_RE.search(u))
    if not (title_hint or url_hint):
        return False
    return len(_SEO_PRICE_LINE_RE.findall(c)) >= _SEO_PRICE_STACK_MIN


def slugify(text: str, maxlen: int = 40) -> str:
    """文件名安全化：去掉路径非法字符与空白，保留中文。"""
    slug = _UNSAFE_RE.sub("_", (text or "").strip()).strip("._")
    return slug[:maxlen] or "untitled"


def resolve_data_root() -> Path:
    """data 根目录：环境变量 RENT_ASSIST_DATA > ~/.rent-assist/data
    （个人数据与 skill 目录分离，经 auth_common.data_dir 统一解析并自动创建）。"""
    return ac.data_dir()


def _to_int(v, default=0) -> int:
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


# published_at 兜底格式（ISO8601 由 fromisoformat 直接处理，含 douban 的 "2026-08-11 00:38:57"）
_DT_FALLBACK_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%Y年%m月%d日",
)


def parse_published_at(value) -> "datetime | None":
    """解析 published_at：ISO8601（含 Z/时区偏移）与常见日期字符串；失败返回 None。"""
    s = str(value or "").strip()
    if not s:
        return None
    iso = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
    try:
        dt = datetime.fromisoformat(iso)
        # 带时区的换成本地时间再去掉 tzinfo，便于与本地 now 比较
        return dt.astimezone().replace(tzinfo=None) if dt.tzinfo is not None else dt
    except ValueError:
        pass
    for fmt in _DT_FALLBACK_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def diff_months(from_dt: "datetime", to_dt: "datetime") -> int:
    """从 from_dt 到 to_dt 的完整月数（按日折算，不足一月不算）。"""
    months = (to_dt.year - from_dt.year) * 12 + (to_dt.month - from_dt.month)
    if to_dt.day < from_dt.day:
        months -= 1
    return months


def norm_entity_text(text: str) -> str:
    """实体匹配用归一化：NFKC、去空白、casefold。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "")).casefold()


def normalize_text_head(text: str, n: int = 100) -> str:
    """全角→半角、casefold、去空白与标点，取前 n 字，用于粗去重。"""
    if not text:
        return ""
    out = []
    for ch in unicodedata.normalize("NFKC", text):
        if ch.isalnum():
            out.append(ch.casefold())
    return "".join(out)[:n]


def find_non_overlapping(text: str, keywords):
    """长词优先的非重叠命中。返回 (命中词去重列表, 命中次数区间)。

    解决 "加vx/vx" 重复计数：短词出现在已被更长词占用的区间内不计数。
    """
    if not text:
        return []
    spans = []
    for kw in keywords:
        if not kw:
            continue
        start = 0
        while True:
            i = text.find(kw, start)
            if i < 0:
                break
            spans.append((i, i + len(kw), kw))
            start = i + len(kw)
    # 先长后短、再按位置，贪心取不重叠区间
    spans.sort(key=lambda s: (-(s[1] - s[0]), s[0]))
    taken = []
    hit_words = []
    for s, e, kw in spans:
        if any(s < te and ts < e for ts, te, _ in taken):
            continue
        taken.append((s, e, kw))
        if kw not in hit_words:
            hit_words.append(kw)
    return hit_words


def iter_raw_rows(raw_dir: Path, query: str):
    """遍历 raw-dir 下 *.jsonl，按 query 行字段互含或文件名包含匹配。"""
    q = (query or "").casefold()
    files = sorted(raw_dir.glob("*.jsonl"))
    if not files:
        print(f"warn: {raw_dir} 下没有 *.jsonl", file=sys.stderr)
    for fp in files:
        fname_matched = (q in fp.name.casefold()) if q else True
        try:
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            print(f"warn: 无法读取 {fp.name}: {e}", file=sys.stderr)
            continue
        for lineno, line in enumerate(lines, 1):
            # 空白行静默跳过（不告警）；顺带去掉行首 BOM，防 BOM 行触发坏行警告
            line = line.lstrip("﻿").strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("非 object 行")
            except (json.JSONDecodeError, ValueError) as e:
                print(f"warn: 跳过坏行 {fp.name}:{lineno} ({e})", file=sys.stderr)
                continue
            rq = str(row.get("query") or "").casefold()
            if rq:
                if q and not (q in rq or rq in q):
                    continue
            else:
                if not fname_matched:
                    continue
            yield row


def merge_url_rows(a: dict, b: dict) -> dict:
    """同 URL 两条 raw 记录合并保优。

    背景：web 源 Exa 摘要记录与 Jina 全文记录同 URL 先后入库，
    旧逻辑"首见保留"会丢后到的全文。抖音口播转写批次与主批次同 URL 同理——
    带【口播转写】的 content 更长、按此规则自然胜出，无需特判。合并规则：
    - 以 content 更长的一条为主体（标题/正文/评论列表随主体）；
    - likes / comments_count 取两者较大值；
    - published_at 保留更新的（能解析的优先，都可解析取更晚；都解析失败取非空）；
    - platform / extra / author 合并非空字段，冲突时主体（全文）优先。
    """
    len_a = len(str(a.get("content") or ""))
    len_b = len(str(b.get("content") or ""))
    win, lose = (a, b) if len_a >= len_b else (b, a)
    merged = dict(win)

    for key in ("likes", "comments_count"):
        wv, lv = _to_int(win.get(key), 0), _to_int(lose.get(key), 0)
        merged[key] = lose.get(key) if lv > wv else win.get(key)

    comments_w = win.get("comments") if isinstance(win.get("comments"), list) else []
    comments_l = lose.get("comments") if isinstance(lose.get("comments"), list) else []
    if len(comments_l) > len(comments_w):
        merged["comments"] = comments_l

    dt_w, dt_l = parse_published_at(win.get("published_at")), parse_published_at(lose.get("published_at"))
    if dt_l and (not dt_w or dt_l > dt_w):
        merged["published_at"] = lose.get("published_at")
    elif not dt_w and not dt_l and not str(win.get("published_at") or "").strip():
        merged["published_at"] = lose.get("published_at")

    for key in ("platform", "author", "title"):
        if not str(merged.get(key) or "").strip():
            merged[key] = lose.get(key)

    extra_w = win.get("extra") if isinstance(win.get("extra"), dict) else {}
    extra_l = lose.get("extra") if isinstance(lose.get("extra"), dict) else {}
    extra = {}
    for src in (extra_l, extra_w):  # 主体（win）后写入，非空值覆盖
        for k, v in src.items():
            if v not in (None, "", [], {}):
                extra[k] = v
    merged["extra"] = extra
    return merged


def clean_row(row: dict, old: bool = False, entity_hit: bool = True,
              seek_post: bool = False) -> dict:
    """单行 -> 精简项（风险/广告/房源识别）。old=发布超过 24 个月，heat_score 轻微衰减。"""
    title = str(row.get("title") or "")
    content = str(row.get("content") or "")
    comments_raw = row.get("comments") or []
    if not isinstance(comments_raw, list):
        comments_raw = []
    comments = []
    for c in comments_raw:
        if isinstance(c, dict):
            comments.append({
                "text": str(c.get("text") or "")[:150],
                "likes": _to_int(c.get("likes")),
                "author": str(c.get("author") or "")[:30],
            })
        elif isinstance(c, str):
            comments.append({"text": c[:150], "likes": 0, "author": ""})
    comments.sort(key=lambda c: c["likes"], reverse=True)

    head = f"{title}\n{content}"

    # ③ 风险命中：标题+正文+全部评论
    corpus = head + "\n" + "\n".join(c["text"] for c in comments)
    matched_categories, matched_keywords = [], {}
    for key, cat in RISK_CATEGORIES.items():
        hits = find_non_overlapping(corpus, cat["keywords"])
        if hits:
            matched_categories.append(key)
            matched_keywords[key] = hits

    # ② 广告标记：标题+正文命中 AD_KEYWORDS >= 2 个不同词
    ad_words = find_non_overlapping(head, AD_KEYWORDS)
    ad_suspect = len(ad_words) >= 2

    # v3 房源帖识别
    listing_hit = bool(find_non_overlapping(head, LISTING_KEYWORDS))
    price_hint = extract_price(head)
    room_hit = find_non_overlapping(head, ROOM_KEYWORDS)
    is_listing = listing_hit or bool(price_hint and room_hit)

    likes = _to_int(row.get("likes"))
    comments_count = _to_int(row.get("comments_count"), len(comments))
    # 讨论热度优先：heat_score = 评论数*3 + 点赞（排序主键）；关键词命中数为次键。
    # 旧帖轻微时间衰减（×0.8）保持整数。
    kw_hits = sum(len(hits) for hits in matched_keywords.values())
    heat_score = comments_count * 3 + likes
    if old:
        heat_score = int(heat_score * 0.8 + 0.5)

    # 口播转写帖：转写文案是核心证据，截断放宽 500→2000（采集侧本就截 2000）
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    content_cap = 2000 if (extra.get("asr") or "【口播转写】" in content) else 500

    item = {
        "platform": str(row.get("platform") or ""),
        "url": str(row.get("url") or ""),
        "title": title[:120],
        "content": content[:content_cap],
        "published_at": str(row.get("published_at") or ""),
        "likes": likes,
        "comments_count": comments_count,
        "comments_top": comments[:8],
        "matched_categories": matched_categories,
        "matched_keywords": matched_keywords,
        "ad_suspect": ad_suspect,
        "is_listing": is_listing,
        "price_hint": price_hint,
        "price_int": price_to_int(price_hint),
        "room_hint": room_hit,
        "old": bool(old),
        "entity_hit": bool(entity_hit),
        "heat_score": heat_score,
        "kw_hits": kw_hits,
    }
    # 透传 raw extra 的 note_id / note_type（xhs 溯源用，存在才拷；extra 已在上方读取）
    for key in ("note_id", "note_type"):
        if extra.get(key) not in (None, ""):
            item[key] = extra[key]
    if seek_post:
        item["seek_post"] = True
    return item


def main():
    ap = argparse.ArgumentParser(description="rent-assist 清洗 raw jsonl")
    ap.add_argument("--query", required=True, help="采集时的搜索词")
    ap.add_argument("--raw-dir", default=None, help="默认 ~/.rent-assist/data/raw")
    ap.add_argument("--out", default=None, help="默认 ~/.rent-assist/data/cleaned/<query slug>.json")
    ap.add_argument("--max-age-months", type=int, default=0, metavar="N",
                    help="发布距今超过 N 个月的条目剔除（默认 0=不过滤，仅标记 old）")
    ap.add_argument("--aliases", default="", metavar="A,B",
                    help="实体别名（逗号分隔）。实体闸门 = query + 别名任一在标题+正文命中（去空格子串匹配）；"
                         "不传时兜底为仅 query 主词")
    ap.add_argument("--no-entity-filter", action="store_true",
                    help="关闭实体兜底剔除（仍标记 entity_hit，但不剔除，兼容旧行为）")
    ap.add_argument("--keep-seek", action="store_true",
                    help="保留求租帖（默认剔除，meta.seek_posts 计数）")
    ap.add_argument("--city", default="",
                    help="城市消歧（默认不启用）。启用时剔除明显非该城市的条目"
                         "（web 项 URL 含其他城市子域/城市名；标题/正文前 200 字含"
                         "其他城市名+租赁词，覆盖全部平台）；本市信号（本市名或"
                         "行政区/地标词）白名单兜底保留，计 meta.city_mismatch")
    args = ap.parse_args()

    data_root = resolve_data_root()
    raw_dir = Path(args.raw_dir) if args.raw_dir else data_root / "raw"
    out_path = Path(args.out) if args.out else data_root / "cleaned" / f"{slugify(args.query)}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 实体闸门词条：query 主词 + 别名（兜底：未传 --aliases 时仅用 query 主词）
    entity_terms = []
    for term in [args.query] + re.split(r"[,，;；]", args.aliases or ""):
        t = norm_entity_text(term)
        if t and t not in entity_terms:
            entity_terms.append(t)
    entity_filter_on = not args.no_entity_filter

    now_dt = datetime.now()
    max_age = args.max_age_months if args.max_age_months and args.max_age_months > 0 else 0
    time_distribution = {"within_1y": 0, "y1_2": 0, "older_24m": 0, "unknown": 0}
    excluded_old = 0
    filtered_no_entity = 0
    seek_posts = 0
    buy_sell_posts = 0
    city_mismatch = 0
    city_kept_signal = 0
    city_skipped_non_web = 0
    listing_pages = 0
    douyin_relaxed = 0
    seo_pages = 0

    seen_hashes, items = set(), []
    total_raw = 0
    merged_same_url = 0
    # 阶段一：同 URL 合并保优（web 源 Exa 摘要先到、Jina 全文后到，
    # 保留 content 更长者，likes/comments_count 取大，非空字段合并）
    rows, url_idx = [], {}
    for row in iter_raw_rows(raw_dir, args.query):
        total_raw += 1
        url = str(row.get("url") or "")
        idx = url_idx.get(url) if url else None
        if idx is not None:
            rows[idx] = merge_url_rows(rows[idx], row)
            merged_same_url += 1
            continue
        rows.append(row)
        if url:
            url_idx[url] = len(rows) - 1

    # 阶段二：正文前 100 字归一化 MD5 粗去重 + 时效 + 实体闸门 + 清洗
    for row in rows:
        h = hashlib.md5(
            normalize_text_head(str(row.get("content") or "") or str(row.get("title") or "")).encode("utf-8")
        ).hexdigest()
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        # ④ 时效：缺失/解析失败 -> old=False 计入 unknown；>24 个月 -> old=True。
        #    超过 --max-age-months 的剔除；time_distribution 只统计保留条目（各桶之和=total_kept）
        pub_dt = parse_published_at(row.get("published_at"))
        age = diff_months(pub_dt, now_dt) if pub_dt else None
        if max_age and age is not None and age > max_age:
            excluded_old += 1
            continue
        old = False
        if age is None:
            time_distribution["unknown"] += 1
        else:
            if age <= 12:
                time_distribution["within_1y"] += 1
            elif age <= 24:
                time_distribution["y1_2"] += 1
            else:
                time_distribution["older_24m"] += 1
            old = age > 24
        # ⑤ 实体兜底：标题+正文（去空格）不含 query/别名任一 → entity_hit=false，
        #    默认剔除；--no-entity-filter 时仅标记不剔除。命中判定不掺入评论/点赞。
        head_norm = norm_entity_text(f"{row.get('title') or ''}{row.get('content') or ''}")
        entity_hit = any(t in head_norm for t in entity_terms) if entity_terms else True
        if not entity_hit and entity_terms:
            # ⑤a 抖音放宽（#18）：抖音标题带小区名比例低、信号在正文/评论——
            #     正文不含标的词时，标题命中或任一 top 评论命中（标的词或 query
            #     地标词）即保留，计 meta.douyin_relaxed
            if str(row.get("platform") or "").lower() == "douyin":
                relaxed_terms = douyin_relaxed_terms(entity_terms, args.query)
                title_norm = norm_entity_text(str(row.get("title") or ""))
                cmt_raw = row.get("comments")
                cmt_texts = []
                if isinstance(cmt_raw, list):
                    for cm in cmt_raw:
                        cmt_texts.append(
                            str(cm.get("text") or "") if isinstance(cm, dict)
                            else str(cm or ""))
                cmt_norm = norm_entity_text("".join(cmt_texts))
                if any(t in title_norm or t in cmt_norm for t in relaxed_terms):
                    entity_hit = True
                    douyin_relaxed += 1
            if not entity_hit:
                if entity_filter_on:
                    filtered_no_entity += 1
                    continue
        # ⑤b 城市消歧（--city 启用）：URL 城市子域仅判 web 项；标题/正文前 200 字的
        #     异城名+租赁词判定覆盖全部平台（实测济宁/青岛/上海撞名帖均来自
        #     xhs/douban/douyin）；本市信号白名单兜底（meta.city_kept_signal）
        if args.city:
            platform_name = str(row.get("platform") or "").lower()
            is_web = platform_name == "web"
            if detect_city_mismatch(str(row.get("url") or ""),
                                    str(row.get("title") or ""),
                                    str(row.get("content") or ""), args.city,
                                    web=is_web):
                city_mismatch += 1
                continue
            if has_city_signal(str(row.get("url") or ""),
                               str(row.get("title") or ""),
                               str(row.get("content") or ""), args.city):
                city_kept_signal += 1
            if not is_web:
                city_skipped_non_web += 1
        # ⑤c 列表页/导航页（web 源）：标题命中走势/聚合/问答页模式，
        #     且 comments=0 且 likes=0（防误杀有互动的正常帖）
        if (str(row.get("platform") or "").lower() == "web"
                and _to_int(row.get("comments_count"), len(row.get("comments") or [])) == 0
                and _to_int(row.get("likes")) == 0
                and detect_listing_page(str(row.get("title") or ""))):
            listing_pages += 1
            continue
        # ⑤d SEO 模板列表页（#19，web 源）：标题/URL 命中挂牌模板特征且正文
        #     为多条挂牌价格堆叠（如"优优好房"筛选页）→ 剔除并计 meta.seo_pages；
        #     特征保守（需双重信号），宁可漏杀不误杀正常口碑帖
        if (str(row.get("platform") or "").lower() == "web"
                and detect_seo_listing_page(row.get("title"), row.get("url"),
                                            row.get("content"))):
            seo_pages += 1
            continue
        # ⑥ 求租帖：发帖人本人在找房（非出租非居住评价），识别即计数（meta.seek_posts），
        #    默认剔除；--keep-seek 保留并打 seek_post 标记
        seek_post = detect_seek_post(str(row.get("title") or ""), str(row.get("content") or ""))
        if seek_post:
            seek_posts += 1
            if not args.keep_seek:
                continue
        # ⑦ 买房/卖房帖：标题命中买卖词库（仅标题，防正文顺带提及误杀租住内容），
        #    识别即计数（meta.buy_sell_posts）并剔除
        if detect_buy_sell_post(str(row.get("title") or "")):
            buy_sell_posts += 1
            continue
        items.append(clean_row(row, old=old, entity_hit=entity_hit, seek_post=seek_post))

    # 讨论热度优先：heat_score（评论数*3+点赞）为主，关键词命中数为次
    items.sort(key=lambda it: (it["heat_score"], it["kw_hits"]), reverse=True)
    discussion_top = [
        {"url": it["url"], "title": it["title"], "comments_count": it["comments_count"]}
        for it in sorted(items, key=lambda x: x["comments_count"], reverse=True)
        if it["comments_count"] > 0
    ][:3]

    result = {
        "query": args.query,
        "generated_at": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "total_raw": total_raw,
        "total_kept": len(items),
        "time_distribution": time_distribution,
        "meta": {
            "max_age_months": max_age,
            "excluded_old": excluded_old,
            "entity_filter": entity_filter_on,
            "entity_terms": entity_terms,
            "filtered_no_entity": filtered_no_entity,
            "seek_posts": seek_posts,
            "buy_sell_posts": buy_sell_posts,
            "city": args.city or "",
            "city_mismatch": city_mismatch,
            "city_kept_signal": city_kept_signal,
            "city_skipped_non_web": city_skipped_non_web,
            "listing_pages": listing_pages,
            "douyin_relaxed": douyin_relaxed,
            "seo_pages": seo_pages,
            "keep_seek": bool(args.keep_seek),
            "merged_same_url": merged_same_url,
            "heat_sorted": True,
            "discussion_top": discussion_top,
        },
        "items": items,
    }
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"kept {len(items)}/{total_raw} -> {out_path} "
          f"(time_dist={time_distribution}, excluded_old={excluded_old}, "
          f"filtered_no_entity={filtered_no_entity}, seek_posts={seek_posts}, "
          f"buy_sell_posts={buy_sell_posts}, merged_same_url={merged_same_url}, "
          f"city_mismatch={city_mismatch}, listing_pages={listing_pages}, "
          f"douyin_relaxed={douyin_relaxed}, seo_pages={seo_pages})")


if __name__ == "__main__":
    main()
