"""cuts.py — ручные вырезы из готового выпуска: сбой связи, дубль, оговорка.

Бот просит ведущего назвать промежуток «как в плеере» (время готового MP3, с заставкой), а здесь он
переводится в сырую шкалу дорожки A, края сдвигаются в ближайшие паузы (на стык приходится кроссфейд
P.XF = 0,4 с — в нём не должно быть речи ни одного из ведущих), и вырез записывается в plan.json["cuts"].
Саму склейку (обе дорожки, кроссфейд, сдвиг глав и расшифровки) делает podcast_core.render.

Всё здесь — чистые функции над файлами выпуска (sync.json, transcript.json, plan.json), Telegram не нужен.
"""
import re
import time
from pathlib import Path

import podcast as P

XF = P.XF                 # кроссфейд на стыке, с (общий с render)
PAD = 0.05                # запас вокруг слов и сегментов речи: границы в расшифровке неточны
MIN_GAP = 0.2             # паузы короче — на стык не годятся
REACH = 4.0               # на сколько секунд от названного времени ищем паузу
LEAD_OUT = 0.12           # тишины после последнего слова до начала кроссфейда (край «до выреза»)
LEAD_IN = 0.2             # тишины перед первым словом после стыка (край «после выреза»)
MIN_CUT = 1.0             # короче вырезать нет смысла

TS = r"\d{1,2}(?::\d{2}){1,2}"
RANGE = re.compile(rf"^\s*({TS})\s*(?:[-–—]|до|по|\s)\s*({TS})\s*(.*)$", re.S)
GLITCH = re.compile(r"прервал|пропал[аи]? (?:связь|звук)|связь пропала|плохая связь|не слышу|не слышно|тебя не слыш"
                    r"|вырез|скажи ещё раз|скажи еще раз|повтори|заново|начнём заново|давай ещё раз", re.I)


def intro_offset(cfg):
    """Сколько секунд от начала MP3 до первой секунды выпуска (заставка минус её наложение)."""
    intro = P.find_asset(cfg, "intro")
    if not intro:
        return 0.0
    li = P.duration(intro)
    return li - min(cfg["intro_overlap"], li / 2)


def edit_to_raw(segs, x):
    """Время от начала смонтированного звука (куски склеены с кроссфейдом XF) → сырое время дорожки A.
    Обратная к P.to_edit. Без вырезов — S + x."""
    pos = 0.0
    for k, (a, b, _) in enumerate(segs):
        ln = b - a
        if k == len(segs) - 1 or x < pos + ln - XF:
            return a + (x - pos if k == 0 else max(0.0, x - pos))
        pos += ln - XF
    return segs[0][0] + x


def pieces(plan):
    """Куски выпуска после вырезов плана: [(a, b, "")] в сырой шкале."""
    return P.keep_segments(P.parse_ts(plan["start"]), P.parse_ts(plan["end"]), plan.get("cuts"))


def speech_spans(work, names):
    """Где кто-то говорит (сырая шкала A): речевые сегменты обеих дорожек + слова расшифровки, с запасом PAD."""
    work = Path(work)
    spans = [(a, b) for a, b in (P.jload(work / "sync.json", {}) or {}).get("segments") or []]
    tr = P.jload(work / "transcript.json")
    if tr:
        for u in P.utterances(tr, names):
            spans += [(w["start"] / 1000, w["end"] / 1000) for w in u[4]]
    merged = []
    for a, b in sorted((a - PAD, b + PAD) for a, b in spans):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def gaps(spans, lo, hi):
    """Паузы между речью в [lo, hi]: [(начало, конец)] не короче MIN_GAP."""
    out, prev = [], lo
    for a, b in spans:
        if a - prev >= MIN_GAP and prev >= lo and a <= hi:
            out.append((prev, a))
        prev = max(prev, b)
    if hi - prev >= MIN_GAP:
        out.append((prev, hi))
    return out


def nearest_gap(gs, x):
    """Пауза для края выреза около времени x: ближе и длиннее лучше (короткая пауза внутри фразы хуже
    полноценной в секунде от названного времени). None, если ближе REACH паузы нет."""
    best = None
    for g0, g1 in gs:
        d = max(0.0, g0 - x, x - g1)
        score = d - 1.5 * min(g1 - g0, 1.0)
        if d <= REACH and (best is None or score < best[0]):
            best = (score, g0, g1)
    return best and (best[1], best[2])


def snap_cut(work, names, S, E, f, t):
    """Края выреза [f, t] (сырое время) → в паузы. Кусок до выреза заканчивается в f2, после — начинается в t2;
    окно кроссфейда [f2 − XF, f2] и [t2, t2 + XF] попадает в тишину. → (f2, t2). ValueError, если паузы рядом нет."""
    gs = gaps(speech_spans(work, names), S, E)
    gf, gt = nearest_gap(gs, f), nearest_gap(gs, t)
    if not gf or not gt:
        raise ValueError("рядом с краем нет паузы — назови время чуть точнее (вырез должен начинаться и кончаться в паузе)")
    # край «до выреза»: в длинной паузе — после LEAD_OUT тишины; в короткой окно кроссфейда начинается в хвосте
    # последнего слова (оно затухает естественно) и заканчивается ДО начала следующей речи
    f2 = min(gf[0] + XF + LEAD_OUT, gf[1] + PAD - 0.03)
    t2 = max(gt[0] - 0.1, gt[1] - XF - LEAD_IN)
    if t2 - f2 < MIN_CUT:
        raise ValueError(f"после привязки к паузам вырез получился {t2 - f2:.1f} с — слишком коротко")
    return round(f2, 2), round(t2, 2)


def fmt(t):
    t = int(round(t))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:02d}:{t % 60:02d}"


def parse_range(text):
    """«55:39-56:09 сбой связи» → (55:39 в секундах, 56:09 в секундах, "сбой связи")."""
    m = RANGE.match(text or "")
    if not m:
        raise ValueError("нужно два времени, например «55:39-56:09»")
    a, b = P.parse_ts(m.group(1)), P.parse_ts(m.group(2))
    if b <= a:
        raise ValueError("конец раньше начала")
    return a, b, m.group(3).strip()


def add_cut(work, cfg, a_player, b_player, note=""):
    """Вырез по времени готового MP3 (a_player…b_player): в plan.json["cuts"]. → запись выреза.
    Времена переводятся в сырые с учётом уже сделанных вырезов, края — в паузы."""
    work = Path(work)
    plan, meta = P.jload(work / "plan.json"), P.jload(work / "meta.json")
    S, E = P.parse_ts(plan["start"]), P.parse_ts(plan["end"])
    segs, t_main = pieces(plan), intro_offset(cfg)
    f, t = edit_to_raw(segs, a_player - t_main), edit_to_raw(segs, b_player - t_main)
    if f < S - 0.5 or t > E + 0.5:
        raise ValueError("вырез выходит за пределы выпуска — начало и конец обрезаются кнопкой «Обрезка»")
    f2, t2 = snap_cut(work, meta["names"], S, E, max(f, S), min(t, E))
    cut = {"from": f2, "to": t2, "note": note, "label": f"{fmt(a_player)}–{fmt(b_player)}", "at": round(time.time(), 1)}
    plan["cuts"] = sorted((plan.get("cuts") or []) + [cut], key=lambda c: c["from"])
    P.jsave(work / "plan.json", plan)
    return cut


def remove_last_cut(work):
    """Убрать вырез, добавленный последним (по полю `at`). → удалённая запись или None."""
    work = Path(work)
    plan = P.jload(work / "plan.json")
    cuts = plan.get("cuts") or []
    if not cuts:
        return None
    gone = max(cuts, key=lambda c: c.get("at", 0))
    cuts.remove(gone)
    plan["cuts"] = cuts
    P.jsave(work / "plan.json", plan)
    return gone


def cuts_note(work, cfg):
    """Строки для сообщения о выпуске: что вырезано и где теперь стыки (время готового MP3). [] — вырезов нет."""
    work = Path(work)
    plan = P.jload(work / "plan.json", {}) or {}
    cuts = plan.get("cuts") or []
    if not cuts:
        return []
    segs, t_main, lines = pieces(plan), intro_offset(cfg), []
    for c in cuts:
        seam = t_main + P.to_edit(segs, c["from"]) - XF / 2
        lines.append(f"✂️ Вырезано {int(round(c['to'] - c['from']))} с"
                     + (f" ({c['note']})" if c.get("note") else "")
                     + f" · стык на {fmt(seam)}" + (f" · в прошлой версии {c['label']}" if c.get("label") else ""))
    return lines


def glitches(work, names, S, E, limit=6):
    """Места, похожие на сбой связи или просьбу вырезать: [(сырое время, «имя: фраза»)] — для подсказки в боте."""
    tr = P.jload(Path(work) / "transcript.json")
    out = []
    for u in (P.utterances(tr, names) if tr else []):
        if S <= u[0] <= E and GLITCH.search(u[3]):
            m = GLITCH.search(u[3])
            text = u[3][max(0, m.start() - 25): m.end() + 35].strip()
            out.append((u[0], f"{u[2]}: …{text}…"))
    return sorted(out)[:limit]


def apply_edits(work, start=None, end=None, chapters_text=None):
    """P.apply_edits с учётом вырезов: время «по готовому MP3» переводится в сырое через куски плана."""
    work = Path(work)
    plan, sync = P.jload(work / "plan.json"), P.jload(work / "sync.json")
    cfg = P.load_cfg(work)
    segs, t_main, old_S = pieces(plan), intro_offset(cfg), P.parse_ts(plan["start"])
    to_raw = lambda t: edit_to_raw(segs, t - t_main)
    if chapters_text is not None:
        ch = []
        for line in chapters_text.splitlines():
            m = re.match(r"\s*(\d{1,2}(?::\d{2}){1,2})\s*[-–—]?\s*(.+)", line)
            if m:
                ch.append({"start": round(max(old_S, to_raw(P.parse_ts(m.group(1)))), 2), "title": m.group(2).strip()})
        plan["chapters"] = ch
    if start is not None or end is not None:
        s = to_raw(P.parse_ts(start)) if start is not None else plan["start"]
        e = to_raw(P.parse_ts(end)) if end is not None else plan["end"]
        lim = sync.get("timeline_end") or sync["dur_a"]
        plan["start"], plan["end"] = round(max(0.0, s), 2), round(min(lim, e), 2)
    plan["source"] = (plan.get("source") or "") + "+manual"
    P.jsave(work / "plan.json", plan)
    return plan
