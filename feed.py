"""Свой RSS-фид подкаста: площадки (Apple Podcasts, Spotify, Яндекс Музыка, VK…) сами забирают выпуски по нему.

Хранение — папка $DATA/feed:
    channel.xml   шапка фида (<channel> без выпусков), ссылки внутри — через {BASE}
    items.json    выпуски: [{guid, n, title, pub, xml}], xml — готовый <item> со ссылками через {BASE}
    media/        <guid>.mp3, <guid>.jpg (обложка выпуска, если есть), cover.jpg (обложка подкаста)
    feed.xml      собранный фид (build), его отдаёт веб-сервер бота по адресу FEED_URL/feed.xml

Старт — импорт чужого фида (import_feed): шапка и выпуски переносятся как есть, с теми же guid, только ссылки на
аудио и обложки указывают на нас (иначе площадки покажут дубли). Новый выпуск — add_episode() после одобрения обоих.

CLI (на сервере, от владельца папки данных):
    python3 feed.py import <папка с feed.xml, cover.* и <guid>.mp3> --data <DATA> --base https://…/
    python3 feed.py build --data <DATA> --base https://…/
"""
import argparse
import email.utils
import html
import json
import os
import re
import shutil
import time
from pathlib import Path

NAME_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,80}\.(mp3|jpg|png)$")   # что вообще можно отдать из media/
CTYPE = {".mp3": "audio/mpeg", ".jpg": "image/jpeg", ".png": "image/png"}
N_RE = re.compile(r"(?:#|№|No\.?\s*)(\d+)")


def paths(data):
    root = Path(data) / "feed"
    return root, root / "media", root / "items.json", root / "channel.xml", root / "feed.xml"


def _atomic(p, text):
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def episode_n(title):
    """Номер выпуска из заголовка («#30: …», «Show No 5», «№12») или None."""
    m = N_RE.search(title or "")
    return int(m.group(1)) if m else None


def _tag(xml, name):
    m = re.search(rf"<{name}[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>", xml, re.S)
    return m.group(1).strip() if m else ""


def import_feed(src, data, link=True):
    """Перенос чужого фида: src — папка с feed.xml, cover.* и аудио <guid>.mp3 (+ <guid>.jpg). Файлы переносятся
    жёсткими ссылками (link=True, без лишнего места) или копией. → число выпусков."""
    src = Path(src)
    root, media, items_f, chan_f, _ = paths(data)
    media.mkdir(parents=True, exist_ok=True)
    x = (src / "feed.xml").read_text(encoding="utf-8")
    head, *chunks = x.split("<item>")
    chunks[-1] = chunks[-1].split("</channel>")[0]

    def put(a, b):
        if b.exists():
            return
        try:
            os.link(a, b) if link else shutil.copy2(a, b)
        except OSError:
            shutil.copy2(a, b)

    cover = next(iter(sorted(src.glob("cover.*"))), None)
    if cover:
        put(cover, media / f"cover{cover.suffix}")
        url = "{BASE}media/cover" + cover.suffix
        head = re.sub(r"(<image>\s*<url>).*?(</url>)", rf"\g<1>{url}\g<2>", head, flags=re.S)
        head = re.sub(r'(<itunes:image href=")[^"]*(")', rf"\g<1>{url}\g<2>", head)
    head = re.sub(r"\s*<itunes:new-feed-url>.*?</itunes:new-feed-url>", "", head, flags=re.S)
    head = re.sub(r"\s*<atom:link[^>]*/>", "", head)
    head = re.sub(r"\s*<lastBuildDate>.*?</lastBuildDate>", "", head, flags=re.S)
    _atomic(chan_f, head.rstrip() + "\n")

    items = []
    for c in chunks:
        it = "<item>" + c.split("</item>")[0] + "</item>"
        guid = _tag(it, "guid")
        if not re.fullmatch(r"[0-9A-Za-z._-]{1,80}", guid):
            raise ValueError(f"guid не годится для имени файла: {guid!r}")
        mp3 = src / f"{guid}.mp3"
        if not mp3.exists():
            raise FileNotFoundError(mp3)
        put(mp3, media / f"{guid}.mp3")
        it = re.sub(r'(<enclosure url=")[^"]*(" length=")\d+', rf"\g<1>{{BASE}}media/{guid}.mp3\g<2>{mp3.stat().st_size}", it)
        if (src / f"{guid}.jpg").exists():
            put(src / f"{guid}.jpg", media / f"{guid}.jpg")
            it = re.sub(r'(<itunes:image href=")[^"]*(")', rf"\g<1>{{BASE}}media/{guid}.jpg\g<2>", it)
        title = html.unescape(_tag(it, "title"))
        pub = email.utils.parsedate_to_datetime(_tag(it, "pubDate")).timestamp() if _tag(it, "pubDate") else 0
        items.append({"guid": guid, "n": episode_n(title), "title": title, "pub": pub, "xml": it})
    _atomic(items_f, json.dumps(items, ensure_ascii=False, indent=1))
    return len(items)


def load_items(data):
    f = paths(data)[2]
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []


def build(data, base):
    """Собрать feed.xml из channel.xml и items.json (новые сверху); base — публичный адрес со слэшем на конце."""
    root, _, _, chan_f, feed_f = paths(data)
    base = base.rstrip("/") + "/"
    items = sorted(load_items(data), key=lambda i: i["pub"], reverse=True)
    head = chan_f.read_text(encoding="utf-8").replace("{BASE}", base)
    self_link = f'<atom:link href="{html.escape(base)}feed.xml" rel="self" type="application/rss+xml"/>'
    now = email.utils.formatdate(time.time(), usegmt=True)
    head = head.replace("<channel>", f"<channel>\n        {self_link}\n        <lastBuildDate>{now}</lastBuildDate>", 1)
    if "xmlns:atom" not in head:
        head = head.replace("<rss ", '<rss xmlns:atom="http://www.w3.org/2005/Atom" ', 1)
    body = "\n".join("        " + i["xml"].replace("{BASE}", base) for i in items)
    _atomic(feed_f, head.rstrip() + "\n" + body + "\n    </channel>\n</rss>\n")
    return feed_f


def _cdata(s):
    return "<![CDATA[" + s.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def description_html(summary, chapters_txt, cfg=None):
    """Описание выпуска как в прежнем хостинге: текст, подвал (feed_footer_html, например ссылка на канал),
    таймкоды по строкам и сноски (feed_footnotes: {«слово»: «текст сноски»} — если слово встречается)."""
    cfg = cfg or {}
    esc = lambda s: html.escape(s).replace("\n", "<br />")
    parts = [f"<p>{esc(summary)}</p>"] if summary else []
    if cfg.get("feed_footer_html"):
        parts.append(cfg["feed_footer_html"])
    if chapters_txt:
        parts.append(f"<p>{esc(chapters_txt.strip())}</p>")
    text = f"{summary}\n{chapters_txt}"
    for word, note in (cfg.get("feed_footnotes") or {}).items():
        if word.lower() in text.lower():
            parts.append(f"<p><em>{html.escape(note)}</em></p>")
    return "".join(parts)


def add_episode(data, base, mp3, n, title, summary="", chapters_txt="", duration=0, cfg=None, pub=None):
    """Новый выпуск в фид: копия MP3 в media/, <item>, пересборка feed.xml. Выпуск с тем же номером уже есть →
    ничего не меняет. → (guid, True если добавлен)."""
    _, media, items_f, _, _ = paths(data)
    items = load_items(data)
    same = [i for i in items if i.get("n") == n]
    if same:
        return same[0]["guid"], False
    guid = f"ep{n}-{int(time.time())}"
    media.mkdir(parents=True, exist_ok=True)
    dst = media / f"{guid}.mp3"
    shutil.copy2(mp3, dst.with_suffix(".mp3.tmp"))
    os.replace(dst.with_suffix(".mp3.tmp"), dst)
    pub = pub or time.time()
    head = f"#{n}: {title}"
    d = int(duration)
    xml = (f"<item><title>{_cdata(head)}</title>"
           f"<description>{_cdata(description_html(summary, chapters_txt, cfg))}</description>"
           f'<guid isPermaLink="false">{guid}</guid>'
           f"<pubDate>{email.utils.formatdate(pub, usegmt=True)}</pubDate>"
           f'<enclosure url="{{BASE}}media/{guid}.mp3" length="{dst.stat().st_size}" type="audio/mpeg"/>'
           f"<itunes:title>{_cdata(title)}</itunes:title>"
           f"<itunes:duration>{d // 3600:02d}:{d % 3600 // 60:02d}:{d % 60:02d}</itunes:duration>"
           f"<itunes:episode>{n}</itunes:episode><itunes:episodeType>full</itunes:episodeType>"
           f"<itunes:explicit>false</itunes:explicit></item>")
    items.append({"guid": guid, "n": n, "title": head, "pub": pub, "xml": xml})
    _atomic(items_f, json.dumps(items, ensure_ascii=False, indent=1))
    build(data, base)
    return guid, True


def parse_range(header, size):
    """Заголовок Range → (start, end) включительно, None — отдать целиком, False — диапазон не годится (416).
    Поддерживаем один диапазон: bytes=a-b, bytes=a-, bytes=-k (так делают плееры)."""
    if not header:
        return None
    m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", header)
    if not m or (not m.group(1) and not m.group(2)):
        return None                                         # непонятный или несколько диапазонов — весь файл
    if m.group(1):
        a = int(m.group(1))
        b = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
    else:
        k = int(m.group(2))
        if k == 0:
            return False
        a, b = max(0, size - k), size - 1
    return (a, b) if a <= b and a < size else False


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["import", "build"])
    ap.add_argument("src", nargs="?")
    ap.add_argument("--data", required=True)
    ap.add_argument("--base", required=True)
    a = ap.parse_args()
    if a.cmd == "import":
        print("выпусков:", import_feed(a.src, a.data))
    print("фид:", build(a.data, a.base))


if __name__ == "__main__":
    main()
