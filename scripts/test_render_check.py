#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""报告模板数据完整性检查（改版防护网，基线快照对比）。

不变量：模板渲染后"未出现的标量字段"集合，不许比基线快照多出新缺失——
基线里已有的缺失是模板本来就不展示的字段（如 poi.address），仅提示不判罚；
新出现的缺失（当前缺失 ⊄ 基线缺失）= 改版丢字段，exit 1 并逐项列出。

基线: scripts/render_baseline.json（git 跟踪；首次运行自动生成，结构为
{updated_at, templates: {模板名: {用例名: [缺失项...]}}}）。有意变更模板/用例
后跑 `--update-baseline` 刷新基线再提交。

用法:
    python test_render_check.py [模板名 ...]   # 默认 report.html.j2
    python test_render_check.py --update-baseline
"""
import argparse
import html as html_mod
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import render as R                                    # noqa: E402
from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: E402

TPL_DIR = Path(__file__).resolve().parents[1] / "templates"
BASELINE_PATH = Path(__file__).resolve().parent / "render_baseline.json"


def make_env():
    return Environment(loader=FileSystemLoader(str(TPL_DIR)),
                       autoescape=select_autoescape(["html", "j2"]))


def walk(v, path=""):
    if isinstance(v, dict):
        for k, x in v.items():
            yield from walk(x, f"{path}.{k}" if path else str(k))
    elif isinstance(v, list):
        for i, x in enumerate(v):
            yield from walk(x, f"{path}[{i}]")
    elif isinstance(v, bool) or v is None:
        return
    else:
        yield path, v


# 数字会被模板格式化（%.1f 公里），跳过仅比较存在性由父对象名兜底
SKIP_KEYS = {"distance_m", "radius_m"}


def missing_of(ctx, html_out):
    miss = []
    for path, v in walk(ctx):
        key = path.rsplit(".", 1)[-1].split("[")[0]
        if key in SKIP_KEYS:
            continue
        s = str(v).strip()
        if not s:
            continue
        if (s in html_out or html_mod.escape(s, quote=True) in html_out
                or html_mod.escape(s, quote=False) in html_out):
            continue
        miss.append(f"{path}={s[:36]}")
    return miss


def mk_ctx(analysis, cleaned, geo_raw):
    ctx = R.build_context(analysis, cleaned)
    geo = R._norm_geo(geo_raw) if geo_raw else None
    ctx["geo"] = geo
    ctx["jsapi_key"] = "test-jsapi-key" if geo else ""
    ctx["jsapi_secret"] = ""
    ctx["map_ready"] = bool(geo)
    return ctx


def base_cleaned():
    return {"total_raw": 385, "total_kept": 224, "generated_at": "2026-08-15 18:00",
            "meta": {"time_distribution": {"recent_1y": 12, "y1_2": 5, "older": 3}},
            "items": [
                {"url": "https://t.cn/a1", "title": "天通苑住了一年说点实话",
                 "published_at": "2026-07-01", "platform": "xhs", "is_listing": False},
                {"url": "https://t.cn/a2", "title": "天通苑个人转租一居室急",
                 "published_at": "2026-08-10", "platform": "douban", "is_listing": True,
                 "price_hint": "3300", "room_hint": ["一居"]},
            ]}


def full_analysis(mode):
    return {
        "mode": mode, "target": {"name": "测试小区", "city": "北京", "type": "小区"},
        "overall_score": 66, "risk_level": "中风险",
        "verdict": "通勤优先者可考虑，噪音敏感者慎选。",
        "coverage": {"note": "四源合计 224 条，时间分布健康。"},
        "findings": [
            {"risk": "二房东比例高", "severity": "high", "confidence": "medium",
             "summary": "多条帖子提到转租来自二房东。",
             "evidence": [{"title": "天通苑租房避坑实录", "url": "https://t.cn/a1",
                           "platform": "xhs", "quote": "这里九成是二房东，押金难退。",
                           "published_at": "2026-07-01"}]},
            {"risk": "地铁早高峰拥挤", "severity": "low", "confidence": "low",
             "summary": "5 号线早高峰需要等两趟。", "evidence": []},
        ],
        "dimensions": [{"name": "房东中介", "score": 72, "evidence_idx": [0]},
                       {"name": "噪音", "score": 28, "evidence_idx": []},
                       {"name": "通勤", "score": 0, "evidence_idx": []}],
        "positive": ["绿化好，楼下就是公园。", "生活成本低，超市多。"],
        "suggestions": ["签合同前核对产权证。", "押金条款逐条确认。"],
        "listings": [{"title": "天通苑个人转租一居室", "url": "https://t.cn/a2",
                      "price_hint": "3300", "room_hint": ["一居"], "platform": "douban",
                      "published_at": "2026-08-10"}],
        "candidates": [{"name": "本一区", "pros": "离地铁近。", "cons": "房龄老。",
                        "commute": "到国贸约 60 分钟。"}],
        "disclaimer": "本报告仅为公开舆情聚合，不构成决策依据。",
    }


GEO = {
    "geocode": {"location": "116.417,40.111", "formatted_address": "北京市昌平区测试小区"},
    "around": {"radius_m": 1500, "groups": [
        {"group": "地铁", "pois": [{"name": "天通苑南站", "distance_m": 800,
                                    "address": "立汤路", "location": "116.41,40.12"}]},
        {"group": "超市", "pois": [{"name": "物美超市", "distance_m": 1500,
                                    "address": "东小口", "location": "116.42,40.10"}]}]},
    "noise": {"total": 1, "hits": [{"type": "高架", "name": "立汤路高架", "distance_m": 1100,
                                    "address": "立汤路", "location": "116.42,40.11"}]},
    "route": {"mode": "transit", "summary": "5号线转1号线。", "distance_m": 24000,
              "duration_min": 62, "transfers": 1,
              "origin_location": "116.417,40.111", "dest_location": "116.46,39.908"},
}


def cases():
    a = full_analysis("diligence")
    yield "diligence 全字段无geo", mk_ctx(a, base_cleaned(), None)

    # 用户提供通勤目的地（coverage.note 带「通勤目的地：」标记）：模板应渲染通勤块
    a = full_analysis("recommend")
    a["coverage"]["note"] = "四源合计 224 条，时间分布健康；通勤目的地：国贸（用户提供）。"
    yield "recommend 全字段带geo用户提供通勤地", mk_ctx(a, base_cleaned(), GEO)

    # geo 里有路线但用户未提供通勤目的地：模板不得渲染通勤块（数据仍在 tojson 兜底）
    a = full_analysis("recommend")
    yield "recommend geo无通勤标记", mk_ctx(a, base_cleaned(), GEO)

    a = full_analysis("locate")
    a.update({"verdict": "预算内首选回龙观。", "findings": [], "dimensions": [],
              "listings": [], "positive": []})
    yield "locate 空发现", mk_ctx(a, base_cleaned(), None)

    a = full_analysis("listings")
    a.update({"overall_score": None, "findings": [], "dimensions": [],
              "candidates": [], "positive": [], "verdict": ""})
    yield "listings 无评分", mk_ctx(a, base_cleaned(), None)

    a = full_analysis("diligence")
    a.update({"findings": [], "dimensions": [], "listings": [], "candidates": [],
              "positive": [], "suggestions": [], "verdict": "", "overall_score": 0,
              "coverage": {"note": ""}, "risk_level": ""})
    c = base_cleaned()
    c["meta"]["time_distribution"] = "近1年 12 条 / 更早 5 条"
    yield "边界全空", mk_ctx(a, c, None)

    # 真实数据（存在才跑；数据目录解析同 auth_common，但不建目录）
    root = Path(os.environ.get("RENT_ASSIST_DATA") or r"E:\租房\data")
    if not root.exists():
        root = Path.home() / ".rent-assist" / "data"
    ap = root / "analysis" / "天通苑.json"
    cp = root / "cleaned" / "天通苑.json"
    gp = root / "geo" / "天通苑.json"
    if ap.is_file():
        yield "真实天通苑", mk_ctx(json.loads(ap.read_text(encoding="utf-8")),
                                 json.loads(cp.read_text(encoding="utf-8")) if cp.is_file() else {},
                                 json.loads(gp.read_text(encoding="utf-8")) if gp.is_file() else None)


def load_baseline():
    """读基线快照；缺失/损坏返回 None（视为首次运行，重建基线）。"""
    try:
        data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("templates"), dict):
        return None
    return data


def save_baseline(results):
    BASELINE_PATH.write_text(
        json.dumps({"updated_at": datetime.now().isoformat(timespec="seconds"),
                    "templates": results}, ensure_ascii=False, indent=1,
                   sort_keys=True),
        encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(
        description="报告模板数据完整性检查（基线快照对比：新缺失即失败）")
    ap.add_argument("templates", nargs="*", default=[],
                    help="模板文件名（templates/ 下），默认 report.html.j2")
    ap.add_argument("--update-baseline", action="store_true",
                    help="把本次各模板×用例的缺失集合刷新进基线（有意变更后用）")
    args = ap.parse_args()
    names = args.templates or ["report.html.j2"]
    env = make_env()

    # results[模板][用例] = 渲染后未出现在 HTML 里的标量列表（本次实测）
    results = {n: {} for n in names}
    rendered = {}
    for cname, ctx in cases():
        seg = []
        for n in names:
            h = env.get_template(n).render(**ctx)
            rendered[n] = h
            results[n][cname] = missing_of(ctx, h)
            seg.append(f"{n}: 缺 {len(results[n][cname])}")
        print(f"[{cname}] " + " | ".join(seg))
        for n in names:
            for m in results[n][cname][:6]:
                print(f"    {n} 未出现: {m}")
        # 通勤块功能闸门：渲染与否取决于 analysis 是否声明用户提供通勤目的地
        newest = rendered[names[-1]]
        if "用户提供通勤地" in cname:
            assert 'class="route-box"' in newest and "通勤测算" in newest, \
                f"[{cname}] 用户提供通勤目的地时未渲染通勤块"
        if "无通勤标记" in cname:
            assert 'class="route-box"' not in newest and "通勤测算" not in newest, \
                f"[{cname}] 未确认通勤目的地时不应渲染通勤块"
            assert "噪音源排查" not in newest, f"[{cname}] 周边噪音块应整体删除"

    baseline = load_baseline()
    if baseline is None or args.update_baseline:
        save_baseline(results)
        why = ("不存在，已生成" if baseline is None
               else "已按本次结果刷新（--update-baseline）")
        print(f"[基线] {BASELINE_PATH} {why}；之后每次运行将对比基线，"
              f"出现基线之外的新缺失即失败。")
        print("完整性检查通过（基线模式，本次仅落基线）")
        return

    bad = 0
    for n in names:
        base_cases = baseline["templates"].get(n)
        if not isinstance(base_cases, dict):
            print(f"[基线] 模板 {n} 无基线记录，本次缺失不作判罚；"
                  f"有意变更请带它跑 --update-baseline 收录。")
            continue
        for cname, cur_miss in results[n].items():
            if cname not in base_cases:
                print(f"[基线] 用例「{cname}」不在基线中，其当前缺失全部视为"
                      f"新缺失（--update-baseline 可收录）。")
            base_miss = set(base_cases.get(cname) or [])
            new_miss = [m for m in cur_miss if m not in base_miss]
            known = len(cur_miss) - len(new_miss)
            if known:
                print(f"    {n}「{cname}」基线已有缺失 {known} 项"
                      f"（模板本就不展示，不判罚）")
            if new_miss:
                bad += 1
                print(f"    !! {n}「{cname}」相对基线新缺失 {len(new_miss)} 项:")
                for m in new_miss[:8]:
                    print(f"       {m}")
    if bad:
        print(f"FAIL: {bad} 个用例相对基线出现新缺失（改版丢字段）；"
              f"若为有意变更，跑 --update-baseline 刷新基线后复核。")
        sys.exit(1)
    print("完整性检查通过（未出现基线之外的新缺失）")


if __name__ == "__main__":
    main()
