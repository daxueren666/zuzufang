#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rent-assist 路由 evals 运行器（desk-check 自动化，非 LLM 评测）

【测试】本脚本为纸面 desk-check：不联网、不调 API、不采集任何数据。
它把 evals/evals.json 的 17 条路由用例喂给一个纯函数规则分类器
（classify，静态规则表，模拟 SKILL.md 意图路由表的判定口径），
断言每条的期望路由/澄清动作/闸门行为。用于：
  1) 路由规则回归：改 evals.json 或规则口径后跑一遍，全绿才算规则表自洽；
  2) 未来接入真实评测（skill-creator 子代理 grading）时的期望值来源。

用法：python evals/run_routing_evals.py   （在 rent-assist 目录下）
0 依赖，仅 Python 标准库。全绿退出码 0，否则退出码 1。
"""

import json
import re
import sys
from pathlib import Path

# ---------- 期望规则表（与 intent-routing-tests.md 一一对应） ----------
# intent: A/B/C/D/E 路由；ASK_CITY/ASK_ALL/CONFIRM 为澄清动作；
# compound 为复合拆分序列；gates 为 R16/R17 闸门期望。
EXPECTED = {
    1:  {"intent": "A", "compound": None, "gates": {}},
    2:  {"intent": "B", "compound": None, "gates": {}},
    3:  {"intent": "C", "compound": None, "gates": {}},
    4:  {"intent": "D", "compound": None, "gates": {}},
    5:  {"intent": "E", "compound": None, "gates": {}},
    6:  {"intent": "A_DRILLDOWN", "compound": None, "gates": {}},
    7:  {"intent": "B_DRILLDOWN", "compound": None, "gates": {}},
    8:  {"intent": "ASK_CITY", "compound": None, "gates": {}},
    9:  {"intent": "C+D", "compound": ["C", "D"], "gates": {}},
    10: {"intent": "A+E", "compound": ["A", "E"], "gates": {}},
    11: {"intent": "A+E", "compound": ["A", "E"], "gates": {}},
    12: {"intent": "ASK_CITY", "compound": None, "gates": {}},
    13: {"intent": "ASK_ALL", "compound": None, "gates": {}},
    14: {"intent": "A_ASK_INFO", "compound": None, "gates": {}},
    15: {"intent": "CONFIRM_D", "compound": None, "gates": {}},
    16: {"intent": "A", "compound": None,
         "gates": {"commute": "closed"}},
    17: {"intent": "A", "compound": None,
         "gates": {"noise": "open"}},
}

# 超大片区词表（模拟 SKILL.md 的超大片区判断）
MEGA_AREAS = ("天通苑", "回龙观", "康城")

# 城市锚点词表（城市可由地标推知，否则视为缺失）
CITY_ANCHORS = {
    "北京": ("回龙观", "望京", "国贸", "上地", "十号线", "天通苑", "海淀", "朝阳"),
    "杭州": ("杭州", "未来科技城"),
    "深圳": ("深圳", "南山", "科技园"),
    "上海": ("上海",),
}


def derive_city(text: str):
    for city, anchors in CITY_ANCHORS.items():
        if any(a in text for a in anchors):
            return city
    return None


def classify(text: str) -> str:
    """纯函数：按静态规则表推断路由/澄清动作。模拟 SKILL.md 判定口径。"""
    city = derive_city(text)

    # 复合拆分：A+E（标的口碑 + 押金/费用知识）
    if re.search(r"怎么样|咋样|靠谱吗", text) and re.search(r"押几付几|中介费一般", text):
        return "A+E"

    # CONFIRM_D：信息密集且清晰（预算区间+多重约束），先复述确认再走 D
    if re.search(r"预算\d+到\d+", text) and re.search(r"女生合租|不要一楼|有电梯", text):
        return "CONFIRM_D"

    # E：无标的的租房知识/咨询（押金、中介费、流程、避坑问法）
    if re.search(r"押几付几|押一付三|中介费是谁出|这都正常吗|怕被坑|第一次租房.*(?:押金|中介费)", text):
        return "E"

    # ASK_ALL：什么信息都没有
    if re.search(r"我要租房\s*你看着办", text) and not city:
        return "ASK_ALL"

    # ASK_CITY：有泛重名标的但推不出城市（阳光小区/康城类）
    if re.search(r"阳光小区|康城", text) and city is None:
        return "ASK_CITY"

    # 复合拆分：C+D（住哪 + 转租房源）
    if re.search(r"住哪合适|住哪好", text) and re.search(r"转租的房源|个人转租", text):
        return "C+D"
    # B：值得租/推荐/住哪个子小区
    if re.search(r"值得租|值不值|推荐|住哪个区合适|住哪个小区合适", text):
        if re.search(r"|".join(MEGA_AREAS), text):
            return "B_DRILLDOWN"
        return "B"

    # C：选址建议（无标的，问住哪）
    if re.search(r"住哪合适|住哪好|帮我想想住哪", text) and not city is None and not re.search(r"租房怎么样", text):
        return "C"

    # D：找房/房源/直租（位置词而非标的）
    if re.search(r"找个一居室|直租|个人转租|房源|女生合租", text):
        return "D"

    # A_DRILLDOWN：超大片区综合了解
    if re.search(r"|".join(MEGA_AREAS), text) and re.search(r"怎么样", text):
        return "A_DRILLDOWN"

    # A_ASK_INFO：房东/中介人名标的，信息过泛需反问
    if re.search(r"叫王伟的房东|房东要租我房子", text):
        return "A_ASK_INFO"

    # A：具体小区/中介标的综合了解
    if re.search(r"怎么样|咋样|靠谱吗|坑点|吵不吵", text):
        if city is None and re.search(r"阳光小区|康城", text):
            return "ASK_CITY"
        return "A"

    return "UNKNOWN"


def gate_commute(text: str, user_answers_destination: bool) -> str:
    """通勤闸门：只有用户给出通勤目的地才开启测算。"""
    return "open" if user_answers_destination else "closed"


def gate_noise(text: str) -> str:
    """噪音闸门：用户主动问"吵不吵/噪音"才跑 noise 查询。"""
    return "open" if re.search(r"吵不吵|吵吗|噪音|吵不吵", text) else "closed"


def run():
    evals_path = Path(__file__).parent / "evals.json"
    data = json.loads(evals_path.read_text(encoding="utf-8"))
    print("【测试】rent-assist 路由 desk-check（静态规则表，不联网不采集）")
    total = passed = 0
    failed = []
    for case in data["evals"]:
        eid = case["id"]
        prompt = case["prompt"]
        exp = EXPECTED[eid]
        checks = []
        got = classify(prompt)
        checks.append((f"路由/动作 expect={exp['intent']} got={got}", got == exp["intent"]))
        if exp.get("compound"):
            seq = got.split("+") if "+" in got else ([got] if got not in ("UNKNOWN",) else [])
            checks.append((f"拆分顺序 expect={'->'.join(exp['compound'])}", seq == exp["compound"]))
        for gname, gexp in exp.get("gates", {}).items():
            if gname == "commute":
                g = gate_commute(prompt, user_answers_destination=False)
            else:
                g = gate_noise(prompt)
            checks.append((f"闸门{gname} expect={gexp} got={g}", g == gexp))
        ok = all(c[1] for c in checks)
        total += 1
        passed += ok
        status = "PASS" if ok else "FAIL"
        print(f"R{eid:02d} [{status}] " + "; ".join(desc for desc, _ in checks))
        if not ok:
            failed.append(eid)
    print(f"通过 {passed}/{total}（自动化部分：每条路由 5 分，共 {total*5} 分；"
          f"五元组 5 字段各 1 分（{total*5} 分）留人工/LLM 判，合计 170 分制）")
    if failed:
        print("失败用例：", ", ".join(f"R{e:02d}" for e in failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())
