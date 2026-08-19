# -*- coding: utf-8 -*-
"""rent-assist 词库与正则（纯数据，stdlib only）。

clean.py 粗分类正本：8 类风险词、广告词、房源帖特征词、求租帖特征词、价格/户型提取。
语义复核（否定语境剔除等）由 Claude 读 cleaned 数据完成，本模块只做字面命中。
"""
import re

# ---------------------------------------------------------------------------
# 8 类风险词（key 稳定英文标识 + 中文 label + 缺省 severity + keywords 15-25 词）
# ---------------------------------------------------------------------------
RISK_CATEGORIES = {
    "housing_quality": {
        "label": "房屋质量",
        "severity": "high",
        "keywords": [
            "漏水", "发霉", "墙皮脱落", "掉墙皮", "电路老化", "水管爆", "水管坏",
            "串味", "蟑螂", "老鼠", "下水道堵", "返潮", "墙面开裂", "窗户漏风",
            "暖气不热", "空调坏", "马桶堵", "跳闸", "隔音差", "地板翘", "天花板掉",
            "水压小", "热水器坏", "异味", "潮湿",
        ],
    },
    "landlord_agent": {
        "label": "房东中介",
        "severity": "high",
        "keywords": [
            "二房东", "克扣", "随意涨租", "涨房租", "赶人", "换锁", "黑中介",
            "合同陷阱", "中介跑路", "房东失联", "态度恶劣", "骚扰", "强制清退",
            "押金纠纷", "口头承诺", "说话不算数", "欺瞒", "加价", "转租纠纷",
            "赖账", "辱骂", "威胁", "断水断电", "不维修", "推诿",
        ],
    },
    "deposit_fee": {
        "label": "押金费用",
        "severity": "high",
        "keywords": [
            "扣押金", "不退押金", "退押金难", "水电加价", "乱收费", "违约金",
            "卫生费", "服务费", "克扣押金", "收中介费", "隐藏收费", "网费贵",
            "物业费转嫁", "涨水电", "赖押金", "退房扣钱", "折旧费", "天价账单",
            "押金拖着", "收费不明",
        ],
    },
    "fake_listing": {
        "label": "虚假房源",
        "severity": "high",
        "keywords": [
            "假房源", "照骗", "钓鱼", "低价引流", "一房多挂", "图片不符",
            "虚假房源", "房源不实", "看房套路", "挂羊头", "诱饵", "假图片",
            "实地不一样", "二道贩子", "信息不符", "虚假低价", "图文不符", "白跑一趟",
        ],
    },
    "noise": {
        "label": "噪音",
        "severity": "medium",
        "keywords": [
            "吵", "噪音", "夜市", "高架", "铁路", "广场舞", "KTV", "隔音不好",
            "装修", "吵闹", "彻夜", "烧烤摊", "马路边", "鼾声", "隔壁吵",
            "楼道吵", "施工", "夜宵街", "鸣笛", "轰鸣",
        ],
    },
    "property_mgmt": {
        "label": "物业",
        "severity": "medium",
        "keywords": [
            "物业差", "物业不作为", "电梯坏", "电梯常坏", "垃圾没人清", "脏乱差",
            "停水停电", "门禁坏", "没人管", "车位紧张", "绿化差", "楼道杂物",
            "物业费贵", "维修慢", "供暖差", "垃圾堆", "管理混乱",
        ],
    },
    "safety": {
        "label": "治安",
        "severity": "high",
        "keywords": [
            "被盗", "撬锁", "尾随", "治安差", "偏僻", "失窃", "偷电动车", "入室",
            "打架", "暗巷", "夜路危险", "黑车", "传销", "群租混乱", "陌生人进出",
            "没有摄像头", "报警", "被盗过", "锁被撬",
        ],
    },
    "commute_amenity": {
        "label": "通勤配套",
        "severity": "low",
        "keywords": [
            "通勤远", "地铁远", "生活不便", "买菜不便", "交通不便", "公交少",
            "没地铁", "周边荒", "商业少", "外卖都点不到", "医院远", "学校远",
            "上班远", "单程一小时", "换乘麻烦", "打车难", "超市远", "配套差",
            "寸草不生", "太偏了",
        ],
    },
}

# 便捷映射
CATEGORY_LABELS = {k: v["label"] for k, v in RISK_CATEGORIES.items()}
CATEGORY_SEVERITY = {k: v["severity"] for k, v in RISK_CATEGORIES.items()}
CATEGORY_ORDER = list(RISK_CATEGORIES.keys())

SEVERITY_LABELS = {"high": "高", "medium": "中", "low": "低"}

# ---------------------------------------------------------------------------
# 广告/引流贴特征词（命中 >=2 个不同词才标记 ad_suspect，clean.py 实现长词优先）
# 注意与 LISTING_KEYWORDS 刻意不重叠，避免误伤真实个人转租帖
# ---------------------------------------------------------------------------
AD_KEYWORDS = [
    "加微信", "加vx", "加V", "vx", "威信", "微信号", "私信我", "私信秒回",
    "扫码", "扫一扫", "中介费全免", "手慢无", "低价引流", "限时特价",
    "优惠大放送", "房源充足", "包入住", "免押金", "零中介费", "招租热线",
    "电话速联", "长期有效", "随时看房", "cozy apartment", "温馨小窝",
    "品质公寓", "急租联系",
]

# ---------------------------------------------------------------------------
# v3：房源帖特征词（is_listing 判定：命中>=1，或 价格+户型 同时命中）
# ---------------------------------------------------------------------------
LISTING_KEYWORDS = [
    "转租", "直租", "整租", "合租", "主卧", "次卧", "房东直租", "无中介",
    "个人转租", "急转", "急租", "拎包入住", "押一付一", "押一付三", "招租",
    "出租", "转租中", "找室友", "寻室友", "房源出租",
]

# 户型/朝向词（room_hint = 命中词列表）
ROOM_KEYWORDS = [
    "一居室", "两居室", "三居室", "一居", "两居", "三居", "一室", "两室",
    "三室", "主卧", "次卧", "独卫", "朝南", "南北通透", "单间", "开间", "阳台",
]

# 价格提取：匹配 1500/月、2500元、¥2800、月租3000、房租:2600 等形式。
# 3-6 位数字 + 前后无数字边界，避免匹配手机号/楼栋号；无命中返回空串。
# 两层：先找带单位/货币符的形式（PRICE_RE），找不到再找 "月租/租金/房租+数字" 前缀形式
# （PRICE_RE_PRE，返回纯数字），防止 "月租2500元" 被前缀分支截断成 "月租2500"。
PRICE_RE = re.compile(
    r"(?:[¥￥]\s*(?<!\d)(\d{3,6})(?!\d))"                        # ¥2800 / ￥ 2800
    r"|(?<!\d)(\d{3,6})(?!\d)\s*(?:元|块)(?:\s*/?\s*月|每月)?"    # 2500元 / 2500块/月 / 2600元/月
    r"|(?<!\d)(\d{3,6})(?!\d)\s*/\s*月"                           # 1500/月
)
PRICE_RE_PRE = re.compile(r"(?:月租|租金|房租)\s*[:：]?\s*(?<!\d)(\d{3,6})(?!\d)")  # 月租3000


def extract_price(text: str) -> str:
    """返回第一处价格匹配（如 "2500元"、"¥2800"、"1500/月"、"3000"），无则空串。"""
    if not text:
        return ""
    m = PRICE_RE.search(text)
    if m:
        return m.group(0).strip()
    m = PRICE_RE_PRE.search(text)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# 价格字符串 -> int（与 extract_price 配套，clean.py 存为 price_int 供预算筛选）。
# 规则：去 ¥/￥/元/块/千分位逗号/空白后取数；区间（-/–/~/至/到）取下限；
# "1.8k"→1800、"1.2万"→12000；无有效数字（3-6 位）返回 None。
# ---------------------------------------------------------------------------
_PRICE_CLEAN_RE = re.compile(r"[¥￥\s,，]")
_PRICE_WAN_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*[万萬]")
_PRICE_K_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*[kK]")
_PRICE_RANGE_RE = re.compile(r"(?<!\d)(\d{3,6})(?!\d)\s*[-–~～至到]\s*\d{3,6}(?!\d)")
_PRICE_PLAIN_RE = re.compile(r"(?<!\d)(\d{3,6})(?!\d)")


def price_to_int(text) -> "int | None":
    """价格字符串转整数（月租元）。解析失败返回 None。

    >>> price_to_int("¥2800"), price_to_int("1800-2200"), price_to_int("1.8k")
    (2800, 1800, 1800)
    """
    s = _PRICE_CLEAN_RE.sub("", str(text or "").strip())
    if not s:
        return None
    m = _PRICE_WAN_RE.search(s)
    if m:
        return int(round(float(m.group(1)) * 10000))
    m = _PRICE_K_RE.search(s)
    if m:
        return int(round(float(m.group(1)) * 1000))
    m = _PRICE_RANGE_RE.search(s)
    if m:
        return int(m.group(1))
    m = _PRICE_PLAIN_RE.search(s)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# 求租帖特征（seek_post）：发帖人本人在找房（非出租、非居住评价），对口碑/房源
# 分析是纯噪声。宁可漏判不可误杀（错杀出租帖代价高）：
#   - 强词命中标题即判：明确找房意向，出租帖标题几乎不会出现。不做出租侧词
#     豁免——求租帖常写"找房东直租/月底入住"，且"有无出租/寻房源"本身
#     就含出租侧字面。
#   - 仅正文命中（强词或弱词）时降级：含出租侧特征词不判（web 全文页常夹带
#     其他帖子的求租标题），且须同时命中预算句式，仅预算数字不判。
# ---------------------------------------------------------------------------
SEEK_STRONG_KEYWORDS = [
    "求租", "求房东", "求转租", "求短租", "求房源", "求好房源",
    "蹲一个", "蹲个", "蹲房", "捞一下", "有房踢我", "有无出租",
    "有房源吗", "有转租吗", "有没有房东", "寻房源", "急租求",
    "求推荐", "求告知", "求推",
]
SEEK_WEAK_KEYWORDS = ["找房", "想租", "想租个", "想找个", "本人找", "帮找"]

# 预算句式（组合信号）：预算3000 / 2500以内 / 2k以内 / 1.5w以内
SEEK_BUDGET_RE = re.compile(
    r"预算\s*[¥￥]?\s*\d"
    r"|(?<!\d)\d{3,5}\s*以内"
    r"|[\d.]+\s*[kKwW]\s*以内"
)

# 出租侧特征词：供给侧表述，出现时正文级求租信号不判（防误伤）
RENTAL_SIDE_KEYWORDS = [
    "出租", "转租", "直租", "招租", "房东", "房源", "看房", "入住", "搬走", "到期",
]


# 求租句式（v4：真实漏杀回补）。强词出现在正文时会被出租侧词豁免挡掉（防 web 全文
# 夹带），但求租帖正文天然含"房东直租/有没有房东"等字面 → 漏杀。句式足够特异
# （求+户型 / 有没有+房源侧 / 想租+户型），命中标题或正文即判，不走出租侧豁免。
SEEK_PATTERNS = [
    re.compile(r"求房东直租"),
    re.compile(r"有没有.{0,6}(房东|直租|转租|房源)"),
    re.compile(r"求推荐房源"),
    re.compile(r"想租.{0,3}(一居|两居|三居|一室|次卧|主卧|单间|开间)"),
    re.compile(r"求.{0,2}(一居|两居|三居|次卧|主卧|单间|开间)"),
]
# 仅限标题的求租句式："找房子"太泛，正文出现（如攻略文"来北京先找房子"）不判；
# 标题=发帖意图，且排除后文跟"出租"的供给侧写法。
SEEK_TITLE_PATTERNS = [re.compile(r"找房子(?!.*出租)")]


def detect_seek_post(title, content) -> bool:
    """求租帖判定（标题+正文综合）。标题强词即判；句式命中即判（不走出租侧豁免）；
    其余正文信号需无出租侧词+预算句式。"""
    title = title or ""
    text = "%s\n%s" % (title, content or "")
    if any(kw in title for kw in SEEK_STRONG_KEYWORDS):
        return True
    if any(p.search(title) for p in SEEK_TITLE_PATTERNS):
        return True
    if any(p.search(text) for p in SEEK_PATTERNS):
        return True
    if any(kw in text for kw in RENTAL_SIDE_KEYWORDS):
        return False
    if not any(kw in text for kw in SEEK_STRONG_KEYWORDS + SEEK_WEAK_KEYWORDS):
        return False
    return bool(SEEK_BUDGET_RE.search(text))


# ---------------------------------------------------------------------------
# 买房/卖房帖（buy_sell_post）：租房语境外的买卖讨论/挂牌页，对口碑/房源分析是噪声。
# 仅匹配标题（长文正文常顺带提"买房/楼市"，如出租房改造随笔、小区报道，正文级
# 匹配会误杀真实租住内容）。注意"房价"租房语境常见（租金对比房价），不单独做模式。
# ---------------------------------------------------------------------------
BUY_SELL_PATTERNS = [
    re.compile(r"买房"), re.compile(r"购房"), re.compile(r"卖房"),
    re.compile(r"售房"), re.compile(r"二手房"), re.compile(r"楼市"),
    re.compile(r"房贷"), re.compile(r"房子.{0,6}难卖"),
    re.compile(r"学区房.{0,8}[买购]"),
]


def detect_buy_sell_post(title) -> bool:
    """买房/卖房帖判定（仅标题字面命中）。"""
    return any(p.search(title or "") for p in BUY_SELL_PATTERNS)


# ---------------------------------------------------------------------------
# 城市消歧（--city 启用）：辅线小区名全国重名率高（如"龙泽苑"），web 源常混入
# 外地同名小区帖（晋城/宜兴/绍兴/保定/南阳/乌兰察布 58 同城与地方房产站）。
# 判据保守——宁可漏杀不可误杀：仅 web 项判定，URL 含其他城市子域/城市名，
# 或标题同时含"其他城市名 + 租赁侧词"（晋城58同城租房 / 宜兴房产超市出租 等）。
# xhs/douban/douyin 无可靠城市信号，不判。
# ---------------------------------------------------------------------------
OTHER_CITY_WORDS = [
    # 原黑名单（实测异地 58/地方房产站）
    "晋城", "宜兴", "绍兴", "保定", "南阳", "乌兰察布",
    # COMMON_CITY_WORDS 24 城并入（除目标城市本身在运行时跳过）
    "北京", "上海", "广州", "深圳", "杭州", "南京", "成都", "武汉",
    "西安", "重庆", "天津", "苏州", "长沙", "郑州", "青岛", "大连",
    "厦门", "合肥", "济南", "福州", "昆明", "宁波", "无锡", "佛山",
    # 实测漏网：济宁（龙泽苑撞名城市）+ 常见异地租房帖城市
    "济宁", "洛阳", "潍坊", "临沂", "烟台", "威海", "淄博", "唐山",
    "石家庄", "邯郸", "沧州", "徐州", "常州", "南通", "扬州", "嘉兴",
    "温州", "泉州", "漳州", "东莞", "珠海", "中山", "惠州", "汕头",
    "太原", "沈阳", "长春", "哈尔滨", "贵阳", "南宁", "兰州", "南昌",
    "柳州", "海口", "株洲", "宜昌", "襄阳", "芜湖", "蚌埠",
    # v2 实测漏网：搜"永丰"混入的同名异地帖来源城市（安居客/58 站点页）
    "海宁", "天门", "吉安", "博罗", "香港",
    # 上海辖区/指代词（龙泽苑撞名实测：上海闵行君莲 / 浦西 / 双柏路帖）
    "闵行", "浦西", "双柏路",
]

# 常见城市词：豆瓣首词降级时跳过，防"北京 龙泽苑"降级成"北京"泛洪
COMMON_CITY_WORDS = ["北京", "上海", "广州", "深圳", "杭州", "南京", "成都", "武汉",
                     "西安", "重庆", "天津", "苏州", "长沙", "郑州", "青岛", "大连",
                     "厦门", "合肥", "济南", "福州", "昆明", "宁波", "无锡", "佛山"]
# 城市名 -> URL 中的常见形态（pinyin 子域 + 中文城市名），小写匹配
OTHER_CITY_URL_TOKENS = {
    "晋城": ["jincheng", "晋城"],
    "宜兴": ["yixing", "宜兴"],
    "绍兴": ["shaoxing", "绍兴"],
    "保定": ["baoding", "保定"],
    "南阳": ["nanyang", "南阳"],
    "乌兰察布": ["wulanchabu", "乌兰察布"],
    "北京": ["beijing", "bj.", "北京"],
    "上海": ["shanghai", "上海"],
    "广州": ["guangzhou", "gz.", "广州"],
    "深圳": ["shenzhen", "sz.", "深圳"],
    "杭州": ["hangzhou", "hz.", "杭州"],
    "南京": ["nanjing", "nj.", "南京"],
    "成都": ["chengdu", "cd.", "成都"],
    "武汉": ["wuhan", "武汉"],
    "西安": ["xian", "西安"],
    "重庆": ["chongqing", "cq.", "重庆"],
    "天津": ["tianjin", "tj.", "天津"],
    "苏州": ["suzhou", "苏州"],
    "长沙": ["changsha", "长沙"],
    "郑州": ["zhengzhou", "郑州"],
    "青岛": ["qingdao", "qd.", "青岛"],
    "大连": ["dalian", "大连"],
    "厦门": ["xiamen", "厦门"],
    "合肥": ["hefei", "合肥"],
    "济南": ["jinan", "济南"],
    "福州": ["fuzhou", "福州"],
    "昆明": ["kunming", "昆明"],
    "宁波": ["ningbo", "宁波"],
    "无锡": ["wuxi", "无锡"],
    "佛山": ["foshan", "佛山"],
    "济宁": ["jining", "济宁"],
    "洛阳": ["luoyang", "洛阳"],
    "潍坊": ["weifang", "潍坊"],
    "临沂": ["linyi", "临沂"],
    "烟台": ["yantai", "烟台"],
    "威海": ["weihai", "威海"],
    "淄博": ["zibo", "淄博"],
    "唐山": ["tangshan", "唐山"],
    "石家庄": ["shijiazhuang", "石家庄"],
    "邯郸": ["handan", "邯郸"],
    "沧州": ["cangzhou", "沧州"],
    "徐州": ["xuzhou", "徐州"],
    "常州": ["changzhou", "常州"],
    "南通": ["nantong", "南通"],
    "扬州": ["yangzhou", "扬州"],
    "嘉兴": ["jiaxing", "嘉兴"],
    "温州": ["wenzhou", "温州"],
    "泉州": ["quanzhou", "泉州"],
    "漳州": ["zhangzhou", "漳州"],
    "东莞": ["dongguan", "dg.", "东莞"],
    "珠海": ["zhuhai", "珠海"],
    "中山": ["zhongshan", "zs.", "中山"],
    "惠州": ["huizhou", "惠州"],
    "汕头": ["shantou", "汕头"],
    "太原": ["taiyuan", "太原"],
    "沈阳": ["shenyang", "沈阳"],
    "长春": ["changchun", "长春"],
    "哈尔滨": ["harbin", "哈尔滨"],
    "贵阳": ["guiyang", "贵阳"],
    "南宁": ["nanning", "南宁"],
    "兰州": ["lanzhou", "兰州"],
    "南昌": ["nanchang", "南昌"],
    "柳州": ["liuzhou", "柳州"],
    "海口": ["haikou", "海口"],
    "株洲": ["zhuzhou", "株洲"],
    "宜昌": ["yichang", "宜昌"],
    "襄阳": ["xiangyang", "襄阳"],
    "芜湖": ["wuhu", "芜湖"],
    "蚌埠": ["bengbu", "蚌埠"],
    "海宁": ["haining", "海宁"],
    "天门": ["tianmen", "天门"],
    "吉安": ["jian", "吉安"],
    "博罗": ["boluo", "博罗"],
    "香港": ["hongkong", "hk.", "香港"],
}

# 标题级组合信号：其他城市名 + 租赁侧词（防"城市名"单独出现误杀）
_CITY_RENTAL_CONTEXT_RE = re.compile(
    r"租房|出租|直租|转租|合租|整租|房租|房价|房产|房源|小区|58同城|入手")

# 本市信号白名单（--city=<城市> 时启用）：标题/URL/正文前 200 字命中 → 保留。
# 目前仅北京维护信号词（行政区 + 租房热区地标）；其他城市暂只走黑名单。
CITY_SIGNAL_WORDS = {
    "北京": [
        "北京",
        "海淀", "朝阳", "昌平", "西城", "东城", "丰台", "通州", "大兴",
        "顺义", "房山", "门头沟", "石景山", "亦庄", "回龙观", "天通苑",
        "西二旗", "中关村", "上地", "望京", "西三旗", "沙河",
        "立水桥", "北苑", "酒仙桥", "五道口", "国贸", "亮马桥", "霍营",
        # 注意不放"N号线"线号词："15号线"（上海）含子串"5号线"会误触发白名单
        "回龙观西大街", "龙泽地铁站", "beijing", "bj.58",
    ],
}


def has_city_signal(url, title, content, city) -> bool:
    """本市信号判定：标题/URL/正文前 200 字含本市名或本市行政区/地标词。
    URL 只认 pinyin 子域/城市名字面（中文信号词不查 URL，防"朝阳"等地名撞车）。"""
    city = (city or "").strip()
    if not city:
        return False
    url_l = (url or "").lower()
    title = title or ""
    head = "%s\n%s" % (title, (content or "")[:200])
    if city in title or city in head:
        return True
    for tok in CITY_SIGNAL_WORDS.get(city, []):
        low = tok.lower()
        if low in url_l:
            return True
        if tok in head:
            return True
    return False


def detect_city_mismatch(url, title, content, city, web: bool = True) -> bool:
    """明显非目标城市条目判定（保守，宁漏杀不误杀）。优先级：
    1) URL 黑名单命中（其他城市子域/城市名，仅 web——xhs/douban/douyin 的
       URL 无城市子域，跳过省遍历）→ 剔；
    2) 本市信号（标题/URL/正文前 200 字含本市名或本市行政区/地标词）→ 留；
    3) 标题/正文前 200 字含明确异城名 + 租赁上下文 → 剔。实测漏网均为
       xhs/douban/douyin 的撞名小区帖（济宁房东直租 / 青岛李沧租房避雷 /
       直租浦西15号线双柏路），故标题级判定覆盖全部平台；
    4) 其余 → 留。"""
    city = (city or "").strip()
    if not city:
        return False
    url = (url or "").lower()
    title = title or ""
    head = "%s\n%s" % (title, (content or "")[:200])
    # 1) 黑名单优先剔（仅 web 项做 URL 判定）
    if web:
        for other in OTHER_CITY_WORDS:
            if other == city:
                continue
            if any(tok in url for tok in OTHER_CITY_URL_TOKENS.get(other, [other.lower()])):
                return True
    # 2) 本市信号白名单兜底
    if has_city_signal(url, title, content, city):
        return False
    # 3) 明确异城名 + 租赁上下文（标题/正文前 200 字，覆盖全部平台）
    for other in OTHER_CITY_WORDS:
        if other == city:
            continue
        if other in head and _CITY_RENTAL_CONTEXT_RE.search(head):
            return True
    return False


# ---------------------------------------------------------------------------
# 列表页/导航页过滤（web 源）：标题命中聚合页/走势页/问答页模式，非真实帖子。
# 仅对 comments=0 且 likes=0 的 web 项生效（防误杀有互动的正常帖）。
# ---------------------------------------------------------------------------
LISTING_PAGE_PATTERNS = [
    re.compile(r"出租信息$"),
    re.compile(r"出租房源信息"),
    re.compile(r"租金走势"),
    re.compile(r"价格走势"),
    re.compile(r"房屋出租价格"),
    re.compile(r"租房价格信息"),
    re.compile(r"小区怎么样.*物业费.*好不好"),
]


def detect_listing_page(title) -> bool:
    """列表页/导航页判定（仅标题字面命中）。"""
    return any(p.search(title or "") for p in LISTING_PAGE_PATTERNS)
