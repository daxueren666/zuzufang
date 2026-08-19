# -*- coding: utf-8 -*-
"""rent-assist 报告渲染：analysis.json (+ cleaned.json + 可选 geo.json) -> 单文件 HTML。

四种模式共用一套模板：diligence 尽调 / recommend 推荐 / locate 选址 / listings 房源。
analysis 字段可能缺省，一律回退默认值；severity/confidence 中英归一化。

--geo data/geo/<标的>.json 时注入地理块（地图 + 通勤/噪音/配套文字层）；
JSAPI 密钥读 ~/.rent-assist/keys.env 的 AMAP_JSAPI_KEY / AMAP_JSAPI_SECRET（环境
变量优先；旧位置 data/keys.env 兼容回退。AMAP_WEB_KEY 绝不进模板），key 缺失时
地图块自动降级为文字版。

用法：
  python render.py --analysis data/analysis.json [--cleaned data/cleaned/x.json] \
      [--geo data/geo/<标的>.json] [--out data/reports/<标的>_<日期>.html]
（data/ 指数据目录 ~/.rent-assist/data，环境变量 RENT_ASSIST_DATA 可覆盖）
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth_common as ac  # noqa: E402  统一数据目录解析（data_dir）
from clean import slugify  # noqa: E402  复用同一 slugify

try:
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    from jinja2 import filters as _j2_filters
except ImportError:
    print("缺少 jinja2：pip install jinja2", file=sys.stderr)
    sys.exit(2)

SKILL_ROOT = Path(__file__).resolve().parent.parent
PLATFORM_LABELS = {
    "xhs": "小红书", "xiaohongshu": "小红书", "douban": "豆瓣",
    "douyin": "抖音", "web": "网页", "complaint12315": "12315", "12315": "12315",
}
_SEV_MAP = {"高": "high", "严重": "high", "high": "high",
            "中": "medium", "medium": "medium",
            "低": "low", "low": "low"}
_SEV_LABEL = {"high": "高", "medium": "中", "low": "低"}
_SEV_RANK = {"high": 0, "medium": 1, "low": 2}
MODES = {"diligence", "recommend", "locate", "listings", "faq"}

# ---------- 展示层过滤器 nohash：去掉 # 开头话题标签 ----------
# 覆盖三种形态：空格分隔（#租房避雷 #租房欺诈）、成对或连排（#天通苑二房东#不退押金）、
# 紧贴正文（…跑路#房东欺诈）。仅用于模板展示层，数据文件保持原样（原文进 title 提示）。
_HASHTAG_RE = re.compile(r"#[^#\s]+(?:#[^#\s]+)*")
_WS_RE = re.compile(r"\s+")


def _nohash(v) -> str:
    """Jinja 过滤器 nohash：剥离话题标签并把空白收紧为单空格。"""
    return _WS_RE.sub(" ", _HASHTAG_RE.sub(" ", str(v or ""))).strip()


# 注册进 jinja2 默认过滤器表：本文件自建 env 与 test_render_check.py 的独立 env 都能用
_j2_filters.FILTERS["nohash"] = _nohash


def _load_json(path: str):
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        print(f"warn: 文件不存在 {path}", file=sys.stderr)
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"warn: 解析失败 {path}: {e}", file=sys.stderr)
        return {}


def _norm_sev(v):
    return _SEV_MAP.get(str(v or "").strip().casefold(), "medium")


def _plain(v) -> str:
    """设计纪律：报告正文零 em/en dash（证据引文保留原文除外）。"""
    return str(v or "").replace("—", "，").replace("–", "-")


def _platform_label(p):
    p = str(p or "").strip()
    return PLATFORM_LABELS.get(p.casefold(), p or "来源不详")


def _score_tier(score):
    try:
        s = int(score)
    except (TypeError, ValueError):
        return "none", 0
    s = max(0, min(100, s))
    if s >= 60:
        return "hi", s
    if s >= 30:
        return "mid", s
    return "low", s


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------- 地理层（--geo / JSAPI 密钥） ----------

def load_jsapi_keys() -> dict:
    """读 AMAP_JSAPI_KEY / AMAP_JSAPI_SECRET（环境变量优先 >
    ~/.rent-assist/keys.env > 旧位置 data/keys.env 兼容回退；KEY=VALUE，# 注释）。

    红线：AMAP_WEB_KEY 只供 geocode.py 本地调用，这里绝不读取、绝不进模板；
    且只把 jsapi_key/jsapi_secret 两个值放进模板上下文，不传整个文件内容。
    文件缺失/键为空 -> 返回缺省项，模板自动降级为文字版。
    """
    out = {"jsapi_key": (os.environ.get("AMAP_JSAPI_KEY") or "").strip(),
           "jsapi_secret": (os.environ.get("AMAP_JSAPI_SECRET") or "").strip()}
    env_path = ac.keys_env_path()  # 统一解析：RENT_ASSIST_KEYS > E:\租房\config\keys.env > ~/.rent-assist/keys.env
    if not env_path.is_file():
        legacy = SKILL_ROOT / "data" / "keys.env"
        if legacy.is_file():
            print(f"warn: 密钥文件位于旧位置 {legacy}，请迁移到 {env_path}"
                  f"（skill 目录可能被整体拷贝/分发）", file=sys.stderr)
            env_path = legacy
    if not env_path.is_file():
        return out
    try:
        lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    wanted = {"amap_jsapi_key": "jsapi_key", "amap_jsapi_secret": "jsapi_secret"}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        dst = wanted.get(k.strip().casefold())
        if not dst:
            continue
        v = v.strip().strip('"').strip("'")
        if v and not v.startswith("<") and not v.startswith("待"):
            if not out[dst]:  # 环境变量已设的值优先，文件只补缺
                out[dst] = v
    return out


def _norm_geo(geo: dict):
    """geo.json -> 模板可渲染结构；字段缺失/非法自动丢弃，整体不可用返回 None。"""
    if not isinstance(geo, dict):
        return None

    def _loc(v):
        s = str(v or "").strip()
        return s if "," in s else ""

    def _poi(p):
        p = p if isinstance(p, dict) else {}
        return {"name": str(p.get("name") or "").strip(),
                "distance_m": _int(p.get("distance_m")),
                "address": str(p.get("address") or "").strip(),
                "location": _loc(p.get("location"))}

    g = geo.get("geocode") if isinstance(geo.get("geocode"), dict) else {}
    geocode = {"location": _loc(g.get("location")),
               "formatted_address": str(g.get("formatted_address") or "").strip(),
               "city": str(g.get("city") or "").strip()}

    around_src = geo.get("around") if isinstance(geo.get("around"), dict) else {}
    groups = []
    for gr in around_src.get("groups") or []:
        if not isinstance(gr, dict):
            continue
        pois = [_poi(p) for p in (gr.get("pois") or []) if isinstance(p, dict)]
        pois = [p for p in pois if p["name"]]
        if pois:
            groups.append({"group": str(gr.get("group") or "").strip() or "配套",
                           "pois": pois})

    noise_src = geo.get("noise") if isinstance(geo.get("noise"), dict) else {}
    hits = []
    for h in noise_src.get("hits") or []:
        if not isinstance(h, dict):
            continue
        p = _poi(h)
        p["type"] = str(h.get("type") or "").strip() or "噪音源"
        if p["name"]:
            hits.append(p)
    total = _int(noise_src.get("total"), len(hits)) or len(hits)
    noise = {"hits": hits, "total": total}

    r = geo.get("route") if isinstance(geo.get("route"), dict) else {}
    route = None
    if _loc(r.get("origin_location")) and _loc(r.get("dest_location")):
        mode = str(r.get("mode") or "").strip()
        route = {"mode": mode,
                 "mode_label": {"transit": "公交", "walking": "步行",
                                "riding": "骑行"}.get(mode, mode or "出行"),
                 "summary": str(r.get("summary") or "").strip(),
                 "distance_m": _int(r.get("distance_m")),
                 "duration_min": _int(r.get("duration_min")),
                 "transfers": _int(r.get("transfers")),
                 "origin_location": _loc(r.get("origin_location")),
                 "dest_location": _loc(r.get("dest_location"))}

    usable = bool(geocode["location"] and (groups or hits or route))
    if not usable:
        return None
    return {"geocode": geocode,
            "around": {"radius_m": _int(around_src.get("radius_m"), 1500) or 1500,
                       "groups": groups},
            "noise": noise, "route": route, "usable": True}


# cleaned.time_distribution -> 模板键名（模板引用 recent_1y/y1_2/older，clean.py 输出 within_1y/older_24m）
_TD_KEY_MAP = {"recent_1y": "recent_1y", "within_1y": "recent_1y",
               "y1_2": "y1_2", "older": "older", "older_24m": "older"}


def _time_distribution(cleaned: dict):
    """透传 cleaned 顶层 time_distribution，归一为模板可用的 str 或 {recent_1y,y1_2,older}。

    缺省/结构非法返回 ""（模板自带空值守卫，不会崩）。
    """
    td = cleaned.get("time_distribution")
    if isinstance(td, str):
        return td.strip()
    if not isinstance(td, dict):
        return ""
    out = {}
    for src, dst in _TD_KEY_MAP.items():
        if src in td and dst not in out:
            try:
                out[dst] = int(td.get(src) or 0)
            except (TypeError, ValueError):
                continue
    return out


def build_context(analysis: dict, cleaned: dict, out_name_hint: str = ""):
    mode = str(analysis.get("mode") or "diligence").strip()
    if mode not in MODES:
        mode = "diligence"

    target = analysis.get("target") or {}
    if not isinstance(target, dict):
        target = {}

    # ---- FAQ 模式：{question, sections, risk_notes, action_checklist, target_name 可选} ----
    faq = None
    if mode == "faq":
        sections = []
        for s in analysis.get("sections") or []:
            if not isinstance(s, dict):
                continue
            pts = [str(p or "").strip() for p in (s.get("points") or [])
                              if str(p or "").strip()]
            if str(s.get("title") or "").strip() or pts:
                sections.append({"title": _plain(s.get("title")).strip() or "要点",
                                 "points": pts})
        faq = {"question": _plain(analysis.get("question")).strip(),
               "sections": sections,
               "risk_notes": [_plain(x) for x in (analysis.get("risk_notes") or [])
                              if str(x).strip()],
               "action_checklist": [_plain(x) for x in (analysis.get("action_checklist") or [])
                                    if str(x).strip()],
               "target_name": str(analysis.get("target_name") or "").strip()}

    t_name = str(target.get("name") or "").strip()
    if mode == "faq":
        t_name = t_name or (faq["target_name"] if faq else "") \
            or (faq["question"][:24] if faq else "") or "租房答疑"
    if not t_name:
        t_name = "未知标的"

    items = cleaned.get("items") or []
    items = items if isinstance(items, list) else []

    # url -> cleaned item（evidence / listings 按 url 关联补全 published_at 等字段）
    cleaned_by_url = {str(it.get("url") or ""): it for it in items
                      if isinstance(it, dict) and it.get("url")}

    # ---- coverage -> 平台徽章（仅 >0 展示）----
    cov = analysis.get("coverage") or {}
    cov = cov if isinstance(cov, dict) else {}
    badges = []
    for key, label in PLATFORM_LABELS.items():
        if key == "xiaohongshu":
            continue
        try:
            n = int(cov.get(key) or 0)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            badges.append({"label": label, "count": n})

    # ---- findings：severity/confidence 归一 + 排序 ----
    findings = []
    for f in analysis.get("findings") or []:
        if not isinstance(f, dict):
            continue
        ev = []
        for e in f.get("evidence") or []:
            if not isinstance(e, dict):
                continue
            url = str(e.get("url") or "")
            src_it = cleaned_by_url.get(url) if url else None
            ev.append({
                "title": str(e.get("title") or "来源帖")[:80],
                "url": url,
                "platform_label": _platform_label(e.get("platform")),
                "quote": str(e.get("quote") or "")[:120],
                # 优先取 analysis 自带 published_at，缺省按 url 回 cleaned items 补
                "published_at": str(e.get("published_at")
                                    or (src_it or {}).get("published_at") or ""),
            })
        sev = _norm_sev(f.get("severity"))
        conf = _norm_sev(f.get("confidence"))
        findings.append({
            "risk": _plain(f.get("risk")).strip() or "未命名风险",
            "severity": sev,
            "severity_label": _SEV_LABEL[sev],
            "confidence": conf,
            "confidence_label": _SEV_LABEL[conf],
            "summary": _plain(f.get("summary")),
            "evidence": ev,
        })
    findings.sort(key=lambda x: (_SEV_RANK[x["severity"]], _SEV_RANK[x["confidence"]]))

    # ---- dimensions：evidence_idx 回链 cleaned items（越界忽略）----
    dimensions = []
    for d in analysis.get("dimensions") or []:
        if not isinstance(d, dict):
            continue
        ev_links = []
        for idx in d.get("evidence_idx") or []:
            try:
                it = items[int(idx)]
            except (ValueError, TypeError, IndexError):
                continue
            if isinstance(it, dict) and it.get("url"):
                ev_links.append({"title": str(it.get("title") or "来源帖")[:80],
                                 "url": str(it.get("url"))})
        tier, score = _score_tier(d.get("score"))
        dimensions.append({"name": str(d.get("name") or "").strip() or "维度",
                           "score": score, "tier": tier, "evidences": ev_links,
                           "reason": _plain(d.get("reason")).strip()})

    # ---- listings：URL 关联补全（cleaned 的 is_listing 数据优先补缺省字段）----
    listings = []
    for l in analysis.get("listings") or []:
        if not isinstance(l, dict):
            continue
        url = str(l.get("url") or "")
        src = cleaned_by_url.get(url, {})
        room = l.get("room_hint") or src.get("room_hint") or []
        if isinstance(room, str):
            room = [room] if room else []
        listings.append({
            "title": str(l.get("title") or src.get("title") or "房源帖")[:120],
            "url": url,
            "price_hint": str(l.get("price_hint") or src.get("price_hint") or ""),
            "room_hint": list(room),
            "platform_label": _platform_label(l.get("platform") or src.get("platform")),
            "published_at": str(l.get("published_at") or src.get("published_at") or ""),
        })
    # analysis.listings 缺省时兜底：直接用 cleaned 中 is_listing 的帖
    if not listings:
        for it in items:
            if isinstance(it, dict) and it.get("is_listing"):
                listings.append({
                    "title": str(it.get("title") or "房源帖")[:120],
                    "url": str(it.get("url") or ""),
                    "price_hint": str(it.get("price_hint") or ""),
                    "room_hint": list(it.get("room_hint") or []),
                    "platform_label": _platform_label(it.get("platform")),
                    "published_at": str(it.get("published_at") or ""),
                })

    # ---- candidates ----
    candidates = []
    for c in analysis.get("candidates") or []:
        if not isinstance(c, dict):
            continue
        candidates.append({
            "name": str(c.get("name") or "").strip() or "片区",
            "pros": _plain(c.get("pros")),
            "cons": _plain(c.get("cons")),
            "commute": _plain(c.get("commute")),
            "evidence": _plain(c.get("evidence")).strip(),
        })

    tier, overall = _score_tier(analysis.get("overall_score"))
    strs = lambda arr, cap=200: [_plain(x) for x in (arr or []) if str(x).strip()][:cap]

    return {
        "mode": mode,
        "target_name": t_name,
        "target_city": str(target.get("city") or ""),
        "target_type": str(target.get("type") or ""),
        "overall_score": overall,
        "overall_tier": tier,
        "risk_level": str(analysis.get("risk_level") or "").strip() or "未知",
        "verdict": _plain(analysis.get("verdict")).strip(),
        "badges": badges,
        "coverage_note": _plain(cov.get("note")),
        "total_raw": cleaned.get("total_raw"),
        "total_kept": cleaned.get("total_kept"),
        "time_distribution": _time_distribution(cleaned),
        "data_generated_at": str(cleaned.get("generated_at") or ""),
        "rendered_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "findings": findings,
        "dimensions": dimensions,
        "faq": faq,
        "positive": strs(analysis.get("positive"), 12),
        "suggestions": strs(analysis.get("suggestions"), 12),
        "listings": listings,
        "candidates": candidates,
        "disclaimer": _plain(analysis.get("disclaimer") or
                             "无数据不等于安全，本报告仅为公开舆情聚合，不构成决策依据。"),
    }


def main():
    ap = argparse.ArgumentParser(description="rent-assist 报告渲染")
    ap.add_argument("--analysis", required=True, help="data/analysis.json")
    ap.add_argument("--cleaned", default=None,
                    help="data/cleaned/*.json（listings 关联补全 / 样本数）")
    ap.add_argument("--geo", default=None,
                    help="data/geo/<标的>.json（地理块：地图+通勤/噪音/配套）")
    ap.add_argument("--out", default=None, help="默认 data/reports/<标的>_<YYYYMMDD>.html")
    args = ap.parse_args()

    analysis = _load_json(args.analysis)
    if not analysis:
        print(f"error: 无法读取 analysis: {args.analysis}", file=sys.stderr)
        sys.exit(1)
    cleaned = _load_json(args.cleaned)
    ctx = build_context(analysis, cleaned)

    # FAQ 模式必填字段校验：question / sections / risk_notes / action_checklist
    if ctx["mode"] == "faq":
        f = ctx["faq"] or {}
        missing = [name for name, v in [("question", f.get("question")),
                                        ("sections", f.get("sections")),
                                        ("risk_notes", f.get("risk_notes")),
                                        ("action_checklist", f.get("action_checklist"))]
                   if not v]
        if missing:
            print(f"error: faq 模式必填字段缺失或为空：{'、'.join(missing)}"
                  "（需要 question、sections[{title,points}]、risk_notes、action_checklist）",
                  file=sys.stderr)
            sys.exit(1)

    # 地理层：--geo 提供 geo.json 时注入地图块；JSAPI 密钥缺失则模板降级为文字版
    geo = _norm_geo(_load_json(args.geo)) if args.geo else None
    if args.geo and geo is None:
        print(f"warn: geo 数据不可用（缺 geocode.location 或无配套/噪音/路线），"
              f"地图块将显示未采集提示: {args.geo}", file=sys.stderr)
    keys = load_jsapi_keys()
    ctx["geo"] = geo
    ctx["jsapi_key"] = keys["jsapi_key"]
    ctx["jsapi_secret"] = keys["jsapi_secret"]
    ctx["map_ready"] = bool(geo and keys["jsapi_key"])
    if geo and not keys["jsapi_key"]:
        print("warn: ~/.rent-assist/keys.env 缺 AMAP_JSAPI_KEY，地图块降级为文字版",
              file=sys.stderr)

    data_root = ac.data_dir()  # 环境变量 RENT_ASSIST_DATA > ~/.rent-assist/data
    out_path = (Path(args.out) if args.out else
                data_root / "reports" /
                f"{slugify(ctx['target_name'])}_{datetime.now():%Y%m%d}.html")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    env = Environment(loader=FileSystemLoader(str(SKILL_ROOT / "templates")),
                      autoescape=select_autoescape(["html", "j2"]))
    env.filters["nohash"] = _nohash
    html = env.get_template("report.html.j2").render(**ctx)
    out_path.write_text(html, encoding="utf-8")
    print(f"rendered [{ctx['mode']}] -> {out_path}")


if __name__ == "__main__":
    main()
