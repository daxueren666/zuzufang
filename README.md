# 租租房 rent-assist

> 一个通用 Agent Skill：一句白话提问，它把公开口碑扒清楚，出一份带地图、能点回原帖的可视化报告。

![Agent Skill](https://img.shields.io/badge/Agent%20Skill-开放格式-blue) ![PolyForm NC](https://img.shields.io/badge/license-PolyForm%20NC-orange) ![Python](https://img.shields.io/badge/python-3.11+-blueviolet)

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

## 可选配置

**地图（高德 key，免费）**——不填则报告自动降级为纯文字版，其余功能不受影响：

1. 注册 [lbs.amap.com](https://lbs.amap.com)，完成个人实名认证
2. 控制台 → 应用管理 → 创建新应用 → 添加两个 Key：服务平台分别选「Web 服务」和「Web 端（JS API）」（JS API 会同时给一个安全密钥）
3. 写入 `~/.rent-assist/keys.env`（KEY=VALUE 纯文本，与 skill 目录分离）：

```ini
AMAP_WEB_KEY=Web服务的Key
AMAP_JSAPI_KEY=Web端JS_API的Key
AMAP_JSAPI_SECRET=JS_API的安全密钥
JINA_API_KEY=可选，jina.ai/reader 免费申请，提升网页源采集限额
```

**首次使用**：部分数据源需扫码登录一次，之后免扫；排障见 `references/auth.md`。

**数据与隐私**：数据、密钥、登录态全部落在本机，不上传——默认 `~/.rent-assist/`，可用环境变量 RENT_ASSIST_DATA / RENT_ASSIST_TOOLS / RENT_ASSIST_KEYS 改位置，`check_deps.py` 会打印实际位置。开发环境为 Windows。

使用中遇到问题：[开个 issue](https://github.com/daxueren666/zuzufang/issues)。

## 边界（必读）

只聚合公开口碑帖：这是"舆情参考"，不是"官方结论"。**无数据 ≠ 安全**，报告不构成决策依据，看房前请按报告里的核对清单实地核验。定位为个人自用工具，请低频使用，禁止批量爬取。**本 skill 仅限个人使用，不允许商业使用。**

## License

[PolyForm Noncommercial 1.0.0](LICENSE)——个人使用免费，禁止商业使用。
