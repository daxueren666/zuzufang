#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rent-assist: 小红书笔记图片下载+压缩（供 Claude 视觉读图）。

collect_xhs.py 采到的图文笔记只有文字；本脚本按笔记逐条调
opencli xiaohongshu download 拉取媒体文件，把图片用 Pillow 压缩
（长边 <=1024、JPEG quality=82，省 token）后写入 <out-dir>/<note_id>/，
并落 manifest.json 登记清单——Claude 后续读 manifest 里的相对路径即可
直接视觉读图，不必碰原图。

用法:
    python fetch_media.py --note-ids "id1,id2,id3"
    python fetch_media.py --note-ids "id1,id2" --max-images-per-note 5
    python fetch_media.py --note-ids "https://www.xiaohongshu.com/explore/<id>?xsec_token=..."

--note-ids: 逗号分隔笔记引用，两种形态:
    - 完整笔记 URL（含 xsec_token；download 只认签名 URL）——直接用;
    - 裸 note_id——回 <data>/raw/*.jsonl 反查该笔记采集时记录的完整 URL
      （匹配 extra.note_id 或 URL 含 id，优先带 xsec_token 的），查不到
      明确报错并列出可用 raw 文件，失败记 manifest.error。
--max-images-per-note: 每篇最多压缩保留的图片数（默认 5，取下载顺序前 N）。

opencli 经 subprocess 一律 node 直调包内 main.js（.CMD shim 会令 -f json
失效，同 collect_xhs.run_opencli）；download 的 --output 显式传绝对路径、
子进程 cwd 钉在 <out-dir>，落盘固定 <out-dir>/<note_id>/，与运行目录无关。

前置条件（见 check_deps.py）:
    - opencli 可用且小红书主站已登录（download 退出码 3/77 或 AUTH_REQUIRED
      → 打印 ensure_auth 指引后本脚本 exit 3）
    - Pillow 已安装（python -c "import PIL" 可验证；缺库按 stderr 提示装）

输出目录结构:
    <out-dir>/<note_id>/img_001.jpg ...   压缩后图片（JPEG）
    <out-dir>/<note_id>/raw/              原始下载（图片压缩后即删，视频保留）
    <out-dir>/<note_id>/manifest.json     {note_id, type, files, fetched_at,
                                           source_url}；失败时另有 error 字段，
                                           检出视频时另有 videos 字段
    type 按实际落盘文件判定: image|video|mixed|unknown（这才是 note_type 的
    可靠来源；collect_xhs 搜索阶段的 note_type 只是不可靠的先验）。
    检出视频不转写，manifest.videos 记相对路径，提示走 asr.py。

退出码: 0 = 至少一篇成功（有图片或视频）；2 = 全部失败 / opencli 缺失 /
            Pillow 缺失；3 = 需登录。
"""

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth_common as ac

DEFAULT_OUT_DIR = ac.data_dir("media")   # ~/.rent-assist/data/media（RENT_ASSIST_DATA 可覆盖）
OP_TIMEOUT = 300                 # 含浏览器下载，放宽
SLEEP_RANGE = (1.5, 3.5)         # 逐笔记请求间隔（秒，随机）
MAX_EDGE = 1024                  # 压缩后长边上限
JPEG_QUALITY = 82
RC_NEEDS_LOGIN = (3, 77)         # opencli download 未登录的退出码（含 auth_common 的 77）
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".flv", ".webm", ".m4v"}
NOTE_ID_RE = re.compile(
    r"xiaohongshu\.com/(?:explore|search_result|discovery/item)/([0-9A-Za-z]+)"
)

PIL_INSTALL_HINT = (
    "[media] Pillow 未安装。请先执行（清华镜像）:\n"
    "[media]   PIP_CACHE_DIR=\"%s\" python -m pip install "
    "pillow -i https://pypi.tuna.tsinghua.edu.cn/simple"
    % ac.tools_dir("pip-cache")
)


def pick(d, *keys, default=None):
    """从 dict 里取第一个非空键的值（防御字段命名变化）。"""
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def parse_json_output(text):
    """opencli -f json 输出解析：纯 JSON 优先，退化到截取首尾括号，失败 None。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None


def run_opencli(args, timeout=OP_TIMEOUT, cwd=None):
    """运行 opencli xiaohongshu 子命令，返回 (rc, stdout, stderr) 文本。

    注意：Windows 下 opencli.CMD shim 经 subprocess 调用时 -f json 不生效
    （输出 YAML 且 rc=1），因此优先用 node 直调包内主脚本
    （同 collect_xhs.run_opencli）。
    """
    exe = shutil.which("opencli")
    if exe is None:
        return 127, "", "opencli not found in PATH"
    mainjs = Path(exe).resolve().parent / "node_modules" / "@jackwener" / "opencli" / "dist" / "src" / "main.js"
    node = shutil.which("node")
    if node and mainjs.exists():
        cmd = [node, str(mainjs), "xiaohongshu"] + args
    else:
        cmd = [exe, "xiaohongshu"] + args
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        return 1, "", "opencli timeout after %ss" % timeout
    return (r.returncode,
            r.stdout.decode("utf-8", errors="replace"),
            r.stderr.decode("utf-8", errors="replace"))


def run_download(note_ref, raw_dir, cwd):
    """opencli xiaohongshu download <note_ref> -f json --output <raw_dir>。

    download 的 --output 缺省是 cwd 下的 ./xiaohongshu-downloads，因此除
    显式传绝对 raw_dir 外，还把子进程 cwd 钉在 out_dir（即 cwd 参数）双
    保险，保证落盘与运行目录无关。
    """
    return run_opencli(
        ["download", note_ref, "-f", "json", "--output", str(raw_dir)],
        cwd=str(cwd))


def extract_files_from_output(out):
    """从 download -f json 输出解析已下载文件路径（防御式，可能为空列表）。

    已知形态一: [{"index":1,"type":"image","status":"downloaded","size":"...",
                  "file"/"path"/...: "<本地路径>"}]
    形态二: 行式 [{"field":"file","value":"<路径>"}, ...]（同 opencli note）。
    只回收"值是已存在的本地文件"的条目；官方帮助只承诺 index/type/status/
    size 四列（未见路径列），所以解析不出就走调用方的目录扫描兜底。
    """
    data = parse_json_output(out)
    rows = data if isinstance(data, list) else (
        [data] if isinstance(data, dict) else [])
    paths = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        kv = ({str(row["field"]): row.get("value")}
              if "field" in row else row)
        for k in ("file", "path", "filename", "local_path", "saved_to",
                  "dest", "output", "name"):
            v = kv.get(k)
            if isinstance(v, str) and v.strip() and Path(v).is_file():
                paths.append(v)
    return paths


def scan_media_files(raw_dir):
    """扫描下载目录里的媒体文件（输出不带路径时的兜底；raw_dir 每笔记独享）。"""
    files = []
    if raw_dir.is_dir():
        for p in sorted(raw_dir.rglob("*")):
            if p.is_file() and (p.suffix.lower() in IMAGE_EXTS or
                                p.suffix.lower() in VIDEO_EXTS):
                files.append(p)
    return files


def compress_image(src, dst, max_edge=MAX_EDGE, quality=JPEG_QUALITY):
    """Pillow 压缩单图: 长边 <=max_edge、JPEG。返回 (w, h)。"""
    from PIL import Image
    with Image.open(src) as im:
        im.load()
        if im.mode not in ("RGB", "L"):    # RGBA/P/CMYK 等 -> RGB（JPEG 不带 alpha）
            im = im.convert("RGB")
        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        im.thumbnail((max_edge, max_edge), resample)   # 只缩不放
        size = im.size
        im.save(dst, "JPEG", quality=quality)
    return size


def ensure_pillow():
    """Pillow 在场才继续；缺库打印安装指引返回 False。"""
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        print(PIL_INSTALL_HINT, file=sys.stderr)
        return False


class NoteUrlNotFound(Exception):
    """裸 note_id 在 raw jsonl 里查不到完整签名 URL（download 无法进行）。"""

    def __init__(self, note_id, raw_dir):
        raw_dir = Path(raw_dir) if raw_dir else ac.data_dir("raw")
        files = sorted(p.name for p in raw_dir.glob("*.jsonl")) \
            if raw_dir.is_dir() else []
        msg = ("裸 note_id=%s 未在 raw jsonl 中查到完整 URL（opencli download "
               "只认带 xsec_token 的签名 URL，凭空拼 explore 形会被拒）。"
               "%s 下可用文件: %s" % (
                   note_id, raw_dir, ", ".join(files) or "（无 jsonl）"))
        super().__init__(msg)


def lookup_note_url(note_id, raw_dir=None):
    """回查 raw jsonl 找 note_id 的完整笔记 URL。返回 (url|None, 来源路径|None)。

    匹配 extra.note_id == note_id 或 URL 含 note_id（行内先做廉价包含预筛，
    免对大 jsonl 全量 json.loads）；同一 id 多条命中时优先带 xsec_token 的。
    """
    raw_dir = Path(raw_dir) if raw_dir else ac.data_dir("raw")
    best = None
    if not raw_dir.is_dir():
        return None, None
    for path in sorted(raw_dir.glob("*.jsonl")):
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    if note_id not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    url = str(rec.get("url") or "").strip()
                    if not url:
                        continue
                    extra = rec.get("extra") or {}
                    if extra.get("note_id") == note_id or note_id in url:
                        if "xsec_token" in url:
                            return url, path       # 带 token 的直接最优
                        best = best or (url, path)
        except OSError:
            continue
    return best if best else (None, None)


def resolve_note_ref(token, raw_dir=None):
    """token（完整 URL/短链/裸 note_id）-> (opencli 引用 URL, note_id)。

    完整 URL（explore/search_result 形，含 xsec_token）与短链直接透传；
    裸 note_id 回 <raw_dir>/*.jsonl 反查采集时记录的完整 URL——查不到抛
    NoteUrlNotFound（str(e) 已含可用 raw 文件清单）。
    """
    t = token.strip()
    m = NOTE_ID_RE.search(t)
    if m:
        ref = t if t.startswith("http") else ("https://" + t.lstrip("/"))
        return ref, m.group(1)
    if t.startswith("http") or "xhslink" in t:      # 短链等，无 id 可提
        slug = re.sub(r"[^0-9A-Za-z_-]+", "", t.split("//", 1)[-1])[:24]
        return t, "note_%s" % (slug or datetime.now().strftime("%H%M%S"))
    url, _src = lookup_note_url(t, raw_dir)
    if not url:
        raise NoteUrlNotFound(t, raw_dir)
    return url, t


def reclaim_fallback_dir(out_dir, note_id, raw_dir):
    """--output 未生效时的兜底回收：opencli download 缺省落 cwd 下
    ./xiaohongshu-downloads/<note_id>/（cwd 已钉在 out_dir，位置可控），
    把该目录里的媒体文件挪进 raw_dir 统一处理，重名自动加 _N 后缀，
    最后清掉空壳目录。
    """
    fb = Path(out_dir) / "xiaohongshu-downloads" / note_id
    if not fb.is_dir():
        return
    for p in fb.rglob("*"):
        if not p.is_file():
            continue
        dst = raw_dir / p.name
        i = 1
        while dst.exists():
            dst = raw_dir / ("%s_%d%s" % (p.stem, i, p.suffix))
            i += 1
        raw_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(dst))
    shutil.rmtree(fb, ignore_errors=True)


def write_manifest(note_dir, manifest):
    note_dir.mkdir(parents=True, exist_ok=True)
    path = note_dir / "manifest.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


def process_note(token, out_dir, max_images):
    """处理单条笔记: 解析引用 -> 下载 -> 压缩 -> manifest。

    返回 (manifest, state)；state ∈ ok|failed|needs_login。
    """
    out_dir = Path(out_dir)
    try:
        note_ref, note_id = resolve_note_ref(token)
    except NoteUrlNotFound as e:
        # 裸 id 回查失败：明确报错（含可用 raw 文件清单）并记 manifest
        note_id = token.strip()
        manifest = {
            "note_id": note_id,
            "type": "unknown",
            "files": [],
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "error": "note_url_not_found",
        }
        print("[media] %s" % e, file=sys.stderr)
        write_manifest(out_dir / note_id, manifest)
        return manifest, "failed"
    note_dir = out_dir / note_id
    raw_dir = note_dir / "raw"
    manifest = {
        "note_id": note_id,
        "type": "unknown",
        "files": [],
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "source_url": note_ref,
    }

    raw_dir.mkdir(parents=True, exist_ok=True)
    rc, out, err = run_download(note_ref, raw_dir, cwd=out_dir)
    blob = ((err or "") + " " + (out or "")).strip()
    if rc != 0:
        if rc in RC_NEEDS_LOGIN or "AUTH_REQUIRED" in blob:
            manifest["error"] = "auth_required"
            return manifest, "needs_login"
        manifest["error"] = "download_failed rc=%s: %s" % (rc, blob[:200])
        write_manifest(note_dir, manifest)
        return manifest, "failed"
    reclaim_fallback_dir(out_dir, note_id, raw_dir)

    # 文件清单 = 输出行解析 ∪ 目录扫描（去重，保序）
    files, seen = [], set()
    for p in [Path(p) for p in extract_files_from_output(out)] + \
            scan_media_files(raw_dir):
        if p not in seen:
            seen.add(p)
            files.append(p)
    images = [f for f in files if f.suffix.lower() in IMAGE_EXTS]
    videos = [f for f in files if f.suffix.lower() in VIDEO_EXTS]

    for i, src in enumerate(images[:max(1, max_images)], 1):
        dst = note_dir / ("img_%03d.jpg" % i)
        try:
            compress_image(src, dst)
            manifest["files"].append(dst.relative_to(note_dir).as_posix())
        except Exception as e:
            print("[media] %s 图片压缩失败跳过 %s: %s" % (
                note_id, src.name, e), file=sys.stderr)

    if videos:
        manifest["videos"] = [v.relative_to(note_dir).as_posix() for v in videos]

    if manifest["files"] and videos:
        manifest["type"] = "mixed"
    elif manifest["files"]:
        manifest["type"] = "image"
    elif videos:
        manifest["type"] = "video"
    else:
        manifest["error"] = "no_media_files rc=0 out=%s" % (out or "")[:200]

    # raw 清理：图片已压缩不再需要；视频保留在 raw/ 供 asr.py 转写
    if not videos:
        shutil.rmtree(raw_dir, ignore_errors=True)
    else:
        for src in images:
            try:
                src.unlink()
            except OSError:
                pass

    write_manifest(note_dir, manifest)
    state = "ok" if manifest["type"] != "unknown" else "failed"
    if manifest["type"] == "video":
        print("[media] %s 是视频笔记，转写请跑: python asr.py --video <该笔记 "
              "raw/ 下的视频文件>" % note_id, file=sys.stderr)
    return manifest, state


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="下载小红书笔记图片并压缩（写 <out-dir>/<note_id>/manifest.json）")
    ap.add_argument("--note-ids", required=True,
                    help="逗号分隔笔记引用：完整 URL（含 xsec_token）或裸 note_id"
                         "（回查 raw jsonl 反查完整 URL）")
    ap.add_argument("--max-images-per-note", type=int, default=5,
                    help="每篇最多压缩保留图片数（默认 5）")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                    help="输出目录（默认 RENT_ASSIST_DATA 下的 media/）")
    args = ap.parse_args()

    tokens = [t.strip() for t in args.note_ids.split(",") if t.strip()]
    if not tokens:
        print("[media] --note-ids 为空", file=sys.stderr)
        sys.exit(2)
    if not ensure_pillow():
        sys.exit(2)
    # 钉成绝对路径：download 的 --output/cwd 都基于它，落盘与运行目录无关
    args.out_dir = args.out_dir.resolve()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    ok = failed = 0
    n_images = n_videos = 0
    for idx, tok in enumerate(tokens):
        if idx > 0:
            time.sleep(random.uniform(*SLEEP_RANGE))
        try:
            manifest, state = process_note(tok, args.out_dir,
                                           args.max_images_per_note)
        except Exception as e:  # 单笔记失败不崩，跳过并继续
            print("[media] 第 %d 条(%s) 处理失败跳过: %s" % (
                idx + 1, tok, e), file=sys.stderr)
            failed += 1
            continue
        if state == "needs_login":
            print("[media] 小红书未登录或登录态已失效（download 被登录墙拦截）。",
                  file=sys.stderr)
            print("[media] 请运行: python ensure_auth.py --platform xhs", file=sys.stderr)
            sys.exit(3)
        if state == "ok":
            ok += 1
            n_images += len(manifest["files"])
            n_videos += len(manifest.get("videos") or [])
        else:
            failed += 1

    print("[media] fetched=%d/%d images=%d videos=%d dir=%s" % (
        ok, ok + failed, n_images, n_videos, args.out_dir))
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
