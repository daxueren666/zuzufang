# 高德开放平台 API 速查（M3 地图版开发用）

> 本 skill 在 M3 需要**两个不同类型的 Key**：REST 通勤分析（geocode.py 用 Web 服务 Key）、
> 报告内嵌地图（report.html.j2 用 Web 端 JS API Key + 安全密钥）。两者不可混用。

## 一、Key 申请流程

1. 注册高德开放平台账号：https://lbs.amap.com ，完成**个人实名认证**（否则无免费配额或配额极低）。
2. 进入控制台 → 应用管理 → 我的应用 → 创建新应用（名称如 rent-assist）。
3. 在应用下"添加 Key"，服务平台分别选择：
   - **Web 服务**：供 geocode.py 调 REST API，得到 Key（纯 Key，无安全密钥）。
   - **Web 端（JS API）**：供报告 HTML 内嵌地图，得到 Key + **安全密钥 securityJsCode**（2021-12-02 之后申请的 JS API Key 必须配合安全密钥，否则报 INVALID_USER_SCODE）。
4. 配置到 `~/.rent-assist/keys.env`（KEY=VALUE 文本文件，位于用户主目录、skill 目录外；脚本优先读同名环境变量，回退读该文件）：
   - `AMAP_WEB_KEY` = Web 服务 Key，只供 geocode.py 本地调 REST，绝不进报告。
   - `AMAP_JSAPI_KEY` / `AMAP_JSAPI_SECRET` = Web 端（JS API）Key 与安全密钥 securityJsCode，供 render.py 读取并注入报告模板。
   旧变量名 `AMAP_KEY` / `AMAP_JS_KEY` / `AMAP_SECURITY_JS_CODE` 已废弃，脚本不识别，不要再用。
5. 配额在控制台 → 配额管理查看；个人免费配额有限（见下），geocode.py 必须用 cache.db 缓存结果。

## 二、个人免费配额（参考值，以控制台实时数据为准）

| 服务 | 个人认证开发者参考配额 |
|---|---|
| 地理编码 / 逆地理编码 | 约 5000 次/日 |
| 搜索（关键词/周边/ID） | 约 100 次/日 |
| 路径规划（步行/骑行/公交/驾车） | 约 100 次/日 |
| JS API 地图加载 | 约 5000 次/日 |

开发纪律：

- 高德 2021-12 与 2023-03 两次下调个人配额，以上数字仅作参考，**上线前以控制台"配额管理"实测为准**。
- geocode.py 所有请求先查 cache.db（key = 接口+参数），命中不重复请求；同一小区/路线 7 天内复用缓存。
- 意图 C 候选片区通勤分析一次任务通常只需 5-15 次规划请求，配额足够，但禁止循环遍历。

## 三、Web 服务 REST 速查

统一域名 `https://restapi.amap.com`，GET 请求，公共参数 `key`。响应 JSON 中看 `status`（"1"成功）、`infocode`（"10000"成功），失败先查 infocode。

### 1. 地理编码（地址 → 坐标）

```
GET /v3/geocode/geo?address=北京市昌平区天通苑&city=北京&key=AMAP_WEB_KEY
```

- 返回 `geocodes[0].location` = "经度,纬度"（GCJ-02 坐标系，全平台统一）。
- 小区名直接当 address 可用；city 参数提升准确率。

### 2. 逆地理编码（坐标 → 地址）

```
GET /v3/geocode/regeo?location=116.41,40.07&key=AMAP_WEB_KEY
```

### 3. 关键词搜索（POI 文本检索）

```
GET /v3/place/text?keywords=地铁站&city=北京&offset=10&key=AMAP_WEB_KEY
```

- 返回 `pois[]`：name / address / location / typecode。
- 用途：工作地地标定位（"国贸"→POI→坐标）、片区地铁/商圈配套统计。

### 4. 周边搜索

```
GET /v3/place/around?location=116.41,40.07&radius=1500&types=地铁 stations 分类码&key=AMAP_WEB_KEY
```

- types 用分类编码（如 150500 地铁站、060100 餐饮、090100 商场），逗号分隔可多个。
- 用途：候选小区 1.5km 内配套统计（地铁站/超市/医院数量，做客观维度评分）。

### 5. 步行路线（v3）

```
GET /v3/direction/walking?origin=116.41,40.07&destination=116.43,40.05&key=AMAP_WEB_KEY
```

- 返回 `route.paths[0]`：distance（米）/ duration（秒）/ steps[]。

### 6. 骑行路线（v4，注意版本不同）

```
GET /v4/direction/bicycling?origin=...&destination=...&key=AMAP_WEB_KEY
```

- v4 返回结构不同：`errcode == 0` 成功，数据在 `data.paths[0].distance/duration`。

### 7. 公交/地铁路线（通勤主力，v3 跨城综合）

```
GET /v3/direction/transit/integrated?origin=...&destination=...&city=北京&cityd=北京&key=AMAP_WEB_KEY
```

- 返回 `route.transits[]`：多方案，各含 cost（元）/ duration / walking_distance / segments[]（逐段公交/步行）。
- 通勤分析取 duration 最短与换乘最少两方案，汇总成"约 X 分钟，换乘 Y 次，步行 Z 米"。

### 8. 驾车路线（备选）

```
GET /v3/direction/driving?origin=...&destination=...&key=AMAP_WEB_KEY
```

错误码速查：10000 成功；10001 Key 不存在；10009/10044 配额超限；10009 之外常见 INVALID_USER_SCODE 为 JS API 密钥问题（REST 无此码）。

## 四、JS API 2.0 内嵌要点（report.html.j2）

### 1. 加载与安全密钥（2021 后申请的 Key 必配）

```html
<script type="text/javascript">
  window._AMapSecurityConfig = { securityJsCode: "由 render.py 注入" };
</script>
<script src="https://webapi.amap.com/maps?v=2.0&key=AMAP_JSAPI_KEY&plugin=AMap.Geocoder,AMap.PlaceSearch,AMap.Transfer,AMap.Walking,AMap.Riding"></script>
```

- `_AMapSecurityConfig` 必须在 maps 脚本**之前**定义。
- 安全密钥直接写前端有泄露风险；本 skill 报告为本地单机 HTML，风险可接受，但在报告中注明"本地自用勿公开分发"。若日后部署，改用 nginx 反向代理 `_AMapService` 方案（官方"安全密钥代理转发"）。

### 2. 常用代码骨架

```javascript
const map = new AMap.Map('map-container', { zoom: 13, center: [116.41, 40.07] }); // GCJ-02

// 小区/工作地标注
new AMap.Marker({ position: [lng, lat], title: '天通苑', map });

// 公交通勤路线（Walking / Riding 同构）
AMap.plugin('AMap.Transfer', () => {
  new AMap.Transfer({ city: '北京', map }).search(
    [fromLng, fromLat], [toLng, toLat], (status, result) => {}
  );
});
```

- 坐标一律 GCJ-02；若混入 GPS/WGS-84 数据需先转换，否则偏移数百米。
- 插件也可按需 `AMap.plugin('AMap.Transfer', cb)` 动态加载，不必全列在 script 标签。

### 3. 报告内嵌约定

- 地图容器给固定高度；失败/离线时降级为纯文本通勤描述（M3 前的文本模式保底逻辑保留）。
- render.py 通过 Jinja2 变量注入 key/securityJsCode/坐标与路线数据，模板内不写死任何 Key。
- JS API 地图瓦片需要联网；离线打开报告时地图区块显示占位提示，其余报告内容不受影响。
- M3 模板视觉评审必须过 taste-skill（process.md 里程碑要求）。

## 五、与 geocode.py 的接口约定（M3 实现）

- CLI 建议：`geocode.py --geocode "地址"` / `--around "lng,lat" --types ... --radius 1500` / `--route "from,to" --mode transit|walk|bike|drive`。
- 输出：单行 JSON（供 Claude 直接读，不打印原始响应全文）。
- 缓存：cache.db 表 (endpoint, params_hash, response_json, created_at)，TTL 7 天。
