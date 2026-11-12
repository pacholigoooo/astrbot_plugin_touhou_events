"""Check behavior and real AstrBot 4.26 dispatch; --live also reads THBWiki."""

import argparse
import asyncio
from datetime import date, timedelta
import importlib
from importlib.metadata import version
import os
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch
from urllib.error import HTTPError
import zipfile

parser = argparse.ArgumentParser()
parser.add_argument("--sdk-root")
parser.add_argument("--dependencies")
parser.add_argument("--live", action="store_true")
args = parser.parse_args()
repo = Path(__file__).resolve().parents[1]
for directory in reversed([args.sdk_root, args.dependencies]):
    if directory:
        sys.path.insert(0, directory)
(repo / ".cache").mkdir(exist_ok=True)
scratch = tempfile.TemporaryDirectory(dir=repo / ".cache")
os.environ["ASTRBOT_ROOT"] = scratch.name

from astrbot.api.message_components import Plain
from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType
from astrbot.core.pipeline.context_utils import call_handler
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import QQOfficialMessageEvent
from astrbot.core.platform.sources.qqofficial_webhook.qo_webhook_event import QQOfficialWebhookMessageEvent
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.star_handler import star_handlers_registry
from astrbot.core.star.updator import PluginUpdator

archive = repo / "dist" / "astrbot_plugin_touhou_events-v1.0.0.zip"
with zipfile.ZipFile(archive) as bundle:
    assert "astrbot_plugin_touhou_events/logo.png" in bundle.namelist()
    assert all("__pycache__" not in name and ".cache" not in name for name in bundle.namelist())
install = Path(scratch.name) / "data/plugins/astrbot_plugin_touhou_events"
install.parent.mkdir(parents=True)
PluginUpdator().unzip_file(str(shutil.copy(archive, Path(scratch.name) / "install.zip")), str(install))
sys.path.insert(0, str(install.parent))
api = importlib.import_module("astrbot_plugin_touhou_events.calendar_api")
main = importlib.import_module("astrbot_plugin_touhou_events.main")
assert Path(main.__file__).parent == install
assert version("astrbot") == "4.26.0", version("astrbot")


def row(uid, place, start="2026-11-13", end="2026-11-13"):
    return {"id": uid + "#ev-1", "title": uid, "startStr": start, "endStr": end,
            "desc": f"{uid} 于 {place} 举办", "type": ["聚会"], "url": "https://thwiki.cc/" + uid}


ROWS = [row("深圳茶会", "深圳市 活动中心"), row("广州茶会", "广州市 艺术馆"),
        row("广州待定活动", "场馆待公布"), row("东京茶会", "东京大田区PiO"),
        row("线上茶会", "线上 Discord"), row("跨天例会", "广州市 公园", "2026-11-11", "2026-11-14")]
ROWS += [row(f"广州小聚{index}", "广州市 图书馆") for index in range(5)]
EVENTS = tuple(api.Event.from_api(item) for item in ROWS)


def check_queries():
    today = date(2026, 11, 12)
    assert api.parse_query("", today).end == date(2026, 12, 11)
    assert api.parse_query("本周", today).start == date(2026, 11, 9)
    assert api.parse_query("下周", today).end == date(2026, 11, 22)
    assert api.parse_query("未来1天", today).start == api.parse_query("未来1天", today).end
    assert api.parse_query("2028-02", today).end == date(2028, 2, 29)
    for bad in ("未来0天", "未来181天", "2026-13", "2026-02-30~2026-03-01", "2026-11-15~2026-11-01", "今天 本周", "第0页"):
        try:
            api.parse_query(bad, today)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)
    guangzhou, shenzhen = EVENTS[1], EVENTS[0]
    assert api.parse_query("广州", today).matches(guangzhou)
    assert not api.parse_query("广州", today).matches(shenzhen)
    assert api.parse_query("深圳", today).matches(shenzhen)
    assert not api.parse_query("深圳", today).matches(guangzhou)
    assert all(api.parse_query("廣東", today).matches(item) for item in (guangzhou, shenzhen))
    assert EVENTS[2].region == "unknown" and not api.parse_query("广州", today).matches(EVENTS[2])
    assert EVENTS[3].region == "overseas" and EVENTS[4].region == "online"
    crossing = EVENTS[5]
    assert api.parse_query("2026-11-14~2026-11-14", today).matches(crossing)
    assert not api.parse_query("2026-11-15~2026-11-15", today).matches(crossing)
    with patch.object(api, "_download_once", side_effect=[HTTPError("", 503, "", {}, None), []]) as fetch:
        assert api._download_range(today, today) == []
        assert fetch.call_count == 2 and api.API_MIRROR in fetch.call_args.args[0]
    with patch.object(api, "_download_once", side_effect=[ROWS * 20, [], []]) as fetch:
        assert api._download_range(today, today + timedelta(days=2)) == []
        assert fetch.call_count == 3
    with patch.object(api, "_download_range", return_value=ROWS) as fetch:
        assert api.download_events(today, today)
        assert fetch.call_args.args[0] == date(2026, 10, 1)
    print("PASS dates, city/province distinction, unknown/overseas/online, inclusive multiday end, mirror and 200-entry splitting")


def make_event(cls, platform, text, user="reader", room="group-a"):
    obj = AstrBotMessage()
    obj.type = MessageType.GROUP_MESSAGE
    obj.message_id, obj.self_id, obj.group_id = "10001", "bot", room
    obj.sender = MessageMember(user_id=user, nickname=user)
    obj.message = [Plain(text)]
    event = cls(text, obj, PlatformMetadata(platform, platform, "instance"), room, None)
    event.is_at_or_wake_command = True
    return event


async def check_sdk():
    plugin = main.TouhouEvents(None)
    registry = [(item, getattr(plugin, handler.handler.__name__))
                for handler in star_handlers_registry if handler.handler.__module__ == main.__name__
                for item in handler.event_filters if isinstance(item, CommandFilter)]
    assert len(registry) == 3

    async def dispatch(event):
        found = [handler for item, handler in registry if item.filter(event, {})]
        assert len(found) == 1
        async for _ in call_handler(event, found[0], **event.get_extra("parsed_params")):
            pass
        result = event.get_result()
        assert all(isinstance(item, Plain) for item in result.chain)
        assert event.call_llm is False
        text = "\n".join(item.text for item in result.chain)
        rendered = await event._parse_to_qqofficial(result)
        assert text in rendered[0]
        return text

    with patch.object(api, "download_events", return_value=EVENTS) as fetch:
        for cls, platform in ((QQOfficialMessageEvent, "qq_official"), (QQOfficialWebhookMessageEvent, "qq_official_webhook")):
            query = make_event(cls, platform, "东方活动 深圳 2026-11", user="alice")
            text = await dispatch(query)
            assert "收录1场" in text and "深圳茶会" in text and "广州茶会" not in text and "https://" not in text
            text = await dispatch(make_event(cls, platform, "东方活动 广州 2026-11", user="bob"))
            assert "深圳茶会" not in text and "下一页" in text
            text = await dispatch(make_event(cls, platform, "东方活动 广州 2026-11 第2页", user="bob"))
            assert "第2/2页" in text and "深圳茶会" not in text
            assert "深圳茶会" in await dispatch(make_event(cls, platform, "东方活动详情 1", user="alice"))
            assert "先发 /东方活动" in await dispatch(make_event(cls, platform, "东方活动详情 1", user="charlie"))
            assert "先发 /东方活动" in await dispatch(make_event(cls, platform, "东方活动详情 1", user="alice", room="other"))
            source = await dispatch(make_event(cls, platform, "东方活动出处 1", user="alice"))
            assert "词条：深圳茶会" in source and "https://" not in source
            plugin.config["qq_link_domains"] = ["thwiki.cc"]
            assert "https://thwiki.cc/" in await dispatch(make_event(cls, platform, "东方活动出处 1", user="alice"))
            plugin.config.clear()
            assert "2026-11-01 至 2026-11-30" in await dispatch(make_event(cls, platform, "东方活动 待确认 2026-11"))
            assert "未来范围" in await dispatch(make_event(cls, platform, "东方活动 未来181天"))
            assert "从今天起30天" in await dispatch(make_event(cls, platform, "东方活动 帮助"))
        assert fetch.call_count == 1, fetch.call_count
    client = api.CalendarClient()
    with patch.object(api, "download_events", side_effect=[TimeoutError("test"), EVENTS]) as fetch:
        try:
            await client.get(date(2026, 11, 1), date(2026, 11, 30))
        except TimeoutError:
            pass
        assert not client.cache
        await asyncio.gather(client.get(date(2026, 11, 1), date(2026, 11, 30)), client.get(date(2026, 11, 1), date(2026, 11, 30)))
        assert fetch.call_count == 2
    with patch.object(plugin.calendar, "get", side_effect=TimeoutError("test")):
        text = await dispatch(make_event(QQOfficialMessageEvent, "qq_official", "东方活动"))
        assert "未能读取" in text and "暂无记录" not in text
    print("PASS AstrBot 4.26 installed ZIP, real command filters, both QQ adapters, six-result paging, isolated references, success-only cache and failure message")


async def live():
    query = api.parse_query("武汉 本周")
    items, fetched = await api.CalendarClient().get(query.start, query.end)
    matched = [item for item in items if query.matches(item)]
    assert items
    print(f"LIVE fetched={fetched.isoformat()} raw={len(items)} Wuhan_this_week={len(matched)}")
    for item in matched:
        print(f"LIVE {item.when()} | {item.title} | {item.place} | {item.url}")


async def run():
    check_queries()
    await check_sdk()
    if args.live:
        await live()


try:
    asyncio.run(run())
finally:
    scratch.cleanup()
