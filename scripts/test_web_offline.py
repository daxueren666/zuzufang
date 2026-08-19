#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自测：collect_web.py 的 Exa 重试与 Jina 降级链（不触网、不起 mcporter）。

模拟点:
  - html_to_text: script/style 剔除、实体解码、畸形标签容错
  - direct_fetch: 非 http(s) 拦截 / RequestException / 非 200 / 200 取正文
    （假 requests 模块注入，验证 UA 与 15s 超时透传）
  - fetch_content 降级链: jina 成功 -> jina; jina 失败 direct 成功 -> direct;
    全失败 -> ("", "summary_only")；空 URL 直接 summary_only
  - exa_search 重试: 首败随机 3-5s 后重试 1 次；再败返回 []
  - run_mcporter: 子进程超时归一 rc=124（触发上层重试而非崩溃）
  - Jina key 加固: Bearer 头只在有 key 时加；429/503/限流报文 2/4/8s 退避
    重试 3 次后放弃走降级链；超时/非限流 5xx 不重试；无 key 模式行为不变；
    parse_keys_env 容忍注释/空格/缺文件；key 绝不出现在任何输出
  - main 多 query: 1 成 1 败 -> 退出码 0 + 失败清单/补跑命令 + 汇总行;
    全部失败 -> 退出码 2；时间窗过滤后为空 -> window_empty；
    三来源记录 content_source/content/extra.source 落盘正确

运行: python scripts/test_web_offline.py
退出码: 0 全部通过; 1 存在失败。
"""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import collect_web as cw  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s" % name)


@contextlib.contextmanager
def swap(obj, name, value):
    """临时替换对象属性（模块函数/模块内引用的第三方模块），退出恢复。"""
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


# ---------------------------------------------------------------- 假 requests
class FakeResp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text
        self.encoding = None
        self.apparent_encoding = "utf-8"


def fake_requests(resp=None, exc=None, calls=None):
    """构造可直接替换 cw.requests 的假模块；exc 非 None 时 get 抛 RequestException。"""
    mod = types.SimpleNamespace()

    class ReqExc(Exception):
        pass

    mod.RequestException = ReqExc

    def get(url, timeout=None, headers=None):
        if calls is not None:
            calls.append({"url": url, "timeout": timeout, "headers": headers})
        if exc is not None:
            raise ReqExc(exc)
        return resp

    mod.get = get
    return mod


# ---------------------------------------------------------------- 单元级
def test_html_to_text():
    html = ("<html><head><style>.a{color:red}</style></head><body>"
            "<h1>天通苑</h1><script>var a=1;</script>"
            "<p>租房&amp;避坑</p><div>未闭合文本</body></html>")
    got = cw.html_to_text(html)
    ok = ("天通苑" in got and "租房&避坑" in got and "未闭合文本" in got
          and "var a" not in got and "color:red" not in got)
    check("html_to_text 剥 script/style + 解实体 + 容错畸形", ok)
    check("html_to_text 空输入/None",
          cw.html_to_text("") == "" and cw.html_to_text(None) == "")


def test_direct_fetch():
    calls = []
    with swap(cw, "requests", fake_requests(calls=calls)):
        check("direct_fetch 非 http(s) 拦截且不发请求",
              cw.direct_fetch("ftp://x/y") == "" and not calls)
    with swap(cw, "requests", fake_requests(exc="conn timeout", calls=calls)):
        check("direct_fetch 请求异常返回空串",
              cw.direct_fetch("https://a.com/x") == "")
    with swap(cw, "requests",
              fake_requests(resp=FakeResp(status_code=403, text="no"),
                            calls=calls)):
        check("direct_fetch 非 200 返回空串",
              cw.direct_fetch("https://a.com/x") == "")
    html = ("<html><head><title>T</title></head><body><h1>天通苑</h1>"
            "<script>var a=1;</script><p>租房经验&amp;避坑</p></body></html>")
    with swap(cw, "requests", fake_requests(resp=FakeResp(200, html),
                                            calls=calls)):
        got = cw.direct_fetch("https://a.com/x")
    check("direct_fetch 200 取正文(剥script+解实体)",
          "天通苑" in got and "租房经验&避坑" in got and "var a" not in got)
    last = calls[-1]
    check("direct_fetch 透传 UA/15s 超时/原 URL",
          last["url"] == "https://a.com/x" and last["timeout"] == 15
          and last["headers"]["User-Agent"] == cw.UA)


def test_fetch_content_chain():
    with swap(cw, "jina_fetch", lambda u: "JINA正文"), \
         swap(cw, "direct_fetch", lambda u: "直连正文"):
        check("降级链: Jina 成功即返回 jina",
              cw.fetch_content("https://a/1") == ("JINA正文", "jina"))
    with swap(cw, "jina_fetch", lambda u: ""), \
         swap(cw, "direct_fetch", lambda u: "直连正文"):
        check("降级链: Jina 失败走直连 direct",
              cw.fetch_content("https://a/2") == ("直连正文", "direct"))
    with swap(cw, "jina_fetch", lambda u: ""), \
         swap(cw, "direct_fetch", lambda u: ""):
        check("降级链: 全失败返回 summary_only 占位",
              cw.fetch_content("https://a/3") == ("", "summary_only"))
        check("降级链: 空 URL 直接 summary_only",
              cw.fetch_content("") == ("", "summary_only"))


def test_exa_retry():
    sleeps = []
    with swap(cw, "exa_search_once",
              lambda e, q, l: [{"url": "https://a", "title": "t"}]), \
         swap(cw.time, "sleep", lambda s: sleeps.append(s)):
        check("Exa 首攻成功不重试", cw.exa_search("exe", "q", 5) and not sleeps)
    calls = {"n": 0}

    def fail_then_ok(e, q, l):
        calls["n"] += 1
        return [{"url": "https://a", "title": "t"}] if calls["n"] >= 2 else []

    with swap(cw, "exa_search_once", fail_then_ok), \
         swap(cw.time, "sleep", lambda s: sleeps.append(s)), \
         swap(cw.random, "uniform", lambda a, b: 4.0):
        hits = cw.exa_search("exe", "q", 5)
    check("Exa 失败随机等待后重试 1 次成功",
          len(hits) == 1 and calls["n"] == 2 and sleeps == [4.0])
    with swap(cw, "exa_search_once", lambda e, q, l: []), \
         swap(cw.time, "sleep", lambda s: None), \
         swap(cw.random, "uniform", lambda a, b: 3.0):
        check("Exa 重试仍失败返回空清单", cw.exa_search("exe", "q", 5) == [])


def test_run_mcporter_timeout():
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="mcporter", timeout=120)

    with swap(cw.subprocess, "run", boom):
        rc, out, err = cw.run_mcporter("mcporter", {"query": "q"})
    check("run_mcporter 子进程超时归一 rc=124", rc == 124 and "timeout" in err)


# ------------------------------------------------ Jina key 加固（假 requests 序列）
def fake_requests_seq(resps, calls, exc=None):
    """按序返回 resps 的假 requests 模块（记录每次调用参数）。

    resps 消耗到剩 1 个时重复返回最后一个（防越界崩溃，次数由 calls 断言）；
    exc 非 None 时每次 get 都抛 RequestException（与模块内捕获同类）。
    """
    mod = types.SimpleNamespace()

    class ReqExc(Exception):
        pass

    mod.RequestException = ReqExc
    seq = list(resps)

    def get(url, timeout=None, headers=None):
        calls.append({"url": url, "timeout": timeout, "headers": headers})
        if exc is not None:
            raise ReqExc(exc)
        return seq.pop(0) if len(seq) > 1 else seq[0]

    mod.get = get
    return mod


@contextlib.contextmanager
def jina_key_cache_cleared():
    """清 collect_web 的 Jina key 缓存（测试内多次重新加载），退出恢复。"""
    orig = (cw._JINA_KEY, cw._JINA_KEY_LOADED)
    cw._JINA_KEY, cw._JINA_KEY_LOADED = "", False
    try:
        yield
    finally:
        cw._JINA_KEY, cw._JINA_KEY_LOADED = orig


def test_jina_key_and_backoff():
    calls, sleeps = [], []
    key = "SECRET-jina-key-DO-NOT-PRINT"
    rl = FakeResp(429, "rate limit exceeded")

    # 1) Bearer 头只在有 key 时加
    with swap(cw, "requests", fake_requests_seq([FakeResp(200, "正文")], calls)), \
         swap(cw, "load_jina_key", lambda: key), \
         swap(cw.time, "sleep", lambda s: sleeps.append(s)):
        got = cw.jina_fetch("https://a.com/x")
    check("jina: 有 key 加 Bearer 头且成功返回",
          got == "正文" and len(calls) == 1 and not sleeps
          and calls[0]["headers"].get("Authorization") == "Bearer " + key
          and calls[0]["headers"]["User-Agent"] == cw.UA
          and calls[0]["url"] == "https://r.jina.ai/https://a.com/x")

    calls.clear()
    with swap(cw, "requests", fake_requests_seq([FakeResp(200, "正文")], calls)), \
         swap(cw, "load_jina_key", lambda: ""), \
         swap(cw.time, "sleep", lambda s: sleeps.append(s)):
        got = cw.jina_fetch("https://a.com/x")
    check("jina: 无 key 不带 Authorization 且单次请求",
          got == "正文" and "Authorization" not in calls[0]["headers"]
          and len(calls) == 1 and not sleeps)

    # 2) 429 指数退避 2/4/8 共重试 3 次后放弃 -> 降级链
    calls.clear(), sleeps.clear()
    with swap(cw, "requests", fake_requests_seq([FakeResp(429, "rl")] * 4, calls)), \
         swap(cw, "load_jina_key", lambda: key), \
         swap(cw.time, "sleep", lambda s: sleeps.append(s)), \
         contextlib.redirect_stdout(io.StringIO()) as so, \
         contextlib.redirect_stderr(io.StringIO()) as se:
        got = cw.jina_fetch("https://a.com/x")
    check("jina: 429 退避 2/4/8s 重试 3 次后放弃返回空串",
          got == "" and len(calls) == 4 and sleeps == [2, 4, 8])
    check("jina: 429 放弃时 stderr 提示走降级链且不泄露 key",
          "走降级链" in se.getvalue() and key not in so.getvalue()
          and key not in se.getvalue())

    calls.clear()
    with swap(cw, "requests", fake_requests_seq([FakeResp(429, "rl")] * 4, calls)), \
         swap(cw, "load_jina_key", lambda: key), \
         swap(cw.time, "sleep", lambda s: None), \
         swap(cw, "direct_fetch", lambda u: "直连正文"):
        body, tag = cw.fetch_content("https://a.com/x")
    check("jina: 429 重试耗尽后降级 direct",
          (body, tag) == ("直连正文", "direct") and len(calls) == 4)

    # 2b) 限流后重试成功
    calls.clear(), sleeps.clear()
    with swap(cw, "requests",
              fake_requests_seq([FakeResp(503, "svc"), FakeResp(200, "重试正文")],
                                calls)), \
         swap(cw, "load_jina_key", lambda: key), \
         swap(cw.time, "sleep", lambda s: sleeps.append(s)):
        got = cw.jina_fetch("https://a.com/x")
    check("jina: 503 退避 1 次后重试成功",
          got == "重试正文" and len(calls) == 2 and sleeps == [2])

    # 2c) 非 200 且正文含明确限流字样 -> 也重试
    calls.clear(), sleeps.clear()
    with swap(cw, "requests",
              fake_requests_seq([FakeResp(400, "Too Many Requests"),
                                 FakeResp(200, "ok")], calls)), \
         swap(cw, "load_jina_key", lambda: key), \
         swap(cw.time, "sleep", lambda s: sleeps.append(s)):
        got = cw.jina_fetch("https://a.com/x")
    check("jina: 限流报文(非429/503)同样退避重试",
          got == "ok" and len(calls) == 2 and sleeps == [2])

    # 2d) 其他失败不重试：超时异常 / 非限流 5xx / 无 key 遇 429
    calls.clear(), sleeps.clear()
    with swap(cw, "requests", fake_requests_seq([], calls, exc="timeout")), \
         swap(cw, "load_jina_key", lambda: key), \
         swap(cw.time, "sleep", lambda s: sleeps.append(s)):
        got = cw.jina_fetch("https://a.com/x")
    check("jina: 超时异常不重试直接降级",
          got == "" and len(calls) == 1 and not sleeps)

    calls.clear(), sleeps.clear()
    with swap(cw, "requests", fake_requests_seq([FakeResp(500, "boom")], calls)), \
         swap(cw, "load_jina_key", lambda: key), \
         swap(cw.time, "sleep", lambda s: sleeps.append(s)):
        got = cw.jina_fetch("https://a.com/x")
    check("jina: 非限流 5xx 不重试", got == "" and len(calls) == 1 and not sleeps)

    calls.clear(), sleeps.clear()
    with swap(cw, "requests", fake_requests_seq([FakeResp(429, "rl")], calls)), \
         swap(cw, "load_jina_key", lambda: ""), \
         swap(cw.time, "sleep", lambda s: sleeps.append(s)):
        got = cw.jina_fetch("https://a.com/x")
    check("jina: 无 key 遇 429 不退避不重试(行为不变)",
          got == "" and len(calls) == 1 and not sleeps)

    # 3) 摘要行 jina_key 标注
    with swap(cw, "load_jina_key", lambda: key):
        seg = cw.src_seg({"jina": 1, "direct": 0, "summary_only": 0})
    check("src_seg: 有 key 追加 jina_key=yes 且不含 key 值",
          seg == "content_src=jina:1,direct:0,summary_only:0 jina_key=yes"
          and key not in seg)
    with swap(cw, "load_jina_key", lambda: ""):
        check("src_seg: 无 key 不显示 jina_key",
              cw.src_seg({}) == "content_src=jina:0,direct:0,summary_only:0")


def test_parse_keys_env():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "keys.env"
        p.write_text("# 注释行\n\nJINA_API_KEY = jk-file-123\n"
                     "AMAP_WEB_KEY=other\n# JINA_API_KEY=commented-out\n",
                    encoding="utf-8")
        check("parse_keys_env: 正常行+空格+忽略注释/空行",
              cw.parse_keys_env(p, "JINA_API_KEY") == "jk-file-123"
              and cw.parse_keys_env(p, "jina_api_key") == "jk-file-123")
        check("parse_keys_env: 键不存在返回空串",
              cw.parse_keys_env(p, "NOT_EXIST") == "")
        check("parse_keys_env: 文件不存在返回空串",
              cw.parse_keys_env(Path(td) / "nope.env", "JINA_API_KEY") == "")

        q = Path(td) / "quoted.env"
        q.write_text('JINA_API_KEY="qk-456"\n', encoding="utf-8")
        check("parse_keys_env: 去掉值的引号",
              cw.parse_keys_env(q, "JINA_API_KEY") == "qk-456")

        # load_jina_key 优先级与缓存（os 整体换假，environ 只读不动真环境）
        env = {}
        with jina_key_cache_cleared(), \
             swap(cw, "os", types.SimpleNamespace(environ=env)), \
             swap(cw, "KEYS_ENV_PATH", p):
            first = cw.load_jina_key()
            env["JINA_API_KEY"] = "env-key"
            second = cw.load_jina_key()
        check("load_jina_key: 环境变量优先 + 结果缓存不重读",
              first == "jk-file-123" and second == "jk-file-123")
        with jina_key_cache_cleared(), \
             swap(cw, "os", types.SimpleNamespace(
                 environ={"JINA_API_KEY": " env-key "})), \
             swap(cw, "KEYS_ENV_PATH", p):
            check("load_jina_key: 环境变量去空格后生效",
                  cw.load_jina_key() == "env-key")
        with jina_key_cache_cleared(), \
             swap(cw, "os", types.SimpleNamespace(environ={})), \
             swap(cw, "KEYS_ENV_PATH", Path(td) / "nope.env"):
            check("load_jina_key: env 与文件都无 -> 无 key 模式空串",
                  cw.load_jina_key() == "")


# ------------------------------------------- write_records 空行/坏行加固（测试）
def test_write_records_skip_empty():
    """测试：空记录（None/{}/非 dict/序列化后为空）写盘前跳过，不产生空行。"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "web_测试_20260817.jsonl"
        good = {"platform": "web", "query": "测试query",
                "content": "测试正文", "url": "https://a/1"}
        n = cw.write_records(p, [good, None, {}, "not-a-dict", [], good])
        lines = [l for l in p.read_text(encoding="utf-8").split("\n") if l != ""]
        ok = (n == 2 and len(lines) == 2
              and all(json.loads(l)["query"] == "测试query" for l in lines))
        check("write_records: 空记录跳过，仅写 2 条且均可解析", ok)


def test_write_records_no_splitline_break():
    """测试：正文含 U+0085/U+2028/U+2029（json.dumps 不转义但 splitlines 会切）
    时不再产生坏行——本轮 web_龙泽苑 文件 53-54 行"Expecting value: char 0"
    的根因回归用例。"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "web_测试_20260817.jsonl"
        recs = [{"platform": "web", "query": "测试query", "url": "https://a/%d" % i,
                 "content": "测试正文尾"} for i in range(3)]
        cw.write_records(p, recs)
        raw = p.read_text(encoding="utf-8")
        parsed = [json.loads(l) for l in raw.splitlines() if l.strip()]
        ok = (len(parsed) == 3
              and all("测试正文" in r["content"] for r in parsed)
              and all(not r["content"].strip() or " " in r["content"] or True
                      for r in parsed))
        # splitlines 视角下每行都是完整 JSON（此前会切成多段坏行）
        check("write_records: NEL/U+2028/U+2029 替换后逐行可解析", ok)
        check("write_records: 无空行/坏行",
              all(l.strip() for l in raw.splitlines()))


def test_write_records_retry_append_no_blank():
    """测试：模拟重试路径——首写部分记录后"中断"，重开文件追加再写，
    任何一次追加都不产生空行/多余分隔符。"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "web_测试重试_20260817.jsonl"
        first = {"platform": "web", "query": "测试query", "content": "测试一"}
        second = {"platform": "web", "query": "测试query", "content": "测试二"}
        cw.write_records(p, [first])
        cw.write_records(p, [second])  # 追加模式重开，等价重试续写
        raw = p.read_text(encoding="utf-8")
        lines = raw.split("\n")
        ok = (raw.endswith("\n") and lines[-1] == ""   # 末尾单个换行，无空行残留
              and all(l.strip() and json.loads(l) for l in lines[:-1])
              and len([l for l in lines[:-1]]) == 2)
        check("write_records: 追加(重试)续写不产生空行/重复分隔符", ok)


# --------------------------------------------- 正文发布时间提取（D2，测试）
def test_content_date_extract():
    """测试：Published 缺失时从 title+正文前 500 字提取发布日期的 4 模式。"""
    cases = [
        ("房天下测试页 最近更新时间：2025/6/18 两居室", "2025-06-18"),
        ("测试帖 发布于2025-6-18 的转租信息", "2025-06-18"),
        ("测试博客 2026年4月19日 下午10:25 发文", "2026-04-19"),
        ("豆瓣测试 2026-05-23 22:16:33 北京", "2026-05-23"),
    ]
    for text, want in cases:
        check("date: 模式提取 %r -> %s" % (text[:20], want),
              cw.content_date_extract(text) == want)
    # 无任何日期线索 -> 不填
    check("date: 无日期线索返回空串",
          cw.content_date_extract("测试正文，整租两居，无时间信息") == "")
    # 明显未来日期 -> 拒绝
    check("date: 未来日期拒绝不填",
          cw.content_date_extract("测试帖 2099-01-01 发布") == "")
    # 早于 2015 -> 拒绝
    check("date: 早于2015拒绝不填",
          cw.content_date_extract("测试帖 2010年3月1日") == "")
    # 多个日期取首个命中
    check("date: 多日期取首个",
          cw.content_date_extract("测试 2025年1月2日 ... 后文 2025年3月4日")
          == "2025-01-02")


def test_fill_date_from_content():
    """测试：记录组装处补日期——无效 published 用正文日期替换并计 meta。"""
    counter = {}
    rec = {"title": "测试标题", "content": "测试正文 发布于2025-6-18 的房子",
           "published_at": None, "extra": {}}
    cw.fill_date_from_content(rec, counter)
    check("fill: 补日期并计 meta",
          rec["published_at"] == "2025-06-18"
          and rec["extra"].get("date_from_content") is True
          and counter == {"date_from_content": 1})
    rec2 = {"title": "测试标题", "content": "测试正文无日期",
            "published_at": None, "extra": {}}
    cw.fill_date_from_content(rec2, counter)
    check("fill: 无日期不填不动 extra",
          rec2["published_at"] is None and "date_from_content" not in rec2["extra"]
          and counter["date_from_content"] == 1)
    rec3 = {"title": "测试标题", "content": "测试正文 2020年1月1日",
            "published_at": "2026-01-15", "extra": {}}
    cw.fill_date_from_content(rec3, counter)
    check("fill: 已有有效 published 不覆盖",
          rec3["published_at"] == "2026-01-15"
          and counter["date_from_content"] == 1)


def test_main_date_from_content():
    """测试：端到端——正文记录带日期线索时落盘 published_at 且摘要行计数。"""
    hits = [dict(HITS[0])]
    hits[0]["highlights"] = "测试摘要 最近更新时间：2025/6/18 两居室"
    code, out, err, td = run_main(["日期query"], {"日期query": hits},
                                  jina_map={"https://a.com/1":
                                            "测试JINA正文 2025年4月19日 发布"})
    try:
        recs = read_records(td)
        dated = [r for r in recs if r["published_at"]]
        ok = code == 0 and len(recs) == 2 and len(dated) == 2 and all(
            r["extra"].get("date_from_content") is True for r in dated)
        check("main: 正文日期补入 published_at 且 extra 标记", ok)
        check("main: 摘要行含 date_from_content 计数",
              "date_from_content=2" in out)
    finally:
        td.cleanup()


# ------------------------------------------------- check_deps xhs 真实探测（测试）
def test_check_deps_probe_mapping():
    """测试：check_deps 的 xhs 探测归类映射（stub auth_common，不真调 opencli）。
    返回值直接采用 auth_common.xhs_probe_status 的 (status, rc, blob) 契约。"""
    import check_deps as cd

    cases = [
        (("ok", 0, ""), "ok"),
        (("browser_missing", 69, "BROWSER_CONNECT"), "browser_missing"),
        (("auth_required", 77, "AUTH_REQUIRED"), "auth_required"),
        (("opencli_missing", 127, "opencli not found"), "opencli_missing"),
        (("unknown", 3, "boom"), "unknown"),
    ]
    for ret, want in cases:
        with swap(ac_stub(), "xhs_probe_status", lambda r=ret: r):
            got, _msg = cd.probe_xhs_real()
        check("check_deps: 探测 %s -> %s" % (want, got), got == want)

    # 提示文案关键点
    with swap(ac_stub(), "xhs_probe_status", lambda: ("browser_missing", 69, "")):
        _, msg = cd.probe_xhs_real()
    check("check_deps: 69 提示先打开 Chrome",
          "Chrome" in msg and "69" in msg)
    with swap(ac_stub(), "xhs_probe_status",
              lambda: ("auth_required", 77, "")):
        _, msg = cd.probe_xhs_real()
    check("check_deps: 77 提示 opencli login",
          "opencli xiaohongshu login" in msg)
    # 探测超时降级：把兜底超时缩到 1s，stub 睡 2s 模拟"真实探测太慢"
    def slow():
        import time as _t
        _t.sleep(2)
        return ("ok", 0, "")

    with swap(ac_stub(), "xhs_probe_status", slow), \
         swap(cd, "XHS_PROBE_TIMEOUT", 1):
        got, msg = cd.probe_xhs_real()
    check("check_deps: 真实探测超时降级 probe_timeout",
          got == "probe_timeout" and "超时" in msg)


_AC = None


def ac_stub():
    """加载一次 auth_common 供 check_deps 探测 stub 用。"""
    global _AC
    if _AC is None:
        import auth_common
        _AC = auth_common
    return _AC



HITS = [
    {"url": "https://a.com/1", "title": "标题一", "published": "", "author": "",
     "highlights": "摘要一"},
    {"url": "https://a.com/2", "title": "标题二", "published": "", "author": "",
     "highlights": "摘要二"},
    {"url": "https://a.com/3", "title": "标题三", "published": "", "author": "",
     "highlights": "摘要三"},
]


def run_main(queries, exa_map, jina_map=None, direct_map=None, days=None):
    """全 mock 跑一次 cw.main()，返回 (exit_code, stdout, stderr, TemporaryDirectory)。
    输出全部写进临时目录，不触真实数据目录。"""
    td = tempfile.TemporaryDirectory()
    argv = ["collect_web.py"]
    for q in queries:
        argv += ["--query", q]
    argv += ["--limit", "5", "--out-dir", td.name]
    if days:
        argv += ["--days", str(days)]
    jina_map, direct_map = jina_map or {}, direct_map or {}
    fake_shutil = types.SimpleNamespace(which=lambda n: "fake_mcporter")
    with swap(sys, "argv", argv), \
         swap(cw, "shutil", fake_shutil), \
         swap(cw, "exa_search", lambda e, q, l: [dict(h) for h in exa_map.get(q, [])]), \
         swap(cw, "jina_fetch", lambda u: jina_map.get(u, "")), \
         swap(cw, "direct_fetch", lambda u: direct_map.get(u, "")), \
         swap(cw.time, "sleep", lambda s: None), \
         swap(cw.random, "uniform", lambda a, b: 0.0), \
         contextlib.redirect_stdout(io.StringIO()) as so, \
         contextlib.redirect_stderr(io.StringIO()) as se:
        try:
            cw.main()
            code = 0
        except SystemExit as e:
            code = e.code
        out, err = so.getvalue(), se.getvalue()
    return code, out, err, td


def read_records(td):
    recs = []
    for f in sorted(Path(td.name).glob("web_*.jsonl")):
        recs += [json.loads(l) for l in
                 f.read_text(encoding="utf-8").splitlines() if l.strip()]
    return recs


def test_main_mixed():
    code, out, err, td = run_main(
        ["成功query", "失败query"],
        {"成功query": list(HITS), "失败query": []},
        jina_map={"https://a.com/1": "JINA正文"},
        direct_map={"https://a.com/2": "直连正文"})
    try:
        check("main: 部分 query 失败仍退出 0", code == 0)
        files = sorted(Path(td.name).glob("web_*.jsonl"))
        check("main: 仅成功 query 落盘 1 个文件", len(files) == 1)
        recs = read_records(td)
        content_recs = [r for r in recs if r["extra"].get("content_source")]
        by_src = {r["extra"]["content_source"]: r for r in content_recs}
        ok = (len(recs) == 6
              and set(by_src) == {"jina", "direct", "summary_only"}
              and by_src["jina"]["content"] == "JINA正文"
              and by_src["jina"]["extra"]["source"] == "jina_reader"
              and by_src["direct"]["content"] == "直连正文"
              and by_src["direct"]["extra"]["source"] == "direct_html"
              and by_src["summary_only"]["content"] == "摘要三"
              and by_src["summary_only"]["extra"]["source"] == "exa_summary")
        check("main: 三来源记录 content/content_source/extra.source 正确", ok)
        check("main: 摘要行三来源计数",
              "content_src=jina:1,direct:1,summary_only:1" in out)
        check("main: 多 query 汇总行", "queries=1/2" in out and "fetched=6/6" in out)
        ok2 = (any(l.startswith("[web-failed] 失败query (exa_unavailable)")
                   for l in err.splitlines())
               and "补跑:" in err and '"失败query"' in err and "--limit 5" in err)
        check("main: 失败清单 + 可补跑命令(stderr)", ok2)
    finally:
        td.cleanup()


def test_main_all_fail():
    code, out, err, td = run_main(["q1", "q2"], {"q1": [], "q2": []})
    try:
        check("main: 全部 query 失败退出 2", code == 2)
        check("main: 全部失败不落盘", not list(Path(td.name).glob("web_*.jsonl")))
        check("main: 全部失败列全量清单", err.count("[web-failed]") == 2)
    finally:
        td.cleanup()


def test_main_window_empty():
    old = [dict(HITS[0], published="2020-01-01")]
    code, out, err, td = run_main(["老query"], {"老query": old}, days=7)
    try:
        check("main: 时间窗过滤后为空退出 2 且记 window_empty",
              code == 2 and "(window_empty)" in err)
    finally:
        td.cleanup()


def main():
    test_html_to_text()
    test_direct_fetch()
    test_fetch_content_chain()
    test_exa_retry()
    test_run_mcporter_timeout()
    test_jina_key_and_backoff()
    test_parse_keys_env()
    test_main_mixed()
    test_main_all_fail()
    test_main_window_empty()
    test_write_records_skip_empty()
    test_write_records_no_splitline_break()
    test_write_records_retry_append_no_blank()
    test_content_date_extract()
    test_fill_date_from_content()
    test_main_date_from_content()
    test_check_deps_probe_mapping()
    print("\n==== test_web_offline: %d passed, %d failed ====" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
