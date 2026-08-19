#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rent-assist: 范围外说明报告生成器（P13 最小修复）。

背景: inbox 直达模式下手机提交了非租房问题（如"测试"），会话判定不出
正式报告，但门面页 serve_home 的 /status 只盯 data/reports/ 目录等新报告，
没有产物就永远"等待中"，用户死等无反馈。

修法: 会话跑本脚本，在 data/reports/ 下落一份轻量单文件 HTML 说明页，
复用现有"新报告自动弹出"通道，手机端自然弹出解释页。

用法:
  python out_of_scope.py --text "<用户原话>" [--note "<补充说明>"]

产物: auth_common.data_dir("reports")/范围外说明_YYYYMMDD_HHMMSS.html
（时间戳防覆盖），stdout 打印产物绝对路径一行供会话引用。
"""

import argparse
import html
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth_common as ac  # noqa: E402

DEFAULT_NOTE = ("本助手只处理租房相关问题（小区口碑/找房/选址/租房咨询）。"
                "请重新提交一个租房问题。")

PAGE_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>范围外说明 · 租房助手</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
    background: #edf3fb; color: #1f3a5f; min-height: 100vh;
    display: flex; align-items: center; justify-content: center; padding: 24px;
  }}
  .card {{
    background: #fff; border-radius: 16px; max-width: 375px; width: 100%;
    padding: 28px 22px; box-shadow: 0 4px 16px rgba(31, 95, 191, .10);
    border-top: 4px solid #1f5fbf; text-align: center;
  }}
  .badge {{
    display: inline-block; background: #edf3fb; color: #1f5fbf;
    border-radius: 999px; font-size: 12px; padding: 4px 12px; margin-bottom: 16px;
  }}
  h1 {{ font-size: 21px; color: #1f5fbf; margin-bottom: 16px; line-height: 1.4; }}
  .quote {{
    background: #f5f8fc; border-left: 3px solid #1f5fbf; border-radius: 8px;
    padding: 10px 12px; font-size: 15px; color: #33475e; margin-bottom: 16px;
    text-align: left; word-break: break-all;
  }}
  .note {{ font-size: 14px; color: #4a5d75; line-height: 1.7; margin-bottom: 18px; }}
  .time {{ font-size: 12px; color: #8aa0bd; border-top: 1px solid #e4ecf6;
    padding-top: 12px; }}
</style>
</head>
<body>
  <div class="card">
    <span class="badge">租房助手 · 范围外说明</span>
    <h1>这条提问不在租房助手范围内</h1>
    <div class="quote">__TEXT__</div>
    <p class="note">__NOTE__</p>
    <p class="time">__TIME__</p>
  </div>
</body>
</html>
"""


def build_page(text: str, note: str, now_str: str) -> str:
    return (PAGE_TMPL
            .replace("__TEXT__", html.escape(text))
            .replace("__NOTE__", html.escape(note))
            .replace("__TIME__", html.escape(now_str)))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="生成范围外说明报告（data/reports/，供门面页自动弹出）")
    ap.add_argument("--text", required=True, help="用户原话")
    ap.add_argument("--note", default=None,
                    help="补充说明（缺省用默认范围提示文案）")
    args = ap.parse_args()

    now = datetime.now()
    out = ac.data_dir("reports") / ("范围外说明_%s.html" % now.strftime("%Y%m%d_%H%M%S"))
    page = build_page(args.text, args.note or DEFAULT_NOTE,
                      now.strftime("%Y-%m-%d %H:%M:%S"))
    out.write_text(page, encoding="utf-8")
    print(out.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
