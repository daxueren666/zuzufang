#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rent-assist 门面页本地服务：输入需求 → 自动拉终端跑 Claude → 报告自动打开。

端口 8770（默认绑 0.0.0.0，手机同 WiFi 可访问；设 RENT_ASSIST_BIND=127.0.0.1 可仅本机）。路由：
  GET  /                    门面页（templates/landing.html）
  HEAD/GET /ping            探测（页面上按钮据此前后切换 直连/复制 模式）
  POST /run   {text}        写 prompt 临时文件并拉起可见终端: claude "<prompt>"
  GET  /status?since=ms     data/reports 里 mtime > since 的最新报告文件名（没有则 null）
  GET  /reports/<name>      直接打开报告
启动即 webbrowser.open 门面页。用 RENT_ASSIST_LAUNCH_CMD 环境变量可替换拉起命令（测试用）。
"""
import html as _html
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote, quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth_common as ac  # noqa: E402

PORT = int(os.environ.get("RENT_ASSIST_PORT", "8770"))
BIND = os.environ.get("RENT_ASSIST_BIND", "0.0.0.0")   # 0.0.0.0=手机同WiFi可访问；127.0.0.1=仅本机

# 演示模式：RENT_ASSIST_DEMO=1 时不拉 Claude，任何输入都直接弹出这份预渲染报告
DEMO = bool(os.environ.get("RENT_ASSIST_DEMO"))
DEMO_REPORT = "幸福小区_演示.html"

# 直达模式：RENT_ASSIST_INBOX=1 时命令写入 inbox 队列，由正在运行的 Claude 会话接收处理
# （不再另开终端窗口；会话侧用 Monitor 盯 queue.jsonl，并定期 touch heartbeat）
INBOX = os.environ.get("RENT_ASSIST_INBOX") == "1"
INBOX_DIR = ac.data_dir("inbox")
QUEUE_FILE = INBOX_DIR / "queue.jsonl"
HEARTBEAT_FILE = INBOX_DIR / "heartbeat"


def _reports_index_html():
    """历史报告目录页（蓝白风、内联 CSS 单文件自足）。文件名经 html.escape + URL quote 防 XSS。"""
    items = []
    try:
        files = sorted(REPORTS_DIR.glob("*.html"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:50]
        items = [(f.name, f.stat().st_mtime) for f in files]
    except OSError:
        pass
    if items:
        rows = "".join(
            '<li><a href="/reports/%s">%s</a><span class="t">%s</span></li>' % (
                quote(p[0]), _html.escape(p[0][:-5] if p[0].endswith(".html") else p[0]),
                time.strftime("%Y-%m-%d %H:%M", time.localtime(p[1])))
            for p in items)
        body = '<ul class="list">%s</ul>' % rows
    else:
        body = '<p class="empty">暂无报告</p>'
    return """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,"><title>历史报告 · 租租房</title><style>
body{font-family:"Microsoft YaHei",sans-serif;background:#edf3fb;color:#1a4f9e;margin:0;padding:24px;}
.card{max-width:640px;margin:0 auto;background:#fff;border-radius:14px;padding:20px 24px;
box-shadow:0 4px 16px rgba(31,95,191,.12);}
h1{font-size:20px;margin:0 0 16px;color:#1f5fbf;}
.list{list-style:none;margin:0;padding:0;}
.list li{display:flex;justify-content:space-between;align-items:center;gap:12px;
padding:11px 4px;border-bottom:1px solid #dce9f9;}
.list li:last-child{border-bottom:none;}
.list a{color:#1f5fbf;text-decoration:none;font-size:15px;word-break:break-all;}
.list a:hover{text-decoration:underline;}
.list .t{color:#8aa3c6;font-size:13px;white-space:nowrap;}
.empty{color:#8aa3c6;text-align:center;padding:24px 0;}
</style></head><body><div class="card"><h1>历史报告</h1>%s</div></body></html>""" % body


def demo_report_name():
    f = REPORTS_DIR / DEMO_REPORT
    if f.is_file():
        return DEMO_REPORT
    newest = max(REPORTS_DIR.glob("*.html"), key=lambda p: p.stat().st_mtime, default=None)
    return newest.name if newest else None
LANDING = Path(__file__).resolve().parents[1] / "templates" / "landing.html"
REPORTS_DIR = ac.data_dir("reports")
WORK_DIR = Path(os.environ.get("RENT_ASSIST_WORK_DIR")
                or (Path(r"E:\租房") if Path(r"E:\租房").exists() else Path.cwd()))
# claude 在项目目录里跑，权限设置才生效；他机无 E:\租房 时用启动目录


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[%s] %s" % (time.strftime("%H:%M:%S"), fmt % args))

    # ----------------------------------------------------------- helpers
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    # ----------------------------------------------------------- GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/ping":
            alive = False
            try:
                alive = time.time() - HEARTBEAT_FILE.stat().st_mtime < 30
            except OSError:
                pass
            self._send(200, json.dumps({"ok": True, "alive": alive}).encode("utf-8"))
            return
        if u.path == "/":
            self._send(200, LANDING.read_bytes(), "text/html; charset=utf-8")
            return
        if u.path == "/status":
            if DEMO:
                time.sleep(1.5)   # 稍作停顿更像真实跑了一小会儿
                self._send(200, json.dumps({"report": demo_report_name()},
                                            ensure_ascii=False).encode("utf-8"))
                return
            since = float((parse_qs(u.query).get("since") or ["0"])[0]) / 1000.0
            newest, mtime = None, 0
            try:
                for f in REPORTS_DIR.glob("*.html"):
                    m = f.stat().st_mtime
                    if m > since and m > mtime:
                        newest, mtime = f.name, m
            except OSError:
                pass
            self._send(200, json.dumps({"report": newest}).encode("utf-8"))
            return
        if u.path.rstrip("/") == "/reports":
            self._send(200, _reports_index_html().encode("utf-8"),
                       "text/html; charset=utf-8")
            return
        if u.path.startswith("/reports/"):
            name = Path(unquote(u.path[len("/reports/"):])).name  # 防目录穿越
            f = REPORTS_DIR / name
            if f.is_file():
                self._send(200, f.read_bytes(), "text/html; charset=utf-8")
                return
        self._send(404, b'{"error":"not found"}')

    # ----------------------------------------------------------- POST
    def do_POST(self):
        if urlparse(self.path).path != "/run":
            self._send(404, b'{"error":"not found"}')
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            text = str(data.get("text") or "").strip()[:500]
        except (ValueError, UnicodeDecodeError):
            text = ""
        if not text:
            self._send(400, b'{"ok":false,"error":"empty text"}')
            return
        if DEMO:
            self._send(200, json.dumps({"ok": True, "demo": True}).encode("utf-8"))
            return
        if INBOX:
            # 直达模式：写入队列，由正在运行的 Claude 会话（Monitor 盯队列）接收处理
            try:
                INBOX_DIR.mkdir(parents=True, exist_ok=True)
                with open(QUEUE_FILE, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                         "text": text}, ensure_ascii=False) + "\n")
                self._send(200, json.dumps({"ok": True, "inbox": True}).encode("utf-8"))
            except OSError as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))
            return
        prompt = "请用 rent-assist skill 处理：" + text
        try:
            pf = Path(tempfile.gettempdir()) / ("rent_ask_%d.txt" % time.time())
            pf.write_text(prompt, encoding="utf-8")
            if os.environ.get("RENT_ASSIST_LAUNCH_CMD"):
                cmd = ["cmd", "/k", os.environ["RENT_ASSIST_LAUNCH_CMD"]]
            else:
                # 终端里: claude "<prompt全文>"（经临时文件取，避开引号转义）
                ps = "claude (Get-Content -Raw '%s')" % str(pf).replace("'", "''")
                cmd = ["powershell", "-NoExit", "-Command", ps]
            subprocess.Popen(cmd, cwd=str(WORK_DIR))
            self._send(200, json.dumps({"ok": True}).encode("utf-8"))
        except OSError as e:
            self._send(500, json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))


def _lan_ips():
    """本机局域网 IPv4（UDP connect 8.8.8.8 探测，不实际发包）。"""
    ips = set()
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        import socket
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127.") and ":" not in ip:
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


def main():
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    url = "http://127.0.0.1:%d/" % PORT
    if not os.environ.get("RENT_ASSIST_NO_BROWSER"):
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    print("rent-assist 门面页已启动（关闭本窗口即停止）")
    print("  电脑访问: %s" % url)
    if INBOX:
        print("  命令直达模式：手机/页面提交的问题将送入正在运行的 Claude 会话处理")
        print("  （需电脑端 Claude 会话已开启接收；否则页面会显示未连接提示）")
    if BIND == "0.0.0.0":
        for ip in _lan_ips():
            print("  手机访问(需同一WiFi): http://%s:%d/" % (ip, PORT))
        print("  注: 首次启动若 Windows 防火墙弹窗，请勾选\"专用网络\"并允许")
    try:
        ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
