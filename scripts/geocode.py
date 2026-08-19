# -*- coding: utf-8 -*-
"""rent-assist 高德地理层：geocode / around / noise / route（Web 服务 REST + SQLite 永久缓存）。

密钥：AMAP_WEB_KEY，优先级：环境变量 > E:\租房\config\keys.env > ~/.rent-assist/
keys.env（KEY=VALUE，支持 # 注释行）> 旧位置 data/keys.env（兼容回退，打印迁移
警告）；启动时读一次；绝不硬编码/打印/写入任何输出。文件缺失或无该键 -> stderr
提示后 exit 2。

缓存：~/.rent-assist/data/cache.db（RENT_ASSIST_DATA 可覆盖）表 geocode_cache(key, resp, created_at)，命中直接用、永不过期；
cache key = endpoint+参数（不含 key），每请求在 stderr 打 cache=hit|miss。

配额纪律：搜索/路径规划个人日配额有限；around/noise 支持 --groups/--sources 只查
指定组（逗号分隔组名），复跑同参数 0 新请求。

用法：
  python geocode.py geocode "天通苑" [--city 北京]
  python geocode.py around "116.42,40.07" [--city 北京] [--radius 1500] \
      [--groups 地铁站,超市便利店] [--custom "健身房"]
  python geocode.py noise "116.42,40.07" [--city 北京] [--radius 1500] \
      [--sources 高架桥,KTV]
  python geocode.py route "天通苑" "国贸" [--mode transit|walking|riding] [--city 北京]

--custom：自定义关键词组（逗号可分隔多个），按关键词周边检索取最近 5 并入
groups 输出（group 名=关键词）；单独使用时不查预置 8 组，省配额。

输出：单行紧凑 JSON（stdout）；错误/诊断走 stderr + exit 2，不抛 traceback。
"""
import argparse
import json
import math
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    print("缺少 requests：pip install requests", file=sys.stderr)
    sys.exit(2)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth_common as ac  # noqa: E402  统一数据目录解析（data_dir）

SKILL_ROOT = Path(__file__).resolve().parent.parent
BASE_URL = "https://restapi.amap.com"
TIMEOUT = 12
TOP_N = 3            # around/noise 每组取最近 N 个
CUSTOM_TOP_N = 5     # --custom 关键词组取最近 N 个（覆盖预置组之外的按需查询）
DEFAULT_RADIUS = 1500

# (组名, types 首选, 关键词兜底)；types 空结果时自动用关键词再查一次
AROUND_GROUPS = [
    ("地铁站", "150500", "地铁站"),
    ("公交站", "150700", "公交站"),
    ("超市便利店", "060101|060102", "超市"),
    ("医院", "090100|090200|090300", "医院"),
    ("药店", "090600", "药店"),
    ("学校", "141201|141202|141203", "学校"),
    ("公园", "110101", "公园"),
    ("餐饮", "050000", "餐厅"),
]

# 噪音源（关键词为主，types 为空则跳过）：1.5km 内命中即提示
NOISE_SOURCES = [
    ("高架桥", "", "高架桥"),
    ("铁路", "", "铁路"),
    ("火车道口", "", "道口"),
    ("夜市", "", "夜市"),
    ("酒吧街", "", "酒吧"),
    ("KTV", "", "KTV"),
    ("工厂", "", "工厂"),
    ("高速出入口", "", "收费站"),
]

_COORD_RE = re.compile(r"^\d{1,3}\.\d+,\d{1,3}\.\d+$")
_INFOCODE_MSG = {
    "10001": r"Key 不存在或无效（检查 E:\租房\config\keys.env 或 ~/.rent-assist/keys.env 的 AMAP_WEB_KEY）",
    "10003": "Key 已过期或被禁用",
    "10009": "配额超限（今日个人配额用尽，明天再试或清理 cache.db 复用）",
    "10044": "配额超限（今日请求次数超限）",
}


class AmapError(Exception):
    """高德调用/解析失败（网络、配额、无结果等），main 统一转 stderr + exit 2。"""


# ---------- 密钥（红线：只进内存，不进任何输出） ----------
_KEY = None


def _keys_env_path() -> Path:
    """密钥文件位置：优先 E:\租房\config\keys.env（数据盘，skill 外），缺失时
    ~/.rent-assist/keys.env（skill 外，防随 skill 分发泄露）。

    旧位置 SKILL_ROOT/data/keys.env 仅作兼容回退（打印迁移警告）。
    """
    home_path = Path(r"E:\租房\config\keys.env") if Path(r"E:\租房\config\keys.env").is_file() else (Path.home() / ".rent-assist" / "keys.env")
    if home_path.is_file():
        return home_path
    legacy = SKILL_ROOT / "data" / "keys.env"
    if legacy.is_file():
        print(f"warn: 密钥文件位于旧位置 {legacy}，请迁移到 {home_path}"
              f"（skill 目录可能被整体拷贝/分发）", file=sys.stderr)
        return legacy
    return home_path


def load_amap_key() -> str:
    """读 AMAP_WEB_KEY（优先级：环境变量 > E:\租房\config\keys.env >
    ~/.rent-assist/keys.env > 旧位置回退）；缺失/无效 -> exit 2。"""
    global _KEY
    if _KEY:
        return _KEY
    env_val = (os.environ.get("AMAP_WEB_KEY") or "").strip()
    if env_val:
        _KEY = env_val
        return _KEY
    env_path = _keys_env_path()
    if not env_path.is_file():
        print(f"error: 密钥文件缺失: {env_path}（需包含 AMAP_WEB_KEY=...，"
              f"申请方法见 references/amap-api.md）", file=sys.stderr)
        sys.exit(2)
    try:
        lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        print(f"error: 无法读取 {env_path}: {e}", file=sys.stderr)
        sys.exit(2)
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip().casefold() == "amap_web_key":
            v = v.strip().strip('"').strip("'")
            if v:
                _KEY = v
                return _KEY
    print(f"error: {env_path} 中未找到有效的 AMAP_WEB_KEY", file=sys.stderr)
    sys.exit(2)


# ---------- SQLite 永久缓存 ----------
_conn = None


def _db():
    global _conn
    if _conn is not None:
        return _conn
    db_path = ac.data_dir("cache.db")
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(db_path))
        _conn.execute("CREATE TABLE IF NOT EXISTS geocode_cache ("
                      "key TEXT PRIMARY KEY, resp TEXT, created_at TEXT)")
        _conn.commit()
    except sqlite3.Error as e:
        print(f"warn: 缓存库不可用({e})，本次直连不缓存", file=sys.stderr)
        _conn = None
    return _conn


def cache_get(key: str):
    conn = _db()
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT resp FROM geocode_cache WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def cache_put(key: str, resp: str):
    conn = _db()
    if conn is None:
        return
    try:
        conn.execute("INSERT OR REPLACE INTO geocode_cache(key, resp, created_at) "
                     "VALUES(?, ?, ?)",
                     (key, resp, datetime.now().isoformat(timespec="seconds")))
        conn.commit()
    except sqlite3.Error as e:
        print(f"warn: 缓存写入失败: {e}", file=sys.stderr)


# ---------- HTTP ----------
def _errcode_msg(data: dict) -> str:
    code = str(data.get("infocode") or data.get("errcode") or "?")
    info = str(data.get("info") or data.get("errmsg") or "").strip()
    base = _INFOCODE_MSG.get(code, f"高德接口失败 infocode={code}")
    return f"{base}（{info}）" if info else base


def amap_get(endpoint: str, params: dict, tag: str = "") -> dict:
    """GET BASE_URL+endpoint（带缓存）。缓存 key 不含 API key；resp 原文入库。"""
    req_key = f"{endpoint}?{urlencode(sorted(params.items()))}"
    label = f"{endpoint}:{tag}" if tag else endpoint
    cached = cache_get(req_key)
    if cached is not None:
        print(f"cache=hit {label}", file=sys.stderr)
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            print(f"warn: 缓存内容损坏，重新请求 {label}", file=sys.stderr)
    print(f"cache=miss {label}", file=sys.stderr)
    try:
        resp = requests.get(BASE_URL + endpoint, params=dict(params, key=load_amap_key()),
                            timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise AmapError(f"网络请求失败（{label}）: {e}") from None
    try:
        data = resp.json()
    except ValueError:
        raise AmapError(f"高德返回非 JSON（HTTP {resp.status_code}）") from None
    ok = (data.get("errcode") == 0) if endpoint.startswith("/v4/") \
        else (str(data.get("status")) == "1")
    if not ok:
        raise AmapError(_errcode_msg(data))
    cache_put(req_key, resp.text)
    return data


# ---------- 工具 ----------
def _s(v) -> str:
    """Amap 空字段常为 []，统一转安全字符串。"""
    return "" if isinstance(v, list) else str(v or "").strip()


def _i(v, default=0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _haversine(loc_a: str, loc_b: str) -> int:
    """两个 'lng,lat' 间球面距离（米），POI 无 distance 字段时兜底。"""
    try:
        a_lng, a_lat = map(float, str(loc_a).split(","))
        b_lng, b_lat = map(float, str(loc_b).split(","))
    except ValueError:
        return 0
    rad = 6371000
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    h = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b_lng - a_lng) / 2) ** 2)
    return int(2 * rad * math.asin(math.sqrt(h)))


def _out_json(obj: dict):
    print(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def _split_names(names_arg: str) -> list:
    """逗号（含中文逗号）分隔 -> 去空格非空列表。"""
    return [x.strip() for x in (names_arg or "").replace("，", ",").split(",") if x.strip()]


def _pick(specs, names_arg: str, what: str):
    """--groups/--sources 过滤；未知名 -> AmapError（列出合法名）。"""
    if not names_arg:
        return specs
    wanted = _split_names(names_arg)
    by_name = {spec[0]: spec for spec in specs}
    unknown = [n for n in wanted if n not in by_name]
    if unknown:
        raise AmapError(f"未知{what}: {','.join(unknown)}；"
                        f"可选: {','.join(by_name)}")
    return [by_name[n] for n in wanted]


# ---------- 地理编码 ----------
def _geocode(address: str, city: str = "") -> dict:
    """地址 -> {location, formatted_address, city}；无结果时回退 POI 文本检索。"""
    params = {"address": address}
    if city:
        params["city"] = city
    data = amap_get("/v3/geocode/geo", params, tag=address)
    geos = data.get("geocodes") or []
    if geos:
        g = geos[0]
        loc = _s(g.get("location"))
        if loc:
            return {"location": loc,
                    "formatted_address": _s(g.get("formatted_address")),
                    "city": _s(g.get("city")) or _s(g.get("province")) or city}
    # 地标/商圈名（如"国贸"）geo 接口可能无结果 -> POI 关键词兜底
    p = {"keywords": address, "offset": 5}
    if city:
        p["city"] = city
    data = amap_get("/v3/place/text", p, tag=f"geo-fallback:{address}")
    pois = data.get("pois") or []
    for poi in pois:
        loc = _s(poi.get("location"))
        if loc:
            return {"location": loc,
                    "formatted_address": _s(poi.get("name")) or address,
                    "city": _s(poi.get("cityname")) or city}
    raise AmapError(f"地理编码无结果: {address}" + (f"（city={city}）" if city else ""))


def resolve_location(text: str, city: str = ""):
    """'lng,lat' 或地址 -> (location, 显示名, 推断city)。地址先过 geocode（走缓存）。"""
    s = (text or "").strip().replace("，", ",").replace(" ", "")
    if _COORD_RE.match(s):
        return s, s, ""
    geo = _geocode(s, city)
    return geo["location"], geo["formatted_address"] or s, geo["city"]


# ---------- POI 周边搜索 ----------
def _search_pois(location: str, radius: int, types: str = "", keywords: str = "",
                 city: str = "", tag: str = "") -> list:
    params = {"location": location, "radius": radius, "offset": 8, "sortrule": "distance"}
    if types:
        params["types"] = types
    if keywords:
        params["keywords"] = keywords
    if city:
        params["city"] = city
    data = amap_get("/v3/place/around", params, tag=tag)
    out = []
    for p in data.get("pois") or []:
        name = _s(p.get("name"))
        loc = _s(p.get("location"))
        if not name:
            continue
        d = p.get("distance")
        dist = _i(d, -1) if not isinstance(d, list) and _s(d) else -1
        if dist < 0:
            dist = _haversine(location, loc)
        out.append({"name": name, "distance_m": dist,
                    "address": _s(p.get("address")), "location": loc})
    out.sort(key=lambda x: x["distance_m"])
    return out


def _search_group(location: str, radius: int, name: str, types: str, keywords: str,
                  city: str = "") -> list:
    """一组搜索：types 优先，空结果且有兜底关键词时再查关键词，取最近 TOP_N。"""
    pois = []
    if types:
        pois = _search_pois(location, radius, types=types, city=city, tag=f"{name}/types")
    if not pois and keywords:
        pois = _search_pois(location, radius, keywords=keywords, city=city,
                            tag=f"{name}/kw")
    return pois[:TOP_N]


# ---------- 子命令 ----------
def cmd_geocode(args):
    _out_json(_geocode(args.address, args.city))


def cmd_around(args):
    location, _, _ = resolve_location(args.target, args.city)
    custom_kws = _split_names(args.custom)
    # --custom 单独给定时跳过预置 8 组只查关键词（按需查询省配额）；与 --groups 同给则叠加
    if custom_kws and not args.groups:
        groups = []
    else:
        groups = _pick(AROUND_GROUPS, args.groups, "分组")
    out_groups, errors = [], 0
    for name, types, kw in groups:
        try:
            pois = _search_group(location, args.radius, name, types, kw, args.city)
            out_groups.append({"group": name, "count": len(pois), "pois": pois})
        except AmapError as e:
            errors += 1
            print(f"warn: 分组 {name} 查询失败: {e}", file=sys.stderr)
            out_groups.append({"group": name, "count": 0, "pois": [], "error": str(e)})
    for kw in custom_kws:
        try:
            pois = _search_pois(location, args.radius, keywords=kw, city=args.city,
                                tag=f"{kw}/custom")[:CUSTOM_TOP_N]
            out_groups.append({"group": kw, "count": len(pois), "pois": pois})
        except AmapError as e:
            errors += 1
            print(f"warn: 自定义分组 {kw} 查询失败: {e}", file=sys.stderr)
            out_groups.append({"group": kw, "count": 0, "pois": [], "error": str(e)})
    total = len(groups) + len(custom_kws)
    if total and errors == total:
        raise AmapError("所有分组查询均失败（网络/配额问题，见 stderr）")
    _out_json({"location": location, "radius_m": args.radius, "groups": out_groups})


def cmd_noise(args):
    location, _, _ = resolve_location(args.target, args.city)
    sources = _pick(NOISE_SOURCES, args.sources, "噪音源类型")
    hits, errors = [], 0
    for name, types, kw in sources:
        try:
            for p in _search_group(location, args.radius, name, types, kw, args.city):
                hits.append({"type": name, **p})
        except AmapError as e:
            errors += 1
            print(f"warn: 噪音源 {name} 查询失败: {e}", file=sys.stderr)
    if sources and errors == len(sources):
        raise AmapError("所有噪音源查询均失败（网络/配额问题，见 stderr）")
    hits.sort(key=lambda x: x["distance_m"])
    _out_json({"location": location, "radius_m": args.radius,
               "total": len(hits), "hits": hits})  # total=0 -> 报告层写"未发现明显噪音源"


def _route_transit(origin_loc, dest_loc, city):
    data = amap_get("/v3/direction/transit/integrated",
                    {"origin": origin_loc, "destination": dest_loc,
                     "city": city, "cityd": city}, tag="transit")
    route = data.get("route") or {}
    transit = (route.get("transits") or [None])[0]
    if not transit:
        raise AmapError("未找到公交方案（两点过近或 --city 不对，可试 --mode walking）")
    segs = transit.get("segments") or []
    # 方案总距离：transit.distance，缺失时按各段步行+首条 busline 距离累加兜底
    dist = _i(transit.get("distance")) or (
        sum(_i((s.get("walking") or {}).get("distance")) for s in segs)
        + sum(_i(((s.get("bus") or {}).get("buslines") or [{}])[0].get("distance"))
              for s in segs))
    dur = _i(transit.get("duration"))
    walk = _i(transit.get("walking_distance"))
    parts, bus_count = [], 0
    for s in segs:
        wd = _i((s.get("walking") or {}).get("distance"))
        if wd > 0:
            parts.append(f"步行{wd}米")
        buslines = (s.get("bus") or {}).get("buslines") or []
        if buslines:
            b = buslines[0]
            nm = re.sub(r"[（(].*?[)）]", "", _s(b.get("name")) or "公交")
            dep = _s((b.get("departure_stop") or {}).get("name"))
            arr = _s((b.get("arrival_stop") or {}).get("name"))
            parts.append(nm + (f"({dep}→{arr})" if dep and arr else ""))
            bus_count += 1
    transfers = max(0, bus_count - 1)
    summary = "公交：" + "→".join(parts) + f"，约{round(dur / 60)}分钟"
    if transfers:
        summary += f"，换乘{transfers}次"
    if walk:
        summary += f"，步行{walk}米"
    out = {"distance_m": dist, "duration_min": round(dur / 60), "summary": summary,
           "transfers": transfers, "walking_distance_m": walk}
    cost = _s(transit.get("cost"))
    if cost:
        out["cost_yuan"] = cost
    return out


def _route_walking(origin_loc, dest_loc):
    data = amap_get("/v3/direction/walking",
                    {"origin": origin_loc, "destination": dest_loc}, tag="walking")
    path = ((data.get("route") or {}).get("paths") or [None])[0]
    if not path:
        raise AmapError("未找到步行方案（距离过远？高德步行上限约 100km）")
    dist, dur = _i(path.get("distance")), _i(path.get("duration"))
    return {"distance_m": dist, "duration_min": round(dur / 60),
            "summary": f"步行约{dist}米，约{round(dur / 60)}分钟"}


def _route_riding(origin_loc, dest_loc):
    data = amap_get("/v4/direction/bicycling",
                    {"origin": origin_loc, "destination": dest_loc}, tag="riding")
    path = ((data.get("data") or {}).get("paths") or [None])[0]
    if not path:
        raise AmapError("未找到骑行方案")
    dist, dur = _i(path.get("distance")), _i(path.get("duration"))
    return {"distance_m": dist, "duration_min": round(dur / 60),
            "summary": f"骑行约{dist}米，约{round(dur / 60)}分钟"}


def cmd_route(args):
    origin_loc, _, city_o = resolve_location(args.origin, args.city)
    dest_loc, _, city_d = resolve_location(args.dest, args.city)
    if args.mode == "transit":
        city = args.city or city_o or city_d or "北京"
        res = _route_transit(origin_loc, dest_loc, city)
    elif args.mode == "walking":
        res = _route_walking(origin_loc, dest_loc)
    else:
        res = _route_riding(origin_loc, dest_loc)
    _out_json({"mode": args.mode,
               "origin": args.origin, "destination": args.dest,
               "origin_location": origin_loc, "dest_location": dest_loc,
               **res})


def main():
    ap = argparse.ArgumentParser(
        description="rent-assist 高德地理层（geocode/around/noise/route，全量缓存）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("geocode", help="地址 -> {location, formatted_address, city}")
    g.add_argument("address", help="地址/小区名，如 天通苑")
    g.add_argument("--city", default="", help="城市（可选，提升准确率）")
    g.set_defaults(func=cmd_geocode)

    a = sub.add_parser("around", help="周边配套：分组 POI 各取最近 3")
    a.add_argument("target", help="lng,lat 或地址")
    a.add_argument("--city", default="", help="城市（可选）")
    a.add_argument("--radius", type=int, default=DEFAULT_RADIUS, help="搜索半径米，默认 1500")
    a.add_argument("--groups", default="", help="逗号分隔组名子集（默认全部 8 组，省配额）")
    a.add_argument("--custom", default="",
                   help="自定义关键词组（逗号可分隔多个），按关键词周边检索取最近 5 并入 "
                        "groups（group 名=关键词）；单独使用时不查预置 8 组，省配额")
    a.set_defaults(func=cmd_around)

    n = sub.add_parser("noise", help="噪音源排查：高架桥/铁路/夜市/KTV/工厂等")
    n.add_argument("target", help="lng,lat 或地址")
    n.add_argument("--city", default="", help="城市（可选）")
    n.add_argument("--radius", type=int, default=DEFAULT_RADIUS, help="搜索半径米，默认 1500")
    n.add_argument("--sources", default="",
                   help="逗号分隔噪音源类型子集（默认全部 8 类，省配额）")
    n.set_defaults(func=cmd_noise)

    r = sub.add_parser("route", help="通勤路线（origin/dest 支持地址或 lng,lat）")
    r.add_argument("origin", help="起点：地址或 lng,lat")
    r.add_argument("dest", help="终点：地址或 lng,lat")
    r.add_argument("--mode", choices=["transit", "walking", "riding"], default="transit",
                   help="transit=公交综合（默认） walking=步行 riding=骑行")
    r.add_argument("--city", default="", help="公交方案城市（默认从地址推断）")
    r.set_defaults(func=cmd_route)

    args = ap.parse_args()
    load_amap_key()  # 启动即校验密钥文件（红线：缺失 -> exit 2），不打印内容
    try:
        args.func(args)
    except AmapError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
