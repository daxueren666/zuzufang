# 租租房 rent-assist

> 一个通用 Agent Skill：一句白话提问，它把公开口碑扒清楚，出一份带地图、能点回原帖的可视化报告。

![Agent Skill](https://img.shields.io/badge/Agent%20Skill-开放格式-blue) ![MIT](https://img.shields.io/badge/license-MIT-green) ![Python](https://img.shields.io/badge/python-3.11+-blueviolet)

![门面页](docs/landing-desktop.png)

![报告页](docs/report-desktop.png)

## 它能做什么

租房前该搞清楚的事，直接问：

| 你问 | 它给 |
|---|---|
| "天通苑怎么样？" | 尽调报告：八维评分、风险发现、房源价格、好评精选，每条结论都能点回原帖 |
| "这家中介/房东靠谱吗？" | 口碑帖 + 12315 投诉记录交叉验证 |
| "国贸上班预算 3000 住哪合适？" | 3-5 个候选片区对比：通勤、价格、口碑 + 地图 |
| "龙泽苑有个人转租吗？" | 近 7 天转租房源卡片（点开就是原帖）+ 防骗清单 |
| "押金一般押几付几？" | 秒回直答 + 可勾选的行动清单报告 |

2019 年的好评压不过 2026 年的差评：老帖自动降权，每条证据都标发布时间。

## 装好后怎么用

对你的 AI 助手说人话就行。它会追问缺的信息（城市、预算、量级），确认后开始采集分析：

> 帮我查下回龙观龙泽苑的租房口碑，预算 3000，合租

一段时间后出一份 HTML 报告（手机直接看），附一句话结论和"适合什么样的人"。同一小区 7 天内再问，直接复用上次数据，不重采。

## 安装

```bash
git clone https://github.com/daxueren666/zuzufang <你的 skills 目录>/rent-assist

pip install -r <你的 skills 目录>/rent-assist/requirements.txt

# 依赖自检：缺什么、怎么补，它会逐项告诉你
python <你的 skills 目录>/rent-assist/scripts/check_deps.py
```

> 完整功能需要能执行本地命令的 CLI agent（采集要跑 Python、扫码登录）；网页版聊天环境只能把它当租房知识库用。

- 地图功能需自备高德 key（免费申请，方法见 `references/amap-api.md`；不填则报告自动降级为纯文字版，其余功能不受影响）
- 部分数据源首次使用需扫码登录一次，之后免扫（排障见 `references/auth.md`）
- 开发环境为 Windows，换机器遇到路径问题看 SKILL.md「脚本与数据约定」

## 边界（必读）

只聚合公开口碑帖：这是"舆情参考"，不是"官方结论"。**无数据 ≠ 安全**，报告不构成决策依据，看房前请按报告里的核对清单实地核验。定位为个人自用工具，请低频使用，禁止批量爬取。

## License

[MIT](LICENSE)
