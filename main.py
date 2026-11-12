import http.client
from math import ceil
from time import monotonic
from urllib.parse import urlsplit

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

from .calendar_api import API_ROOT, PAGE_SIZE, REGIONS, SOURCE_NAME, CalendarClient, parse_query

QQ_OFFICIAL = {"qq_official", "qq_official_webhook"}
HELP = """东方活动日历
/东方活动　中国，从今天起30天
/东方活动 武汉 本周
/东方活动 江苏 未来60天
/东方活动 2027-01 第2页
/东方活动 2027-01-01~2027-01-31
/东方活动 全部　也可选海外、线上、待确认
/东方活动详情 1　查看上次结果的日期和地点
/东方活动出处 1　查看上次结果的原词条
/东方活动出处　查看公共数据源
时间按北京时间，本周为周一至周日。多个关键词需同时匹配；每页6场。中国范围含港澳台，地点无法识别时归入待确认。跨天活动回溯至查询起始月份的前月月初。"""


def plain(event, text):
    return event.plain_result(text).use_markdown(False).use_t2i(False)


class TouhouEvents(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.calendar = CalendarClient()
        self.recent = {}

    def reader(self, event):
        return (event.get_platform_id(), event.get_session_id(), event.get_sender_id())

    def reference(self, event, query):
        if not query.isdigit() or int(query) < 1:
            raise ValueError("请填上次结果前的序号，例如 /东方活动详情 1。")
        previous = self.recent.get(self.reader(event))
        if not previous or monotonic() - previous[0] > 1800:
            raise ValueError("请先发 /东方活动 查询；结果序号保留30分钟。")
        index = int(query) - 1
        if index >= len(previous[1]):
            raise ValueError("上次结果中没有这个序号，请按显示的序号查询。")
        return previous[1][index], previous[2]

    def source_line(self, event, url):
        if event.get_platform_name() in QQ_OFFICIAL:
            domains = {domain.strip().lower() for domain in self.config.get("qq_link_domains", [])}
            if urlsplit(url).hostname not in domains:
                return "QQ官方当前未配置此来源域名；可在THBWiki按上方词条名检索。"
        return url

    @filter.command("东方活动", alias={"東方活動", "东方去哪玩"})
    async def activities(self, event: AstrMessageEvent, query: GreedyStr):
        """按省市、关键词和时间查询东方展会与茶会。"""
        event.should_call_llm(False)
        if query.strip() == "帮助":
            return plain(event, HELP)
        try:
            parsed = parse_query(query)
        except ValueError as exc:
            return plain(event, f"{exc}\n发 /东方活动 帮助 查看用法。")
        try:
            events, fetched = await self.calendar.get(parsed.start, parsed.end)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, http.client.HTTPException) as exc:
            logger.warning("东方活动日历读取失败：%s", exc)
            return plain(event, "这次未能读取THBWiki活动日历，请稍后再试。\n发 /东方活动出处 可查看数据源。")
        selected = tuple(item for item in events if parsed.matches(item))
        pages = max(1, ceil(len(selected) / PAGE_SIZE))
        if parsed.page > pages:
            return plain(event, f"本次筛选共{len(selected)}场、{pages}页；请使用 第1页 至 第{pages}页。")
        offset = (parsed.page - 1) * PAGE_SIZE
        visible = selected[offset:offset + PAGE_SIZE]
        self.recent[self.reader(event)] = (monotonic(), visible, fetched)
        if len(self.recent) > 256:
            del self.recent[next(iter(self.recent))]
        label = " · ".join((REGIONS[parsed.region], " ".join(parsed.terms))).strip(" ·")
        lines = [f"东方活动 · {label}", f"{parsed.start.isoformat()} 至 {parsed.end.isoformat()}（含首尾，北京时间）", f"收录{len(selected)}场 · 第{parsed.page}/{pages}页"]
        if not visible:
            lines.append("来源在这个时间与筛选范围内暂无记录。可扩大时间，或用 全部 / 待确认 查看其他地点。")
        for index, item in enumerate(visible, 1):
            lines.extend((f"{index}. {item.when()}  {item.title[:100]}", f"   {item.place[:140] or '地点待确认'}"))
        if parsed.page < pages:
            next_query = " ".join(replace_page for replace_page in query.split() if not (replace_page.startswith("第") and replace_page.endswith("页")))
            lines.append(f"下一页：/东方活动 {next_query} 第{parsed.page + 1}页".replace("  ", " "))
        lines.append(f"资料读取 {fetched:%m-%d %H:%M} · 成功结果缓存5分钟")
        if visible:
            lines.append("详情：/东方活动详情 1；出处：/东方活动出处 1")
        else:
            lines.append("数据源：/东方活动出处")
        lines.append("活动以主办方最新公告为准。")
        return plain(event, "\n".join(lines))

    @filter.command("东方活动详情", alias={"東方活動詳情"})
    async def detail(self, event: AstrMessageEvent, query: GreedyStr):
        """查看自己在当前会话上次查询的活动详情。"""
        event.should_call_llm(False)
        try:
            item, fetched = self.reference(event, query.strip())
        except ValueError as exc:
            return plain(event, str(exc))
        return plain(event, "\n".join((item.title, f"日期：{item.when()}（含首尾）", f"地点：{item.place or '来源未提供可识别的地点'}", f"范围：{REGIONS[item.region]}" + (f" · {item.province}" if item.province else ""), f"类型：{'、'.join(item.types) or '来源未分类'}", f"资料读取：{fetched:%Y-%m-%d %H:%M} 北京时间", f"来源：{SOURCE_NAME}", f"词条：{item.uid.split('#', 1)[0]}", f"原词条入口：/东方活动出处 {query.strip()}", "活动以主办方最新公告为准。")))

    @filter.command("东方活动出处", alias={"東方活動出處"})
    async def source(self, event: AstrMessageEvent, query: GreedyStr):
        """查看THBWiki公开数据源或某个结果的原词条。"""
        event.should_call_llm(False)
        if query.strip():
            try:
                item, _ = self.reference(event, query.strip())
            except ValueError as exc:
                return plain(event, str(exc))
            text = f"{SOURCE_NAME}\n活动：{item.title}\n词条：{item.uid.split('#', 1)[0]}"
            text += "\n" + (self.source_line(event, item.url) if item.url else "此条来源未提供可用的词条链接。")
        else:
            text = f"{SOURCE_NAME}\nTHBWiki提供公开JSON活动接口和ICS订阅。\n源词条：日程表\n"
            text += self.source_line(event, "https://thwiki.cc/日程表")
            if event.get_platform_name() not in QQ_OFFICIAL or "calendar.thbwiki.cc" in self.config.get("qq_link_domains", []):
                text += f"\nAPI文档：{API_ROOT}/\nICS：{API_ROOT}/api/ics"
            text += "\nWiki文字遵循CC BY-NC-SA 3.0，引用作品版权归原作者。插件代码为MIT。\n只读社区收录；结果不代表主办方实时公告。"
        return plain(event, text)
