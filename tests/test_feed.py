"""RSS-фид: импорт чужого фида с теми же guid, сборка, новый выпуск, докачка по Range и раздача ботом."""
import http.client
import threading
import xml.etree.ElementTree as ET

import feed as F

SRC = """<?xml version="1.0" encoding="UTF-8"?><rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" version="2.0">
    <channel>
        <title><![CDATA[тест]]></title>
        <image><url>https://old.example/cover.jpg</url><title>тест</title></image>
        <lastBuildDate>Sun, 11 Oct 2026 18:13:14 GMT</lastBuildDate>
        <itunes:image href="https://old.example/cover.jpg"/>
        <itunes:new-feed-url>https://old.example/feed</itunes:new-feed-url>
        <item><title><![CDATA[#2: второй]]></title><guid isPermaLink="false">bbb-2</guid>
            <pubDate>Sun, 11 Oct 2026 18:12:17 GMT</pubDate>
            <enclosure url="https://old.example/2.mp3" length="999" type="audio/mpeg"/></item>
        <item><title><![CDATA[тест No 1]]></title><guid isPermaLink="false">aaa-1</guid>
            <pubDate>Sat, 10 Jan 2026 10:00:00 GMT</pubDate>
            <enclosure url="https://old.example/1.mp3" length="999" type="audio/mpeg"/>
            <itunes:image href="https://old.example/1.jpg"/></item>
    </channel>
</rss>
"""
BASE = "https://pod.example/"


def make_src(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "feed.xml").write_text(SRC, encoding="utf-8")
    (src / "cover.jpg").write_bytes(b"jpg")
    (src / "bbb-2.mp3").write_bytes(b"2" * 20)
    (src / "aaa-1.mp3").write_bytes(b"1" * 10)
    (src / "aaa-1.jpg").write_bytes(b"j1")
    return src


def test_import_and_build(tmp_path):
    data = tmp_path / "data"
    assert F.import_feed(make_src(tmp_path), data) == 2
    items = {i["guid"]: i for i in F.load_items(data)}
    assert items["bbb-2"]["n"] == 2 and items["aaa-1"]["n"] == 1
    xml = F.build(data, BASE).read_text(encoding="utf-8")
    assert "old.example" not in xml                                  # всё указывает на нас
    ch = ET.fromstring(xml.encode()).find("channel")
    its = ch.findall("item")
    assert [i.findtext("guid") for i in its] == ["bbb-2", "aaa-1"]   # те же guid, новые сверху
    enc = its[0].find("enclosure")
    assert enc.get("url") == BASE + "media/bbb-2.mp3" and enc.get("length") == "20"   # длина — по нашему файлу
    assert (data / "feed/media/aaa-1.jpg").exists() and (data / "feed/media/cover.jpg").exists()
    assert xml.count("<lastBuildDate>") == 1 and 'rel="self"' in xml


def test_add_episode(tmp_path):
    data = tmp_path / "data"
    F.import_feed(make_src(tmp_path), data)
    mp3 = tmp_path / "x.mp3"
    mp3.write_bytes(b"3" * 30)
    cfg = {"feed_footer_html": "<p>канал</p>", "feed_footnotes": {"Instagram": "* сноска"}}
    guid, added = F.add_episode(data, BASE, mp3, 3, "третий", "про Instagram & жизнь", "00:09 начало\n05:00 дальше",
                                3725, cfg)
    assert added
    assert F.add_episode(data, BASE, mp3, 3, "третий")[1] is False                  # тот же номер — не дублируем
    assert F.add_episode(data, BASE, mp3, 2, "второй")[1] is False                  # импортированный тоже
    it = ET.fromstring(F.paths(data)[4].read_bytes()).find("channel").findall("item")[0]
    assert it.findtext("guid") == guid and it.findtext("title") == "#3: третий"
    assert it.findtext("{http://www.itunes.com/dtds/podcast-1.0.dtd}duration") == "01:02:05"
    d = it.findtext("description")
    assert "&amp; жизнь" in d and "00:09 начало<br />05:00 дальше" in d and "<p>канал</p>" in d and "* сноска" in d


def test_parse_range():
    assert F.parse_range(None, 100) is None
    assert F.parse_range("bytes=0-", 100) == (0, 99)
    assert F.parse_range("bytes=10-19", 100) == (10, 19)
    assert F.parse_range("bytes=90-500", 100) == (90, 99)
    assert F.parse_range("bytes=-10", 100) == (90, 99)
    assert F.parse_range("bytes=100-", 100) is False
    assert F.parse_range("bytes=0-1,5-6", 100) is None                # несколько диапазонов — весь файл
    assert F.parse_range("items=1-2", 100) is None


def test_bot_serves_feed(tmp_path, monkeypatch):
    import bot as B
    data = tmp_path / "data"
    F.import_feed(make_src(tmp_path), data)
    F.build(data, BASE)
    monkeypatch.setattr(B, "DATA", data)
    monkeypatch.setattr(B, "FEED_URL", BASE)
    srv = B.LimitedServer(("127.0.0.1", 0), B.SyncHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def get(path, method="GET", **h):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            c.request(method, path, headers=h)
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        st, h, body = get("/feed.xml")
        assert st == 200 and h["content-type"].startswith("application/rss+xml") and b"bbb-2" in body
        st, h, body = get("/media/bbb-2.mp3", Range="bytes=5-9")
        assert st == 206 and body == b"22222" and h["content-range"] == "bytes 5-9/20"
        st, h, body = get("/media/bbb-2.mp3", "HEAD")
        assert st == 200 and h["content-length"] == "20" and h["accept-ranges"] == "bytes" and body == b""
        assert get("/media/bbb-2.mp3", Range="bytes=50-")[0] == 416
        for bad in ("/media/../items.json", "/media/items.json", "/media/%2e%2e/feed.xml", "/items.json",
                    "/channel.xml", "/media/"):
            assert get(bad)[0] == 404, bad
        monkeypatch.setattr(B, "FEED_URL", "")
        assert get("/feed.xml")[0] == 404                              # без FEED_URL фида нет
    finally:
        srv.shutdown()
