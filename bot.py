#!/usr/bin/env python3
"""bot.py — Telegram-бот подкаста: живая карточка выпуска в личке у каждого ведущего.

Жизненный цикл выпуска, state["ep"]["status"]:
  waiting    — выпуск открыт (кнопка «➕ Записали выпуск №N» или первый присланный файл); ждём дорожки от обоих
  grace      — обе дорожки есть: GRACE с на замену файла, потом старт (▶️ начать сейчас / ✋ подождать)
  processing — сборка; в карточке этапы с прогрессом и временем готовности (<tg-time> — у каждого в своём поясе)
               mode: run (синхр. → расшифровка → план → превью) · preview (только превью) · render (полный выпуск)
  preview    — превью 2–3 мин (начало, самое оживлённое место, конец) у обоих; полный выпуск — только после
               «✅ Собирай полный» от любого из двоих; правки (обрезка, EQ, компрессор, сдвиг) пересобирают превью
  review     — MP3 + архив отправлены обоим; «✅ Одобряю» у каждого; правки → снова processing, одобрения сбрасываются
  approved   — одобрен обоими: publish() — в свой RSS-фид (FEED_URL, feed.py) и в канал (PUBLISH_CHAT)
  failed     — сборка упала; «🔁 Ещё раз»
Карточка — одно сообщение в чате каждого ведущего (закреплено), ep["cards"][uid]; правится на месте.
Заново внизу (с новым закрепом) — только при открытии, после доставки готового выпуска и по нажатию на статус.
Нижняя клавиатура — короткий статус; Telegram обновляет её только вместе с новым сообщением, поэтому она
приезжает с событиями («📥 Аня: дорожка на месте»). Нажатие на статус — карточка заново внизу.
Напоминание — одно: на следующий день после открытия в REMIND_HOUR:00 по поясу того, кто ещё не прислал.

Транспорт: локальный Telegram Bot API (контейнер telegram-bot-api, --local) — файлы до 2 ГБ;
getFile отдаёт абсолютный путь в общем томе /var/lib/telegram-bot-api.
Выпуски: /data/jobs/<№>_<ггммдд>/ (meta/sync/transcript/plan/levels/preview/report.json, tracks/, preview/, out/).
Состояние: /data/state2.json. Тестовый режим: ep["test_to"] = uid — всё уходит только ему (audience()).
Карта проекта, грабли, типовые задачи — AGENTS.md.
"""
import hashlib
import hmac
import json
import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import traceback
import zipfile
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import cuts as C
import feed as F
import podcast as P

DATA = Path(os.environ.get("DATA", "/data"))
JOBS = DATA / "jobs"
ASSETS = DATA / "assets"
INCOMING = JOBS / "_incoming"
API = os.environ.get("BOT_API", "http://telegram-bot-api:8081")
def _secret(env, file_env, default_file):
    """Секрет: из переменной окружения env, иначе из файла (путь — file_env или default_file); нет — ''."""
    if os.environ.get(env):
        return os.environ[env].strip()
    f = Path(os.environ.get(file_env, default_file))
    return f.read_text().strip() if f.exists() else ""


TOKEN = _secret("BOT_TOKEN", "BOT_TOKEN_FILE", "/secrets/bot_token")
if not TOKEN:
    raise SystemExit("нет токена бота: задайте BOT_TOKEN или положите его в /secrets/bot_token")
AAI_KEY_FILE = Path(os.environ.get("AAI_KEY_FILE", "/secrets/assemblyai_key"))
PODCAST = os.environ.get("PODCAST_NAME", "подкаст")
START_N = int(os.environ.get("START_N", "0"))           # номер последнего уже вышедшего выпуска
GRACE = int(os.environ.get("GRACE_SECONDS", "30"))
REMIND_HOUR = int(os.environ.get("REMIND_HOUR", "13"))
MIN_TRACK = 300                                          # короче 5 минут — переспросить
KEEP_TRACKS_DAYS = int(os.environ.get("KEEP_TRACKS_DAYS", "30"))
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")            # https-адрес мини-приложения синхросигнала (Tailscale Funnel)
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8790"))
WEBAPP_HTML = Path(__file__).with_name("webapp.html")

# PEOPLE="111111111:Аня:Europe/Lisbon:ж,222222222:Боря:Asia/Tbilisi" — порядок = каналы A/B; «ж» — женский род в текстах
PEOPLE, ORDER = {}, []
for item in os.environ.get("PEOPLE", "").split(","):
    parts = item.strip().split(":")
    if len(parts) >= 2 and parts[0].isdigit():
        PEOPLE[int(parts[0])] = {"name": parts[1], "tz": parts[2] if len(parts) > 2 else os.environ.get("TZ", "UTC"),
                                 "f": len(parts) > 3 and parts[3].strip().lower() in ("ж", "f")}   # род: «готова»
        ORDER.append(int(parts[0]))

AUDIO_EXT = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".opus", ".aif", ".aiff", ".mp4", ".mov", ".caf",
             ".amr", ".3gp", ".webm", ".mkv", ".wma", ".m4v"}
ARCH_EXT = {".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz"}

STAGES = [("sync", "синхронизация"), ("transcribe", "расшифровка"), ("plan", "начало, конец и главы"),
          ("preview", "превью"),
          ("clean", "чистка и громкость дорожек"), ("mix", "сведение и компрессор"), ("final", "заставки и MP3")]
LABEL = dict(STAGES)
MODE_KEYS = {"run": ["sync", "transcribe", "plan", "preview"], "preview": ["preview"], "render": ["clean", "mix", "final"]}
RATIOS = [1.5, 2, 3, 4, 6]                                # ступени компрессора для «мягче / жёстче»
NR_STEPS = [0, 6, 8, 12]                                  # сила шумодава, дБ (0 — выключен); без нейромодели

PUBLISH_CHAT = os.environ.get("PUBLISH_CHAT", "")         # канал для публикации после одобрения обоих (@имя или -100…)
FEED_URL = os.environ.get("FEED_URL", "")                 # публичный адрес RSS-фида (без feed.xml); пусто — фида нет
# ожидаемая длительность этапа, минут на час записи (замерено на Pi 5) — для ETA
EXPECT = {"sync": 0.5, "transcribe": 2.5, "plan": 1.0, "preview": 2.0, "clean": 3.6, "mix": 1.4, "final": 3.6}

STATE_F = DATA / "state2.json"
lock = threading.RLock()
jobs_q = queue.Queue()
S = {}
_last_flush = [0.0]
_strangers = {}                                          # чужой Telegram ID → когда ему отвечали


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


P.log = lambda m: log("  " + m)


# ───────────────────────── Telegram ─────────────────────────

def tg(method, files=None, _t=120, **params):
    """Вызов метода Bot API (локальный сервер). Словари/списки в параметрах сериализуются в JSON.
    _t — HTTP-таймаут (не путать с параметром timeout у getUpdates). Ошибка API → RuntimeError.
    """
    data = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
            for k, v in params.items() if v is not None}
    try:
        r = requests.post(f"{API}/bot{TOKEN}/{method}", data=data, files=files, timeout=_t)
        j = r.json()
    except (requests.RequestException, ValueError) as e:
        # токен — в адресе запроса: убираем его из текста ошибки, иначе он попадёт в лог и в чат
        raise RuntimeError(f"{method}: {type(e).__name__}: " + str(e).replace(TOKEN, "<token>")) from None
    if not j.get("ok"):
        raise RuntimeError(f"{method}: {j.get('description')}")
    return j["result"]


def safe(method, **kw):
    """tg() без исключений: ошибка пишется в лог (кроме «message is not modified»), возвращается None."""
    try:
        return tg(method, **kw)
    except Exception as e:  # noqa: BLE001
        if "not modified" not in str(e):
            log(f"{method}: {e}")
        return None


def send(chat, text, kb=None, **kw):
    """sendMessage с HTML-разметкой и без превью ссылок."""
    return safe("sendMessage", chat_id=chat, text=text, parse_mode="HTML", reply_markup=kb,
                link_preview_options={"is_disabled": True}, **kw)


def react(chat, mid, emoji):
    """Реакция бота на сообщение (👀 принял → ⚡ распаковываю → 👌 готово / 😢 ошибка)."""
    safe("setMessageReaction", chat_id=chat, message_id=mid, reaction=[{"type": "emoji", "emoji": emoji}])


def btn(text, data, style=None, disabled=False):
    """Inline-кнопка. style: success (зелёная) / danger (красная) / primary (синяя); disabled — серая, не нажимается (Bot API 10.3)."""
    b = {"text": text, "callback_data": data}
    if style:
        b["style"] = style
    if disabled:
        b["disabled"] = {}
    return b


def kbtn(text, style=None):
    """Кнопка нижней (reply) клавиатуры; её текст приходит боту обычным сообщением."""
    return {"text": text, **({"style": style} if style else {})}


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tgtime(ts, fmt, tz):
    """Время, которое Telegram покажет в поясе читающего; внутри — запасной текст."""
    fallback = datetime.fromtimestamp(ts, ZoneInfo(tz)).strftime("%H:%M" if fmt == "t" else "%d.%m")
    return f'<tg-time unix="{int(ts)}" format="{fmt}">{fallback}</tg-time>'


def name(uid):
    """Имя ведущего по Telegram ID (из PEOPLE)."""
    return PEOPLE.get(int(uid), {}).get("name", "?")


# ───────────────────────── состояние ─────────────────────────

def st_load():
    """Состояние из state2.json с значениями по умолчанию (offset getUpdates, номер последнего выпуска, текущий выпуск)."""
    s = P.jload(STATE_F, {})
    s.setdefault("offset", P.jload(DATA / "state.json", {}).get("offset", 0))
    s.setdefault("last_n", START_N)
    s.setdefault("ep", None)
    s.setdefault("await", {})
    s.setdefault("history", [])
    s.setdefault("pending_new", {})
    s.setdefault("topics", [])                      # темы и заметки для будущих выпусков (topic_*)
    s.setdefault("topic_seq", 0)
    s.setdefault("topics_v", 0)                     # растёт при любой правке — мини-приложение перечитывает список
    return s


def save():
    """Сохранить состояние (атомарно). Вызывается после каждого апдейта и в фоновых потоках."""
    P.jsave(STATE_F, S)


def aai_key():
    """Ключ AssemblyAI: ASSEMBLYAI_API_KEY или файл секретов (AAI_KEY_FILE)."""
    return _secret("ASSEMBLYAI_API_KEY", "AAI_KEY_FILE", "/secrets/assemblyai_key")


def cfg_for(job=None):
    """Настройки обработки для выпуска (podcast.load_cfg) + путь к заставкам и имя подкаста."""
    os.environ["PODCAST_SETTINGS"] = str(DATA / "settings.json")
    c = P.load_cfg(job)
    c["assets"] = str(ASSETS)
    c["podcast_name"] = c.get("podcast_name") or PODCAST
    return c


def ep():
    """Текущий выпуск (state['ep']) или None."""
    return S.get("ep")


def audience():
    """Кому слать: обоим ведущим, а в тестовом режиме выпуска (ep["test_to"]) — только одному."""
    e = ep()
    return [int(e["test_to"])] if e and e.get("test_to") else ORDER


# ───────────────────────── карточка ─────────────────────────

def bar(f, n=8):
    k = int(round(max(0.0, min(1.0, f)) * n))
    return "▰" * k + "▱" * (n - k)


def approvals_line(e):
    """«Аня ✅ · Боря ⏳» — кто одобрил выпуск."""
    return " · ".join(f"{name(u)} {'✅' if e['approvals'].get(str(u)) else '⏳'}" for u in ORDER)


def tracks_ready(e):
    """Обе дорожки на месте и подтверждены (короткие < 5 мин требуют «Да, она»)."""
    return all(str(u) in e["tracks"] and e["tracks"][str(u)].get("ok") for u in ORDER)


def nn_denoise(cfg):
    """Задана ли нейросетевая модель шумодава (RNNoise, настройка rnnoise_model): тогда шумодав — «нейро / выкл», без градаций."""
    return bool(cfg.get("rnnoise_model") and Path(cfg["rnnoise_model"]).is_file())


def noise_label(cfg):
    """Как назвать шумодав в карточке: «нейро», «12 дБ» или «выкл»."""
    if not cfg["denoise"]:
        return "выкл"
    return "нейро" if nn_denoise(cfg) else (f"{cfg['denoise_nr']} дБ" if cfg["denoise_nr"] > 0 else "выкл")


def card(uid):
    """Текст и inline-кнопки живой карточки выпуска для ведущего uid (в его часовом поясе).
    Одна функция на все состояния: waiting/grace → processing → preview → review/approved, failed, cancelled.
    """
    e = ep()
    n, st = e["n"], e["status"]
    tz = PEOPLE[uid]["tz"]
    L, rows = [], []
    if st in ("waiting", "grace"):
        L += [f"🎙 <b>{PODCAST} №{n}</b>", f"<i>{tgtime(e['opened_at'], 'wD', tz)}</i>", ""]
        for u in ORDER:
            t = e["tracks"].get(str(u))
            if not t:
                L.append(f"{name(u)} · ⏳ жду дорожку")
            else:
                warn = "" if t.get("ok") else " ⚠️ точно она?"
                L.append(f"{name(u)} · ✅ {P.fmt_ts(t['dur'])} · <code>{esc(t['file'][:32])}</code>{warn}")
        if WEBAPP_URL:
            mk = pending_marks()
            L.append("🔔 синхросигнал: " + " · ".join(
                f"{name(u)} {'×' + str(len(mk[u])) if mk[u] else '—'}" for u in ORDER))
        L.append("")
        if st == "grace":
            left = max(0, int(e["grace_until"] - time.time()))
            L.append(f"Обе дорожки на месте — начинаю через <b>{left} с</b>")
            L.append("<i>Пришлёшь другой файл до старта — он заменит твой.</i>")
            rows = [[btn("▶️ Начать сейчас", "start", "success"), btn("✋ Подождать", "hold")]]
        else:
            if tracks_ready(e):
                L.append("⏸ Обе дорожки на месте. Нажми ▶️, когда будете готовы.")
            elif str(uid) not in e["tracks"]:
                L.append("<i>Пришли свою дорожку: аудио, видео, архив или ссылку на Google Drive.</i>")
            rows = [[btn("▶️ Начать", "start", "success", disabled=not tracks_ready(e)),
                     btn("🗑 Отменить", "cancel?", "danger")]]
    elif st == "processing":
        what = {"run": "собираю", "preview": "собираю превью", "render": "собираю полный выпуск"}.get(e.get("mode"), "собираю")
        L += [f"⚙️ <b>{PODCAST} №{n}</b> · {what}", ""]
        for key in e.get("stage_keys") or [k for k, _ in STAGES]:
            label = LABEL[key]
            s = e["stages"].get(key, {})
            if s.get("st") == "done":
                L.append(f"✅ {label}" + (f" · {esc(s['info'])}" if s.get("info") else ""))
            elif s.get("st") == "run":
                f = s.get("frac") or 0
                L.append(f"⚙️ {label}" + (f" {bar(f)} {int(f * 100)}%" if f else "…"))
            elif s.get("st") == "skip":
                L.append(f"➖ <s>{label}</s>")
            else:
                L.append(f"◻️ {label}")
        L += ["", f"готово ≈ {tgtime(e['eta'], 't', tz)}"]
    elif st == "preview":
        r = e["preview"]
        cfg = cfg_for(e["job"])
        bname = name(ORDER[1])
        L += [f"🎧 <b>{PODCAST} №{n}</b> · превью",
              f"«{esc(r.get('title', ''))}» · выпуск будет ≈ {P.fmt_ts(r['full_duration'])}"]
        if r.get("first_words"):
            L += ["", "▶️ <b>начало</b>" + (f" · <i>{esc(r['start_reason'])}</i>" if r.get("start_reason") else ""),
                  f"<blockquote expandable>{esc(r['first_words'])}</blockquote>", "",
                  "⏹ <b>конец</b>" + (f" · <i>{esc(r['end_reason'])}</i>" if r.get("end_reason") else ""),
                  f"<blockquote expandable>{esc(r['last_words'])}</blockquote>"]
        bd = int(round(r.get("b_delay", 0) * 1000))
        profs = profiles_of(cfg)
        eq_txt = ("EQ: " + ", ".join(f"{name(u)} — {profs[i] or 'без профиля'}" for i, u in enumerate(ORDER))
                  if cfg["eq"] and cfg.get("mic_profiles") and len(cfg["mic_profiles"]) > 1 else f"EQ {'вкл' if cfg['eq'] else 'выкл'}")
        nz = noise_label(cfg)
        L += ["", f"компрессор {cfg['comp_ratio']:g}:1 · {eq_txt} · {bname} {'+' if bd > 0 else ''}{bd} мс",
              f"шумодав {nz} · де-эссер {'вкл' if cfg['deesser'] else 'выкл'}"
              f" · выравнивание {'вкл' if cfg['dynaudnorm'] else 'выкл'}",
              "", "<i>Послушай превью выше: синхронизация, громкость, эффекты. Всё ок — соберу полный выпуск (~8 мин). "
                  "Правки пересоберут только превью.</i>"]
        i = RATIOS.index(cfg["comp_ratio"]) if cfg["comp_ratio"] in RATIOS else 2
        rows = [[btn("✅ Собирай полный", "full", "success")],
                [btn("✂️ Начало/конец", "ptrim"), btn(f"🎛 EQ: {'вкл' if cfg['eq'] else 'выкл'}", "peq")],
                [btn("🎚 Мягче", "csoft", disabled=i == 0), btn("🎚 Жёстче", "chard", disabled=i == len(RATIOS) - 1)],
                [btn(f"⏪ {bname} −100 мс", "bm"), btn(f"⏩ {bname} +100 мс", "bp")]]
        if cfg["eq"] and len(cfg.get("mic_profiles") or {}) > 1:
            rows.append([btn(f"🎙 {name(u)}: {profs[i] or '—'}", f"prof:{i}") for i, u in enumerate(ORDER)])
        rows.append([btn(f"🔇 Шумодав {nz}", "nr"), btn(f"〰 Де-эссер {'вкл' if cfg['deesser'] else 'выкл'}", "dees"),
                     btn(f"📈 Выравн. {'вкл' if cfg['dynaudnorm'] else 'выкл'}", "dyn")])
    elif st in ("review", "approved"):
        r = e["report"]
        L += [f"🎧 <b>{PODCAST} №{n}</b> · " + ("одобрен 🎉" if st == "approved" else "на проверке"),
              f"«{esc(r.get('title', ''))}» · {P.fmt_ts(r['duration'])} · {r['lufs']} LUFS"]
        if r.get("first_words"):
            L += ["", "▶️ <b>начало</b>" + (f" · <i>{esc(r['start_reason'])}</i>" if r.get("start_reason") else ""),
                  f"<blockquote expandable>{esc(r['first_words'])}</blockquote>", "",
                  "⏹ <b>конец</b>" + (f" · <i>{esc(r['end_reason'])}</i>" if r.get("end_reason") else ""),
                  f"<blockquote expandable>{esc(r['last_words'])}</blockquote>"]
        L += ["", approvals_line(e)]
        if (e.get("published") or {}).get("link"):
            L.append(f"📢 в канале: {e['published']['link']}")
        if e.get("notes"):
            L += [""] + [f"<i>{esc(x)}</i>" for x in e["notes"]]
        cn = C.cuts_note(e["job"], cfg_for(e["job"])) if e.get("job") else []
        if cn:
            L += [""] + [esc(x) for x in cn]
        if st == "review":
            L += ["", "<i>Послушай и одобри. Любая правка сбросит одобрения.</i>"]
            mine = e["approvals"].get(str(uid))
            rows.append([btn("↩️ Снять одобрение", "unapprove") if mine else btn("✅ Одобряю", "approve", "success")])
        eq = cfg_for(e.get("job"))["eq"]
        rows += [[btn("✂️ Обрезка", "trim"), btn("📑 Главы", "chap")],
                 [btn("🪚 Вырезать кусок", "cut")] + ([btn("↩️ Вернуть вырез", "uncut")] if cn else []),
                 [btn(f"🎛 EQ: {'вкл' if eq else 'выкл'}", "eq"), btn("🔁 Пересобрать", "rerender")]]
    elif st == "failed":
        L += [f"😢 <b>{PODCAST} №{n}</b> · сборка упала", "", f"<code>{esc(e.get('error', '')[-500:])}</code>"]
        rows = [[btn("🔁 Ещё раз", "retry", "primary"), btn("🗑 Отменить", "cancel?", "danger")]]
    elif st == "cancelled":
        L += [f"🗑 <s>{PODCAST} №{n}</s> · отменён"]
    if e.get("test_to") and L:
        L[0] += " · 🧪 тест, видишь только ты"
    return "\n".join(L), ({"inline_keyboard": rows} if rows else None)


def profiles_of(cfg):
    """Профили микрофонов ведущих (по порядку ORDER) из настроек выпуска; '' — не задан."""
    p = list(cfg.get("track_profile") or [])
    return [p[i] if i < len(p) else "" for i in range(len(ORDER))]


def status_text():
    """Строка статуса для нижней клавиатуры («🎙 №12 · Аня ✅ · Боря ⏳»); None — выпуска нет."""
    e = ep()
    if not e or e["status"] in ("approved", "cancelled"):
        return None
    n, st = e["n"], e["status"]
    if st in ("waiting", "grace"):
        return f"🎙 №{n} · " + " · ".join(f"{name(u)} {'✅' if str(u) in e['tracks'] else '⏳'}" for u in ORDER)
    if st == "processing":
        return f"⚙️ №{n} · собираю"
    if st == "preview":
        return f"🎧 №{n} · превью ждёт проверки"
    if st == "review":
        return f"👂 №{n} · " + approvals_line(e)
    return f"😢 №{n} · ошибка"


def reply_kb():
    """Нижняя клавиатура: статус + «➕ Записали выпуск №N», когда можно открыть новый.
    Telegram меняет её только вместе с новым сообщением — поэтому она прикладывается к событиям.
    """
    e = ep()
    st = status_text()
    rows = [[kbtn(st)]] if st else []
    if not e or e["status"] in ("review", "approved", "cancelled"):
        rows.append([kbtn(f"➕ Записали выпуск №{S['last_n'] + 1}", "primary"), kbtn("💡 Тема")])
    else:
        rows.append([kbtn("💡 Тема")])
    return {"keyboard": rows, "resize_keyboard": True, "is_persistent": True}


def flush_cards(force=False):
    """Править карточки у обоих; во время сборки — не чаще раза в 4 с."""
    e = ep()
    if not e or (not force and time.time() - _last_flush[0] < 4):
        return
    _last_flush[0] = time.time()
    for uid in audience():
        mid = e["cards"].get(str(uid))
        text, kb = card(uid)
        if mid:
            safe("editMessageText", chat_id=uid, message_id=mid, text=text, parse_mode="HTML",
                 reply_markup=kb or {"inline_keyboard": []})


def new_card(uid):
    """Карточка заново внизу чата (старую удаляем) и в закреп."""
    e = ep()
    old = e["cards"].get(str(uid))
    if old:
        safe("unpinChatMessage", chat_id=uid, message_id=old)
        safe("deleteMessage", chat_id=uid, message_id=old)
    text, kb = card(uid)
    m = send(uid, text, kb, disable_notification=True)   # карточка — без звука; со звуком только готовый выпуск
    if m:
        e["cards"][str(uid)] = m["message_id"]
        safe("pinChatMessage", chat_id=uid, message_id=m["message_id"], disable_notification=True)


def event(text, to=None, silent=False):
    """Событие обоим (или выбранным) — заодно обновляет нижнюю клавиатуру."""
    for uid in to or audience():
        send(uid, text, reply_kb(), disable_notification=silent)


# ───────────────────────── выпуск: открытие / дорожки / старт ─────────────────────────

def open_episode(by):
    """Открыть выпуск №(last_n+1) и показать карточки. False — нельзя: текущий ещё не закончен."""
    e = ep()
    if e and e["status"] in ("waiting", "grace", "processing", "preview", "failed"):
        return False
    if e:
        S["history"] = (S["history"] + [{"n": e["n"], "job": e.get("job"), "status": e["status"],
                                          "title": (e.get("report") or {}).get("title")}])[-30:]
        for uid in audience():
            if e["cards"].get(str(uid)):
                safe("unpinChatMessage", chat_id=uid, message_id=e["cards"][str(uid)])
    S["last_n"] += 1
    S["ep"] = {"n": S["last_n"], "status": "waiting", "opened_at": time.time(), "opened_by": by,
               "tracks": {}, "cards": {}, "approvals": {}, "stages": {}, "reminded": False}
    log(f"открыт №{S['last_n']} ({name(by)})")
    for uid in audience():
        event(f"🎙 Открыт выпуск №{S['last_n']}" + ("" if uid == by else f" — {name(by)} ждёт твою дорожку"), [uid])
        new_card(uid)
    return True


def media_of(m):
    """Сообщение → (file_id, имя файла, размер, тип, mime) для аудио/документа/голосового/видео/фото, иначе None."""
    for kind in ("audio", "document", "voice", "video", "video_note"):
        x = m.get(kind)
        if x:
            fname = x.get("file_name") or f"{kind}_{m['message_id']}" + {"voice": ".ogg", "video_note": ".mp4",
                                                                         "video": ".mp4"}.get(kind, "")
            return x["file_id"], fname, x.get("file_size", 0), kind, x.get("mime_type", "")
    if m.get("photo"):
        x = m["photo"][-1]
        return x["file_id"], f"photo_{m['message_id']}.jpg", x.get("file_size", 0), "photo", "image/jpeg"
    return None


def download(file_id, dest):
    """Скачать файл через локальный Bot API: getFile отдаёт абсолютный путь в общем томе — копируем и удаляем исходник."""
    f = tg("getFile", _t=1800, file_id=file_id)
    src = Path(f["file_path"])                # --local: абсолютный путь в общем томе
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    try:
        src.unlink()                          # место на томе сервера не копим
    except OSError:
        pass
    return dest


MAX_FILE = int(2.1e9)                                     # как у локального Bot API: до 2 ГБ


def drive_download(url, dest):
    """Ссылка Google Drive (доступ «всем, у кого есть ссылка») → файл. → (путь, исходное имя)."""
    m = re.search(r"/file/d/([\w-]+)", url) or re.search(r"[?&]id=([\w-]+)", url)
    if not m:
        raise ValueError("не нашёл ID файла в ссылке")
    r = requests.get(f"https://drive.usercontent.google.com/download?id={m.group(1)}&export=download&confirm=t",
                     stream=True, timeout=60)
    r.raise_for_status()
    if "text/html" in r.headers.get("content-type", ""):
        raise ValueError("Google Drive не отдал файл: проверь доступ «всем, у кого есть ссылка». "
                         "Если доступ открыт — пришли дорожку файлом прямо в Telegram (до 2 ГБ), так надёжнее")
    fname = (re.findall(r'filename="?([^";]+)', r.headers.get("content-disposition", "")) or ["drive.audio"])[0]
    if int(r.headers.get("content-length", "0") or 0) > MAX_FILE:
        raise ValueError("файл больше 2 ГБ")
    dest = dest.with_suffix(Path(fname).suffix.lower() or ".audio")
    dest.parent.mkdir(parents=True, exist_ok=True)
    got, deadline = 0, time.time() + 3 * 3600
    with open(dest, "wb") as f:
        for chunk in r.iter_content(1 << 20):
            got += len(chunk)
            if got > MAX_FILE or time.time() > deadline:
                f.close()
                dest.unlink(missing_ok=True)
                raise ValueError("файл больше 2 ГБ или качается слишком долго")
            f.write(chunk)
    return dest, fname


def dur_or_0(p):
    """Длительность или 0, если файл не читается как аудио."""
    try:
        return P.duration(p)
    except Exception:  # noqa: BLE001
        return 0.0


def extract_audio(arc):
    """Архив → самая длинная аудиодорожка внутри (мусор вроде __MACOSX и ._* пропускаем).
    → (путь, имя внутри архива, сколько всего аудио нашлось)"""
    d = arc.with_suffix(".x")
    d.mkdir()
    try:
        # сначала оглавление: защита от «zip-бомб» (слишком много файлов или распакованный объём)
        lst = subprocess.run(["bsdtar", "-tvf", str(arc)], check=True, capture_output=True, text=True, timeout=300).stdout
        rows = [ln.split() for ln in lst.splitlines() if ln.strip()]
        total = sum(int(r[4]) for r in rows if len(r) > 4 and r[4].isdigit())
        if len(rows) > 2000 or total > 2 * MAX_FILE:
            raise ValueError("архив слишком большой или в нём слишком много файлов")
        subprocess.run(["bsdtar", "--no-same-owner", "--no-same-permissions", "-xf", str(arc), "-C", str(d)],
                       check=True, capture_output=True, timeout=1800)
        cands = [p for p in d.rglob("*") if not p.is_symlink() and p.is_file() and p.suffix.lower() in AUDIO_EXT
                 and "__MACOSX" not in p.parts and not p.name.startswith("._")]
        if not cands:
            raise ValueError("в архиве не нашлось аудио")
        best = max(cands, key=lambda p: (dur_or_0(p), p.stat().st_size))
        out = arc.with_name(arc.stem + "_inner" + best.suffix.lower())
        shutil.move(best, out)
        return out, best.name, len(cands)
    finally:
        shutil.rmtree(d, ignore_errors=True)
        arc.unlink(missing_ok=True)


def take_track(m, uid, drive_url=None):
    """Скачать, достать аудио, проверить, положить в выпуск. Идёт в своём потоке — большие файлы долго."""
    chat, mid = m["chat"]["id"], m["message_id"]
    react(chat, mid, "👀")
    tmp = INCOMING / f"{int(time.time())}_{uid}_{mid}"
    try:
        if drive_url:
            path, fname = drive_download(drive_url, tmp)
        else:
            fid, fname, size, kind, mime = media_of(m)
            path = download(fid, tmp.with_suffix(Path(fname).suffix.lower() or ".audio"))
        note = ""
        if path.suffix.lower() in ARCH_EXT:
            react(chat, mid, "⚡")
            path, inner, cnt = extract_audio(path)
            note = f"из архива взял {inner}" + (f" (самый длинный из {cnt} аудиофайлов)" if cnt > 1 else "")
            fname = inner
        dur = dur_or_0(path)
        if dur <= 0:
            raise ValueError("не получилось прочитать как аудио")
    except Exception as e:  # noqa: BLE001
        log(traceback.format_exc())
        react(chat, mid, "😢")
        send(chat, f"😢 Не получилось взять файл: {esc(e)}")
        return
    with lock:
        e = ep()
        if not e or e["status"] not in ("waiting", "grace"):
            path.unlink(missing_ok=True)
            send(chat, "Выпуск уже собирается — этот файл не взял.")
            return
        old = e["tracks"].get(str(uid))
        if old:
            Path(old["path"]).unlink(missing_ok=True)
        e["tracks"][str(uid)] = {"path": str(path), "file": fname, "dur": dur, "ok": dur >= MIN_TRACK, "msg": mid}
        react(chat, mid, "👌")
        log(f"№{e['n']}: дорожка {name(uid)} {fname} {P.fmt_ts(dur)}" + (" (замена)" if old else ""))
        if note:
            send(chat, f"📦 {esc(note)}", disable_notification=True)
        if dur < MIN_TRACK:
            send(chat, f"⚠️ Дорожка всего {P.fmt_ts(dur)}. Это точно запись выпуска?",
                 {"inline_keyboard": [[btn("Да, она", "confirm", "success"), btn("Пришлю другую", "noop")]]})
        event(f"📥 {name(uid)}: дорожка {'заменена' if old else 'на месте'} · {P.fmt_ts(dur)}", silent=True)
        maybe_grace()
        flush_cards(force=True)
        save()


def maybe_grace():
    """Если обе дорожки готовы — запустить 30-секундный отсчёт до старта (или продлить его после замены файла)."""
    e = ep()
    if e["status"] == "waiting" and tracks_ready(e) and not e.get("hold"):
        e["status"], e["grace_until"] = "grace", time.time() + GRACE
    elif e["status"] == "grace":
        e["grace_until"] = max(e["grace_until"], time.time() + 10)   # заменили файл — чуть подождём ещё


def start_episode():
    """Перенести дорожки в папку выпуска jobs/<№>_<дата>/tracks (A, B — по порядку PEOPLE) и начать сборку."""
    e = ep()
    job = JOBS / f"{e['n']:03d}_{time.strftime('%y%m%d')}"
    (job / "tracks").mkdir(parents=True, exist_ok=True)
    tracks = []
    for i, uid in enumerate(ORDER):
        t = e["tracks"][str(uid)]
        dst = job / "tracks" / f"{'AB'[i]}{Path(t['path']).suffix}"
        shutil.move(t["path"], dst)
        t["path"] = str(dst)
        tracks.append(dst)
    P.new_job(job, tracks, [name(u) for u in ORDER], n=e["n"])
    mk = pending_marks(e["n"])
    if any(mk.values()):
        meta = P.jload(job / "meta.json")
        meta["sync_marks"] = [[m["t"] for m in mk[u]] for u in ORDER]
        meta["sync_marks_meta"] = [[{k: m.get(k) for k in ("t", "err", "rtt", "lat")} for m in mk[u]] for u in ORDER]
        P.jsave(job / "meta.json", meta)
        log(f"№{e['n']}: синхросигналы " + ", ".join(f"{name(u)} {len(mk[u])}" for u in ORDER))
    S["marks_since"] = time.time()
    S["marks"] = [m for m in S.get("marks", []) if m["at"] > time.time() - 7 * 86400]
    e["job"] = str(job)
    begin_processing("run")


def begin_processing(mode):
    """Поставить сборку в очередь. mode: run (синхр. → расшифровка → план → превью),
    preview (только превью), render (полный выпуск). Сбрасывает одобрения, карточку переносит вниз.
    """
    e = ep()
    e["status"], e["mode"], e["approvals"], e["notes"] = "processing", mode, {}, []
    D = max(t["dur"] for t in e["tracks"].values()) / 3600 if e["tracks"] else 1.0
    keys = MODE_KEYS[mode]
    e["stage_keys"], e["stages"] = keys, {k: {"st": "todo"} for k in keys}
    e["dur_h"], e["t0"] = D, time.time()
    e["eta"] = time.time() + sum(EXPECT[k] * 60 * D for k in keys)
    jobs_q.put(e["n"])
    text = {"run": "Собираю", "preview": "Пересобираю превью", "render": "Собираю полный выпуск"}[mode]
    if mode == "render" and e.get("report"):
        text = "Пересобираю полный выпуск"
    event(f"⚙️ {text} №{e['n']}", silent=True)
    for uid in audience():                         # карточка с прогрессом — вниз, перед глазами
        new_card(uid)
    save()


# ───────────────────────── сборка ─────────────────────────

def on_progress(key, frac=None, info=None):
    """Колбэк из podcast.py: отметить этап, долю выполнения и инфо; пересчитать ETA; обновить карточки (не чаще 4 с)."""
    with lock:
        e = ep()
        if not e or e["status"] != "processing":
            return
        st, order = e["stages"], e.get("stage_keys") or [k for k, _ in STAGES]
        if key not in order:
            return
        for k in order[: order.index(key)]:
            if st[k]["st"] == "run":
                st[k]["st"] = "done"
            elif st[k]["st"] == "todo":
                st[k]["st"] = "skip"
        s = st[key]
        s["st"] = "done" if frac is not None and frac >= 1.0 else "run"
        if frac is not None:
            s["frac"] = frac
        if info:
            s["info"] = info
        D = e["dur_h"]
        left = sum(EXPECT[k] * 60 * D for k in order[order.index(key) + 1:] if st[k]["st"] == "todo")
        cur = EXPECT[key] * 60 * D * (1 - (frac or 0)) if s["st"] == "run" else 0
        e["eta"] = time.time() + cur + left
        flush_cards()


def worker():
    """Поток сборки: берёт номер из очереди, вызывает podcast.run/render, по итогу — finish_preview/finish или failed."""
    while True:
        n = jobs_q.get()
        with lock:
            e = ep()
            if not e or e["n"] != n or e["status"] != "processing":
                continue
            job, mode = e["job"], e["mode"]
        t0 = time.time()
        try:
            log(f"сборка №{n} ({mode})")
            cfg = cfg_for(job)
            if mode == "run":
                rep = P.run(job, cfg, on_progress, key=aai_key(), preview=True)
            elif mode == "preview":
                rep = P.render(job, cfg, on_progress, preview=True)
            else:
                rep = P.render(job, cfg, on_progress)
            log(f"№{n}: {'превью' if mode != 'render' else 'выпуск'} за {int(time.time() - t0) // 60} мин")
            finish_preview(rep) if mode != "render" else finish(rep)    # сами берут lock только на состояние
        except Exception as ex:  # noqa: BLE001
            log(traceback.format_exc())
            with lock:
                e = ep()
                e["status"], e["error"] = "failed", str(ex)
                event(f"😢 Сборка №{n} упала")
                flush_cards(force=True)
                save()


def tg_thumb(job):
    """Миниатюра обложки для Telegram (JPEG ≤ 320×320, ≤ 200 КБ — иначе sendAudio отказывает) или None.
    В сам MP3 обложка вшивается в полном размере (podcast.write_tags)."""
    cover = P.find_asset(cfg_for(job), "cover")
    if not cover or cover.suffix.lower() not in (".jpg", ".jpeg", ".png"):
        return None
    th = Path(job) / "thumb.jpg"
    try:
        if not th.exists() or th.stat().st_mtime < cover.stat().st_mtime:
            P.ff(["-i", cover, "-vf", "scale=320:320:force_original_aspect_ratio=decrease", "-q:v", "4", th])
        return th if th.stat().st_size <= 200_000 else None
    except Exception as ex:  # noqa: BLE001 — без миниатюры выпуск всё равно уходит
        log(f"миниатюра обложки: {ex}")
        return None


def finish_preview(rep):
    """Превью готово: разослать аудио (второму — по file_id, без повторной загрузки), статус preview, карточки вниз.
    Вызывается БЕЗ lock (как finish): загрузка — без блокировки, состояние — под ней."""
    with lock:
        e = ep()
        e["status"], e["preview"], e["preview_at"], e["reminded_preview"] = "preview", rep, time.time(), {}
        save()
        recipients, kb, job = audience(), reply_kb(), e["job"]
    lines = [f"🎧 <b>Превью №{rep['n']}</b> · заставка и первая минута — послушай и подтверди в карточке"]
    audio_id = None
    for uid in recipients:
        try:
            if audio_id:
                tg("sendAudio", chat_id=uid, audio=audio_id, caption="\n".join(lines), parse_mode="HTML",
                   reply_markup=kb)
            else:
                files = {"audio": (rep["file"], open(rep["path"], "rb"))}
                th = tg_thumb(job)
                if th:
                    files["thumbnail"] = ("thumb.jpg", open(th, "rb"))
                try:
                    r = tg("sendAudio", files=files, _t=600, chat_id=uid,
                           caption="\n".join(lines), parse_mode="HTML", title=f"превью №{rep['n']}",
                           performer=" и ".join(name(u) for u in ORDER), duration=math.ceil(rep["duration"]),
                           reply_markup=kb)
                finally:
                    for f in files.values():
                        f[1].close()
                audio_id = r["audio"]["file_id"]
        except Exception as ex:  # noqa: BLE001
            log(f"превью {name(uid)}: {ex}")
    with lock:
        for uid in audience():
            new_card(uid)
        save()


def finish(rep, quiet=False, note=""):
    """Полный выпуск готов: MP3 (подпись в формате канала) и zip-архив обоим, статус review, заметки о проблемах.
    Вызывается БЕЗ lock: состояние меняется под lock, а загрузка в Telegram (может идти минуты) — без него,
    чтобы бот всё это время отвечал на кнопки и сообщения. Если в выпуске есть вырезы — приписка о них к MP3 и архиву.
    quiet=True — только эти два сообщения: карточки не переносятся вниз, а правятся на месте (ручная доставка);
    note — строка-приписка к MP3 и архиву (например, что именно изменилось при ручной пересборке)."""
    with lock:
        e = ep()
        job = Path(e["job"])
        out = job / "out"
        plan, meta = P.jload(job / "plan.json"), P.jload(job / "meta.json")
        e["status"], e["report"], e["approvals"] = "review", rep, {}
        notes = []
        if plan.get("llm_error"):
            notes.append("⚠️ главы не получились: " + plan["llm_error"][:120])
        if meta.get("transcribe_error"):
            notes.append("⚠️ расшифровка не удалась: " + meta["transcribe_error"][:120])
        sy = rep["sync"]
        sig = (P.jload(job / "sync.json") or {}).get("signal") or {}
        if sig.get("rejected") is not None:
            notes.append(f"⚠️ синхросигнал разошёлся с разговором на {sig['rejected'] * 1000:+.0f} мс (часы сверились плохо) — синхронизировал по разговору")
        if sy["mode"] == "turns" and sy["confidence"] < 1.3:
            notes.append("⚠️ синхронизация неуверенная — послушай паузы между репликами")
        full_sync = P.jload(job / "sync.json") or {}
        if full_sync.get("mode") == "signal" and full_sync.get("by_talk"):
            d = (full_sync["offset"] - full_sync["by_talk"]["offset"]) * 1000
            ce = [x for x in ((full_sync.get("signal") or {}).get("clock_err") or []) if x is not None]
            notes.append(f"🔔 синхронизация по сигналу; по разговору было бы на {d:+.0f} мс иначе"
                         + (f" (часы ±{'/±'.join(str(x) for x in ce)} мс)" if ce else ""))
            try:                              # статистика: насколько сигнал и разговор расходятся на живых записях
                with open(JOBS / "signal_stats.jsonl", "a") as fh:
                    fh.write(json.dumps({"n": e["n"], "at": time.time(), "signal": full_sync["offset"],
                                         "talk": full_sync["by_talk"]["offset"], "delta_ms": round(d, 1),
                                         "clock_err": ce, "drift_ms_h": full_sync.get("drift_ms_per_hour")}) + "\n")
            except OSError as ex:
                log(f"статистика сигнала: {ex}")
        e["notes"] = notes
        save()
        recipients, kb = audience(), reply_kb()

    cn = ([note] if note else []) + C.cuts_note(job, cfg_for(str(job)))                 # приписка только в копиях ведущих; в канал уходит чистая подпись
    tail = ("\n\n" + "\n".join(cn)) if cn else ""
    caption = rep.get("caption", "")
    if len(caption) + len(tail) > 1024:
        caption = caption[: 1020 - len(tail)] + "…"
    caption += tail
    zp = job / f"{Path(rep['file']).stem}.zip"
    with zipfile.ZipFile(zp, "w") as z:
        for f in sorted(out.iterdir()):
            z.write(f, f.name, zipfile.ZIP_STORED if f.suffix == ".mp3" else zipfile.ZIP_DEFLATED)
    audio_id = doc_id = None
    for uid in recipients:                         # второму — по file_id, без повторной загрузки
        try:
            if audio_id:
                tg("sendAudio", chat_id=uid, audio=audio_id, caption=caption)
            else:
                files = {"audio": (rep["file"], open(out / rep["file"], "rb"))}
                th = tg_thumb(job)
                if th:
                    files["thumbnail"] = ("thumb.jpg", open(th, "rb"))
                try:
                    r = tg("sendAudio", files=files, _t=1800, chat_id=uid, caption=caption,
                           title=f"#{rep['n']}: {rep['title']}" if rep.get("n") else rep["title"],
                           performer=" и ".join(meta["names"]), duration=math.ceil(rep["duration"]))
                finally:
                    for f in files.values():
                        f[1].close()
                audio_id = r["audio"]["file_id"]
                with lock:
                    if ep() and ep().get("job") == str(job):
                        ep()["audio_id"] = audio_id       # для публикации в канал без повторной загрузки
            cap = "📦 Архив: MP3, описание, главы, расшифровка, субтитры" + (("\n" + "\n".join(cn)) if cn else "")
            if doc_id:
                tg("sendDocument", chat_id=uid, document=doc_id, caption=cap, reply_markup=kb)
            else:
                with open(zp, "rb") as f:
                    r = tg("sendDocument", files={"document": (zp.name, f)}, _t=1800, chat_id=uid, caption=cap,
                           reply_markup=kb)
                doc_id = r["document"]["file_id"]
        except Exception as ex:  # noqa: BLE001
            log(f"доставка {name(uid)}: {ex}")
    zp.unlink(missing_ok=True)
    with lock:
        if quiet:
            flush_cards(force=True)
        else:
            for uid in audience():
                new_card(uid)
        save()


# ───────────────────────── сообщения, кнопки ─────────────────────────

HELP = f"""<b>Как выходит выпуск</b>
1. После записи нажмите внизу «➕ Записали выпуск» — или просто пришлите файл, выпуск откроется сам.
2. Каждый присылает свою дорожку: аудио, видео, голосовую, архив (достану аудио изнутри) или ссылку на Google Drive. До 2 ГБ.
3. Когда обе на месте — через {GRACE} с начинаю (можно успеть заменить файл). Ход сборки — в закреплённой карточке.
4. Сначала придёт превью — заставка и первая минута: проверить синхронизацию, громкость и эффекты. Можно поправить начало, EQ, компрессор, сдвиг дорожки. Всё ок — «✅ Собирай полный» (хватит одного из вас).
5. Готовый MP3 и архив придут обоим. Послушайте и нажмите «✅ Одобряю».

🔔 <b>Синхросигнал</b> (кнопка «🔔 Синхро» слева от поля ввода): во время записи нажмите «Сигнал» — в начале и, если не трудно, в конце. Звук — на динамик рядом с микрофоном. Бот совместит дорожки по нему точнее, чем по разговору. Там же — таймер записи от первого сигнала.

💡 <b>Темы</b>: кнопка «💡 Тема» внизу → напиши тему. Второй увидит её сразу. Голосовать («огонь / можно / мимо»), разбирать на темы и заметки, отмечать «в выпуск» и «обсудили» — во вкладке «Темы» в «🔔 Синхро».

Если второй дорожки нет или превью никто не проверил — на следующий день в {REMIND_HOUR}:00 напомню.

/intro /outro /cover — заменить заставки и обложку
/status — показать карточку
/cancel — отменить ввод темы"""

COMMANDS = [("status", "карточка текущего выпуска"), ("help", "как пользоваться"),
            ("intro", "заменить вступительную заставку"), ("outro", "заменить финальную заставку"),
            ("cover", "заменить обложку")]


def on_message(m, uid):
    """Входящее сообщение ведущего: заставки/обложка по команде, дорожки (файл/ссылка Drive), ответы на
    «обрезку»/«главы», нажатия нижней клавиатуры, команды.
    """
    chat = m["chat"]["id"]
    text = (m.get("text") or "").strip()
    aw = S["await"].get(str(uid))
    md = media_of(m)

    if aw and aw["kind"] in ("intro", "outro", "cover") and md:
        fid, fname, size, kind, mime = md
        ext = Path(fname).suffix.lower() or (".jpg" if kind == "photo" else ".mp3")
        for old in ASSETS.glob(aw["kind"] + ".*"):
            old.unlink()
        download(fid, ASSETS / f"{aw['kind']}{ext}")
        S["await"].pop(str(uid))
        send(chat, f"✅ {'Обложка сохранена' if aw['kind'] == 'cover' else 'Заставка сохранена'}. Будет во всех следующих выпусках.")
        return

    drive = re.search(r"https?://\S*drive\.google\.com\S*", text)
    if md or drive:
        if md and md[3] == "photo":
            send(chat, "Это картинка. Для обложки — /cover.")
            return
        if md and md[3] == "document" and not (Path(md[1]).suffix.lower() in AUDIO_EXT | ARCH_EXT
                                               or md[4].startswith(("audio/", "video/"))):
            send(chat, "Не похоже на аудио, видео или архив.")
            return
        e = ep()
        if e and e["status"] in ("processing", "preview"):
            send(chat, f"Сейчас идёт №{e['n']} — новый выпуск можно будет открыть после.")
            return
        if e and e["status"] in ("review", "failed"):
            S["pending_new"][str(uid)] = {"m": m, "drive": drive.group(0) if drive else None}
            send(chat, f"№{e['n']} ещё не одобрен. Это дорожка <b>нового</b> выпуска №{S['last_n'] + 1}?",
                 {"inline_keyboard": [[btn(f"Да, открыть №{S['last_n'] + 1}", "newep", "primary"), btn("Нет", "noop")]]})
            return
        if not e or e["status"] in ("approved", "cancelled"):
            open_episode(uid)
        threading.Thread(target=take_track, args=(m, uid, drive.group(0) if drive else None), daemon=True).start()
        return

    if aw and aw["kind"] == "ptrim" and text and not text.startswith("/") and text[:1] not in "➕🎙⚙👂😢🎧💡":
        S["await"].pop(str(uid))
        e = ep()
        if not e or e["status"] != "preview" or e["n"] != aw["n"]:
            send(chat, "Превью уже не на проверке.")
            return
        try:
            parts = text.replace("—", "-").replace("–", "-").replace(",", ".").split()
            if not parts or len(parts) > 2:
                raise ValueError("нужно одно или два числа")
            ds = float(parts[0])
            de = float(parts[1]) if len(parts) == 2 else 0.0
            plan_f = Path(e["job"]) / "plan.json"
            plan = P.jload(plan_f)
            sync = P.jload(Path(e["job"]) / "sync.json")
            lim = sync.get("timeline_end") or sync["dur_a"]
            plan["start"] = round(max(0.0, plan["start"] + ds), 2)
            plan["end"] = round(min(lim, plan["end"] + de), 2)
            plan["source"] = (plan.get("source") or "") + "+manual"
            plan["start_reason"] = plan["end_reason"] = ""
            P.jsave(plan_f, plan)
        except Exception as ex:  # noqa: BLE001
            send(chat, f"Не понял: {esc(ex)}. Нажми кнопку ещё раз.")
            return
        event(f"✏️ {name(uid)}: начало {ds:+g} с, конец {de:+g} с — пересобираю превью", silent=True)
        begin_processing("preview")
        return

    if aw and aw["kind"] == "cut" and text and not text.startswith("/") and text[:1] not in "➕🎙⚙👂😢🎧💡":
        S["await"].pop(str(uid))
        e = ep()
        if not e or e["status"] not in ("review", "approved") or e["n"] != aw["n"]:
            send(chat, "Этот выпуск уже не на проверке.")
            return
        try:
            a, b, note = C.parse_range(text)
            cut = C.add_cut(e["job"], cfg_for(e["job"]), a, b, note)
        except Exception as ex:  # noqa: BLE001
            send(chat, f"Не получилось: {esc(ex)}. Нажми «🪚 Вырезать кусок» ещё раз.")
            return
        event(f"✂️ {name(uid)}: вырезаю {cut['label']} ({int(round(cut['to'] - cut['from']))} с)"
              + (f" — {esc(note)}" if note else "") + " — пересобираю, одобрения сброшены", silent=True)
        begin_processing("render")
        return

    if aw and aw["kind"] in ("trim", "chapters") and text and not text.startswith("/") and text[:1] not in "➕🎙⚙👂😢🎧💡":
        S["await"].pop(str(uid))
        e = ep()
        if not e or e["status"] not in ("review", "approved") or e["n"] != aw["n"]:
            send(chat, "Этот выпуск уже не на проверке.")
            return
        try:
            if aw["kind"] == "trim":
                parts = text.replace("—", "-").replace("–", "-").split()
                if not parts or len(parts) > 2:
                    raise ValueError("нужно одно или два времени")
                st = None if parts[0] == "-" else parts[0]
                en = parts[1] if len(parts) == 2 and parts[1] != "-" else None
                C.apply_edits(e["job"], start=st, end=en)
                what = f"обрезка {text}"
            else:
                if not re.search(r"\d{1,2}:\d{2}", text):
                    raise ValueError("не нашёл строк вида «12:34 название»")
                C.apply_edits(e["job"], chapters_text=text)
                what = "новые главы"
        except Exception as ex:  # noqa: BLE001
            send(chat, f"Не понял: {esc(ex)}. Нажми кнопку ещё раз.")
            return
        event(f"✏️ {name(uid)}: {esc(what)} — пересобираю, одобрения сброшены", silent=True)
        begin_processing("render")
        return

    if aw and aw["kind"] == "topic" and text:
        S["await"].pop(str(uid))
        if not text.startswith("/") and text[:1] not in "➕🎙⚙👂😢🎧💡🔔":
            topic_add(uid, text)
            return
        if text == "/cancel":
            send(chat, "Ок, без темы.", reply_kb())
            return

    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else None
    if text.startswith("💡"):
        S["await"][str(uid)] = {"kind": "topic"}
        send(chat, "💡 Напиши тему одним сообщением — можно в несколько строк. Передумал — /cancel")
    elif text.startswith("➕"):
        if not open_episode(uid):
            new_card(uid)
    elif cmd in ("/start", "/help"):
        send(chat, HELP, reply_kb())
    elif cmd in ("/intro", "/outro", "/cover"):
        k = cmd[1:]
        S["await"][str(uid)] = {"kind": k}
        cur = P.find_asset(cfg_for(), k)
        send(chat, ("Пришли картинку для обложки (квадрат, jpg)." if k == "cover"
                    else f"Пришли аудиофайл {'вступительной' if k == 'intro' else 'финальной'} заставки.")
             + (f"\nСейчас: {cur.name}" if cur else "\nСейчас: нет."))
    elif cmd == "/status" or (text[:1] in "🎙⚙👂😢🎧" and text):
        if ep() and ep()["status"] != "cancelled":
            new_card(uid)
        else:
            send(chat, "Открытого выпуска нет.", reply_kb())
    elif text.startswith("🔔"):
        send(chat, "Синхросигнал открывается кнопкой «🔔 Синхро» слева от поля ввода (меню бота).")
    elif text:
        send(chat, "Пришли дорожку файлом или нажми кнопку внизу. /help — подробнее.", reply_kb())


def on_callback(q, uid):
    """Нажатие inline-кнопки карточки (старт, отмена, превью-правки, одобрение, правки готового выпуска)."""
    data = q.get("data", "")
    chat, qmid = q["message"]["chat"]["id"], q["message"]["message_id"]
    e = ep()
    ans = None
    if data == "noop":
        safe("deleteMessage", chat_id=chat, message_id=qmid)
    elif data == "newep":
        p = S["pending_new"].pop(str(uid), None)
        safe("deleteMessage", chat_id=chat, message_id=qmid)
        if p and open_episode(uid):
            threading.Thread(target=take_track, args=(p["m"], uid, p["drive"]), daemon=True).start()
    elif not e or e["status"] == "cancelled":
        ans = "Нет открытого выпуска"
    elif data == "confirm":
        t = e["tracks"].get(str(uid))
        if t:
            t["ok"] = True
            maybe_grace()
            flush_cards(force=True)
        safe("deleteMessage", chat_id=chat, message_id=qmid)
    elif data == "start" and e["status"] in ("waiting", "grace") and tracks_ready(e):
        e.pop("hold", None)
        start_episode()
    elif data == "hold" and e["status"] == "grace":
        e["status"], e["hold"] = "waiting", True
        event(f"✋ {name(uid)}: подождём со стартом №{e['n']}", silent=True)
        flush_cards(force=True)
    elif data == "cancel?" and e["status"] in ("waiting", "grace", "failed"):
        send(chat, f"Отменить выпуск №{e['n']}? Присланные дорожки удалятся.",
             {"inline_keyboard": [[btn("🗑 Да, отменить", "cancel", "danger"), btn("Нет", "noop")]]})
    elif data == "cancel" and e["status"] in ("waiting", "grace", "failed"):
        safe("deleteMessage", chat_id=chat, message_id=qmid)
        for t in e["tracks"].values():
            Path(t["path"]).unlink(missing_ok=True)
        if e.get("job"):
            shutil.rmtree(e["job"], ignore_errors=True)
        e["status"] = "cancelled"
        S["last_n"] = e["n"] - 1
        flush_cards(force=True)
        for u in audience():
            if e["cards"].get(str(u)):
                safe("unpinChatMessage", chat_id=u, message_id=e["cards"][str(u)])
        S["ep"] = None
        event(f"🗑 {name(uid)} отменил(а) выпуск №{e['n']}")
    elif data == "retry" and e["status"] == "failed":
        begin_processing(e.get("mode") or "run")
    elif data == "full" and e["status"] == "preview":
        event(f"▶️ {name(uid)}: превью ок — собираю полный выпуск №{e['n']}", silent=True)
        begin_processing("render")
    elif data == "ptrim" and e["status"] == "preview":
        S["await"][str(uid)] = {"kind": "ptrim", "n": e["n"]}
        send(chat, "Насколько сдвинуть <b>начало</b> и <b>конец</b>, в секундах:\n"
                   "<code>+3</code> — начать на 3 с позже\n"
                   "<code>-2</code> — начать на 2 с раньше\n"
                   "<code>0 -5</code> — начало как есть, закончить на 5 с раньше\n"
                   "<code>+3 +2</code> — оба сразу")
    elif (data in ("peq", "csoft", "chard", "bm", "bp", "nr", "dees", "dyn") or data.startswith("prof:")) \
            and e["status"] == "preview":
        job = Path(e["job"])
        cfg = cfg_for(e["job"])
        if data in ("bm", "bp"):
            sf = job / "sync.json"
            sy = P.jload(sf)
            sy["b_delay"] = round(sy.get("b_delay", 0.0) + (0.1 if data == "bp" else -0.1), 3)
            P.jsave(sf, sy)
            what = f"{name(ORDER[1])} {int(round(sy['b_delay'] * 1000)):+d} мс"
        else:
            stf = job / "settings.json"
            st = P.jload(stf, {})
            if data == "peq":
                st["eq"] = not cfg["eq"]
                what = f"EQ {'вкл' if st['eq'] else 'выкл'}"
            elif data.startswith("prof:"):        # следующий профиль микрофона для этого ведущего (+ «авто»)
                i = int(data.split(":")[1])
                names = list((cfg.get("mic_profiles") or {}).keys()) + ["авто"]
                profs = profiles_of(cfg)
                cur = profs[i] if profs[i] in names else names[-1]
                profs[i] = names[(names.index(cur) + 1) % len(names)]
                st["track_profile"] = profs
                what = f"профиль {name(ORDER[i])} — {profs[i]}"
            elif data == "nr" and nn_denoise(cfg):
                st["denoise"] = not cfg["denoise"]
                what = f"шумодав {'нейро' if st['denoise'] else 'выкл'}"
            elif data == "nr":
                cur = cfg["denoise_nr"] if cfg["denoise"] else 0
                nxt = NR_STEPS[(NR_STEPS.index(cur) + 1) % len(NR_STEPS)] if cur in NR_STEPS else 0
                st["denoise"], st["denoise_nr"] = nxt > 0, nxt
                what = f"шумодав {nxt or 'выкл'}{' дБ' if nxt else ''}"
            elif data == "dees":
                st["deesser"] = not cfg["deesser"]
                what = f"де-эссер {'вкл' if st['deesser'] else 'выкл'}"
            elif data == "dyn":
                st["dynaudnorm"] = not cfg["dynaudnorm"]
                what = f"выравнивание {'вкл' if st['dynaudnorm'] else 'выкл'}"
            else:
                i = RATIOS.index(cfg["comp_ratio"]) if cfg["comp_ratio"] in RATIOS else 2
                i = max(0, min(len(RATIOS) - 1, i + (1 if data == "chard" else -1)))
                st["comp_ratio"] = RATIOS[i]
                what = f"компрессор {RATIOS[i]:g}:1"
            P.jsave(stf, st)
        event(f"✏️ {name(uid)}: {what} — пересобираю превью", silent=True)
        begin_processing("preview")
    elif data in ("approve", "unapprove") and e["status"] == "review":
        e["approvals"][str(uid)] = data == "approve"
        if all(e["approvals"].get(str(u)) for u in ORDER):
            e["status"] = "approved"
            event(f"🎉 №{e['n']} одобрен обоими!")
            threading.Thread(target=publish, daemon=True).start()
        else:
            event(f"{'✅' if data == 'approve' else '↩️'} {name(uid)} "
                  f"{'одобрил(а)' if data == 'approve' else 'снял(а) одобрение'} №{e['n']}", silent=True)
        flush_cards(force=True)
    elif data in ("trim", "chap") and e["status"] in ("review", "approved"):
        if data == "trim":
            S["await"][str(uid)] = {"kind": "trim", "n": e["n"]}
            send(chat, "Пришли новое начало и конец <b>по времени готового MP3</b>:\n"
                       "<code>0:45 58:10</code> — обрезать до 0:45 и после 58:10\n"
                       "<code>0:45</code> — только начало · <code>- 58:10</code> — только конец\n"
                       f"Сейчас выпуск длится {P.fmt_ts(e['report']['duration'])}.")
        else:
            S["await"][str(uid)] = {"kind": "chapters", "n": e["n"]}
            cur = Path(e["job"]) / "out" / "chapters.txt"
            send(chat, "Скопируй, поправь и пришли обратно (время — по готовому MP3):\n\n"
                       f"<code>{esc(cur.read_text().strip() if cur.exists() else '00:00 начало')}</code>")
    elif data == "cut" and e["status"] in ("review", "approved"):
        S["await"][str(uid)] = {"kind": "cut", "n": e["n"]}
        plan = P.jload(Path(e["job"]) / "plan.json")
        cfg = cfg_for(e["job"])
        t_main, segs = C.intro_offset(cfg), C.pieces(plan)
        hints = [f"• {C.fmt(t_main + P.to_edit(segs, t))} — {esc(txt)}"
                 for t, txt in C.glitches(e["job"], P.jload(Path(e["job"]) / "meta.json")["names"],
                                          P.parse_ts(plan["start"]), P.parse_ts(plan["end"]))]
        send(chat, "Пришли, какой кусок вырезать, <b>по времени готового MP3</b> (как в таймкодах поста):\n"
                   "<code>55:39-56:09</code> или <code>55:39-56:09 сбой связи</code> — слова после времён станут пометкой\n"
                   "Края я сам сдвину в ближайшие паузы, а стык сделаю плавным; главы и расшифровка сдвинутся."
                   + (f"\n\n🔎 <b>Возможные места:</b>\n" + "\n".join(hints) if hints else "")
                   + f"\n\nСейчас выпуск длится {P.fmt_ts(e['report']['duration'])}.")
    elif data == "uncut" and e["status"] in ("review", "approved"):
        gone = C.remove_last_cut(e["job"])
        if gone:
            event(f"↩️ {name(uid)}: вырез {gone.get('label', '')} возвращён — пересобираю, одобрения сброшены", silent=True)
            begin_processing("render")
        else:
            ans = "Вырезов нет"
    elif data in ("eq", "rerender") and e["status"] in ("review", "approved"):
        what = "пересборка"
        if data == "eq":
            stf = Path(e["job"]) / "settings.json"
            st = P.jload(stf, {})
            st["eq"] = not cfg_for(e["job"])["eq"]
            P.jsave(stf, st)
            what = f"EQ {'вкл' if st['eq'] else 'выкл'}"
        event(f"✏️ {name(uid)}: {what} — пересобираю, одобрения сброшены", silent=True)
        begin_processing("render")
    else:
        ans = "Сейчас это недоступно"
    safe("answerCallbackQuery", callback_query_id=q["id"], text=ans)


def publish():
    """После одобрения обоих: выпуск в свой RSS-фид (FEED_URL, его забирают площадки) и аудио с подписью в канал
    PUBLISH_CHAT. Что не настроено — подсказка в чат. В тестовом режиме ничего не публикует."""
    with lock:
        e = ep()
        if not e or e["status"] != "approved":
            return
        rep, audio_id, n, test, job = e.get("report") or {}, e.get("audio_id"), e["n"], e.get("test_to"), e.get("job")
    if test:
        event(f"ℹ️ №{n} не опубликован: тестовый режим.", silent=True)
        return
    publish_feed(n, rep, job)
    if not PUBLISH_CHAT or not audio_id:
        why = "канал не настроен (PUBLISH_CHAT)" if not PUBLISH_CHAT else "нет аудио"
        event(f"ℹ️ №{n} не опубликован в канал автоматически: {why}. Перешли аудио в канал — подпись уже в нужном формате.",
              silent=True)
        return
    caption = rep.get("caption", "")
    if len(caption) > 1024:
        caption = caption[:1020] + "…"
    try:
        m = tg("sendAudio", chat_id=PUBLISH_CHAT, audio=audio_id, caption=caption)
        chat = m.get("chat", {})
        link = f"https://t.me/{chat['username']}/{m['message_id']}" if chat.get("username") else ""
        with lock:
            e = ep()
            if e and e["n"] == n:
                e["published"] = {"chat": PUBLISH_CHAT, "message_id": m["message_id"], "link": link, "at": time.time()}
                save()
        event(f"📢 №{n} опубликован в канале" + (f": {link}" if link else ""))
        log(f"№{n} опубликован в {PUBLISH_CHAT}")
    except Exception as ex:  # noqa: BLE001
        log(f"публикация №{n}: {ex}")
        event(f"😢 Не получилось опубликовать №{n} в канал: {esc(str(ex)[:200])}. "
              "Бот должен быть админом канала с правом публиковать.")


def publish_feed(n, rep, job):
    """Выпуск в RSS-фид: копия MP3, описание (текст + подвал + таймкоды), пересборка feed.xml. Без FEED_URL или
    без импортированной шапки фида — ничего не делает. Выпуск с тем же номером второй раз не добавляется."""
    if not FEED_URL or not (DATA / "feed" / "channel.xml").exists() or not job:
        return
    out = Path(job) / "out"
    try:
        chap = (out / "chapters.txt").read_text().strip() if (out / "chapters.txt").exists() else ""
        guid, added = F.add_episode(DATA, FEED_URL, out / rep["file"], n, rep.get("title", ""),
                                    rep.get("summary", ""), chap, rep.get("duration", 0), cfg_for(job))
        with lock:
            e = ep()
            if e and e["n"] == n:
                e["feed"] = {"guid": guid, "at": time.time()}
                save()
        if added:
            event(f"🎧 №{n} в RSS-фиде — площадки подхватят сами (обычно от минут до суток)", silent=True)
        log(f"фид: №{n} {'добавлен' if added else 'уже был'} ({guid})")
    except Exception as ex:  # noqa: BLE001
        log(f"фид №{n}: {traceback.format_exc()}")
        event(f"😢 Не получилось добавить №{n} в RSS-фид: {esc(str(ex)[:200])}")


# ───────────────────────── темы для будущих выпусков ─────────────────────────
# Добавляются в боте (кнопка «💡 Тема» → текст), смотрят и голосуют в мини-приложении (вкладка «Темы»).
# S["topics"]: [{id, text, by, at, kind: topic|note, votes: {uid: fire|ok|no}, status: open|next|done, n}]

TOPIC_VOTES = ("fire", "ok", "no")
TOPIC_MAX = 2000                                         # символов в одной теме


def webapp_btn(text, tab=None):
    """Inline-кнопка, открывающая мини-приложение (с подписью initData — в отличие от нижней клавиатуры)."""
    if not WEBAPP_URL:
        return None
    url = WEBAPP_URL + (("&" if "?" in WEBAPP_URL else "?") + f"tab={tab}" if tab else "")
    return {"text": text, "web_app": {"url": url}}


def topic_add(uid, text):
    """Новая тема от ведущего: сохранить, ответить автору, показать второму (оба видят, кто предложил)."""
    text = text.strip()[:TOPIC_MAX]
    S["topic_seq"] += 1
    t = {"id": S["topic_seq"], "text": text, "by": uid, "at": time.time(), "kind": "topic",
         "votes": {}, "status": "open", "n": None}
    S["topics"].append(t)
    S["topics_v"] += 1
    save()
    b = webapp_btn("🗳 Темы", "topics")
    kb = {"inline_keyboard": [[b]]} if b else None
    send(uid, f"💡 Тема <b>Т-{t['id']:02d}</b> сохранена. Голосовать и разбирать — во вкладке «Темы» в «🔔 Синхро».", kb)
    for u in audience():
        if u != uid:
            send(u, f"💡 {esc(name(uid))} предлагает тему <b>Т-{t['id']:02d}</b>:\n\n{esc(text)}", kb)
    log(f"тема Т-{t['id']:02d} от {name(uid)}")


def topic_episode():
    """К какому выпуску отнести «обсудили»: открытый/последний выпуск, иначе последний вышедший."""
    e = ep()
    return e["n"] if e and e["status"] != "cancelled" else S["last_n"]


def topic_op(uid, j):
    """Правка темы из мини-приложения (под lock): vote / kind / status / del. → None или текст ошибки."""
    op = j.get("op")
    t = next((x for x in S["topics"] if x["id"] == j.get("id")), None)
    if not t:
        return "нет такой темы"
    if op == "vote":
        v = j.get("v")
        if v is None:
            t["votes"].pop(str(uid), None)
        elif v in TOPIC_VOTES:
            t["votes"][str(uid)] = v
        else:
            return "не тот голос"
    elif op == "kind":
        if j.get("kind") not in ("topic", "note"):
            return "не тот вид"
        t["kind"] = j["kind"]
    elif op == "status":
        st = j.get("status")
        if st not in ("open", "next", "done"):
            return "не тот статус"
        t["status"], t["n"] = st, (topic_episode() if st == "done" else None)
    elif op == "tdel":
        if t["by"] != uid:
            return "удалить может только автор"
        S["topics"].remove(t)
    else:
        return "нет такой операции"
    S["topics_v"] += 1
    save()
    log(f"тема Т-{t['id']:02d}: {name(uid)} {op} {j.get('v') or j.get('kind') or j.get('status') or ''}")
    return None


def topics_payload():
    """Копия списка тем (вызывать под lock; отдаётся уже без него)."""
    return {"topics": [dict(t, votes=dict(t["votes"])) for t in S["topics"]], "tv": S["topics_v"]}


def rec_payload(n):
    """Таймер записи для мини-приложения: от первого сигнала выпуска («начало») до второго («конец»), мс сервера."""
    marks = pending_marks(n)
    firsts = [v[0]["t"] for v in marks.values() if v]
    seconds = [v[1]["t"] for v in marks.values() if len(v) > 1]
    return {"n": n, "start": min(firsts) * 1000 if firsts else None, "end": min(seconds) * 1000 if seconds else None}


# Сессия мини-приложения: initData живёт час, а «Синхро» держат открытым всю запись. Первый запрос с
# действующей подписью получает токен (HMAC от токена бота, 12 ч), дальше он продлевается каждым ping.
SESSION_TTL = 12 * 3600


def session_token(uid):
    exp = int(time.time()) + SESSION_TTL
    key = hmac.new(b"PodSession", TOKEN.encode(), hashlib.sha256).digest()
    return f"{uid}.{exp}.{hmac.new(key, f'{uid}.{exp}'.encode(), hashlib.sha256).hexdigest()[:40]}"


def check_session(tok):
    """Токен сессии → Telegram ID или None (подделан или истёк)."""
    try:
        uid, exp, sig = str(tok or "").split(".")
        key = hmac.new(b"PodSession", TOKEN.encode(), hashlib.sha256).digest()
        good = hmac.new(key, f"{uid}.{exp}".encode(), hashlib.sha256).hexdigest()[:40]
        if hmac.compare_digest(good, sig) and int(exp) > time.time():
            return int(uid)
    except (ValueError, TypeError):
        pass
    return None


def handle(u):
    """Один апдейт Telegram: служебные «закрепил сообщение» удаляем, чужих не пускаем (отвечаем их ID)."""
    m, q = u.get("message"), u.get("callback_query")
    if m and m.get("pinned_message"):          # служебное «бот закрепил сообщение» — убираем, чтобы не мусорить
        safe("deleteMessage", chat_id=m["chat"]["id"], message_id=m["message_id"])
        return
    who = (m or q or {}).get("from", {}).get("id")
    if who not in PEOPLE:
        log(f"чужой {who}: {'сообщение' if m else 'кнопка'}")          # содержимое чужих сообщений не пишем
        if m and time.time() - _strangers.get(who, 0) > 3600:           # отвечаем не чаще раза в час
            _strangers[who] = time.time()
            send(m["chat"]["id"], f"Это закрытый бот. Твой Telegram ID: <code>{who}</code>")
        return
    if q:
        on_callback(q, who)
    elif m:
        on_message(m, who)


# ───────────────────────── мини-приложение «Синхросигнал» ─────────────────────────
# Страница webapp.html сверяет часы с сервером (GET time), проигрывает сигнал в «круглый» момент T
# и сообщает T (POST mark, с подписью Telegram initData). Бот хранит отметки до старта выпуска и кладёт
# их в meta.json["sync_marks"]; podcast.analyze ищет сигнал в дорожках и синхронизирует по нему.

def sync_n():
    """К какому выпуску относится синхросигнал сейчас: открытый (ждёт дорожки) или следующий по счёту."""
    e = ep()
    return e["n"] if e and e["status"] in ("waiting", "grace") else S["last_n"] + 1


def pending_marks(n=None):
    """Отметки синхросигнала выпуска №n (по умолчанию — sync_n()): {uid: [отметки по времени]}.
    Старые отметки без номера — по окну «после старта прошлого выпуска, не старше 3 суток»."""
    n = n or sync_n()
    since = max(S.get("marks_since", 0), time.time() - 3 * 86400)
    out = {u: [] for u in ORDER}
    for m in S.get("marks", []):
        if m["uid"] in out and (m.get("n") == n if "n" in m else m["at"] >= since):
            out[m["uid"]].append(m)
    for u in out:
        out[u].sort(key=lambda m: m["t"])
    return out


def check_init_data(init):
    """Проверка подписи Telegram WebApp initData (HMAC по токену бота). → Telegram ID или None."""
    d = dict(parse_qsl(init or "", keep_blank_values=True))
    h = d.pop("hash", "")
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    if not h or not hmac.compare_digest(hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest(), h):
        return None
    age = time.time() - int(d.get("auth_date", "0") or 0)
    if age > 3600 or age < -60:                          # подпись не старше часа и не из будущего
        return None
    try:
        return int(json.loads(d.get("user", "{}")).get("id"))
    except (ValueError, TypeError):
        return None


PRESENCE = {}                                  # uid → {"at", "ready", "err"} — кто сейчас открыл мини-приложение
FIRE = {}                                      # общий сигнал: {"T": мс по часам сервера, "by": имя}
web_lock = threading.Lock()                    # PRESENCE и FIRE — из нескольких потоков веб-сервера; общий lock не берём,
                                               # чтобы мини-приложение не ждало долгих операций бота
FIRE_LEAD_MS = 2000                            # столько нужно, чтобы команда дошла до второго устройства (опрос ~0,7 с)


class SyncHandler(BaseHTTPRequestHandler):
    timeout = 10                                   # медленных клиентов (slowloris) отключаем
    """Веб-сервер мини-приложения (префикс /podsync допускается).
    GET /       страница (сама по себе ничего не умеет без подписи Telegram);
    GET /time   время сервера, мс — для сверки часов (ничего не раскрывает);
    POST /api   {initData | token, op}: ping — присутствие, общий сигнал, таймер записи (rec), версия тем (tv)
                и токен сессии; fire — запустить сигнал у всех готовых; mark — отметка «сигнал прозвучал в T»;
                topics / vote / kind / status / tdel — темы будущих выпусков. Пускаем только ведущих из PEOPLE:
                по свежей подписи initData (≤ 1 ч) или по токену сессии (12 ч, продлевается каждым ping).
    GET|HEAD /feed.xml, /media/<файл>  RSS-фид и его аудио/обложки (только если задан FEED_URL): публично,
                только чтение из DATA/feed, имена по белому списку, докачка по Range (её требует Apple)."""

    def _path(self):
        p = self.path.split("?")[0]
        while p.startswith("/podsync"):
            p = p[len("/podsync"):]
        return p or "/"

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    _hits = {}                                     # IP → время последних запросов (общий словарь класса)

    def _limited(self, cap=60, bucket=""):
        """Не больше cap запросов за 10 с с одного адреса (страница делает ~1,5/с; плееры фида — чаще, у них
        свой счётчик bucket); иначе 429."""
        ip = (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip() + bucket
        now = time.time()
        with web_lock:
            q = [t for t in SyncHandler._hits.get(ip, []) if now - t < 10]
            q.append(now)
            SyncHandler._hits[ip] = q
            if len(SyncHandler._hits) > 1000:      # не копить адреса бесконечно
                SyncHandler._hits = {k: v for k, v in SyncHandler._hits.items() if v and now - v[-1] < 10}
        if len(q) > cap:
            self.close_connection = True
            self._send(429, {"ok": False})
            return True
        return False

    def do_HEAD(self):
        p = self._path()
        if FEED_URL and (p == "/feed.xml" or p.startswith("/media/")):
            if not self._limited(300, "|feed"):
                self._feed_file(p, head=True)
        else:
            self._send(404, b"", "text/plain")

    def _feed_file(self, p, head=False):
        """Файл фида с поддержкой Range (один диапазон); всё остальное — 404."""
        root = DATA / "feed"
        name = p[len("/media/"):] if p.startswith("/media/") else "feed.xml"
        if p.startswith("/media/") and not F.NAME_RE.match(name):
            return self._send(404, b"", "text/plain")
        f = root / ("media/" + name if p.startswith("/media/") else name)
        try:
            fh = open(f, "rb")
        except OSError:
            return self._send(404, b"", "text/plain")
        with fh:
            size = os.fstat(fh.fileno()).st_size
            ctype = "application/rss+xml; charset=utf-8" if name == "feed.xml" else F.CTYPE[Path(name).suffix]
            rng = F.parse_range(self.headers.get("Range"), size)
            if rng is False:
                self.send_response(416)
                self.send_header("content-range", f"bytes */{size}")
                self.send_header("content-length", "0")
                self.end_headers()
                return
            a, b = rng or (0, size - 1)
            self.send_response(206 if rng else 200)
            self.send_header("content-type", ctype)
            self.send_header("accept-ranges", "bytes")
            self.send_header("cache-control", "max-age=300" if name == "feed.xml" else "max-age=86400")
            if rng:
                self.send_header("content-range", f"bytes {a}-{b}/{size}")
            self.send_header("content-length", str(b - a + 1 if size else 0))
            self.end_headers()
            if head or not size:
                return
            self.connection.settimeout(60)                # большой файл медленному клиенту — дольше обычных 10 с
            fh.seek(a)
            left = b - a + 1
            try:
                while left > 0:
                    chunk = fh.read(min(1 << 16, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass                                      # плеер передумал или перемотал — это нормально

    def do_GET(self):
        p = self._path()
        if FEED_URL and (p == "/feed.xml" or p.startswith("/media/")):
            if not self._limited(300, "|feed"):
                self._feed_file(p)
            return
        if self._limited():
            return
        if p == "/time":
            self._send(200, {"t": time.time() * 1000})
        elif p in ("/", "/index.html"):
            self._send(200, WEBAPP_HTML.read_bytes(), "text/html; charset=utf-8")
        else:
            self._send(404, {"ok": False})

    def do_POST(self):
        if self._limited():
            return
        if self._path() != "/api":
            return self._send(404, {"ok": False})
        try:
            n = int(self.headers.get("content-length", "0") or 0)
        except ValueError:
            n = -1
        if not 0 <= n <= 8192:                     # тело читаем только маленькое — ещё до проверки подписи
            self.close_connection = True
            return self._send(413, {"ok": False})
        try:
            j = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"ok": False, "error": "bad json"})
        uid = check_init_data(j.get("initData"))
        if uid not in PEOPLE:
            uid = check_session(j.get("token")) or uid
        if uid not in PEOPLE:
            why = "пустая подпись (открыто не из меню бота?)" if not j.get("initData") else \
                  ("подпись не сошлась" if uid is None else f"чужой {uid}")
            log(f"мини-приложение: отказ — {why}")
            return self._send(403, {"ok": False, "error": "только из бота"})
        now = time.time()
        op = j.get("op")
        if op in ("topics", "vote", "kind", "status", "tdel"):
            with lock:
                bad = topic_op(uid, j) if op != "topics" else None
                out = topics_payload()
            return self._send(200, dict(out, ok=bad is None, error=bad))
        if op == "mark":
            with web_lock:
                PRESENCE[uid] = {"at": now, "ready": bool(j.get("ready")), "err": j.get("err")}
            return self._send(200, self._mark(uid, j))
        with web_lock:
            PRESENCE[uid] = {"at": now, "ready": bool(j.get("ready")), "err": j.get("err")}
            if op == "fire":
                if not (FIRE and FIRE["T"] > now * 1000):           # уже летит — второй раз не запускаем
                    FIRE.update(T=round(now * 1000 + FIRE_LEAD_MS), by=name(uid))
                    log(f"синхросигнал: запустил {name(uid)}, готовы: "
                        + ", ".join(name(u) for u, p in PRESENCE.items() if p["ready"] and now - p["at"] < 3))
            presence, fire = dict(PRESENCE), (dict(FIRE) if FIRE and FIRE["T"] > now * 1000 - 3000 else None)
        n = sync_n()
        counts = {u: len(v) for u, v in pending_marks(n).items()}
        people = [{"id": u, "name": name(u), "online": u in presence and now - presence[u]["at"] < 3, "marks": counts[u],
                   "ready": u in presence and now - presence[u]["at"] < 3 and presence[u]["ready"],
                   "err": presence.get(u, {}).get("err"), "f": PEOPLE[u]["f"]} for u in ORDER]
        self._send(200, {"me": uid, "people": people, "fire": fire, "n": n, "rec": rec_payload(n),
                         "tv": S.get("topics_v", 0), "token": session_token(uid)})

    def _mark(self, uid, j):
        def num(x):
            try:
                return int(float(x))
            except (TypeError, ValueError):
                return None
        try:
            T = float(j.get("t", 0)) / 1000
        except (TypeError, ValueError):
            return {"ok": False, "error": "нет времени сигнала"}
        if abs(T - time.time()) > 120:
            return {"ok": False, "error": "время сигнала не сходится с сервером"}
        j = {"err": num(j.get("err")), "rtt": num(j.get("rtt")), "lat": num(j.get("lat"))}
        if len(pending_marks()[uid]) >= 10:
            return {"ok": False, "error": "у этого выпуска уже 10 сигналов"}
        with lock:
            n = sync_n()
            S.setdefault("marks", []).append({"uid": uid, "n": n, "t": T, "err": j.get("err"), "rtt": j.get("rtt"),
                                               "lat": j.get("lat"), "at": time.time()})
            k = len(pending_marks(n)[uid])
            which = "начало" if k == 1 else "конец" if k == 2 else f"доп. {k}"
            log(f"синхросигнал №{n} {name(uid)} #{k}: {datetime.fromtimestamp(T).strftime('%H:%M:%S.%f')[:-3]} "
                f"(часы ±{j.get('err')} мс, сеть {j.get('rtt')} мс, задержка вывода {j.get('lat')} мс)")
            send(uid, f"🔔 Выпуск №{n}: синхросигнал №{k} ({which}) записан · {tgtime(T, 'T', PEOPLE[uid]['tz'])}"
                      + (f" · часы ±{j.get('err')} мс" if j.get("err") is not None else ""),
                 disable_notification=True)
            if ep() and ep()["status"] in ("waiting", "grace"):
                flush_cards(force=True)
            save()
        return {"ok": True, "n": n, "k": k, "which": which}

    def log_message(self, *a):
        pass


class LimitedServer(ThreadingHTTPServer):
    """ThreadingHTTPServer с ограничением одновременных соединений: лишние сразу закрываются."""
    daemon_threads = True
    slots = threading.BoundedSemaphore(32)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def http_server():
    """Веб-сервер мини-приложения на HTTP_PORT (наружу — только через Funnel узла Tailscale)."""
    LimitedServer(("0.0.0.0", HTTP_PORT), SyncHandler).serve_forever()


# ───────────────────────── фоновые: отсчёт, напоминание, очистка ─────────────────────────

def ticker():
    """Фон каждые 5 с: отсчёт до старта, напоминания (недостающая дорожка, непроверенное превью) на следующий
    день в REMIND_HOUR по поясу получателя.
    """
    while True:
        time.sleep(5)
        try:
            with lock:
                e = ep()
                if not e:
                    continue
                if e["status"] == "grace":
                    if time.time() >= e["grace_until"]:
                        start_episode()
                    else:
                        flush_cards(force=True)
                elif e["status"] == "preview":
                    for uid in audience():
                        if e.setdefault("reminded_preview", {}).get(str(uid)):
                            continue
                        tz = ZoneInfo(PEOPLE[uid]["tz"])
                        made = datetime.fromtimestamp(e["preview_at"], tz)
                        at = (made + timedelta(days=1)).replace(hour=REMIND_HOUR, minute=0, second=0, microsecond=0)
                        if datetime.now(tz) >= at:
                            e["reminded_preview"][str(uid)] = True
                            event(f"👋 Превью №{e['n']} ждёт проверки — послушай и нажми «✅ Собирай полный».", [uid])
                            log(f"напоминание о превью {name(uid)}")
                    save()
                elif e["status"] == "waiting" and not e.get("reminded") and len(e["tracks"]) == 1:
                    missing = next(u for u in ORDER if str(u) not in e["tracks"])
                    tz = ZoneInfo(PEOPLE[missing]["tz"])
                    opened = datetime.fromtimestamp(e["opened_at"], tz)
                    at = (opened + timedelta(days=1)).replace(hour=REMIND_HOUR, minute=0, second=0, microsecond=0)
                    if datetime.now(tz) >= at:
                        other = next(u for u in ORDER if str(u) in e["tracks"])
                        e["reminded"] = True
                        event(f"👋 Напоминаю про №{e['n']}: {name(other)} уже прислал(а) дорожку, жду твою.", [missing])
                        log(f"напоминание {name(missing)}")
                    save()
        except Exception:  # noqa: BLE001
            log(traceback.format_exc())


def cleanup():
    """Исходники старых выпусков удаляем через KEEP_TRACKS_DAYS; out/ и json остаются."""
    while True:
        cut = time.time() - KEEP_TRACKS_DAYS * 86400
        cur = (ep() or {}).get("job")
        for j in JOBS.glob("*/tracks"):
            if j.stat().st_mtime < cut and cur != str(j.parent):
                shutil.rmtree(j, ignore_errors=True)
                log(f"cleanup: {j}")
        live = {t["path"] for t in ((ep() or {}).get("tracks") or {}).values()}
        for f in INCOMING.glob("*"):
            if f.stat().st_mtime < time.time() - 7 * 86400 and str(f) not in live:
                shutil.rmtree(f, ignore_errors=True) if f.is_dir() else f.unlink(missing_ok=True)
        time.sleep(86400)


def main():
    """Старт: ждём локальный Bot API, восстанавливаем состояние (прерванная сборка продолжается с того же этапа),
    запускаем фоновые потоки и long polling getUpdates.
    """
    global S
    os.umask(0o007)                                      # файлы данных: владелец и группа хоста, «все» — нет
    for d in (JOBS, INCOMING, ASSETS):
        d.mkdir(parents=True, exist_ok=True)
    for i in range(60):                       # локальный Bot API стартует не мгновенно
        try:
            me = tg("getMe")
            break
        except Exception as ex:  # noqa: BLE001
            if i == 59:
                raise
            log(f"жду Bot API: {ex}")
            time.sleep(5)
    S = st_load()
    log(f"@{me['username']} запущен; ведущие: " + ", ".join(f"{name(u)} ({PEOPLE[u]['tz']})" for u in ORDER)
        + f"; последний №{S['last_n']}")
    tg("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
    # «🔔 Синхро» — кнопка меню бота (слева от поля ввода): только так мини-приложение получает подпись initData,
    # из кнопки нижней клавиатуры Telegram открывает его без подписи
    # Кнопка ставится только в чатах ведущих; по умолчанию (для всех остальных, кто нашёл бота) — обычное меню команд,
    # чтобы чужие не видели и не открывали мини-приложение.
    safe("setChatMenuButton", menu_button={"type": "commands"})
    if WEBAPP_URL:
        for u in ORDER:
            safe("setChatMenuButton", chat_id=u, menu_button={"type": "web_app", "text": "🔔 Синхро", "web_app": {"url": WEBAPP_URL}})
    with lock:
        e = ep()
        if e and e["status"] == "processing":
            log(f"продолжаю сборку №{e['n']}")
            done = {k for k in ("sync", "transcribe", "plan") if e.get("job") and
                    (Path(e["job"]) / {"sync": "sync.json", "transcribe": "transcript.json", "plan": "plan.json"}[k]).exists()}
            e["stage_keys"] = MODE_KEYS[e.get("mode") or "run"]
            e["stages"] = {k: ({"st": "done"} if k in done else {"st": "todo"}) for k in e["stage_keys"]}
            event(f"🔁 Бот перезапустился — продолжаю сборку №{e['n']}", silent=True)
            for uid in audience():
                new_card(uid)
            jobs_q.put(e["n"])
        elif e and e["status"] in ("waiting", "grace"):
            if e["status"] == "grace":
                e["grace_until"] = time.time() + GRACE
            maybe_grace()
            for uid in audience():                 # выпуск подготовлен вручную (без карточек) — показать
                if not e["cards"].get(str(uid)):
                    event(f"🎙 Выпуск №{e['n']} — дорожки на месте", [uid])
                    new_card(uid)
        save()
    for f in (worker, ticker, cleanup, http_server):
        threading.Thread(target=f, daemon=True).start()
    while True:
        try:
            ups = tg("getUpdates", _t=70, timeout=50, offset=S["offset"] + 1,
                     allowed_updates=["message", "callback_query"])
        except Exception as ex:  # noqa: BLE001
            log(f"getUpdates: {ex}")
            time.sleep(5)
            continue
        for u in ups:
            with lock:
                S["offset"] = u["update_id"]
                try:
                    handle(u)
                except Exception:  # noqa: BLE001
                    log(traceback.format_exc())
                save()


if __name__ == "__main__":
    main()
