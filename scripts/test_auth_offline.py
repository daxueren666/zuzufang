#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自测：ensure_auth / collect_* 登录态分支（不触网、不弹浏览器、不真开 Chrome）。

模拟点:
  - xhs 探测三路径：exit 77(AUTH_REQUIRED) / 69(BROWSER_CONNECT) / 0(成功)
    —— 用假 subprocess 返回值（替换 auth_common.run_xhs_probe）注入
  - ensure_auth 轮询等待扫码：POLL_INTERVAL 调小，模拟"先 77 后成功"与"一直 77 超时"
  - douban 持久化 context：假 playwright 模块，验证 launch_persistent_context
    参数与 context.close() 落盘语义；级别2 分支矩阵 = 假 stdin(None/非tty/tty)
    × 档案(存在/不存在) × 静默试探(成功/失败)：成功→无头直采不弹窗，失败→
    非交互 NeedsLogin(main exit 3)/交互弹可见窗+轮询等待(wait_douban_login,
    不再 input())；无头遇滑块记被拦
  - douban 公共函数：non_interactive / douban_logged_in(纯函数) /
    wait_douban_login(cookie 出现→True / 一直无→超时 False)；
    ensure_douban 交互完整流程：档案已有 dbclv→秒 ok 不弹窗；无 dbclv→
    headless 试探+headed 弹窗+轮询(成功 ok / 超时 timeout)；非交互=只探测
  - collect_douban 一级 HTTP 403 → 随机 2-5s 后重试 1 次(403→200 恢复 /
    403→403 仍被拦)
  - collect_xhs exit 3（需登录）/ exit 2（无结果）
  - collect_douyin 缓存探测与登记

运行: python scripts/test_auth_offline.py
退出码: 0 全部通过; 1 存在失败。
"""

import json
import os
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import auth_common as ac          # noqa: E402
import ensure_auth as ea          # noqa: E402
import collect_xhs as cx          # noqa: E402
import collect_douyin as cdn      # noqa: E402
import collect_douban as cdb      # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s" % name)


class TmpState:
    """把 auth_state.json 指到临时目录，测试间互不污染。"""

    def __enter__(self):
        self.td = tempfile.TemporaryDirectory()
        self.orig = ac.AUTH_STATE_PATH
        ac.AUTH_STATE_PATH = Path(self.td.name) / "auth_state.json"
        return self.td

    def __exit__(self, *a):
        ac.AUTH_STATE_PATH = self.orig
        self.td.cleanup()
        return False


class FakeStdin:
    def isatty(self):
        return False


# ---------------------------------------------------------------- auth_common
def test_auth_state_roundtrip():
    with TmpState():
        ac.update_auth_state("xhs", True, probe="opencli_search")
        st = ac.load_auth_state()
        ok1 = (st.get("xhs", {}).get("ok") is True
               and "verified_at" in st["xhs"]
               and st["xhs"]["probe"] == "opencli_search")
        ac.update_auth_state("xhs", False, reason="login_timeout")
        st = ac.load_auth_state()
        ok2 = st["xhs"]["ok"] is False and st["xhs"]["reason"] == "login_timeout"
        check("auth_state 写读回环+覆盖更新", ok1 and ok2)
    # 本机可能存在真实 auth_state.json, 指到不存在的临时路径再断言空读
    with tempfile.TemporaryDirectory() as td:
        orig_path = ac.AUTH_STATE_PATH
        ac.AUTH_STATE_PATH = Path(td) / "auth_state.json"
        try:
            check("auth_state 不存在时返回 {}", ac.load_auth_state() == {})
        finally:
            ac.AUTH_STATE_PATH = orig_path


def test_xhs_probe_classify():
    cases = [
        ((0, '[{"title":"x"}]', ""), "ok"),
        ((77, "", "AUTH_REQUIRED: login wall"), "auth_required"),
        ((1, "", "error: AUTH_REQUIRED ..."), "auth_required"),   # 文本兜底
        ((69, "", "BROWSER_CONNECT: chrome not running"), "browser_missing"),
        ((127, "", "opencli not found in PATH"), "opencli_missing"),
        ((1, "", "boom"), "unknown"),
    ]
    allok = True
    for (ret, expect) in cases:
        orig = ac.run_xhs_probe
        ac.run_xhs_probe = lambda r=ret: r
        try:
            status = ac.xhs_probe_status()[0]
        finally:
            ac.run_xhs_probe = orig
        if status != expect:
            allok = False
            print("    期望 %s 得到 %s (输入 %s)" % (expect, status, ret))
    check("xhs 探测分类 77/69/成功/文本兜底", allok)


def test_run_xhs_probe_missing_opencli():
    import shutil
    orig = shutil.which
    shutil.which = lambda name: None
    try:
        rc, out, err = ac.run_xhs_probe()
        ok = rc == 127 and "opencli" in err
    finally:
        shutil.which = orig
    check("opencli 缺失 → run_xhs_probe rc=127", ok)


def test_douyin_cache_probe():
    with tempfile.TemporaryDirectory() as td:
        empty = Path(td) / "dy_empty"
        empty.mkdir()
        full = Path(td) / "dy_full"
        full.mkdir()
        (full / "Cookies").write_text("x")
        ok1 = ac.douyin_cache_exists(empty) is False
        ok2 = ac.douyin_cache_exists(full) is True
        ok3 = ac.douyin_cache_exists(Path(td) / "nope") is False
        check("douyin 缓存探测（空目录/非空/不存在）", ok1 and ok2 and ok3)


def test_douban_probe_fake_http():
    fake = types.ModuleType("requests")

    class FakeResp:
        def __init__(self, code):
            self.status_code = code

    fake.get = lambda url, headers=None, timeout=None: FakeResp(200)
    orig = sys.modules.get("requests")
    sys.modules["requests"] = fake
    try:
        ok1 = ac.probe_douban_http() == 200
        fake.get = lambda url, headers=None, timeout=None: FakeResp(403)
        ok2 = ac.probe_douban_http() == 403

        def boom(*a, **k):
            raise RuntimeError("net down")
        fake.get = boom
        ok3 = ac.probe_douban_http() is None
    finally:
        if orig is not None:
            sys.modules["requests"] = orig
        else:
            sys.modules.pop("requests", None)
    check("douban HTTP 探测 200/403/异常", ok1 and ok2 and ok3)


# ---------------------------------------------------------------- ensure_auth
def _patch(target_module, name, value):
    orig = getattr(target_module, name)
    setattr(target_module, name, value)
    return orig


def test_ensure_xhs_ok():
    with TmpState():
        orig = _patch(ea.ac, "run_xhs_probe", lambda: (0, '[{"title":"x"}]', ""))
        orig_open = _patch(ea, "open_chrome", lambda url: True)
        try:
            st = ea.ensure_xhs(600)
            entry = ac.load_auth_state().get("xhs", {})
            ok = st == "ok" and entry.get("ok") is True
        finally:
            _patch(ea.ac, "run_xhs_probe", orig)
            _patch(ea, "open_chrome", orig_open)
    check("ensure_xhs 探测成功 → ok + 写状态", ok)


def test_ensure_xhs_poll_until_success():
    with TmpState():
        seq = [(77, "", "AUTH_REQUIRED: login wall"),
               (77, "", "AUTH_REQUIRED: login wall"),
               (0, "[]", "")]
        opened = []
        orig = _patch(ea.ac, "run_xhs_probe", lambda: seq.pop(0))
        orig_open = _patch(ea, "open_chrome", lambda url: opened.append(url) or True)
        orig_poll = _patch(ea, "POLL_INTERVAL", 0.05)
        try:
            st = ea.ensure_xhs(30)
            entry = ac.load_auth_state().get("xhs", {})
            ok = (st == "ok" and opened == [ea.XHS_LOGIN_URL]
                  and entry.get("ok") is True)
        finally:
            _patch(ea.ac, "run_xhs_probe", orig)
            _patch(ea, "open_chrome", orig_open)
            _patch(ea, "POLL_INTERVAL", orig_poll)
    check("ensure_xhs 77→开Chrome→轮询至登录成功（不秒退）", ok)


def test_ensure_xhs_timeout():
    with TmpState():
        orig = _patch(ea.ac, "run_xhs_probe", lambda: (77, "", "AUTH_REQUIRED"))
        orig_open = _patch(ea, "open_chrome", lambda url: True)
        orig_poll = _patch(ea, "POLL_INTERVAL", 0.05)
        try:
            st = ea.ensure_xhs(1)
            entry = ac.load_auth_state().get("xhs", {})
            ok = st == "timeout" and entry.get("ok") is False \
                and entry.get("reason") == "login_timeout"
        finally:
            _patch(ea.ac, "run_xhs_probe", orig)
            _patch(ea, "open_chrome", orig_open)
            _patch(ea, "POLL_INTERVAL", orig_poll)
    check("ensure_xhs 一直未登录 → 等待超时并登记", ok)


def test_ensure_xhs_browser_missing():
    with TmpState():
        orig = _patch(ea.ac, "run_xhs_probe", lambda: (69, "", "BROWSER_CONNECT"))
        orig_open = _patch(ea, "open_chrome", lambda url: True)
        try:
            st = ea.ensure_xhs(600)
            entry = ac.load_auth_state().get("xhs", {})
            ok = st == "need_login" \
                and entry.get("reason") == "browser_not_connected"
        finally:
            _patch(ea.ac, "run_xhs_probe", orig)
            _patch(ea, "open_chrome", orig_open)
    check("ensure_xhs 69 → 提示先开 Chrome（不进轮询）", ok)


def test_ensure_douyin():
    with TmpState(), tempfile.TemporaryDirectory() as td:
        full = Path(td) / "dy_full"
        full.mkdir()
        (full / "Cookies").write_text("x")
        orig = ac.DOUYIN_DATA_DIR
        try:
            ac.DOUYIN_DATA_DIR = full
            st1 = ea.ensure_douyin(600)
            e1 = ac.load_auth_state().get("douyin", {})
            ac.DOUYIN_DATA_DIR = Path(td) / "nope"
            st2 = ea.ensure_douyin(600)
            e2 = ac.load_auth_state().get("douyin", {})
        finally:
            ac.DOUYIN_DATA_DIR = orig
        ok = (st1 == "ok" and e1.get("ok") is True and e1.get("cache") is True
              and st2 == "need_login" and e2.get("ok") is False)
    check("ensure_douyin 有缓存=ok / 无缓存=need_login", ok)


def test_ensure_douban():
    fake = types.ModuleType("requests")

    class FakeResp:
        def __init__(self, code):
            self.status_code = code

    state = {"code": 200}
    fake.get = lambda url, headers=None, timeout=None: FakeResp(state["code"])
    orig = sys.modules.get("requests")
    orig_dir = ac.DOUBAN_PROFILE_DIR
    sys.modules["requests"] = fake
    with TmpState(), tempfile.TemporaryDirectory() as td:
        ac.DOUBAN_PROFILE_DIR = Path(td)  # 模拟已有持久化档案
        try:
            st1 = ea.ensure_douban(600, interactive=False)
            e1 = ac.load_auth_state().get("douban", {})
            state["code"] = 403
            st2 = ea.ensure_douban(600, interactive=False)
            e2 = ac.load_auth_state().get("douban", {})

            def boom(*a, **k):
                raise RuntimeError("net down")
            fake.get = boom
            st3 = ea.ensure_douban(600, interactive=False)
        finally:
            if orig is not None:
                sys.modules["requests"] = orig
            else:
                sys.modules.pop("requests", None)
            ac.DOUBAN_PROFILE_DIR = orig_dir
    ok = (st1 == "ok" and e1.get("mode") == "anonymous"
          and st2 == "need_login" and e2.get("reason") == "http_403"
          and st3 == "env")
    check("ensure_douban(非交互) 200=ok / 403=需登录 / 异常=env", ok)


class FakeLoginContext:
    """cookies() 前 miss_n 次返回空, 之后返回 dbclv(模拟人工登录完成时序)。"""

    def __init__(self, miss_n):
        self.n, self.miss_n = 0, miss_n

    def cookies(self, url=None):
        self.n += 1
        if self.n > self.miss_n:
            return [{"name": "dbclv", "value": "123:abc"}]
        return []


class _EnsureDoubanPwEnv:
    """隔离 _ensure_douban_interactive: 假 playwright + 临时档案路径 + TmpState。"""

    def __init__(self, cookies):
        self.cookies = list(cookies)

    def __enter__(self):
        self.td = tempfile.TemporaryDirectory()
        self.rec = []
        self.keep = _install_fake_playwright(self.rec, self.cookies,
                                             TOPIC_HTML, ROWS_HTML)
        self.orig_dir = ac.DOUBAN_PROFILE_DIR
        ac.DOUBAN_PROFILE_DIR = Path(self.td.name) / "douban-profile"
        self.state = TmpState()
        self.state.__enter__()
        return self

    def __exit__(self, *a):
        self.state.__exit__(*a)
        ac.DOUBAN_PROFILE_DIR = self.orig_dir
        _restore_fake_playwright(self.keep)
        self.td.cleanup()
        return False

    @property
    def launches(self):
        return [r[2] for r in self.rec if r[0] == "launch_persistent_context"]


def test_ensure_douban_interactive_flow():
    logged_cookies = [{"name": "dbclv", "value": "123:abc"},
                      {"name": "bid", "value": "xyz"}]
    # ① 档案已有 dbclv → headless 探测即 ok, 不弹窗(launch 全 headless)
    with _EnsureDoubanPwEnv(logged_cookies) as env:
        st = ea._ensure_douban_interactive(600)
        e = ac.load_auth_state().get("douban", {})
        ok_a = (st == "ok" and env.launches == [True]
                and e.get("mode") == "browser_profile" and e.get("ok") is True)
    # ② 无 dbclv → headless 试探关闭后弹可见窗 + 轮询等待: 成功→ok
    with _EnsureDoubanPwEnv([]) as env:
        waits = []
        orig_wait = _patch(ac, "wait_douban_login",
                           lambda ctx, t, i, _w=waits: _w.append(1) or True)
        try:
            st = ea._ensure_douban_interactive(600)
            e = ac.load_auth_state().get("douban", {})
        finally:
            _patch(ac, "wait_douban_login", orig_wait)
        ok_b = (st == "ok" and env.launches == [True, False]
                and len(waits) == 1 and e.get("mode") == "browser_profile"
                and env.rec[-1] == ("close",))
    # ③ 轮询超时 → timeout + reason=login_timeout(headed context 已关闭)
    with _EnsureDoubanPwEnv([]) as env:
        orig_wait = _patch(ac, "wait_douban_login",
                           lambda ctx, t, i: False)
        try:
            st = ea._ensure_douban_interactive(600)
            e = ac.load_auth_state().get("douban", {})
        finally:
            _patch(ac, "wait_douban_login", orig_wait)
        ok_c = (st == "timeout" and env.launches == [True, False]
                and e.get("ok") is False and e.get("reason") == "login_timeout"
                and env.rec[-1] == ("close",))
    check("ensure_douban 交互流程: 有dbclv秒ok / 无dbclv弹窗轮询成功ok / 超时timeout",
          ok_a and ok_b and ok_c)


def test_ensure_all_and_cli():
    orig_fns = (ea.ensure_xhs, ea.ensure_douyin, ea.ensure_douban)
    try:
        ea.ensure_xhs = lambda t, interactive=True: "ok"
        ea.ensure_douyin = lambda t, interactive=True: "ok"
        ea.ensure_douban = lambda t, interactive=True: "ok"
        rc0 = ea.check_all(600)
        ea.ensure_douyin = lambda t, interactive=True: "need_login"
        rc3 = ea.check_all(600)
        ea.ensure_xhs = lambda t, interactive=True: "env"
        rc2 = ea.check_all(600)

        ea.ensure_xhs = lambda t, interactive=True: "ok"
        argv = sys.argv
        sys.argv = ["ensure_auth.py", "--platform", "xhs", "--timeout", "61"]
        try:
            try:
                ea.main()
                code_ok = False
            except SystemExit as e:
                code_ok = e.code == 0
        finally:
            sys.argv = argv
    finally:
        ea.ensure_xhs, ea.ensure_douyin, ea.ensure_douban = orig_fns
    check("check_all 汇总退出码 0/3/2 + CLI 接线", rc0 == 0 and rc3 == 3
          and rc2 == 2 and code_ok)


# ---------------------------------------------------------------- collect_xhs
def test_collect_xhs_exit3_and_state():
    with TmpState():
        orig = _patch(ac, "xhs_probe_status",
                      lambda: ("auth_required", 77, "AUTH_REQUIRED"))
        argv = sys.argv
        sys.argv = ["collect_xhs.py", "--query", "test"]
        try:
            try:
                cx.main()
                ok1 = False
            except SystemExit as e:
                ok1 = e.code == 3
        finally:
            sys.argv = argv
            _patch(ac, "xhs_probe_status", orig)

        orig = _patch(ac, "xhs_probe_status", lambda: ("ok", 0, ""))
        orig_search = _patch(cx, "search_notes", lambda q, l: [])
        sys.argv = ["collect_xhs.py", "--query", "test"]
        try:
            try:
                cx.main()
                ok2 = False
            except SystemExit as e:
                ok2 = e.code == 2
            entry = ac.load_auth_state().get("xhs", {})
            ok2 = ok2 and entry.get("ok") is True
        finally:
            sys.argv = argv
            _patch(ac, "xhs_probe_status", orig)
            _patch(cx, "search_notes", orig_search)
    check("collect_xhs 需登录→exit 3；探测通过→登记状态", ok1 and ok2)


# ---------------------------------------------------------------- collect_douyin
def test_collect_douyin_hint_marks_state():
    with TmpState(), tempfile.TemporaryDirectory() as td:
        mc = Path(td) / "mc"
        (mc / "browser_data" / "dy_user_data_dir").mkdir(parents=True)
        try:
            cdn.login_state_hint(mc)  # 空缓存目录
            e1 = dict(ac.load_auth_state().get("douyin", {}))
            (mc / "browser_data" / "dy_user_data_dir" / "Cookies").write_text("x")
            cdn.login_state_hint(mc)  # 有缓存
            e2 = dict(ac.load_auth_state().get("douyin", {}))
            cdn.refresh_login_state_after_run(mc)
            e3 = dict(ac.load_auth_state().get("douyin", {}))
        finally:
            pass
        ok = (e1.get("ok") is False and e2.get("ok") is True
              and e2.get("cache") is True and e3.get("ok") is True)
    check("collect_douyin 缓存提示+跑后登记 auth_state", ok)


# ---------------------------------------------------------------- collect_douban（假 Playwright）
ROWS_HTML = (
    '<html><body><table>'
    '<tr><td class="td-subject">'
    '<a href="https://www.douban.com/group/topic/101/" title="天通苑租房两居转租">'
    '天通苑租房两居转租</a></td>'
    '<td class="td-time" title="2026-08-10 12:00">08-10</td>'
    '<td class="td-reply">30 回复</td></tr>'
    '<tr><td class="td-subject">'
    '<a href="https://www.douban.com/group/topic/102/" title="天通苑合租主卧">'
    '天通苑合租主卧</a></td>'
    '<td class="td-time" title="2026-08-11 09:00">08-11</td>'
    '<td class="td-reply">12 回复</td></tr>'
    '</table></body></html>'
)
TOPIC_HTML = (
    '<html><body><h1>天通苑租房两居转租</h1>'
    '<div class="topic-content">转租两居室, 5000/月, 押一付三</div>'
    '<span class="create-time">2026-08-10 12:00</span></body></html>'
)
SLIDER_HTML = '<html><body>滑块验证: 请拖动滑块完成验证</body></html>'
EMPTY_HTML = '<html><body></body></html>'


class FakeTtyStdin:
    def isatty(self):
        return True


class FakePage:
    """按 URL 分派假页面内容: 帖子页 topic_html, 其余(首页/搜索页) list_html。"""

    def __init__(self, ctx, topic_html, list_html):
        self._ctx, self.topic_html, self.list_html = ctx, topic_html, list_html
        self.url = ""

    def goto(self, url, timeout=None):
        self._ctx.rec.append(("goto", url))
        self.url = url

    def wait_for_load_state(self, *a, **k):
        return None

    def content(self):
        return self.topic_html if "/group/topic/" in self.url else self.list_html


class FakeContext:
    def __init__(self, rec, cookies, page):
        self.rec, self._cookies, self.pages = rec, cookies, [page]

    def add_cookies(self, cookies):
        self.rec.append(("add_cookies", len(cookies)))

    def cookies(self, url=None):
        return list(self._cookies)

    def close(self):
        self.rec.append(("close",))


class FakeChromium:
    def __init__(self, rec, cookies, topic_html, list_html):
        self.rec, self.cookies = rec, cookies
        self.topic_html, self.list_html = topic_html, list_html

    def launch_persistent_context(self, user_data_dir=None, headless=None, **kw):
        self.rec.append(("launch_persistent_context", user_data_dir, headless))
        ctx = FakeContext(self.rec, self.cookies, None)
        ctx.pages = [FakePage(ctx, self.topic_html, self.list_html)]
        return ctx


class FakePW:
    def __init__(self, rec, cookies, topic_html, list_html):
        self.chromium = FakeChromium(rec, cookies, topic_html, list_html)


class FakeSyncPW:
    def __init__(self, rec, cookies, topic_html, list_html):
        self.args = (rec, cookies, topic_html, list_html)

    def __enter__(self):
        return FakePW(*self.args)

    def __exit__(self, *a):
        return False


def _install_fake_playwright(rec, cookies, topic_html=TOPIC_HTML,
                             list_html=ROWS_HTML):
    mod = types.ModuleType("playwright")
    api = types.ModuleType("playwright.sync_api")
    api.sync_playwright = lambda: FakeSyncPW(rec, list(cookies),
                                             topic_html, list_html)
    mod.sync_api = api
    keep = (sys.modules.get("playwright"), sys.modules.get("playwright.sync_api"))
    sys.modules["playwright"] = mod
    sys.modules["playwright.sync_api"] = api
    return keep


def _restore_fake_playwright(keep):
    for key, val in zip(("playwright", "playwright.sync_api"), keep):
        if val is not None:
            sys.modules[key] = val
        else:
            sys.modules.pop(key, None)


class DoubanL2Env:
    """隔离跑 level2_playwright: 假 playwright + 临时 profile 路径 + 假 stdin。

    stdin_kind: "none"(sys.stdin=None) / "nontty"(isatty False) / "tty"(isatty True)
    list_html 换 EMPTY_HTML 即模拟静默试探失败(列表页被拦无帖子行)。
    """

    def __init__(self, profile_exists, stdin_kind, cookies=(),
                 cookie_env=None, topic_html=TOPIC_HTML, list_html=ROWS_HTML):
        self.profile_exists = profile_exists
        self.stdin_kind = stdin_kind
        self.cookies = list(cookies)
        self.cookie_env = cookie_env
        self.topic_html, self.list_html = topic_html, list_html

    def __enter__(self):
        self.td = tempfile.TemporaryDirectory()
        self.rec = []
        self.keep = _install_fake_playwright(self.rec, self.cookies,
                                             self.topic_html, self.list_html)
        self.orig_dir = cdb.DOUBAN_PROFILE_DIR
        cdb.DOUBAN_PROFILE_DIR = os.path.join(self.td.name, "douban-profile")
        if self.profile_exists:
            os.makedirs(cdb.DOUBAN_PROFILE_DIR)
            with open(os.path.join(cdb.DOUBAN_PROFILE_DIR, "Cookies"), "w") as fh:
                fh.write("x")
        self.old_env = os.environ.pop("DOUBAN_COOKIE", None)
        if self.cookie_env is not None:
            os.environ["DOUBAN_COOKIE"] = self.cookie_env
        self.old_stdin = sys.stdin
        sys.stdin = {"none": None, "nontty": FakeStdin(),
                     "tty": FakeTtyStdin()}[self.stdin_kind]
        self.orig_sleep = _patch(cdb, "rand_sleep", lambda: None)
        self.state = TmpState()
        self.state.__enter__()
        return self

    def __exit__(self, *a):
        self.state.__exit__(*a)
        _patch(cdb, "rand_sleep", self.orig_sleep)
        sys.stdin = self.old_stdin
        cdb.DOUBAN_PROFILE_DIR = self.orig_dir
        if self.old_env is not None:
            os.environ["DOUBAN_COOKIE"] = self.old_env
        _restore_fake_playwright(self.keep)
        self.td.cleanup()
        return False

    @property
    def launches(self):
        """每次 launch 的 headless 标志序列(顺序)。"""
        return [r[2] for r in self.rec if r[0] == "launch_persistent_context"]


def _catch_level2():
    """跑一次 level2_playwright; 返回 (needs_login_raised, results)。"""
    try:
        return None, cdb.level2_playwright("天通苑", 5, 10, ("租房",), None)
    except cdb.NeedsLogin:
        return True, None


def test_douban_l2_noninteractive_no_material():
    oks = []
    for kind in ("none", "nontty"):
        with DoubanL2Env(False, kind) as env:
            raised, _ = _catch_level2()
            oks.append(raised is True and env.rec == [])
    check("非交互(None/非tty)无档案无cookie → NeedsLogin(exit 3), 不启动浏览器",
          all(oks))


def test_douban_l2_silent_probe_matrix():
    cookies = [{"name": "dbclv", "value": "123456:abc"},
               {"name": "bid", "value": "xyz"}]
    # 档案+dbclv+列表页有行: 静默试探成功 → 无头直采, 不弹窗不等人工(两种 stdin)
    with DoubanL2Env(True, "nontty", cookies=cookies) as env:
        _, res = _catch_level2()
        ok_a = (len(res) == 2 and env.launches == [True]
                and env.rec[-1] == ("close",)
                and ac.load_auth_state().get("douban", {}).get("mode")
                == "browser_profile")
    with DoubanL2Env(True, "tty", cookies=cookies) as env:
        asked = []
        orig_ask = _patch(cdb, "ask", lambda p: asked.append(p) or "")
        try:
            _, res = _catch_level2()
        finally:
            _patch(cdb, "ask", orig_ask)
        ok_b = len(res) == 2 and env.launches == [True] and not asked
    # 试探失败(列表页被拦无帖子行): 非tty → NeedsLogin 且试探 context 已关闭
    with DoubanL2Env(True, "nontty", list_html=EMPTY_HTML) as env:
        raised, _ = _catch_level2()
        ok_c = (raised is True and env.launches == [True]
                and env.rec[-1] == ("close",))
    # 试探失败 + tty → 关试探 context 后弹可见窗口(第二次 launch headless=False),
    # 轮询等待登录(wait_douban_login, 不再 ask/input()); 等到→继续采集(列表空=0行)
    with DoubanL2Env(True, "tty", list_html=EMPTY_HTML) as env:
        asked, waits = [], []
        orig_ask = _patch(cdb, "ask", lambda p: asked.append(p) or "")
        orig_wait = _patch(ac, "wait_douban_login",
                           lambda ctx, t, i, _w=waits: _w.append(1) or True)
        try:
            _, res = _catch_level2()
        finally:
            _patch(cdb, "ask", orig_ask)
            _patch(ac, "wait_douban_login", orig_wait)
        ok_d = (res == [] and env.launches == [True, False]
                and len(waits) == 1 and not asked)
    # 试探失败 + tty + 轮询超时 → NeedsLogin(main exit 3), headed context 已关闭
    with DoubanL2Env(True, "tty", list_html=EMPTY_HTML) as env:
        orig_wait = _patch(ac, "wait_douban_login", lambda ctx, t, i: False)
        try:
            raised, _ = _catch_level2()
        finally:
            _patch(ac, "wait_douban_login", orig_wait)
        ok_e = (raised is True and env.launches == [True, False]
                and env.rec[-1] == ("close",))
    check("静默试探矩阵: 成功→无头直采不弹窗/不等人工; 失败→非tty NeedsLogin, "
          "tty 可见窗+轮询等待(成功继续/超时 NeedsLogin)",
          ok_a and ok_b and ok_c and ok_d and ok_e)


def test_douban_l2_headless_slider():
    with DoubanL2Env(True, "nontty", cookies=[{"name": "dbclv", "value": "x"}],
                     topic_html=SLIDER_HTML) as env:
        asked = []
        orig_ask = _patch(cdb, "ask", lambda p: asked.append(p) or "")
        try:
            _, res = _catch_level2()
        finally:
            _patch(cdb, "ask", orig_ask)
        reasons = [r for (_, _, r) in res]
        ok = (reasons == ["slider_in_headless", "slider_in_headless"]
              and not asked and env.launches == [True])
    check("无头模式遇滑块 → 记 slider_in_headless 不等人工(连续 2 次终止)", ok)


def test_douban_common_helpers():
    # non_interactive: None / 非tty / tty 三态
    orig_stdin = sys.stdin
    try:
        sys.stdin = None
        ok_a = ac.non_interactive() is True
        sys.stdin = FakeStdin()
        ok_b = ac.non_interactive() is True
        sys.stdin = FakeTtyStdin()
        ok_c = ac.non_interactive() is False
    finally:
        sys.stdin = orig_stdin
    # douban_logged_in 纯函数: dbcl2(现行)/dbclv(旧名) 非空/空值/缺失/非 dict 混入/None
    ok_d = (ac.douban_logged_in([{"name": "dbcl2", "value": "1:a"}])
            is True
            and ac.douban_logged_in([{"name": "dbclv", "value": "1:a"}])
            is True
            and ac.douban_logged_in([{"name": "dbclv", "value": ""}]) is False
            and ac.douban_logged_in([{"name": "bid", "value": "x"}]) is False
            and ac.douban_logged_in([None, {"name": "dbclv", "value": "v"}])
            is True
            and ac.douban_logged_in(None) is False)
    # douban_context_logged_in: cookies() 抛异常按未登录
    class BoomCtx:
        def cookies(self, url=None):
            raise RuntimeError("closed")
    ok_e = ac.douban_context_logged_in(BoomCtx()) is False
    check("non_interactive 三态 + douban_logged_in 纯函数 + context 异常兜底",
          ok_a and ok_b and ok_c and ok_d and ok_e)


def test_wait_douban_login():
    import time as _time
    orig_sleep = _time.sleep
    _time.sleep = lambda s: None  # 不真睡
    try:
        # 第 3 次轮询出现 dbclv → True
        ok_a = ac.wait_douban_login(FakeLoginContext(2), timeout=30,
                                    interval=5) is True
        # 一直无 dbclv → 超时 False
        ok_b = ac.wait_douban_login(FakeLoginContext(10 ** 9), timeout=0.001,
                                    interval=5) is False
        # 已登录 → 立即 True(timeout=0 也不误判)
        ok_c = ac.wait_douban_login(FakeLoginContext(0), timeout=0,
                                    interval=5) is True
    finally:
        _time.sleep = orig_sleep
    check("wait_douban_login: dbclv 出现→True / 一直无→超时False / 已登录立即True",
          ok_a and ok_b and ok_c)


def test_collect_douban_403_retry():
    calls, sleeps = [], []

    class Resp:
        def __init__(self, code):
            self.status_code = code

    def fake_safe_get(session, url, timeout=15):
        calls.append(url)
        return Resp(403 if len(calls) == 1 else 200)

    orig_get = _patch(cdb, "safe_get", fake_safe_get)
    orig_sleep = _patch(cdb, "rand_sleep", lambda: sleeps.append(1))
    try:
        r1 = cdb.get_with_retry(object(), "https://x/1")
        # 403 → 403: 仍重试一次后维持被拦
        calls.clear()

        def fake_403(session, url, timeout=15):
            calls.append(url)
            return Resp(403)
        _patch(cdb, "safe_get", fake_403)
        r2 = cdb.get_with_retry(object(), "https://x/2")
        # 200: 不重试
        calls.clear()
        _patch(cdb, "safe_get",
               lambda s, u, timeout=15: calls.append(u) or Resp(200))
        r3 = cdb.get_with_retry(object(), "https://x/3")
    finally:
        _patch(cdb, "safe_get", orig_get)
        _patch(cdb, "rand_sleep", orig_sleep)
    # r1: 403→重试→200(2 次请求 1 次等待); r2: 403→仍 403(2 次请求);
    # r3: 200 直接返回(1 次请求, 无重试)
    ok = (r1.status_code == 200 and len(calls) == 1
          and r2.status_code == 403 and len(sleeps) == 2
          and r3.status_code == 200)
    check("一级 HTTP 403 → 随机等待后重试 1 次(403→200 恢复 / 仍403维持被拦)",
          ok)


def test_collect_douban_main_needs_login_exit3():
    with TmpState(), tempfile.TemporaryDirectory() as td:
        row = {"topic_id": "1", "url": "https://www.douban.com/group/topic/1/",
               "title": "天通苑租房", "published_at": "", "reply_count": 3,
               "group_id": "g", "group_name": "g"}

        class Resp403:
            status_code = 403
            text = ""
            url = "https://www.douban.com/group/topic/1/"

        appended = []

        def fake_l2(*a, **k):
            raise cdb.NeedsLogin()

        orig_l1 = _patch(cdb, "level1_search", lambda *a, **kw: ([row], 0, True, 1))
        orig_get = _patch(cdb, "safe_get",
                          lambda s, u, timeout=15: Resp403())
        orig_sleep = _patch(cdb, "rand_sleep", lambda: None)
        orig_append = _patch(cdb, "append_jsonl",
                             lambda p, r: appended.append(r))
        orig_l2 = _patch(cdb, "level2_playwright", fake_l2)
        argv = sys.argv
        sys.argv = ["collect_douban.py", "--query", "天通苑", "--out-dir", td]
        try:
            try:
                cdb.main()
                code = None
            except SystemExit as e:
                code = e.code
        finally:
            sys.argv = argv
            _patch(cdb, "level2_playwright", orig_l2)
            _patch(cdb, "append_jsonl", orig_append)
            _patch(cdb, "rand_sleep", orig_sleep)
            _patch(cdb, "safe_get", orig_get)
            _patch(cdb, "level1_search", orig_l1)
        ok = code == 3 and len(appended) == 1
    check("collect_douban 需登录 → main exit 3(级别1记录已先落盘)", ok)


def test_collect_douban_word_fallback_first_token():
    """组合词 0 命中 → 自动降级首词重搜一次且成功(仅 word 意图, 全离线 stub)。"""
    calls = []
    row = {"topic_id": "9", "url": "https://www.douban.com/group/topic/9/",
           "title": "回龙观测试帖", "published_at": "", "reply_count": 5,
           "group_id": "g", "group_name": "g"}

    def fake_level1_search(session, query, limit, rental_words, intent="word"):
        calls.append(query)
        if len(calls) == 1:
            return ([], 5, False, 1)   # 首轮组合词 0 命中
        return ([dict(row)], 0, False, 1)  # 降级首词命中

    class Resp200:
        status_code = 200
        text = TOPIC_HTML
        url = row["url"]

    orig_l1 = _patch(cdb, "level1_search", fake_level1_search)
    orig_get = _patch(cdb, "safe_get", lambda s, u, timeout=15: Resp200())
    orig_sleep = _patch(cdb, "rand_sleep", lambda: None)
    argv = sys.argv
    with tempfile.TemporaryDirectory() as td:
        sys.argv = ["collect_douban.py", "--query", "回龙观 测试体验",
                    "--out-dir", td]
        try:
            try:
                cdb.main()
                code = None
            except SystemExit as e:
                code = e.code
        finally:
            sys.argv = argv
            _patch(cdb, "rand_sleep", orig_sleep)
            _patch(cdb, "safe_get", orig_get)
            _patch(cdb, "level1_search", orig_l1)
        files = [f for f in os.listdir(td) if f.endswith(".jsonl")]
        nlines = 0
        if files:
            with open(os.path.join(td, files[0]), encoding="utf-8") as fh:
                nlines = sum(1 for ln in fh if ln.strip())
    ok = (code == 0 and len(calls) == 2 and calls[0] == "回龙观 测试体验"
          and calls[1] == "回龙观" and nlines == 1)
    check("组合词 0 命中 → 降级首词重搜成功(仅一次, word 意图, 测试离线)", ok)


def _l2_search_q(url):
    """从 goto 记录的搜索 URL 里解出已解码的 q 参数。"""
    from urllib.parse import unquote
    import re
    m = re.search(r"[?&]q=([^&]*)", url)
    return unquote(m.group(1)) if m else None


def test_douban_l2_word_fallback_first_token():
    """level2 组合词浏览器 0 命中 → 降级首词重试(重 goto 且过滤词换首词, 测试离线)。

    列表页标题只含"回龙观"不含"测试体验": 组合词 AND 过滤=0 命中触发降级,
    首词"回龙观"过滤命中 1 行(若过滤词没换, 降级后仍是 0 → 用 res 长度断言)。
    """
    rows = (
        '<html><body><table>'
        '<tr><td class="td-subject">'
        '<a href="https://www.douban.com/group/topic/201/" title="回龙观测试合租真实帖">'
        '回龙观测试合租真实帖</a></td>'
        '<td class="td-time" title="2026-08-10 12:00">08-10</td>'
        '<td class="td-reply">8 回复</td></tr>'
        '</table></body></html>'
    )
    with DoubanL2Env(True, "nontty", cookies=[{"name": "dbcl2", "value": "x"}],
                     list_html=rows) as env:
        _, res = (None, cdb.level2_playwright("回龙观 测试体验", 5, 10, (), None))
        searches = [_l2_search_q(r[1]) for r in env.rec
                    if r[0] == "goto" and "/group/search?" in r[1]]
    ok = (searches == ["回龙观 测试体验"] * 2 + ["回龙观"] and len(res) == 1)
    check("level2 组合词 0 命中 → 降级首词重 goto 且过滤词换首词(测试离线)", ok)


def test_douban_l2_word_fallback_skips_city_word():
    """level2 降级首词跳过城市词: "北京 测试小区 怎么样" → 降级词=测试小区(测试离线)。"""
    rows = (
        '<html><body><table>'
        '<tr><td class="td-subject">'
        '<a href="https://www.douban.com/group/topic/202/" title="测试小区测评真实帖">'
        '测试小区测评真实帖</a></td>'
        '<td class="td-time" title="2026-08-11 09:00">08-11</td>'
        '<td class="td-reply">3 回复</td></tr>'
        '</table></body></html>'
    )
    with DoubanL2Env(True, "nontty", cookies=[{"name": "dbcl2", "value": "x"}],
                     list_html=rows) as env:
        _, res = (None, cdb.level2_playwright(
            "北京 测试小区 怎么样", 5, 10, (), None))
        searches = [_l2_search_q(r[1]) for r in env.rec
                    if r[0] == "goto" and "/group/search?" in r[1]]
    ok = (searches == ["北京 测试小区 怎么样"] * 2 + ["测试小区"]
          and len(res) == 1)
    check("level2 城市前缀降级 → 首词取'测试小区'而非城市词'北京'(测试离线)", ok)


def main():
    print("== auth_common ==")
    test_auth_state_roundtrip()
    test_xhs_probe_classify()
    test_run_xhs_probe_missing_opencli()
    test_douyin_cache_probe()
    test_douban_probe_fake_http()
    print("== ensure_auth ==")
    test_ensure_xhs_ok()
    test_ensure_xhs_poll_until_success()
    test_ensure_xhs_timeout()
    test_ensure_xhs_browser_missing()
    test_ensure_douyin()
    test_ensure_douban()
    test_ensure_douban_interactive_flow()
    test_ensure_all_and_cli()
    print("== collect_* 联动 ==")
    test_collect_xhs_exit3_and_state()
    test_collect_douyin_hint_marks_state()
    test_douban_common_helpers()
    test_wait_douban_login()
    test_douban_l2_noninteractive_no_material()
    test_douban_l2_silent_probe_matrix()
    test_douban_l2_headless_slider()
    test_douban_l2_word_fallback_first_token()
    test_douban_l2_word_fallback_skips_city_word()
    test_collect_douban_403_retry()
    test_collect_douban_main_needs_login_exit3()
    test_collect_douban_word_fallback_first_token()
    print()
    print("离线自测: %d 通过 / %d 失败" % (PASS, FAIL))
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
