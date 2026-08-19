#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自测：fetch_media URL 回查/落盘回收 + collect_xhs 详情重试（不触网、不开浏览器）。

模拟点:
  - fetch_media.lookup_note_url: 临时 raw jsonl 造数据——extra.note_id 匹配 /
    URL 含 id 命中、带 xsec_token 优先、坏行跳过、url 为空不算、查无/目录
    不存在返回 None
  - fetch_media.resolve_note_ref: 完整 URL 透传、无 scheme 补 https、短链
    slug 目录名、裸 id 回查命中；未命中抛 NoteUrlNotFound（报错含可用
    raw 文件清单）
  - fetch_media.run_download: 假 subprocess.run 捕获命令行——node 直调
    main.js、-f json、--output 绝对路径、cwd 钉在 out_dir
  - fetch_media.reclaim_fallback_dir: --output 未生效兜底目录的文件挪回
    raw/、重名加 _N 后缀、清空壳目录
  - collect_xhs.fetch_note_detail / fetch_comments: 假 run_opencli 序列
    先败后成 → retries=1 拿到数据；一次成功 retries=0；连败 retries=1
    空结果（失败口径不变）

运行: python scripts/test_media_offline.py
退出码: 0 全部通过; 1 存在失败。
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fetch_media as fm    # noqa: E402
import collect_xhs as cx    # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s" % name)


def _patch(target_module, name, value):
    orig = getattr(target_module, name)
    setattr(target_module, name, value)
    return orig


# ---------------------------------------------------------------- fetch_media
def test_lookup_note_url():
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td)
        lines = [
            # aaa111 两条：先无 token 后带 token → 应取带 token 的
            json.dumps({"url": "https://www.xiaohongshu.com/explore/aaa111"
                               "?xsec_source=pc_search",
                        "extra": {"note_id": "aaa111"}}),
            "broken line mentions bbb222 not json",            # 坏行跳过
            json.dumps({"url": "https://www.xiaohongshu.com/search_result/"
                               "bbb222?xsec_token=TOK1&xsec_source=pc_search",
                        "extra": {}}),                          # URL 含 id 命中
            json.dumps({"url": "https://www.xiaohongshu.com/explore/aaa111"
                               "?xsec_token=TOK2",
                        "extra": {"note_id": "aaa111"}}),
            json.dumps({"url": "", "extra": {"note_id": "ccc333"}}),  # url 空不算
            json.dumps({"url": "https://www.xiaohongshu.com/explore/ddd444",
                        "extra": {"note_id": "zzz999"}}),       # ddd444 只在 url
        ]
        (raw / "xhs_demo_20260101.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")
        (raw / "readme.txt").write_text("x", encoding="utf-8")  # 非 jsonl 不扫
        url, src = fm.lookup_note_url("aaa111", raw)
        ok1 = url is not None and "xsec_token=TOK2" in url and src is not None
        url2, _ = fm.lookup_note_url("bbb222", raw)
        ok2 = url2 is not None and "xsec_token=TOK1" in url2
        ok3 = fm.lookup_note_url("nope000", raw)[0] is None
        ok4 = fm.lookup_note_url("ccc333", raw)[0] is None
        ok5 = fm.lookup_note_url("any", raw / "no_such_dir")[0] is None
        url6, _ = fm.lookup_note_url("ddd444", raw)             # 无 token 也返回
        ok6 = url6 is not None and url6.endswith("ddd444")
        check("lookup_note_url note_id/URL 匹配 + token 优先 + 坏行/空url/查无",
              ok1 and ok2 and ok3 and ok4 and ok5 and ok6)


def test_resolve_note_ref():
    full = ("https://www.xiaohongshu.com/explore/abc123"
            "?xsec_token=T&xsec_source=pc_search")
    ref, nid = fm.resolve_note_ref(full)
    ok1 = ref == full and nid == "abc123"
    ref2, nid2 = fm.resolve_note_ref("www.xiaohongshu.com/explore/abc123")
    ok2 = ref2 == "https://www.xiaohongshu.com/explore/abc123" and nid2 == "abc123"
    ref3, nid3 = fm.resolve_note_ref("http://xhslink.com/aBc-Def")
    ok3 = ref3 == "http://xhslink.com/aBc-Def" and nid3.startswith("note_")
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td)
        want = "https://www.xiaohongshu.com/explore/id777?xsec_token=X"
        (raw / "xhs_q_20260101.jsonl").write_text(
            json.dumps({"url": want, "extra": {"note_id": "id777"}}) + "\n",
            encoding="utf-8")
        ref4, nid4 = fm.resolve_note_ref("id777", raw)
        ok4 = ref4 == want and nid4 == "id777"
        try:
            fm.resolve_note_ref("id888", raw)
            ok5 = False
            msg = ""
        except fm.NoteUrlNotFound as e:
            msg = str(e)
            ok5 = "id888" in msg and "xhs_q_20260101.jsonl" in msg
    check("resolve_note_ref URL 透传/补https/短链/裸id回查/未命中报错列文件",
          ok1 and ok2 and ok3 and ok4 and ok5)


def test_run_download_node_direct():
    if shutil.which("opencli") is None:
        print("  [SKIP] 本机无 opencli，跳过命令行拼装断言")
        return
    captured = {}

    class FakeR:
        returncode = 0
        stdout = b"[]"
        stderr = b""

    def fake_run(cmd, capture_output=False, timeout=None, cwd=None):
        captured["cmd"] = list(cmd)
        captured["cwd"] = cwd
        return FakeR()

    orig = _patch(fm.subprocess, "run", fake_run)
    try:
        rc, out, err = fm.run_download(
            "https://www.xiaohongshu.com/explore/x1?xsec_token=T",
            Path("E:/fake/media/x1/raw"), cwd=Path("E:/fake/media"))
    finally:
        _patch(fm.subprocess, "run", orig)
    cmd = captured.get("cmd") or []
    mainjs = str(Path(shutil.which("opencli")).resolve().parent / "node_modules"
                 / "@jackwener" / "opencli" / "dist" / "src" / "main.js")
    ok = (rc == 0
          and cmd and cmd[0] == shutil.which("node") and cmd[1] == mainjs
          and cmd[2:5] == ["xiaohongshu", "download",
                           "https://www.xiaohongshu.com/explore/x1?xsec_token=T"]
          and cmd[5:7] == ["-f", "json"]
          and cmd[cmd.index("--output") + 1] == str(Path("E:/fake/media/x1/raw"))
          and captured["cwd"] == str(Path("E:/fake/media")))
    check("run_download node直调main.js + -f json + --output绝对路径 + cwd钉out_dir",
          ok)


def test_reclaim_fallback_dir():
    with tempfile.TemporaryDirectory() as td:
        out_dir = Path(td)
        raw = out_dir / "n1" / "raw"
        raw.mkdir(parents=True)
        (raw / "a.jpg").write_bytes(b"old")                     # 重名不动旧文件
        fb = out_dir / "xiaohongshu-downloads" / "n1" / "sub"
        fb.mkdir(parents=True)
        (fb / "a.jpg").write_bytes(b"new")
        (fb / "b.mp4").write_bytes(b"v")
        fm.reclaim_fallback_dir(out_dir, "n1", raw)
        ok = ((raw / "a.jpg").read_bytes() == b"old"
              and (raw / "a_1.jpg").read_bytes() == b"new"
              and (raw / "b.mp4").read_bytes() == b"v"
              and not (out_dir / "xiaohongshu-downloads" / "n1").exists())
        fm.reclaim_fallback_dir(out_dir, "n1", raw)             # 目录不在=空操作
        check("reclaim_fallback_dir 挪回raw/重名加后缀/清空壳", ok)


# ---------------------------------------------------------------- collect_xhs
def _with_fake_opencli(seq_by_cmd):
    """按子命令名分派假返回序列，装到 cx.run_opencli。返回调用记录。"""
    calls = []

    def fake(args, timeout=None):
        name = args[0]
        calls.append(name)
        item = seq_by_cmd[name].pop(0)
        return item

    orig = _patch(cx, "run_opencli", fake)
    orig_rand = _patch(cx.random, "uniform", lambda a, b: 0.0)
    return calls, orig, orig_rand


def test_collect_retry():
    # note 先败(rc=1 垃圾输出)后成 → retries=1 拿到正文，不再标失败
    calls, orig, orig_rand = _with_fake_opencli({
        "note": [(1, "boom", ""),
                 (0, '[{"field":"content","value":"正文内容"}]', "")],
    })
    try:
        detail, shim, retries = cx.fetch_note_detail("https://u1")
        ok1 = (cx.pick(detail, "content") == "正文内容"
               and retries == 1 and calls == ["note", "note"])
    finally:
        _patch(cx, "run_opencli", orig)
        _patch(cx.random, "uniform", orig_rand)

    # note 连败两次 → retries=1 空结果（失败口径由调用方标记）
    calls, orig, orig_rand = _with_fake_opencli({
        "note": [(0, "[]", ""), (0, "[]", "")],
    })
    try:
        detail, shim, retries = cx.fetch_note_detail("https://u2")
        ok2 = detail == {} and retries == 1 and calls == ["note", "note"]
    finally:
        _patch(cx, "run_opencli", orig)
        _patch(cx.random, "uniform", orig_rand)

    # comments 一次成功 → retries=0 不等待
    calls, orig, orig_rand = _with_fake_opencli({
        "comments": [(0, '[{"text":"顶","likes":"3","author":"a"}]', "")],
    })
    try:
        crc, rows, cshim, cretries = cx.fetch_comments("https://u3", 10)
        ok3 = (crc == 0 and rows and rows[0]["text"] == "顶"
               and cretries == 0 and calls == ["comments"])
    finally:
        _patch(cx, "run_opencli", orig)
        _patch(cx.random, "uniform", orig_rand)

    # comments 先败(rc=2 非 0/1)后成 → retries=1，成功返回 rows
    calls, orig, orig_rand = _with_fake_opencli({
        "comments": [(2, "", "err"),
                     (0, '[{"text":"沙发","likes":"1","author":"b"}]', "")],
    })
    try:
        crc, rows, cshim, cretries = cx.fetch_comments("https://u4", 10)
        ok4 = crc == 0 and rows and cretries == 1 and calls == ["comments"] * 2
    finally:
        _patch(cx, "run_opencli", orig)
        _patch(cx.random, "uniform", orig_rand)

    # comments 连败 → rows None + 最后一次 rc 保留（供调用方标失败）
    calls, orig, orig_rand = _with_fake_opencli({
        "comments": [(2, "", "err"), (2, "", "err")],
    })
    try:
        crc, rows, cshim, cretries = cx.fetch_comments("https://u5", 10)
        ok5 = crc == 2 and rows is None and cretries == 1
    finally:
        _patch(cx, "run_opencli", orig)
        _patch(cx.random, "uniform", orig_rand)
    check("collect_xhs note/comments 重试1次: 先败后成计入/一次成功不重试/连败口径不变",
          ok1 and ok2 and ok3 and ok4 and ok5)


def main():
    print("== fetch_media #14 ==")
    test_lookup_note_url()
    test_resolve_note_ref()
    test_run_download_node_direct()
    test_reclaim_fallback_dir()
    print("== collect_xhs #11 残余 ==")
    test_collect_retry()
    print()
    print("离线自测: %d 通过 / %d 失败" % (PASS, FAIL))
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
