#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自测：clean.py / lexicon.py（不触网，临时目录自建自清）。

覆盖:
  - detect_seek_post：典型求租帖命中（标题强词 / 正文组合 / 预算+找房词 /
    求租句式：真实漏杀回补样本）、出租帖不误杀（含出租侧词 / web 全文夹带他人
    求租标题 / 仅预算数字 / 无信号 / 供给帖反例）
  - detect_buy_sell_post：买房/卖房帖标题命中、租住帖（含正文提及买房/房价）不误杀
  - clean_row：raw extra 的 note_id / note_type 透传（存在才拷，缺失/非 dict 安全）
  - clean.main CLI：默认剔除求租帖 + meta.seek_posts 计数，--keep-seek 保留打标

运行: python scripts/test_clean_offline.py
退出码: 0 全部通过; 1 存在失败。
"""

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import clean          # noqa: E402
from lexicon import detect_buy_sell_post, detect_seek_post, strip_scenario_words  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s" % name)


# ---------------------------------------------------------------- 求租帖判定
def test_detect_seek_hit():
    cases = [
        # 标题强词命中（含出租侧字面也不豁免：求租帖常写"找房东直租/月底入住"）
        ("天通苑求租｜9月初入住 诚心找房东直租一居", "", True),
        ("求租房 天通苑东区 17号线。", "", True),
        ("个人求租房东直租三居", "", True),
        ("回龙观立水桥天通苑求转租/房东直租", "", True),
        ("天通苑有无出租的房子", "", True),          # 强词自带"出租"
        ("帮朋友寻房源，天通苑", "", True),
        ("蹲个天通苑主卧", "", True),
        ("北京求好房源租房", "", True),
        ("求推天通苑附近民水电房子", "#天通苑租房 #整租", True),
        # 正文信号：无出租侧词 + 预算句式组合
        ("求求！天通苑北租房", "天通苑北租房，预算2k以内，在天通苑南上班 #找房子", True),
        ("求租房，回龙观，天通苑", "1500 以内，民水电", True),
        ("找房", "想找个开间，2500以内", True),
        # v4 真实漏杀回补（data/raw xhs 天通苑实测样本，测试数据）：
        # 求租句式命中即判，不走出租侧词豁免（正文天然含"房东直租"字面）
        ("找房子", "求房东直租标准一居室 需求：八月底入住 天通苑北七家房东直租."
                  "一居整租#租房 90后一家住（测试样本）", True),
        ("天通苑附近租房", "有没有房东直租啊…… 租房平台中介费太贵了（测试样本）", True),
        ("天通苑附近两居室求房东直租", "", True),
        ("测试帖", "想租个一居室，天通苑东，8月底入住", True),
        ("测试帖", "求个单间，天通苑附近，8月底入住", True),
        ("求推荐房源", "天通苑附近的，测试内容", True),
        # 标题句式："找房子"仅标题判（排除后跟"出租"的供给写法）
        ("找房子", "#天通苑东 #17号线（测试）", True),
    ]
    allok = True
    for title, content, expect in cases:
        got = detect_seek_post(title, content)
        if got != expect:
            allok = False
            print("    期望 %s 得到 %s (%s)" % (expect, got, title))
    check("典型求租帖命中（标题强词/正文+预算组合）", allok)


def test_detect_seek_not():
    cases = [
        # 出租帖：标题/正文供给侧表述 + 弱词不判
        ("天通苑西一区一居室出租", "月租2500元，想租的私聊，随时入住", False),
        ("急急急转租带阳台的超大卧室，含泪转租3200", "", False),
        ("本人房东，自己的房子求稳定租户", "天通苑西二区，首次出租，可看房", False),
        # web 全文页夹带其他帖子的求租标题：正文级信号 + 出租侧词 → 不判
        ("天通苑西二区个人5100两居转租 与房东签合同",
         "小组帖子列表：天通苑两居转租 - 求租天通苑两居室 - 本二区3层两居", False),
        # 仅预算数字无找房词不判；无信号保留
        ("天通苑每个区差别有多大？", "住过天通苑的朋友来帮忙回答一下", False),
        ("北京天通苑租房求助", "求求过来人建议！！！#租房避坑指南 #租房避雷", False),
        ("2500以内有合适的吗", "", False),
        ("天通苑租房攻略｜15个小区实测", "1室3500-4000，2室4500-5000", False),
        # v4 供给帖反例（真实供给样本，句式不得误杀）：含"求租/直租/主卧/找房子"字面
        ("房东直租一居室出租 天通苑 2000元（测试）", "个人房源，采光好，随时看房", False),
        ("个人转租一居急 天通苑（测试）", "急转，精装一居，拎包入住", False),
        ("主卧独卫出租 天通苑东二区（测试）", "合租三居室，主卧带独立卫生间，出租中", False),
        ("天通苑找房子出租的看过来（测试）", "本人在天通苑有房出租，价格美丽", False),
        ("天通苑房子出租信息汇总（测试）", "整租一居 2500元/月，随时入住", False),
        ("测试", "攻略：来北京先找房子再签合同，注意押金条款", False),
    ]
    allok = True
    for title, content, expect in cases:
        got = detect_seek_post(title, content)
        if got != expect:
            allok = False
            print("    期望 %s 得到 %s (%s)" % (expect, got, title))
    check("出租帖/无信号不误杀（含 web 夹带、仅预算数字）", allok)


# ---------------------------------------------------------------- 买房/卖房帖判定
def test_detect_buy_sell():
    # 真实漏杀样本（douyin/web 天通苑实测）应命中
    hit_cases = [
        "天通苑的房子为什么越来越难卖了！ #买房#北京#昌平#天通苑（测试）",
        "在天通苑买房该怎么选？#上热门 #天通苑#北京二手房（测试）",
        "十年北漂，在天通苑买房真的很难以启齿吗（测试）",
        "天通苑二手房最新挂牌（测试）",
        "北京楼市新政下的天通苑（测试）",
        "学区房该怎么买？天通苑家长进（测试）",
    ]
    # 租住内容帖（含正文提及买房/房价的随笔）不应命中：仅标题匹配
    miss_cases = [
        ("和男票动手出租房改造，房子是别人的（测试）", "跟买房比还是杯水车薪，房租倒是月月交"),
        ("天通苑租房价格实测（测试）", "房价高了租金不一定涨，月租2500元"),
        ("天通苑西一区一居室出租（测试）", "月租2500元，随时看房"),
        ("天通苑每个区差别有多大？（测试）", "住过的朋友来聊聊"),
    ]
    ok = all(detect_buy_sell_post(t) for t in hit_cases) and \
        not any(detect_buy_sell_post(t) for t, _ in miss_cases)
    check("买房/卖房帖标题命中、租住帖不误杀（仅标题匹配）", ok)


# ---------------------------------------------------------------- note 透传
def test_clean_row_note_passthrough():
    base = {"platform": "xhs", "url": "https://x/1", "title": "天通苑租房",
            "content": "转租一居", "published_at": "", "likes": 0,
            "comments_count": 0, "comments": []}
    row = dict(base, extra={"note_id": "abc123", "note_type": "normal",
                            "search_rank": 7})
    it = clean.clean_row(row)
    ok1 = (it.get("note_id") == "abc123" and it.get("note_type") == "normal"
           and "search_rank" not in it)
    ok2 = "note_id" not in clean.clean_row(dict(base))
    ok3 = "note_id" not in clean.clean_row(dict(base, extra={"note_id": None}))
    ok4 = "note_id" not in clean.clean_row(dict(base, extra="bad"))
    check("note_id/note_type 透传（存在才拷，缺失/None/非 dict extra 安全）",
          ok1 and ok2 and ok3 and ok4)


# ---------------------------------------------------------------- CLI 接线
def _row(title, content="", url="", extra=None):
    return {"platform": "xhs", "query": "天通苑", "url": url, "title": title,
            "content": content, "published_at": "2026-08-10", "likes": 0,
            "comments_count": 0, "comments": [], "extra": extra or {}}


SEEK_TITLE = "求租天通苑一居室，预算3000以内"
LISTING_TITLE = "天通苑西一区一居室出租，2500元/月"
NEUTRAL_TITLE = "天通苑每个区差别有多大？"


def _run_cli(td, extra_argv):
    raw = Path(td) / "raw_test.jsonl"
    rows = [
        _row(SEEK_TITLE, url="https://x/s"),
        _row(LISTING_TITLE, "房东直租，随时看房", url="https://x/l",
             extra={"note_id": "n1", "note_type": "normal", "search_rank": 1}),
        _row(NEUTRAL_TITLE, "天通苑不同小区差距很大", url="https://x/n"),
    ]
    raw.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                   encoding="utf-8")
    out = Path(td) / "out.json"
    argv = sys.argv
    sys.argv = (["clean.py", "--query", "天通苑", "--raw-dir", td,
                 "--out", str(out)] + extra_argv)
    try:
        clean.main()
    finally:
        sys.argv = argv
    return json.loads(out.read_text(encoding="utf-8"))


def test_cli_default_drop_seek():
    with tempfile.TemporaryDirectory() as td:
        result = _run_cli(td, [])
        titles = [it["title"] for it in result["items"]]
        ok = (result["total_kept"] == 2 and SEEK_TITLE not in titles
              and LISTING_TITLE in titles and NEUTRAL_TITLE in titles
              and result["meta"]["seek_posts"] == 1
              and result["meta"]["keep_seek"] is False)
    check("CLI 默认剔除求租帖 + meta.seek_posts 计数", ok)


def test_cli_keep_seek_flag():
    with tempfile.TemporaryDirectory() as td:
        result = _run_cli(td, ["--keep-seek"])
        seek_items = [it for it in result["items"] if it["title"] == SEEK_TITLE]
        others = [it for it in result["items"] if it["title"] != SEEK_TITLE]
        ok = (result["total_kept"] == 3 and len(seek_items) == 1
              and seek_items[0].get("seek_post") is True
              and all("seek_post" not in it for it in others)
              and result["meta"]["seek_posts"] == 1
              and result["meta"]["keep_seek"] is True)
    check("CLI --keep-seek 保留求租帖并打 seek_post 标记", ok)


def test_cli_note_passthrough_end_to_end():
    with tempfile.TemporaryDirectory() as td:
        result = _run_cli(td, [])
        listing = [it for it in result["items"] if it["title"] == LISTING_TITLE][0]
        neutral = [it for it in result["items"] if it["title"] == NEUTRAL_TITLE][0]
        ok = (listing.get("note_id") == "n1"
              and listing.get("note_type") == "normal"
              and "note_id" not in neutral)
    check("端到端：cleaned items 透传 note_id/note_type", ok)


def test_cli_drop_buy_sell():
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "raw_test.jsonl"
        rows = [
            _row("天通苑的房子为什么越来越难卖了！ #买房（测试）", "二手房讨论",
                 url="https://x/b"),
            _row(LISTING_TITLE, "房东直租，随时看房", url="https://x/l"),
        ]
        raw.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                       encoding="utf-8")
        out = Path(td) / "out.json"
        argv = sys.argv
        sys.argv = ["clean.py", "--query", "天通苑", "--raw-dir", td, "--out", str(out)]
        try:
            clean.main()
        finally:
            sys.argv = argv
        result = json.loads(out.read_text(encoding="utf-8"))
        titles = [it["title"] for it in result["items"]]
        ok = (result["total_kept"] == 1 and LISTING_TITLE in titles
              and result["meta"]["buy_sell_posts"] == 1)
    check("CLI 剔除买房/卖房帖 + meta.buy_sell_posts 计数", ok)


def _run_cli_rows(td, rows, extra_argv):
    raw = Path(td) / "raw_test.jsonl"
    raw.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                   encoding="utf-8")
    out = Path(td) / "out.json"
    argv = sys.argv
    sys.argv = (["clean.py", "--query", "龙泽苑", "--raw-dir", td,
                 "--out", str(out)] + extra_argv)
    try:
        clean.main()
    finally:
        sys.argv = argv
    return json.loads(out.read_text(encoding="utf-8"))


def _web_row(title, content="", url="", likes=0, comments_count=0,
             query="龙泽苑"):
    return {"platform": "web", "query": query, "url": url, "title": title,
            "content": content, "published_at": "2026-08-10", "likes": likes,
            "comments_count": comments_count, "comments": [], "extra": {}}


def test_cli_city_filter():
    with tempfile.TemporaryDirectory() as td:
        rows = [
            # 异城帖（测试）：URL 城市子域 / 标题城市名+租赁词 → 剔
            _web_row("龙泽苑两居室出租（测试）", "晋城本地房源",
                     url="https://jincheng.58.com/zufang/123.shtml"),
            _web_row("晋城龙泽苑租房信息（测试）", "龙泽苑小区租房讨论",
                     url="https://www.jcfang.com/1.html"),
            _web_row("保定龙泽苑房租多少钱（测试）", "龙泽苑租金讨论（测试）",
                     url="https://bd.house.com/1.html"),
            # 北京帖（测试）：留
            _web_row("龙泽苑真实居住体验（测试）", "龙泽苑住了三年的感受（测试）",
                     url="https://beijing.58.com/zufang/456.shtml"),
            # xhs 无可靠城市信号：不判、保留
            dict(_web_row("龙泽苑笔记（测试）", "龙泽苑测评（测试）",
                          url="https://xhs.link/1"), platform="xhs"),
        ]
        result = _run_cli_rows(td, rows, ["--city", "北京"])
        titles = [it["title"] for it in result["items"]]
        ok = (result["meta"]["city_mismatch"] == 3
              and "龙泽苑真实居住体验（测试）" in titles
              and "龙泽苑笔记（测试）" in titles
              and not any("晋城" in t or "保定" in t for t in titles)
              and result["meta"]["city_skipped_non_web"] == 1)
    check("CLI --city 北京：异城 web 帖剔除+计数，北京帖/xhs 保留", ok)


def test_cli_city_filter_v2():
    """P12：黑名单扩容（济宁/青岛/上海）+ 本市信号白名单兜底。"""
    with tempfile.TemporaryDirectory() as td:
        rows = [
            # 实测漏网异城帖（测试）：济宁/青岛/上海 → 剔（URL 黑名单/标题异城+租赁词）
            _web_row("济宁龙泽苑两居室出租（测试）", "龙泽苑整租房源（测试）",
                     url="https://jining.58.com/zufang/1.shtml"),
            _web_row("青岛龙泽苑租房信息（测试）", "龙泽苑小区租房（测试）",
                     url="https://www.qingdaofang.com/1.html"),
            _web_row("上海龙泽苑租房（测试）", "龙泽苑合租（测试）",
                     url="https://shanghai.anjuke.com/zufang/2.shtml"),
            _web_row("济宁龙泽苑房租多少钱（测试）", "龙泽苑租金讨论（测试）",
                     url="https://www.fang.com/1.html"),  # 无城市 URL，靠标题异城+租赁词
            # 北京帖（测试）：含昌平区/回龙观信号 → 保留
            _web_row("龙泽苑真实居住体验（测试）", "坐标昌平区回龙观，龙泽苑住了三年（测试）",
                     url="https://www.zhihu.com/question/1"),
            # 无信号帖（测试）：既无异城信号也无本市信号 → 保留（宁漏杀不误杀）
            _web_row("龙泽苑物业怎么样（测试）", "龙泽苑物业费讨论（测试）",
                     url="https://www.douban.com/group/topic/1"),
        ]
        result = _run_cli_rows(td, rows, ["--city", "北京"])
        titles = [it["title"] for it in result["items"]]
        ok = (result["meta"]["city_mismatch"] == 4
              and result["meta"]["city_kept_signal"] >= 1
              and not any(("济宁" in t or "青岛" in t or "上海" in t) for t in titles)
              and "龙泽苑真实居住体验（测试）" in titles
              and "龙泽苑物业怎么样（测试）" in titles)
    check("CLI --city 北京：济宁/青岛/上海同名帖剔除，昌平/回龙观信号帖与无信号帖保留",
          ok)


def test_cli_city_off_by_default():
    with tempfile.TemporaryDirectory() as td:
        rows = [
            _web_row("晋城龙泽苑租房信息（测试）", "龙泽苑小区租房讨论（测试）",
                     url="https://jincheng.58.com/zufang/1.shtml"),
        ]
        result = _run_cli_rows(td, rows, [])
        ok = (result["total_kept"] == 1
              and result["meta"].get("city_mismatch", 0) == 0)
    check("CLI 默认不启用城市消歧（行为不变）", ok)


def test_cli_listing_page_filter():
    with tempfile.TemporaryDirectory() as td:
        rows = [
            # 列表页/走势页/问答页（测试）：comments=0 且 likes=0 → 剔
            _web_row("西二旗房屋出租信息_北京海淀西二旗出租房源信息-58出租网（测试）",
                     "龙泽苑房源列表页（测试）", url="https://bj.58.com/list1"),
            _web_row("西二旗二室租金价格走势（测试）", "龙泽苑租金走势数据（测试）",
                     url="https://bj.58.com/list2"),
            _web_row("龙泽苑小区怎么样物业费好不好（测试）", "龙泽苑问答页（测试）",
                     url="https://bj.58.com/list3"),
            # 有互动的走势标题帖（测试）：likes>0 → 不误杀
            _web_row("龙泽苑租金价格走势讨论（测试）", "龙泽苑租金涨了（测试）",
                     url="https://bj.58.com/keep1", likes=5),
            # 真实帖（测试）：留
            _web_row("龙泽苑居住吐槽（测试）", "龙泽苑隔音差物业差（测试）",
                     url="https://bj.58.com/keep2", comments_count=3),
        ]
        result = _run_cli_rows(td, rows, [])
        titles = [it["title"] for it in result["items"]]
        dropped = ["58出租网（测试）", "价格走势（测试）", "好不好（测试）"]
        ok = (result["meta"]["listing_pages"] == 3
              and result["total_kept"] == 2
              and "龙泽苑居住吐槽（测试）" in titles
              and "龙泽苑租金价格走势讨论（测试）" in titles
              and not any(any(d in t for d in dropped) for t in titles))
    check("CLI 列表页过滤：走势/聚合/问答页剔除，有互动/真实帖保留", ok)


def test_cli_blank_line_silent(capsys=None):
    import io, contextlib
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "raw_test.jsonl"
        raw.write_text("\n".join([
            json.dumps(_web_row("龙泽苑真实帖（测试）", "龙泽苑讨论（测试）",
                                url="https://x/1"), ensure_ascii=False),
            "",
            "   ",
            "\t",
        ]) + "\n", encoding="utf-8")
        out = Path(td) / "out.json"
        buf = io.StringIO()
        argv = sys.argv
        sys.argv = ["clean.py", "--query", "龙泽苑", "--raw-dir", td,
                    "--out", str(out)]
        try:
            with contextlib.redirect_stderr(buf):
                clean.main()
        finally:
            sys.argv = argv
        result = json.loads(out.read_text(encoding="utf-8"))
        ok = (result["total_kept"] == 1 and "跳过坏行" not in buf.getvalue())
    check("raw 空白行静默跳过（无坏行告警）", ok)


def test_cli_douyin_entity_relaxed():
    """#18：抖音帖正文不含标的词时，标题/top 评论命中标的词或地标词即保留。"""
    with tempfile.TemporaryDirectory() as td:
        rows = [
            # 放宽保留①：标题+正文都无"龙泽苑"，但评论提到 → 保留
            {"platform": "douyin", "query": "龙泽苑", "url": "https://dy/1",
             "title": "租房避坑指南，看完少走弯路（测试）",
             "content": "在北京租房一定要注意这几点（测试）",
             "published_at": "2026-08-10", "likes": 10, "comments_count": 2,
             "comments": [{"text": "龙泽苑附近就这样，坑得很（测试）", "likes": 5}],
             "extra": {}},
            # 放宽保留②：标题/正文均无标的词，评论命中 → 保留
            {"platform": "douyin", "query": "龙泽苑租房", "url": "https://dy/2",
             "title": "回龙观这块住着怎么样（测试）", "content": "讲讲真实感受（测试）",
             "published_at": "2026-08-10", "likes": 3, "comments_count": 1,
             "comments": [{"text": "龙泽苑也差不多情况（测试）", "likes": 2}],
             "extra": {}},
            # 无任何信号 → 仍剔除（放宽不放水）
            {"platform": "douyin", "query": "龙泽苑", "url": "https://dy/3",
             "title": "北京租房攻略大全（测试）", "content": "通用攻略内容（测试）",
             "published_at": "2026-08-10", "likes": 1, "comments_count": 0,
             "comments": [], "extra": {}},
            # 非抖音平台同形态（评论命中）不放宽 → 剔除（行为不变）
            {"platform": "xhs", "query": "龙泽苑", "url": "https://xhs/4",
             "title": "租房避坑指南（测试）", "content": "通用攻略（测试）",
             "published_at": "2026-08-10", "likes": 2, "comments_count": 1,
             "comments": [{"text": "龙泽苑附近就这样（测试）", "likes": 1}],
             "extra": {}},
        ]
        result = _run_cli_rows(td, rows, [])
        titles = [it["title"] for it in result["items"]]
        ok = (result["meta"]["douyin_relaxed"] == 2
              and "租房避坑指南，看完少走弯路（测试）" in titles
              and "回龙观这块住着怎么样（测试）" in titles
              and result["meta"]["filtered_no_entity"] == 2
              and not any(t == "北京租房攻略大全（测试）" for t in titles))
    check("CLI 抖音实体放宽：标题/评论命中即保留 douyin_relaxed 计数，无信号仍剔", ok)


def test_cli_seo_page_filter():
    """#19：SEO 模板列表页（挂牌价格堆叠）剔除并计 meta.seo_pages，口碑帖不误杀。"""
    stack = "\n".join("%d元/月 %d室" % (p, i % 3 + 1)
                      for i, p in enumerate(range(2000, 7000, 800)))
    with tempfile.TemporaryDirectory() as td:
        rows = [
            # 命中①：标题"租房-价格筛选"模板 + 正文价格堆叠 → 剔
            _web_row("优优好房 龙泽苑租房-价格筛选（测试）", stack,
                     url="https://bj.58.com/zufang/"),
            # 命中②："整租·"标题 + 多区间价格 + 堆叠 → 剔
            _web_row("整租·龙泽苑房源大全（测试）",
                     "2000-3000 与 4000-5000\n" + stack,
                     url="https://youfangke.com/zufang/list"),
            # 反例①：URL 是筛选页但正文价格少（正常讨论帖） → 留
            _web_row("龙泽苑房租讨论（测试）", "现在一居大概3000元/月，涨了不少",
                     url="https://bj.58.com/zufang/123.shtml"),
            # 反例②：标题含价格词但正文无堆叠 → 留
            _web_row("龙泽苑租房价格感受（测试）", "龙泽苑租金亲身经历分享（测试）",
                     url="https://zhihu.com/q/9"),
        ]
        result = _run_cli_rows(td, rows, [])
        titles = [it["title"] for it in result["items"]]
        ok = (result["meta"]["seo_pages"] == 2
              and "龙泽苑房租讨论（测试）" in titles
              and "龙泽苑租房价格感受（测试）" in titles
              and not any("优优好房" in t or "整租·" in t for t in titles))
    check("CLI SEO 模板列表页剔除（标题/URL 特征+价格堆叠双信号），口碑帖不误杀", ok)


# ---------------------------------------------------------------- 0 命中剥词 & 转写截断
def test_strip_scenario_words():
    cases = [
        ("北京 龙泽苑 住过", "北京 龙泽苑"),   # 剥场景词，保城市前缀+标的
        ("天通苑怎么样", "天通苑"),           # 无空格组合词（子串匹配）
        ("天通苑租房体验", "天通苑"),         # 长词优先，防"租房"先拆碎
        ("天通苑 个人转租", "天通苑"),
        ("天通苑 租金多少钱", "天通苑"),
        ("天通苑 避坑", "天通苑"),
        ("魏公村上班住哪", "魏公村"),        # 意图 C 问法词（长词优先剥"上班住哪"）
        ("魏公村 通勤租房", "魏公村"),       # 问法词+泛词都剥，剩工作地裸词
        ("北京 魏公村 上班住哪", "北京 魏公村"),  # 保城市前缀
        ("天通苑", None),                    # 裸标的无可剥 → 不重搜
        ("租房", None),                      # 剥完为空 → 不重搜
    ]
    allok = True
    for q, expect in cases:
        got = strip_scenario_words(q)
        if got != expect:
            allok = False
            print("    期望 %s 得到 %s (%s)" % (expect, got, q))
    check("strip_scenario_words 剥场景词（子串/长词优先/保城市前缀，剥不动返回 None）",
          allok)


def test_clean_row_asr_cap():
    base = {"platform": "douyin", "url": "https://d/1", "title": "天通苑口播（测试）",
            "published_at": "", "likes": 0, "comments_count": 0, "comments": []}
    long_asr = "【口播转写】" + "字" * 1200
    asr_it = clean.clean_row(dict(base, content=long_asr,
                                  extra={"asr": True, "video_file": "v.mp4"}))
    prefix_it = clean.clean_row(dict(base, content=long_asr, extra={}))
    normal_it = clean.clean_row(dict(base, content="普通帖" + "字" * 1200,
                                     extra={}))
    ok = (len(asr_it["content"]) == len(long_asr)
          and len(prefix_it["content"]) == len(long_asr)
          and len(normal_it["content"]) == 500)
    check("口播转写帖截断放宽 500→2000（extra.asr 或【口播转写】前缀），普通帖仍 500",
          ok)


def main():
    print("== lexicon.detect_seek_post ==")
    test_detect_seek_hit()
    test_detect_seek_not()
    print("== lexicon.strip_scenario_words ==")
    test_strip_scenario_words()
    print("== clean.clean_row ==")
    test_clean_row_note_passthrough()
    test_clean_row_asr_cap()
    print("== clean.main CLI ==")
    test_cli_default_drop_seek()
    test_cli_keep_seek_flag()
    test_cli_note_passthrough_end_to_end()
    test_cli_drop_buy_sell()
    print("== clean.main 城市消歧/列表页/空行 ==")
    test_cli_city_filter()
    test_cli_city_filter_v2()
    test_cli_city_off_by_default()
    test_cli_listing_page_filter()
    test_cli_douyin_entity_relaxed()
    test_cli_seo_page_filter()
    test_cli_blank_line_silent()
    print()
    print("离线自测: %d 通过 / %d 失败" % (PASS, FAIL))
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
