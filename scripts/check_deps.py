#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rent-assist: 外部依赖检查。

检查 agent-reach 是否安装、xiaohongshu / exa_search 两个后端是否可用。
全部可用退出码 0，任一不可用或 agent-reach 缺失退出码 2（附安装指引）。

xiaohongshu 探测口径：doctor status=ok 不代表真实搜索可用（历史假阳性：
doctor ok 但真实采集 exit 3 / opencli 69）。故 xiaohongshu 在 doctor ok 时
再跑一次 auth_common 的真实搜索探测（opencli xiaohongshu search，limit=1），
按其真实退出码归类: 0=ok; 69=需先打开 Chrome（扩展启用）再重试;
77=需 opencli xiaohongshu login; 127=opencli 未装。真实探测超过 60s 视为
"无法确认"降级提示（不阻塞其他源检查、不改变该源判定）。

用法:
    python check_deps.py
"""

import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth_common as ac  # noqa: E402

TIMEOUT = 120
XHS_PROBE_TIMEOUT = 60   # 真实探测超过 60s 按"无法确认"降级，不阻塞
REQUIRED = ("xiaohongshu", "exa_search")


def probe_xhs_real():
    """跑 auth_common 的 xhs 真实搜索探测（带 XHS_PROBE_TIMEOUT 秒兜底）。

    返回 (probe_status, message)；探测超时/线程异常返回 ("probe_timeout", ...)。
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(ac.xhs_probe_status)
        try:
            status, rc, blob = fut.result(timeout=XHS_PROBE_TIMEOUT)
        except Exception as e:  # TimeoutExpired 及其他异常一律降级
            return "probe_timeout", "真实探测超时/异常(%s)" % type(e).__name__
    if status == "ok":
        return "ok", "真实搜索探测通过 (opencli exit 0)"
    if status == "browser_missing":
        return status, ("真实搜索探测失败 (opencli exit %d)："
                        "先打开 Chrome 浏览器（扩展启用）再重试" % rc)
    if status == "auth_required":
        return status, ("真实搜索探测失败 (opencli exit %d)："
                        "需执行 opencli xiaohongshu login" % rc)
    if status == "opencli_missing":
        return status, "opencli 未安装（随 agent-reach install --system 安装）"
    return status, "真实搜索探测退出码 %d：%s" % (rc, blob[:200])


def print_install_guide(reason):
    print("[check_deps] 依赖检查失败: %s" % reason, file=sys.stderr)
    print()
    print("=" * 62)
    print("依赖安装指引")
    print("=" * 62)
    print()
    print("1. 安装 Agent-Reach（本技能所有采集的统一入口）:")
    print("   从 GitHub 仓库安装: https://github.com/Panniantong/Agent-Reach")
    print("   按仓库 README 完成安装后，执行一次: agent-reach install --system")
    print()
    print("   [警告] PyPI 上的同名包 agent-reach 是冒名包，与上述项目无关。")
    print("   严禁使用 pip install agent-reach 安装，请务必从 GitHub 仓库安装。")
    print()
    print("2. 小红书采集额外依赖 OpenCLI（随 Agent-Reach --system 安装）:")
    print("   a. 确认 opencli 命令可用（agent-reach install --system 会一并处理）;")
    print("   b. 按其文档安装配套的 Chrome 扩展（用于复用浏览器登录态）;")
    print("   c. 首次使用前执行一次: opencli xiaohongshu login")
    print("      （会打开 Chrome 引导登录小红书；登录态过期时重新执行即可。）")
    print()
    print("3. 全网搜索（Exa）经 mcporter 调用，随 Agent-Reach 一并配置，")
    print("   免费且无需 API Key，通常装好 Agent-Reach 即可用。")
    sys.exit(2)


def main():
    print("[check_deps] 数据目录: %s" % ac.data_dir())
    print("[check_deps] 工具目录: %s" % ac.tools_dir())
    kp = ac.keys_env_path()
    print("[check_deps] 密钥文件: %s%s" % (kp, "" if kp.is_file() else "（缺失，可选：高德/Jina key）"))
    exe = shutil.which("agent-reach")
    if exe is None:
        print_install_guide("未找到命令 agent-reach（可能未安装或不在 PATH 中）")

    try:
        r = subprocess.run(
            [exe, "doctor", "--json"], capture_output=True, timeout=TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print_install_guide("执行 agent-reach doctor --json 失败: %s" % e)

    if r.returncode != 0:
        err = r.stderr.decode("utf-8", errors="replace").strip()
        print_install_guide(
            "agent-reach doctor --json 退出码 %d%s"
            % (r.returncode, (": " + err[:300]) if err else "")
        )

    try:
        data = json.loads(r.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        print_install_guide("doctor 输出不是合法 JSON: %s" % e)

    if not isinstance(data, dict) or any(k not in data for k in REQUIRED):
        print_install_guide("doctor 输出缺少 xiaohongshu / exa_search 字段")

    all_ok = True
    for key in REQUIRED:
        item = data.get(key) or {}
        status = str(item.get("status", "unknown"))
        backend = str(item.get("active_backend", "") or "-")
        message = str(item.get("message", "") or "")
        ok = status == "ok"

        # xiaohongshu：doctor ok 只是弱口径，再跑真实搜索探测定真伪
        # （doctor 本身不可用时保持原判定与提示，不额外探测）。
        if key == "xiaohongshu" and ok:
            probe_status, probe_msg = probe_xhs_real()
            if probe_status == "ok":
                message = probe_msg
            elif probe_status == "probe_timeout":
                # 真实探测太慢（>60s）：降级"无法确认"，不阻塞其他源检查
                message = ("doctor ok 但真实探测超时(>%ds)，无法确认可用性，"
                           "不阻塞本次检查" % XHS_PROBE_TIMEOUT)
                print("              [!] %s" % message, file=sys.stderr)
            elif probe_status == "unknown":
                ok = False
                message = probe_msg
                print("              [!] %s" % message, file=sys.stderr)
            else:
                ok = False   # browser_missing / auth_required / opencli_missing
                message = probe_msg
        all_ok = all_ok and ok
        print("%-13s status=%-8s backend=%s" % (key, status, backend))
        if message:
            print("              message: %s" % message)
        if not ok:
            print("              [!] 不可用，请参照上方安装指引排查", file=sys.stderr)

    if all_ok:
        print("[check_deps] 全部依赖正常 (xiaohongshu + exa_search)")
        sys.exit(0)
    print("[check_deps] 存在不可用依赖，退出码 2", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
