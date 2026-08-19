# 租租房 rent-assist

给 [Claude Code](https://claude.com/claude-code) 的国内租房全能助手 skill。你用一句白话提问——"天通苑怎么样""国贸上班预算 3000 住哪合适""押金一般押几付几"——它路由到对应工作流，去小红书 / 豆瓣 / 网页 / 抖音采集**公开口碑帖**，清洗、语义分析、配高德地图，最后产出一份可视化报告。

报告不是聊天记录的复述：每条结论带可点击的原帖溯源链接，发布时间全量展示，老证据自动降权。

![门面页](docs/landing-desktop.png)

![报告页](docs/report-desktop.png)

## 能回答什么（五类意图）

| 意图 | 示例问法 | 产出 |
|---|---|---|
| A 小区综合了解 | "天通苑怎么样""这家中介靠谱吗" | 尽调报告：八维评分 + 关键发现 + 房源价格 + 好评精选 |
| B 推荐评价 | "XX公寓值得租吗""A 和 B 选哪个" | 推荐报告：一句话结论 + 适合人群 + 正反证据 |
| C 选址建议 | "在国贸上班预算 3000 住哪合适" | 选址报告：候选片区对比排名 + 通勤测算 + 地图 |
| D 找转租房源 | "XX附近有没有个人转租" | 房源列表：近 7 天转租帖卡片流（整卡可点开原帖）+ 防骗提示 |
| E 租房咨询 | "押金押几付几""签合同注意什么" | 秒回直答 + FAQ 报告（不采集，基于四阶段核对清单） |

中介 / 房东标的还会另查 12315 投诉记录。超大片区（如天通苑）自动下钻拆子小区逐个对比。

## 工作原理

```
用户提问 → 意图路由 + 五元组提取
  → run_collect.py 批次编排（断点续采 / 7天复用闸门 / 频控）
      ├─ 小红书  opencli（agent-reach）
      ├─ 豆瓣    HTTP 直连 + Playwright 兜底（登录态持久化）
      ├─ 网页    Exa 搜索 + Jina 正文（免 key 可用）
      ├─ 抖音    MediaCrawler（可选口播转写：视频→sherpa-onnx ASR→并入正文）
      └─ 12315   投诉直查（仅中介/房东标的）
  → clean.py 清洗（去重 / 广告过滤 / 求租帖剔除 / 城市消歧 / 8 类风险粗分类 / 老帖标记）
  → Claude 只读清洗后数据做语义分析（analysis.json）
  → geocode.py 高德地理层（定位 / 周边配套 / 通勤路线，全量缓存）
  → render.py 渲染 HTML 报告（五种模式，375px 手机 / 1280px 桌面双端适配）
```

设计要点：

- **双层防御**：搜索词按意图选视角（口碑词 / 供给侧词）挡求租帖，clean.py 兜底再剔一层；同手机号引流帖在分析层识破。
- **时效铁律**：>24 个月的老帖自动降权一档，>48 个月只作背景参考；新旧证据冲突时近期优先并说明变化。
- **诚实边界**：不爬贝壳 / 链家挂牌数据（反爬 + 判例风险），不做全网比价；无数据 ≠ 安全，报告仅为公开舆情聚合，不构成决策依据。
- **频控红线**：每批每平台 ≤20 帖、批间随机间隔 10-30s、同标的 7 天内复用缓存。定位为个人低频自用工具，禁止批量爬取。

## 安装

前置要求：

- [Claude Code](https://claude.com/claude-code) + Python 3.11+
- [agent-reach](https://github.com/Panniantong/Agent-Reach)（`agent-reach install --system`；**PyPI 同名包是冒名包，不要 pip install**），小红书源需要其中 OpenCLI + Chrome 扩展并 `opencli xiaohongshu login` 登录一次
- 可选：[MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)（抖音源）、Jina key（提升网页源限额）

```bash
# Windows
git clone https://github.com/daxueren666/租租房 "%USERPROFILE%\.claude\skills\rent-assist"

# macOS / Linux
git clone https://github.com/daxueren666/租租房 ~/.claude/skills/rent-assist

pip install -r ~/.claude/skills/rent-assist/requirements.txt
```

密钥自备（本仓库不含任何 key）：新建 `<数据盘>/config/keys.env`（或 `~/.rent-assist/keys.env`），按需填：

```ini
AMAP_WEB_KEY=...        # 高德 Web REST，地理层用（申请见 references/amap-api.md）
AMAP_JSAPI_KEY=...      # 地图渲染用；不填则报告自动降级为纯文字版
AMAP_JSAPI_SECRET=...
JINA_API_KEY=...        # 可选
```

豆瓣 / 抖音首次使用需扫码登录一次（登录态落盘后续免扫），排障见 `references/auth.md`。

装好后对 Claude 说任意租房问题即可，skill 自动触发；`python scripts/check_deps.py` 可先做依赖探测。

另有本地门面页（`scripts/serve_home.py`，手机同 WiFi 提交问题、报告生成自动弹出），可选。

## 目录结构

```
SKILL.md               # 工作流正本（五意图路由 / 采集矩阵 / 分析 schema / 纪律）
scripts/               # 采集·清洗·地理·渲染·测试全部脚本（纯 Python，CLI 可单跑）
templates/             # 报告 Jinja2 模板 + 门面页
references/            # 检索词库 / 风险八维词典 / 看房四阶段清单 / 高德 API 速查 / 登录排障
evals/                 # 路由 evals（17 例 desk-check 运行器，零依赖）+ 触发测试
```

## 开发与测试

- 7 套离线自检（`scripts/test_*_offline.py`：asr / auth / clean / intent / media / reuse / web），不联网不采集
- 路由 evals：`python evals/run_routing_evals.py`（17/17 通过；静态规则表 desk-check，非 LLM 评测）
- 模板基线检查：`python scripts/test_render_check.py`（改报告模板前后必跑）

## 已知限制

- 开发环境为 Windows，部分第三方工具路径（MediaCrawler、ASR venv）默认指向 `E:\租房\tools\`，换机器需按需调整
- 数据目录默认 `E:\租房\data`，可用环境变量 `RENT_ASSIST_DATA` 重定向
- 小红书 / 豆瓣 / 抖音依赖登录态与平台风控，失败时按源降级并在报告 coverage 中说明，不阻塞其他源

## License

[MIT](LICENSE)
