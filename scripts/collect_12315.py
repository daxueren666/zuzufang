#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全国 12315 平台投诉公示采集(三段降级)。

段1: requests 直连 tsgs.12315.cn(创宇盾 WAF + hash 路由 SPA, 大概率拿不到数据)。
段2: Jina Reader 代理试一次。
段3: 均失败 -> stderr 提示人工访问, exit 2。

用法:
    python collect_12315.py --query 贝壳找房 --limit 20
"""
import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import auth_common as ac  # noqa: E402  统一数据目录解析（data_dir）

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT_DIR = str(ac.data_dir("raw"))  # ~/.rent-assist/data/raw
DEFAULT_ENTRY_URL = "https://tsgs.12315.cn/"
JINA_PREFIX = "https://r.jina.ai/"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def rand_sleep():
    time.sleep(random.uniform(2, 5))


def sanitize_query(query):
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", query).strip("_")
    return safe[:50] or "query"


def has_complaint_data(text, query):
    """判断返回内容是否含可解析的投诉数据(而非 SPA 空壳/WAF 拦截页)。"""
    if not text or len(text) < 300:
        return False
    if query and query in text:
        return True
    dates = re.findall(r"20\d{2}-\d{2}-\d{2}", text)
    return len(dates) >= 3 and "投诉" in text and len(text) > 1000


def extract_records(text, query, limit):
    """尽力从文本中抽投诉条目: 按空行分段, 保留含企业名或投诉要素的段。"""
    plain = re.sub(r"<(script|style)\b.*?</\1>", " ", text, flags=re.S | re.I)
    records, seen = [], set()
    for para in re.split(r"\n\s*\n", plain):
        para = para.strip()
        if len(para) < 20:
            continue
        if query not in para and "投诉" not in para:
            continue
        key = para[:80]
        if key in seen:
            continue
        seen.add(key)
        dm = re.search(r"(20\d{2}-\d{2}-\d{2})", para)
        records.append({
            "content": para[:500],
            "published_at": dm.group(1) if dm else "",
        })
        if len(records) >= limit:
            break
    return records


def build_record(query, entry_url, item, source):
    return {
        "platform": "complaint12315",
        "query": query,
        "collected_at": datetime.now().isoformat(timespec="seconds"),
        "url": entry_url,
        "title": item.get("title") or ("%s 投诉公示" % query),
        "content": item.get("content", ""),
        "author": "",
        "published_at": item.get("published_at", ""),
        "likes": None,
        "comments_count": None,
        "comments": [],
        "extra": {
            "company": query,
            "source": source,
            "total_complaints": item.get("total_complaints"),
        },
    }


def try_direct(session, entry_url, query):
    """段1: 直连, 双次请求携带 WAF Set-Cookie。返回 (text, ok)。"""
    print("[段1] 直连 %s ..." % entry_url)
    text, status, length = None, None, 0
    for i in range(2):
        try:
            resp = session.get(entry_url, timeout=15, allow_redirects=True)
            status, text, length = resp.status_code, resp.text, len(resp.text)
        except requests.RequestException as exc:
            print("[段1] 第 %d 次请求失败: %s" % (i + 1, exc), file=sys.stderr)
            continue
        if i == 0:
            time.sleep(random.uniform(1, 2))
    if text is not None:
        print("[段1] status=%s len=%s" % (status, length))
    if text is not None and has_complaint_data(text, query):
        return text, True
    print("[段1] 未拿到可解析的投诉数据(疑似创宇盾WAF拦截或SPA空壳)")
    return text, False


def try_jina(session, entry_url, query):
    """段2: Jina Reader 代理试一次。返回 (text, ok)。"""
    jina_url = JINA_PREFIX + entry_url
    print("[段2] 尝试 Jina Reader: %s" % jina_url)
    try:
        resp = session.get(jina_url, timeout=30)
        print("[段2] status=%s len=%s" % (resp.status_code, len(resp.text)))
        if resp.status_code == 200 and has_complaint_data(resp.text, query):
            return resp.text, True
    except requests.RequestException as exc:
        print("[段2] Jina Reader 请求失败: %s" % exc, file=sys.stderr)
    print("[段2] Jina Reader 无有效数据")
    return None, False


def main():
    ap = argparse.ArgumentParser(description="全国12315平台投诉公示采集(三段降级)")
    ap.add_argument("--query", required=True, help="企业名称")
    ap.add_argument("--limit", type=int, default=20, help="最多采集条数, 默认 20")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                    help="jsonl 输出目录, 默认 ~/.rent-assist/data/raw")
    ap.add_argument("--entry-url", default=DEFAULT_ENTRY_URL,
                    help="公示入口 URL, 默认 https://tsgs.12315.cn/")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(
        args.out_dir, "12315_%s_%s.jsonl" % (sanitize_query(args.query),
                                             datetime.now().strftime("%Y%m%d")))

    session = requests.Session()
    session.headers.update(BASE_HEADERS)

    text, ok = try_direct(session, args.entry_url, args.query)
    source = "direct"
    if not ok:
        rand_sleep()
        text, ok = try_jina(session, args.entry_url, args.query)
        source = "jina"
    if not ok:
        print("12315 公示接口不可直连, 建议人工访问 %s 查询企业 %s"
              % (args.entry_url, args.query), file=sys.stderr)
        sys.exit(2)

    items = extract_records(text, args.query, args.limit)
    total = re.search(r"(?:共计|共)\s*(\d+)\s*(?:条|件)", text)
    written = 0
    for item in items:
        if total:
            item["total_complaints"] = int(total.group(1))
        with open(out_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(build_record(args.query, args.entry_url, item, source),
                                ensure_ascii=False) + "\n")
        written += 1
    if written == 0:
        print("[警告] 返回内容含数据标记但未能抽取条目", file=sys.stderr)
        sys.exit(2)
    print("platform=complaint12315 fetched=%d/%d file=%s" % (written, len(items), out_path))
    sys.exit(0)


if __name__ == "__main__":
    main()
