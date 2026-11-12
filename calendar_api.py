"""THBWiki public event API, date queries, and location classification."""

import asyncio
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import unescape
import http.client
import json
import re
import ssl
from time import monotonic
import unicodedata
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_ROOT = "https://calendar.thbwiki.cc"
API_MIRROR = "https://calendar.thwiki.cc"
SOURCE_NAME = "THBWiki 事件日历"
TIMEZONE = timezone(timedelta(hours=8))
CACHE_SECONDS = 300
PAGE_SIZE = 6
REGIONS = {"china": "中国", "overseas": "海外", "online": "线上", "unknown": "地点待确认", "all": "全部地区"}

# Province aliases follow Gensokyo Toolkit's event-places.js; cities also cover
# current small meetups whose source provides a city without a province.
PLACES = [
    ("北京市", "北京"), ("天津市", "天津"), ("河北省", "河北 石家庄 石家莊 唐山 保定 邯郸"),
    ("山西省", "山西 太原 大同"), ("内蒙古自治区", "内蒙古 內蒙古 呼和浩特 包头 包頭"),
    ("辽宁省", "辽宁 遼寧 沈阳 瀋陽 大连 大連 鞍山 锦州 錦州"), ("吉林省", "吉林 长春 長春"),
    ("黑龙江省", "黑龙江 黑龍江 哈尔滨 哈爾濱 大庆 齐齐哈尔"), ("上海市", "上海"),
    ("江苏省", "江苏 江蘇 南京 苏州 蘇州 无锡 無錫 常州 南通 徐州 扬州 鎮江 镇江"),
    ("浙江省", "浙江 杭州 宁波 寧波 温州 溫州 嘉兴 嘉興 绍兴 紹興 金华 金華 台州 湖州"),
    ("安徽省", "安徽 合肥 芜湖 蕪湖 蚌埠"), ("福建省", "福建 福州 厦门 廈門 泉州 漳州"),
    ("江西省", "江西 南昌 赣州 贛州 九江 吉安"), ("山东省", "山东 山東 济南 濟南 青岛 青島 烟台 煙台 潍坊 濰坊 临沂 淄博"),
    ("河南省", "河南 郑州 鄭州 洛阳 洛陽 开封 開封"), ("湖北省", "湖北 武汉 武漢 宜昌 襄阳"),
    ("湖南省", "湖南 长沙 長沙 株洲 湘潭"), ("广东省", "广东 廣東 广州 廣州 深圳 珠海 东莞 東莞 佛山 汕头 汕頭 惠州 中山 江门 江門"),
    ("广西壮族自治区", "广西 廣西 南宁 南寧 桂林 柳州"), ("海南省", "海南 海口 三亚 三亞"),
    ("重庆市", "重庆 重慶"), ("四川省", "四川 成都 绵阳 綿陽 德阳 德陽"),
    ("贵州省", "贵州 貴州 贵阳 貴陽 遵义 遵義"), ("云南省", "云南 雲南 昆明 大理 丽江 麗江"),
    ("西藏自治区", "西藏 拉萨 拉薩"), ("陕西省", "陕西 陝西 西安 咸阳 咸陽 宝鸡 寶雞"),
    ("甘肃省", "甘肃 甘肅 兰州 蘭州"), ("青海省", "青海 西宁 西寧"),
    ("宁夏回族自治区", "宁夏 寧夏 银川 銀川"), ("新疆维吾尔自治区", "新疆 乌鲁木齐 烏魯木齊"),
    ("台湾省", "台湾 台灣 臺灣 台北 臺北 新北 台中 臺中 台南 臺南 高雄 桃园 桃園 新竹 基隆 嘉义 嘉義 屏东 屏東 宜兰 宜蘭 花莲 花蓮 台东 臺東 澎湖 金门 金門"),
    ("香港特别行政区", "香港 Hong Kong"), ("澳门特别行政区", "澳门 澳門 Macao Macau"),
]
OVERSEAS = re.compile(
    r"日本|东京|東京|大阪|京都|名古屋|横滨|横浜|川崎|神奈川|千叶|千葉|埼玉|北海道|札幌|福冈|福岡|广岛|広島|仙台|静冈|静岡|新潟|冲绳|沖縄|美国|美國|纽约|紐約|洛杉矶|洛杉磯|加拿大|温哥华|溫哥華|多伦多|多倫多|英国|英國|伦敦|倫敦|法国|法國|巴黎|德国|德國|澳大利亚|澳大利亞|澳洲|悉尼|墨尔本|墨爾本|新加坡|韩国|韓國|首尔|首爾|马来西亚|馬來西亞|泰国|泰國|越南|印度尼西亚|菲律宾|俄罗斯|俄羅斯|意大利|西班牙|\b(?:japan|tokyo|osaka|kawasaki|usa|canada|france|germany|australia|singapore|korea|seoul|malaysia|thailand|indonesia|vietnam|philippines)\b",
    re.I,
)


def clean(value):
    return " ".join(unescape(re.sub(r"<[^>]*>", "", str(value))).split())


def normalized(value):
    return unicodedata.normalize("NFKC", value).casefold().translate(str.maketrans("內遼寧龍蘇東廣貴雲陝臺灣慶", "内辽宁龙苏东广贵云陕台湾庆"))


def province_of(place):
    text = normalized(place)
    for province, aliases in PLACES:
        if normalized(province) in text or any(normalized(alias) in text for alias in aliases.split()):
            return province
    return ""


def province_filter(term):
    text = normalized(term)
    for province, aliases in PLACES:
        if text in {normalized(province), normalized(aliases.split()[0])}:
            return province
    return ""


def region_of(place):
    if re.search(r"线上|線上|在线|在線|网络|網絡|\bonline\b|\bdiscord\b|https?://", place, re.I):
        return "online"
    if province_of(place) or re.search(r"中国|中國|\bchina\b", place, re.I):
        return "china"
    if OVERSEAS.search(place):
        return "overseas"
    return "unknown"


@dataclass(frozen=True)
class Event:
    uid: str
    title: str
    start: date
    end: date
    place: str
    types: tuple
    url: str
    province: str
    region: str

    @classmethod
    def from_api(cls, row):
        notes = clean(row.get("desc", ""))
        match = re.search(r"\s于\s+(.+?)\s举办(?:\s|$)", notes)
        place = match.group(1).strip() if match else ""
        url = row.get("url", "")
        if urlsplit(url).scheme != "https" or urlsplit(url).hostname not in {"thwiki.cc", "thbwiki.cc"}:
            url = ""
        return cls(
            str(row["id"]), clean(row["title"]), date.fromisoformat(row["startStr"]),
            date.fromisoformat(row["endStr"]), place, tuple(clean(item) for item in row.get("type", [])),
            url, province_of(place), region_of(place),
        )

    def when(self):
        return self.start.isoformat() if self.start == self.end else f"{self.start.isoformat()} 至 {self.end.isoformat()}"


@dataclass(frozen=True)
class Query:
    start: date
    end: date
    region: str
    terms: tuple
    page: int

    def matches(self, event):
        if self.region != "all" and event.region != self.region:
            return False
        if event.start > self.end or event.end < self.start:
            return False
        text = normalized(f"{event.title} {event.place} {event.province} {' '.join(event.types)}")
        return all(normalized(term) in text or (province_filter(term) and province_filter(term) == event.province) for term in self.terms)


def parse_query(text, today=None):
    today = today or datetime.now(TIMEZONE).date()
    start, end = today, today + timedelta(days=29)
    region, page, terms = "china", 1, []
    time_seen, scope_seen = False, False
    scope_words = {"中国": "china", "国内": "china", "海外": "overseas", "线上": "online", "待确认": "unknown", "全部": "all"}
    for token in text.split():
        if token in scope_words:
            if scope_seen:
                raise ValueError("每次选一个地区范围：中国、海外、线上、待确认或全部。")
            region, scope_seen = scope_words[token], True
            continue
        page_match = re.fullmatch(r"第([1-9]\d*)页", token)
        if page_match:
            page = int(page_match[1])
            continue
        future = re.fullmatch(r"未来(\d+)天", token)
        month = re.fullmatch(r"(\d{4})-(\d{2})", token)
        dates = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:~|至|\.\.)(\d{4}-\d{2}-\d{2})", token)
        if token in {"今天", "明天", "本周", "下周"} or future or month or dates:
            if time_seen:
                raise ValueError("每次使用一个时间范围，例如 本周、未来60天 或 2027-01。")
            time_seen = True
            if token == "今天":
                start = end = today
            elif token == "明天":
                start = end = today + timedelta(days=1)
            elif token in {"本周", "下周"}:
                monday = today - timedelta(days=today.weekday())
                start = monday + timedelta(days=7 if token == "下周" else 0)
                end = start + timedelta(days=6)
            elif future:
                days = int(future[1])
                if not 1 <= days <= 180:
                    raise ValueError("未来范围可填1至180天，例如 未来60天。")
                start, end = today, today + timedelta(days=days - 1)
            elif month:
                try:
                    start = date(int(month[1]), int(month[2]), 1)
                    end = start.replace(day=monthrange(start.year, start.month)[1])
                except ValueError:
                    raise ValueError("月份无效，请使用 YYYY-MM，例如 2027-01。") from None
            else:
                try:
                    start, end = date.fromisoformat(dates[1]), date.fromisoformat(dates[2])
                except ValueError:
                    raise ValueError("日期无效，请使用 YYYY-MM-DD~YYYY-MM-DD。") from None
            continue
        if token.startswith("未来") or re.match(r"^\d{4}-", token) or re.fullmatch(r"第.*页", token):
            raise ValueError("时间可写 本周、未来60天、2027-01 或 2027-01-01~2027-01-31；分页写 第2页。")
        terms.append(token)
    if end < start or (end - start).days >= 366:
        raise ValueError("起止日期需按先后排列，单次最多查询366天。")
    return Query(start, end, region, tuple(terms), page)


def _download_once(url):
    tls = ssl.create_default_context()
    tls.set_alpn_protocols(["http/1.1"])
    request = Request(url, headers={"User-Agent": "AstrBot-Touhou-Events/1.0", "Accept": "application/json"})
    with urlopen(request, timeout=12, context=tls) as response:
        body = response.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            raise RuntimeError("THBWiki 活动响应超过2 MB")
        data = json.loads(body.decode("utf-8-sig"))
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ValueError("THBWiki 未返回活动列表")
        return data["results"]


def _download_range(start, end):
    path = f"/api/events/{start.isoformat()}/{end.isoformat()}"
    try:
        rows = _download_once(API_ROOT + path)
    except HTTPError as exc:
        if exc.code < 500:
            raise
        rows = _download_once(API_MIRROR + path)
    except (URLError, OSError, http.client.HTTPException):
        rows = _download_once(API_MIRROR + path)
    # The upstream asks SMW for at most 200 entries. Split only if the cap
    # is reached, so ordinary upcoming queries still use a single request.
    if len(rows) >= 200:
        if start == end:
            raise RuntimeError("单日活动已达上游200条上限，无法完整读取")
        middle = start + (end - start) // 2
        return _download_range(start, middle) + _download_range(middle + timedelta(days=1), end)
    return rows


def download_events(start, end):
    # The API filters by start date. Include the preceding calendar month
    # before intersecting locally, to retain ongoing multi-day activities.
    previous_end = start.replace(day=1) - timedelta(days=1)
    rows = _download_range(previous_end.replace(day=1), end)
    events = [Event.from_api(row) for row in rows]
    return tuple(sorted({event.uid: event for event in events}.values(), key=lambda event: (event.start, event.title)))


class CalendarClient:
    def __init__(self):
        self.cache = {}
        self.lock = asyncio.Lock()

    async def get(self, start, end):
        key = (start, end)
        async with self.lock:
            cached = self.cache.get(key)
            if cached and monotonic() - cached[0] < CACHE_SECONDS:
                return cached[1], cached[2]
            events = await asyncio.to_thread(download_events, start, end)
            fetched = datetime.now(TIMEZONE)
            self.cache[key] = (monotonic(), events, fetched)
            if len(self.cache) > 32:
                del self.cache[next(iter(self.cache))]
            return events, fetched
