#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自测: collect_douban --intent 搜索意图拆分 + run_collect 透传（不触网）。

背景: 口碑类查询用"租房"泛词搜索会混入大量求租帖(发帖人本人在找房, 纯噪声),
原先只靠清洗层 lexicon.detect_seek_post 兜底; 现把意图拆分做进搜索层。

模拟点:
  - title_relevant 两套语义: word(口碑, 默认)=命中 query 全串或其每个分词都
    出现(AND), 不启用固定租赁词表——求租帖标题恰含"租房/合租", 词表形同虚设
    会放行; listing(找房, 旧行为)=命中 query 或租赁词表之一即保留
  - CLI: --intent 默认 word(向后兼容: 显式 --intent listing 走旧行为);
    --rental-words 默认按意图取(listing='转租 直租 合租', 已去泛词"租房";
    word=空=裸 query 搜), 显式传值(含空串)优先
  - level1_search 组装: word 只搜裸 query; listing 与供给侧租赁词双拼,
    intent 穿透到 fetch_list_rows
  - run_collect --douban-intent: 串行 run_one_batch / 并行 build_worker_cmd
    组装 douban 子命令时透传 --intent, 不传(或非 douban 平台)不带该参数

运行: python scripts/test_intent_offline.py
退出码: 0 全部通过; 1 存在失败。
"""

import contextlib
import io
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import collect_douban as cdb   # noqa: E402
import run_collect as rc       # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s" % name)


def _patch(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    return orig


# ---------------------------------------------------------------- title_relevant
def test_title_relevant_word_intent():
    cases = [
        # (标题, query, 期望) —— word: 须命中全串或每个分词(AND), 词表不参与
        ("求租天通苑两居室 预算3000", "天通苑 住过", False),   # 缺"住过"分词
        ("我在天通苑住过三年, 说点感受", "天通苑 住过", True),
        ("天通苑怎么样? 求真实居住体验", "天通苑 怎么样", True),
        ("天通苑避坑指南(中介篇)", "天通苑 避坑", True),
        ("天通苑 住过的人进", "天通苑 住过", True),            # 全串命中
        ("回龙观求租两居室", "天通苑 住过", False),
        ("", "天通苑 住过", False),
    ]
    allok = all(cdb.title_relevant(t, q, intent="word") == expect
                for (t, q, expect) in cases)
    # 显式传 rental 词也不放行求租帖: word 意图词表不参与判定
    nolex = cdb.title_relevant("求租天通苑两居室", "天通苑 住过",
                               ("租房", "合租"), "word") is False
    check("word 意图: 求租标题被拒(AND 分词), 体验标题保留, 词表不参与", allok and nolex)


def test_title_relevant_listing_intent():
    cases = [
        # (标题, query, 期望) —— listing: query 或租赁词表命中之一即保留(旧行为)
        ("天通苑个人转租两居室 5000", "天通苑 个人转租", True),
        ("求租天通苑两居室 预算3000", "天通苑 个人转租", True),  # query 命中仍保留,
        # 搜索层不拦 query 命中的求租帖(泛词双拼已去掉, 混入面收窄), 清洗层兜底
        ("急转主卧 次卧已租", "天通苑", True),                   # 租赁词表仍生效
        ("回龙观求租两居", "天通苑 个人转租", False),            # 无命中即拒
    ]
    allok = all(cdb.title_relevant(t, q, intent="listing") == expect
                for (t, q, expect) in cases)
    check("listing 意图: 出租标题保留(词表/query), 无命中拒绝; 求租帖仅靠 query 命中",
          allok)


# ---------------------------------------------------------------- CLI 默认与兼容
def _parse_cdb(extra):
    argv = sys.argv
    sys.argv = ["collect_douban.py", "--query", "天通苑"] + extra
    try:
        return cdb.build_parser().parse_args()
    finally:
        sys.argv = argv


def _resolve_rental_words(args):
    """复刻 main 的默认解析(3 行), 验证 --rental-words 按意图取默认。"""
    if args.rental_words is None:
        return "" if args.intent == "word" else cdb.DEFAULT_RENTAL_WORDS
    return args.rental_words


def test_cli_intent_defaults():
    a1 = _parse_cdb([])
    ok1 = (a1.intent == "word" and a1.rental_words is None
           and _resolve_rental_words(a1) == "")
    a2 = _parse_cdb(["--intent", "listing"])
    ok2 = (a2.intent == "listing"
           and _resolve_rental_words(a2) == cdb.DEFAULT_RENTAL_WORDS)
    a3 = _parse_cdb(["--rental-words", "个人转租 房东直租"])   # 显式传值优先
    ok3 = _resolve_rental_words(a3) == "个人转租 房东直租"
    a4 = _parse_cdb(["--intent", "listing", "--rental-words", ""])
    ok4 = _resolve_rental_words(a4) == ""                       # 显式空串=裸 query
    try:
        with contextlib.redirect_stderr(io.StringIO()):          # 压掉 argparse usage 噪声
            _parse_cdb(["--intent", "bad"])
        bad_rejected = False
    except SystemExit:
        bad_rejected = True                                     # argparse choices 拒非法值
    words = cdb.split_words(cdb.DEFAULT_RENTAL_WORDS)
    ok5 = words == ["转租", "直租", "合租"] and "租房" not in words
    check("--intent 默认 word/显式 listing; rental-words 默认按意图, 泛词'租房'已出默认词表",
          ok1 and ok2 and ok3 and ok4 and bad_rejected and ok5)


# ---------------------------------------------------------------- level1 组装
def test_level1_search_combos():
    urls, intents = [], []

    def fake_flr(budget, url_for_start, limit, query, rental_words, seen,
                 group_id=None, intent="word"):
        urls.append(url_for_start(0))
        intents.append(intent)
        return [], 0, False

    orig_flr = _patch(cdb, "fetch_list_rows", fake_flr)
    try:
        urls.clear()
        cdb.level1_search(object(), "天通苑 住过", 5, (), intent="word")
        ok1 = (urls == [cdb.SEARCH_URL.format(q=quote("天通苑 住过"), start=0)]
               and intents == ["word"])                     # word: 只搜裸 query
        urls.clear()
        intents.clear()
        cdb.level1_search(object(), "天通苑", 5,
                          ("转租", "直租", "合租"), intent="listing")
        want = [cdb.SEARCH_URL.format(q=quote(c), start=0)
                for c in ("天通苑 转租", "天通苑 直租", "天通苑 合租")]
        ok2 = urls == want and intents == ["listing"] * 3   # listing: 供给侧词双拼
    finally:
        _patch(cdb, "fetch_list_rows", orig_flr)
    check("level1_search: word 裸搜单次 / listing 与'转租 直租 合租'双拼, intent 穿透",
          ok1 and ok2)


# ---------------------------------------------------------------- run_collect 透传
class _FakeCompleted:
    returncode = 0
    stdout = "platform=douban fetched=1/1 filtered_irrelevant=0 file=x"
    stderr = ""


def _parse_rc(extra):
    argv = sys.argv
    sys.argv = ["run_collect.py", "--per-platform", "5",
                "--queries", "天通苑 住过", "--platforms", "douban"] + extra
    try:
        return rc.build_parser().parse_args()
    finally:
        sys.argv = argv


def test_run_collect_intent_passthrough():
    captured = []

    def fake_run(cmd, **k):
        captured.append(cmd)
        return _FakeCompleted()

    orig_run = _patch(rc.subprocess, "run", fake_run)   # rc.subprocess 即全局模块, 记得还原
    try:
        with tempfile.TemporaryDirectory() as td:
            args = _parse_rc(["--douban-intent", "word", "--out-dir", td])
            rc.run_one_batch(rc.SCRIPTS_DIR, "douban", "天通苑 住过", args)
            ok1 = ("--intent" in captured[-1]
                   and captured[-1][captured[-1].index("--intent") + 1] == "word")
            args = _parse_rc(["--out-dir", td])          # 不传: 不带 --intent(用子脚本默认)
            rc.run_one_batch(rc.SCRIPTS_DIR, "douban", "天通苑 住过", args)
            ok2 = "--intent" not in captured[-1]
            args = _parse_rc(["--douban-intent", "listing", "--out-dir", td])
            rc.run_one_batch(rc.SCRIPTS_DIR, "xhs", "天通苑 住过", args)  # 非 douban 不拼
            ok3 = "--intent" not in captured[-1] and "--douban-intent" not in captured[-1]
    finally:
        _patch(rc.subprocess, "run", orig_run)
    check("run_one_batch: --douban-intent 透传 douban --intent; 不传/非 douban 不带",
          ok1 and ok2 and ok3)


def test_run_collect_worker_cmd_passthrough():
    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td) / "run"
        args = _parse_rc(["--douban-intent", "listing"])
        cmd = rc.build_worker_cmd(args, "douban", 10, run_dir)
        ok1 = ("--douban-intent" in cmd
               and cmd[cmd.index("--douban-intent") + 1] == "listing")
        args = _parse_rc([])
        cmd = rc.build_worker_cmd(args, "xhs", 10, run_dir)
        ok2 = "--douban-intent" not in cmd
    check("build_worker_cmd(并行): --douban-intent 随 worker 命令透传", ok1 and ok2)


def main():
    print("== collect_douban 意图拆分 ==")
    test_title_relevant_word_intent()
    test_title_relevant_listing_intent()
    test_cli_intent_defaults()
    test_level1_search_combos()
    print("== run_collect 透传 ==")
    test_run_collect_intent_passthrough()
    test_run_collect_worker_cmd_passthrough()
    print()
    print("离线自测: %d 通过 / %d 失败" % (PASS, FAIL))
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
