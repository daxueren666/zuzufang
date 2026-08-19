# 三平台登录态管理（rent-assist）

统一入口：`python scripts/ensure_auth.py --platform xhs|douyin|douban|all [--timeout 600]`

设计目标：**扫码必须等到成功，不能一闪而过；cookie 要持久化，不能让用户反复扫码。**

---

## 1. 各平台登录机制

### 小红书（xhs）
- 通道：opencli（Agent-Reach）+ Chrome 扩展，复用 Chrome 里的小红书主站登录态。
- **缓存态 ≠ 真实可用**：`opencli xiaohongshu login/whoami` 查的是创作者中心缓存，
  可能 `already_logged_in` 秒退，但主站搜索仍 AUTH_REQUIRED。因此本项目一律用
  **真实探测**：`opencli xiaohongshu search "租房" -f json --limit 1`（一次轻搜索）。
- 真实退出码：`0` 成功；`69` BROWSER_CONNECT（未连上 Chrome，先开 Chrome）；
  `77` AUTH_REQUIRED（主站未登录，需扫码）；`127` opencli 未安装。
- 登录引导（ensure_auth --platform xhs）：探测 77 → 自动 `cmd start chrome` 打开
  主站 → 终端打印指引 → **每 20s 轮询探测直到成功或超时（默认 600s）**，
  成功打印"登录成功"并写 auth_state.json。
- collect_xhs.py 开跑前同样做一次真实探测；需登录时退出码 **3**（需登录），
  不再笼统 exit 2。

### 豆瓣（douban）
- 两级采集：级别1 requests 匿名抓列表（通常免登录）；级别2 Playwright 兜底。
- 探测：HTTP GET `https://www.douban.com/group/search?cat=1019&q=test`（1 次轻请求）。
  `200` 匿名可用；`403` 需登录态。
- **登录态 = douban-profile 持久化目录**（存在且非空=有档案）：级别2 用
  `launch_persistent_context(user_data_dir=<tools>/douban-profile)`，已有
  档案先 headless 静默试一次列表页（15s），通过则全程无头不弹窗；通不过且交互
  终端才 headless=False 弹窗人工登录（cookie 落盘，后续免登录；滑块人工介入
  保留）。备选：`DOUBAN_COOKIE` 环境变量（`'bid=xx; ...'` 或 JSON 数组格式）。
- 失效特征 = 列表 403 + 滑块频发；恢复 = 前台跑一次
  `python scripts/collect_douban.py --query test --limit 1`
  （或 `ensure_auth --platform douban`）。非交互环境需登录时 collect_douban.py
  **exit 3**（级别1结果已落盘）。

### 抖音（douyin）
- 通道：MediaCrawler（`--lt qrcode`），登录态缓存持久化在
  `<tools>/MediaCrawler/browser_data/dy_user_data_dir`。
- 探测：检查该目录存在且非空。有缓存 = 复用登录态；无缓存 = 首跑
  collect_douyin.py 弹可见浏览器扫码，**一次长期有效**。
- collect_douyin.py 跑前打印缓存状态；跑完若生成缓存则更新 auth_state.json。

---

## 2. 何时需要再扫码（cookie 过期特征）

| 平台 | 过期特征 | 处置 |
|------|----------|------|
| xhs  | collect_xhs.py / ensure_auth 探测 exit 77（AUTH_REQUIRED） | `python scripts/ensure_auth.py --platform xhs` 扫码 |
| douban | 列表搜索 403；或级别2 滑块/验证码频发、正文被拦过半（非交互时 exit 3） | 前台跑一次 collect_douban.py 触发级别2 重新登录（或换 DOUBAN_COOKIE） |
| douyin | 搜索结果为空且 MediaCrawler 日志提示需要登录 | 重跑 collect_douyin.py，MediaCrawler 会自动再弹扫码 |

auth_state.json 里的 `ok: false` / `reason` 字段也会给出线索。

---

## 3. ensure_auth.py 用法

```
python scripts/ensure_auth.py --platform xhs      # 探测→开Chrome登录页→轮询等扫码（不秒退）
python scripts/ensure_auth.py --platform douban   # 匿名探测 + 指引
python scripts/ensure_auth.py --platform douyin   # 登录缓存探测
python scripts/ensure_auth.py --platform all      # 三平台只检查不引导，打印三行状态表
python scripts/ensure_auth.py --platform xhs --timeout 600
```

退出码：`0` 就绪；`2` 环境问题（opencli 未装/网络异常）；`3` 需要登录
（未登录/扫码等待超时/Chrome 未开/无抖音缓存/豆瓣 403）。

采集脚本退出码联动：**collect_xhs.py / collect_douban.py exit 3 = 需登录**（0 正常 /
2 无结果 / 3 需登录；xhs 跑 ensure_auth，douban 前台跑一次 collect_douban 后重试）。

---

## 4. auth_state.json 说明

位置：`<data>/auth_state.json`（原子写，UTF-8；auth_common.data_dir() 解析，RENT_ASSIST_DATA 可覆盖；--parallel 下各 worker 写隔离目录不回写主 state）。记录各平台**最近一次实测**
结果，供排障与状态汇总展示；它不是信任凭据——采集脚本每次开跑仍做真实探测。

```json
{
  "xhs":     { "ok": true,  "verified_at": "2026-08-15T12:00:00", "probe": "opencli_search" },
  "douban":  { "ok": true,  "verified_at": "2026-08-15T12:00:00", "mode": "anonymous" },
  "douyin":  { "ok": false, "verified_at": "2026-08-15T12:00:00", "cache": false }
}
```

字段：`ok` 实测是否可用；`verified_at` 最近实测时间；其余为平台附加信息
（xhs `probe`、douban `mode: anonymous|browser_profile|cookie_env` 与失败
`reason`、douyin `cache`）。共享读写工具在 `scripts/auth_common.py`。
