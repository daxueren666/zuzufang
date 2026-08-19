# 租租房 rent-assist

Vibe Coding 这个项目，是因为我发现越来越多的人开始通过社交媒体来找租房攻略和信息，但是这些信息又都散落在各个平台里面。所以我索性做了这个 skill：你输入需求，skill 帮你收集真实的用户讨论和房源信息，再也不用各个平台来回跑，最后还给你一份详细的租房报告，手机端电脑端都能查看。

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

## 准备

要用什么就准备什么——缺哪项，对应功能自动跳过并在报告里说明，不阻塞其他功能：

| 功能 | 需要准备 | 一次性操作 |
|---|---|---|
| 基础（采集/清洗/报告） | Python 3.11+，能执行本地命令的 CLI agent（Claude Code、Codex CLI 等） | — |
| 小红书源 | Chrome 浏览器 | 装 Agent-Reach + Chrome 扩展后 `opencli xiaohongshu login`，扫码一次 |
| 豆瓣源 | — | 首次采集会弹浏览器，登录一次，之后免扫 |
| 抖音源 | — | 首次采集用抖音 App 扫码一次，之后免扫 |
| 地图 | 高德 key（免费，申请步骤见下方「可选配置」） | — |

### 装 Agent-Reach（采集的统一入口，一次性）

```bash
# 1) 安装（pipx 优先）
pipx install https://github.com/Panniantong/agent-reach/archive/main.zip

# 2) 先只读检查环境，确认无误后装系统依赖
agent-reach install --env=auto
agent-reach install --env=auto --system
```

**注意：只能用上面的 GitHub 地址安装。PyPI 上的同名包 agent-reach 是冒名包，不要 `pip install agent-reach`。**

<details>
<summary>没有 pipx / macOS 报 PEP 668 / Windows 商店版 Python（点开看对应命令）</summary>

```bash
# macOS / Linux（Homebrew Python 报 externally-managed-environment 时）
python3 -m venv ~/.agent-reach-venv && source ~/.agent-reach-venv/bin/activate
pip install https://github.com/Panniantong/agent-reach/archive/main.zip
agent-reach install --env=auto --system
```

```powershell
# Windows（PowerShell，装的是 Microsoft Store 版 Python 时）
py -3 -m venv $env:USERPROFILE\.agent-reach-venv
$env:USERPROFILE\.agent-reach-venv\Scripts\Activate.ps1
python -m pip install https://github.com/Panniantong/agent-reach/archive/main.zip
agent-reach install --env=auto --system
```

</details>

### 小红书还要两步（其他源不用）

1. Chrome 装 OpenCLI 扩展：打开 [Chrome 应用商店的 OpenCLI 页面](https://chromewebstore.google.com/detail/opencli/ildkmabpimmkaediidaifkhjpohdnifk)，点「添加至 Chrome」（浏览器扩展不允许命令行代装，这是全程唯一的手动步骤）
2. 执行 `opencli xiaohongshu login`：会拉起 Chrome 扫码登录小红书，一次即可；`opencli doctor` 显示 `Extension: connected` 就通了

装完跑下面安装节的 `check_deps.py` 做总验证。

## 安装

```bash
# <你的 skills 目录> = 你所用 agent 的技能目录，见下方说明
git clone https://github.com/daxueren666/zuzufang <你的 skills 目录>/rent-assist

pip install -r <你的 skills 目录>/rent-assist/requirements.txt

# 依赖自检：缺什么、怎么补，它会逐项告诉你
python <你的 skills 目录>/rent-assist/scripts/check_deps.py
```

「你的 skills 目录」= 你所用的 agent 加载技能的文件夹，clone 进去即可被识别：

- Claude Code：`~/.claude/skills/`（Windows：`%USERPROFILE%\.claude\skills\`）
- Codex CLI：`~/.codex/skills/`
- 其他支持 Skills 开放格式的 agent（Gemini CLI、Cursor 等）：见各自文档，格式通用

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

**数据与隐私**：数据、密钥、登录态全部落在本机，不上传——默认 `~/.rent-assist/`，可用环境变量 RENT_ASSIST_DATA / RENT_ASSIST_TOOLS / RENT_ASSIST_KEYS 改位置，`check_deps.py` 会打印实际位置。开发环境为 Windows。

使用中遇到问题：[开个 issue](https://github.com/daxueren666/zuzufang/issues)。

## 边界（必读）

只聚合公开口碑帖：这是"舆情参考"，不是"官方结论"。**无数据 ≠ 安全**，报告不构成决策依据，看房前请按报告里的核对清单实地核验。定位为个人自用工具，请低频使用，禁止批量爬取。**本 skill 仅限个人使用，不允许商业使用。**

## License

[PolyForm Noncommercial 1.0.0](LICENSE)——个人使用免费，禁止商业使用。
