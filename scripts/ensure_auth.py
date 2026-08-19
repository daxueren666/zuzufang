#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rent-assist: 三平台登录态统一入口（扫码等待 + cookie 持久化 + 状态登记）。

用法:
    python ensure_auth.py --platform xhs            # 探测→未登录则开 Chrome 登录页→轮询等扫码完成
    python ensure_auth.py --platform douban         # 完整登录流程: 档案探测→弹窗登录→轮询等待
    python ensure_auth.py --platform douyin         # MediaCrawler 登录缓存探测
    python ensure_auth.py --platform all            # 三平台只检查不引导，打印三行状态表
    python ensure_auth.py --platform xhs --timeout 600

小红书流程（解决"扫码一闪而过"痛点）:
    探测失败(77) → 自动用 Chrome 打开小红书主站登录页 → 终端打印操作指引 →
    每 20s 自动探测一次，直到扫码成功（打印"登录成功"）或超时，绝不秒退。

豆瓣流程（同样"绝不秒退"）:
    先 headless 快速查 douban-profile 档案 cookie（context.cookies() 判 dbcl2，
    HttpOnly 页面 JS 看不到）；已登录→直接 ok；未登录→弹可见窗口开豆瓣首页，
    每 5s 轮询一次 dbcl2，最长 180s，成功登记 auth_state；超时打印指引非零退出。

退出码: 0 = 所查平台登录态就绪; 2 = 环境问题（opencli 未装/playwright 未装/网络异常）;
        3 = 需要登录（未登录/等待扫码超时/Chrome 未开/无抖音缓存/豆瓣登录超时）。
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth_common as ac

XHS_LOGIN_URL = "https://www.xiaohongshu.com"
POLL_INTERVAL = 20  # 秒（扫码等待轮询间隔；离线测试可调小）

STATUS_TEXT = {"ok": "就绪", "need_login": "需登录", "timeout": "登录超时",
               "env": "环境缺失"}
PLATFORM_TEXT = {"xhs": "小红书", "douyin": "抖音", "douban": "豆瓣"}


def open_chrome(url):
    """Windows 下经 cmd start 打开 Chrome（独立窗口，不阻塞本进程）。"""
    try:
        subprocess.Popen(["cmd", "/c", "start", "chrome", url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError as e:
        print("[xhs] 无法自动打开 Chrome: %s" % e, file=sys.stderr)
        return False


def ensure_xhs(timeout, interactive=True):
    """小红书登录保障。返回 'ok' | 'need_login' | 'timeout' | 'env'。"""
    status, rc, blob = ac.xhs_probe_status()
    if status == "ok":
        print("[xhs] 登录态有效（真实搜索探测通过）")
        ac.update_auth_state("xhs", True, probe="opencli_search")
        return "ok"
    if status == "opencli_missing":
        print("[xhs] 未找到 opencli 命令，请先运行 check_deps.py 按指引安装"
              " Agent-Reach/OpenCLI。", file=sys.stderr)
        return "env"
    if status == "browser_missing":
        print("[xhs] opencli 未连接到 Chrome（退出码 69）。请先打开 Chrome 浏览器"
              "（确认扩展已启用），然后重新运行:", file=sys.stderr)
        print("[xhs]   python ensure_auth.py --platform xhs", file=sys.stderr)
        ac.update_auth_state("xhs", False, reason="browser_not_connected")
        return "need_login"
    if status == "auth_required":
        if not interactive:
            print("[xhs] 未登录（主站搜索被登录墙拦截）。运行下面命令完成扫码登录"
                  "（会等待扫码完成，不会一闪而过）:", file=sys.stderr)
            print("[xhs]   python ensure_auth.py --platform xhs", file=sys.stderr)
            ac.update_auth_state("xhs", False, reason="auth_required")
            return "need_login"
        print("[xhs] 小红书未登录（主站搜索 AUTH_REQUIRED），开始登录引导...")
        print()
        open_chrome(XHS_LOGIN_URL)
        print("[xhs] 已尝试用 Chrome 打开小红书主站（若未弹出，请手动访问 %s）。"
              % XHS_LOGIN_URL)
        print("[xhs] 请在 Chrome 里操作:")
        print("[xhs]   1. 点左上角『登录』")
        print("[xhs]   2. 用小红书 App 扫码确认")
        print("[xhs]   3. 完成后本命令自动继续，每 %d 秒探测一次，最长等待 %d 秒"
              % (POLL_INTERVAL, timeout))
        deadline = time.monotonic() + timeout
        attempt = 0
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL)
            attempt += 1
            st2, rc2, blob2 = ac.xhs_probe_status()
            if st2 == "ok":
                print("[xhs] 登录成功（第 %d 次探测通过），登录态已登记。" % attempt)
                ac.update_auth_state("xhs", True, probe="opencli_search")
                return "ok"
            print("[xhs] 第 %d 次探测: 尚未成功（%s），继续等待扫码..."
                  % (attempt, st2))
        print("[xhs] 等待超时（%d 秒）。若已在 App 扫码仍超时，请确认 Chrome "
              "扩展已启用后重新运行本命令。" % timeout, file=sys.stderr)
        ac.update_auth_state("xhs", False, reason="login_timeout")
        return "timeout"
    # unknown
    print("[xhs] 探测异常（退出码 %s）: %s" % (rc, blob[:200]), file=sys.stderr)
    print("[xhs] 请确认 Chrome 已打开、扩展已启用后重试。", file=sys.stderr)
    return "env"


def ensure_douyin(timeout, interactive=True):
    """抖音登录缓存检查。返回 'ok' | 'need_login'。"""
    if ac.douyin_cache_exists():
        print("[douyin] 已有登录缓存（%s），采集将复用登录态。" % ac.DOUYIN_DATA_DIR)
        print("[douyin] 如终测发现缓存失效，collect_douyin.py 会自动弹浏览器扫码，"
              "一次长期有效。")
        ac.update_auth_state("douyin", True, cache=True)
        return "ok"
    print("[douyin] 未检测到登录缓存: 首跑 collect_douyin.py 会弹浏览器扫码"
          "（用抖音 App 扫），一次长期有效，之后免扫码。", file=sys.stderr)
    ac.update_auth_state("douyin", False, cache=False)
    return "need_login"


def _ensure_douban_interactive(timeout):
    """豆瓣完整登录流程（前台运行）。返回 'ok' | 'timeout' | 'env'。

    ① headless 快速查 douban-profile 档案 cookie（context.cookies() 判 dbcl2，
    HttpOnly 页面 JS 看不到）：已登录 → 登记 ok，不弹窗；
    ② 未登录 → 弹可见窗口（persistent context, headed）开豆瓣首页，终端打印
    指引，每 5s 轮询 dbcl2，最长 180s，登录完成自动继续并登记；超时给指引。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[douban] 未安装 playwright, 无法完成登录流程。请先执行:\n"
              "    pip install playwright && playwright install chromium",
              file=sys.stderr)
        return "env"
    ac.ensure_playwright_browsers_path()
    profile = str(ac.DOUBAN_PROFILE_DIR)
    wait_s = min(max(60, timeout), ac.DOUBAN_LOGIN_WAIT)
    with sync_playwright() as p:
        # ① headless 快速确认现有档案（不弹窗）
        context = None
        try:
            context = ac.launch_douban_profile(p, headless=True)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(ac.DOUBAN_HOME_URL, timeout=20000)
            except Exception:
                pass  # 页面被拦/超时也要看已落盘 cookie
            if ac.douban_context_logged_in(context):
                print("[douban] 登录态有效（档案 %s 检测到 dbcl2），无需登录。"
                      % profile)
                ac.update_auth_state("douban", True, mode="browser_profile",
                                     profile=profile)
                return "ok"
        except Exception as exc:
            print("[douban] 浏览器探测失败: %s" % exc, file=sys.stderr)
            return "env"
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
        # ② 弹可见窗口等待人工登录（绝不秒退）
        print("[douban] 档案未检测到登录 cookie(dbcl2)，即将打开浏览器登录页"
              "（最长等待 %d 秒，每 %d 秒自动检测一次）..."
              % (wait_s, ac.DOUBAN_POLL_INTERVAL))
        try:
            context = ac.launch_douban_profile(p, headless=False)
        except Exception as exc:
            print("[douban] 浏览器启动失败: %s\n    请先执行: "
                  "playwright install chromium" % exc, file=sys.stderr)
            return "env"
        try:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(ac.DOUBAN_HOME_URL, timeout=60000)
            except Exception as exc:
                print("[douban] 打开豆瓣页面失败: %s，请检查网络后重试。" % exc,
                      file=sys.stderr)
                return "env"
            print("[douban] 请在弹出的浏览器窗口内登录豆瓣（右上角『登录』，"
                  "扫码或账号密码；如遇滑块请完成验证）。")
            print("[douban] 完成后本命令自动继续，无需任何按键。")
            if ac.wait_douban_login(context, wait_s, ac.DOUBAN_POLL_INTERVAL):
                print("[douban] 登录成功（检测到 dbcl2），已持久化到 %s，"
                      "后续免登录。" % profile)
                ac.update_auth_state("douban", True, mode="browser_profile",
                                     profile=profile)
                return "ok"
            print("[douban] 等待超时（%d 秒未见登录）。请重新运行: "
                  "python scripts/ensure_auth.py --platform douban 完成登录；"
                  "或设置 DOUBAN_COOKIE 环境变量。" % wait_s, file=sys.stderr)
            ac.update_auth_state("douban", False, reason="login_timeout")
            return "timeout"
        finally:
            if context is not None:
                try:
                    context.close()  # cookie 落盘必须 close
                except Exception:
                    pass


def _ensure_douban_probe():
    """豆瓣匿名可用性探测（非交互/check_all 用，不弹窗）。
    返回 'ok' | 'need_login' | 'env'。"""
    code = ac.probe_douban_http()
    if code == 200:
        print("[douban] 匿名可用（搜索探测 HTTP 200），列表级采集无需登录。")
        ac.update_auth_state("douban", True, mode="anonymous")
        if ac.DOUBAN_PROFILE_DIR.is_dir():
            print("[douban] 已检测到浏览器持久化登录档案（%s），帖子详情级可复用"
                  "登录态。" % ac.DOUBAN_PROFILE_DIR)
        return "ok"
    if code is None:
        print("[douban] 网络探测失败（请求异常），请检查网络后重试。", file=sys.stderr)
        return "env"
    if code == 403:
        print("[douban] 搜索被拦（HTTP 403），需登录态。两种方式:", file=sys.stderr)
        print("[douban]   1. 前台运行 python scripts/ensure_auth.py --platform "
              "douban（弹浏览器登录, 轮询等待, 绝不秒退）;", file=sys.stderr)
        print("[douban]   2. 设置 DOUBAN_COOKIE 环境变量（'bid=xx; ...' 或 JSON "
              "数组格式）后运行 collect_douban.py。", file=sys.stderr)
        ac.update_auth_state("douban", False, reason="http_403")
        return "need_login"
    print("[douban] 探测返回 HTTP %s（非 200/403），按需登录处理，可稍后重试。"
          % code, file=sys.stderr)
    ac.update_auth_state("douban", False, reason="http_%s" % code)
    return "need_login"


def ensure_douban(timeout, interactive=True):
    """豆瓣登录保障入口。返回 'ok' | 'need_login' | 'timeout' | 'env'。

    交互模式（前台运行）= 完整登录流程 _ensure_douban_interactive；
    非交互（check_all）= 只做匿名 HTTP 探测 + 指引，不弹窗。
    """
    if interactive:
        return _ensure_douban_interactive(timeout)
    return _ensure_douban_probe()


def check_all(timeout):
    """三平台依次检查（不引导登录），汇总打印三行状态表。返回退出码。"""
    results = []
    for name in ("xhs", "douyin", "douban"):
        print("-" * 60)
        fn = globals()["ensure_%s" % name]
        st = fn(timeout, interactive=False)
        results.append((name, st))
    print("-" * 60)
    print("三平台登录态汇总（明细: %s）:" % ac.AUTH_STATE_PATH)
    for name, st in results:
        print("  %-8s %-8s %s" % (PLATFORM_TEXT[name], st, STATUS_TEXT.get(st, st)))
    if any(st == "env" for _, st in results):
        return 2
    if any(st != "ok" for _, st in results):
        return 3
    return 0


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="rent-assist 三平台登录态统一管理（探测/扫码等待/状态登记）")
    ap.add_argument("--platform", required=True,
                    choices=["xhs", "douyin", "douban", "all"],
                    help="平台；all = 依次检查并汇总打印三行状态表（不引导登录）")
    ap.add_argument("--timeout", type=int, default=600,
                    help="等待扫码完成的超时秒数（默认 600）")
    args = ap.parse_args()

    if args.platform == "all":
        sys.exit(check_all(max(60, args.timeout)))

    fn = {"xhs": ensure_xhs, "douyin": ensure_douyin, "douban": ensure_douban}[
        args.platform]
    status = fn(max(60, args.timeout), interactive=True)
    sys.exit({"ok": 0, "env": 2}.get(status, 3))


if __name__ == "__main__":
    main()
