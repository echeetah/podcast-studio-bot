"""Вырезы: перевод времени «как в плеере» в сырое, привязка краёв к паузам, примечание о стыках."""
import pytest

import cuts as C
import podcast as P


def make_job(tmp_path, spans, words=(), S=0.0, E=200.0, cuts=None):
    """spans — речь по VAD; words — [(канал, начало, конец, слово)] расшифровки."""
    P.jsave(tmp_path / "meta.json", {"tracks": ["a", "b"], "names": ["Аня", "Боря"], "n": 7})
    P.jsave(tmp_path / "sync.json", {"segments": [list(s) for s in spans], "dur_a": 300.0, "offset": 0.0, "drift": 0.0})
    P.jsave(tmp_path / "plan.json", {"start": S, "end": E, "cuts": cuts or []})
    us = [{"channel": str(ch), "start": int(a * 1000), "end": int(b * 1000), "text": w,
           "words": [{"start": int(a * 1000), "end": int(b * 1000), "text": w}]} for ch, a, b, w in words]
    P.jsave(tmp_path / "transcript.json", {"utterances": us})
    return tmp_path


def test_edit_to_raw_inverts_to_edit():
    segs = P.keep_segments(10.0, 100.0, [{"from": 30.0, "to": 50.0}, {"from": 70.0, "to": 75.0}])
    assert len(segs) == 3
    for raw in (11.0, 29.0, 52.0, 69.0, 80.0, 99.0):
        assert C.edit_to_raw(segs, P.to_edit(segs, raw)) == pytest.approx(raw, abs=1e-6)
    one = P.keep_segments(10.0, 100.0)
    assert C.edit_to_raw(one, 5.0) == 15.0                  # без вырезов — S + x


def test_snap_moves_edges_into_pauses(tmp_path):
    # речь 0–20, пауза 20–20,6 (короткая), речь 20,6–50 (сбой), пауза 50–56, речь 56–90
    spans = [(0, 20), (20.6, 50), (56, 90)]
    j = make_job(tmp_path, spans)
    f2, t2 = C.snap_cut(j, ["Аня", "Боря"], 0, 200, 21.0, 53.0)
    assert f2 == pytest.approx(20.55, abs=0.02)              # короткая пауза: кроссфейд кончается на её конце
    assert f2 - C.XF >= 20.0 and f2 <= 20.6                  # окно кроссфейда не задевает речь до и после
    assert t2 == pytest.approx(56 - C.PAD - C.XF - C.LEAD_IN, abs=0.02)
    assert t2 >= 50.0 and t2 + C.XF <= 56.0                  # окно после выреза — внутри паузы 50–56


def test_snap_refuses_without_pause_or_too_short(tmp_path):
    j = make_job(tmp_path, [(0, 100)])                       # сплошная речь
    with pytest.raises(ValueError, match="нет паузы"):
        C.snap_cut(j, ["Аня", "Боря"], 0, 200, 40.0, 60.0)
    (tmp_path / "x").mkdir()
    j2 = make_job(tmp_path / "x", [(0, 10), (12, 30)])         # оба края в одной короткой паузе
    with pytest.raises(ValueError, match="слишком коротко"):
        C.snap_cut(j2, ["Аня", "Боря"], 0, 200, 10.5, 11.5)


def test_words_count_as_speech(tmp_path):
    # VAD пропустил тихое слово 30–31: пауза не должна проходить через него
    j = make_job(tmp_path, [(0, 20), (40, 60)], words=[(1, 30.0, 31.0, "тихо")])
    gs = C.gaps(C.speech_spans(j, ["Аня", "Боря"]), 0, 200)
    assert all(not (g0 < 31 and g1 > 30) for g0, g1 in gs)


def test_add_cut_uses_player_time_and_existing_cuts(tmp_path, monkeypatch):
    spans = [(0, 20), (26, 40), (46, 90), (96, 150)]
    j = make_job(tmp_path, spans)
    monkeypatch.setattr(C, "intro_offset", lambda cfg: 5.0)     # заставка: речь начинается на 5-й секунде MP3
    cut = C.add_cut(j, {}, 5.0 + 41.0, 5.0 + 95.0, "сбой связи")   # «как в плеере»: 0:46–1:40 → сырое 41–95
    assert 40.0 < cut["from"] < 46.0 and 90.0 < cut["to"] < 96.0 and cut["label"] == "00:46–01:40"
    plan = P.jload(j / "plan.json")
    assert plan["cuts"] == [cut]
    # второй вырез задаётся уже по времени ПОСЛЕ первого: позиции после стыка сдвинулись
    segs = C.pieces(plan)
    x = 5.0 + P.to_edit(segs, 17.0)                             # где теперь сырое 17 с
    assert x == pytest.approx(5.0 + 17.0, abs=1e-6)             # до выреза ничего не сдвинулось
    assert C.edit_to_raw(segs, P.to_edit(segs, 120.0)) == pytest.approx(120.0, abs=1e-6)
    gone = C.remove_last_cut(j)
    assert gone["label"] == "00:46–01:40" and P.jload(j / "plan.json")["cuts"] == []
    assert C.remove_last_cut(j) is None


def test_cuts_note_and_parse_range(tmp_path, monkeypatch):
    j = make_job(tmp_path, [(0, 20), (26, 40)], cuts=[{"from": 21.0, "to": 25.0, "note": "сбой", "label": "00:21–00:25"}])
    monkeypatch.setattr(C, "intro_offset", lambda cfg: 0.0)
    note = C.cuts_note(j, {})
    assert note == ["✂️ Вырезано 4 с (сбой) · стык на 00:20 · в прошлой версии 00:21–00:25"]
    (tmp_path / "y").mkdir()
    assert C.cuts_note(make_job(tmp_path / "y", [(0, 1)]), {}) == []
    assert C.parse_range("55:39-56:09 сбой связи") == (3339.0, 3369.0, "сбой связи")
    assert C.parse_range("1:02:10 – 1:02:40") == (3730.0, 3760.0, "")
    assert C.parse_range("0:10 0:20")[:2] == (10.0, 20.0)
    for bad in ("55:39", "56:09-55:39", "привет"):
        with pytest.raises(ValueError):
            C.parse_range(bad)


def test_glitches_finds_dropout_phrases(tmp_path):
    j = make_job(tmp_path, [(0, 100)], words=[(1, 10.0, 12.0, "связано"), (2, 50.0, 52.0, "прервалось"),
                                              (1, 60.0, 61.0, "повтори")])
    found = C.glitches(j, ["Аня", "Боря"], 0, 100)
    assert [round(t) for t, _ in found] == [50, 60] and "Боря" in found[0][1]


def test_bot_cut_flow(tmp_path, monkeypatch):
    """Кнопка «Вырезать» → время текстом → вырез в плане, сборка запущена, одобрения сброшены; «Вернуть вырез» — обратно."""
    import bot as B
    j = make_job(tmp_path, [(0, 20), (26, 40), (46, 90), (96, 150)])
    monkeypatch.setattr(B, "STATE_F", tmp_path / "state.json")
    monkeypatch.setattr(B, "DATA", tmp_path)
    sent, started = [], []
    monkeypatch.setattr(B, "send", lambda chat, text, kb=None, **kw: sent.append(text))
    monkeypatch.setattr(B, "event", lambda *a, **k: None)
    monkeypatch.setattr(B, "begin_processing", lambda mode: started.append(mode))
    monkeypatch.setattr(B, "safe", lambda *a, **k: None)
    monkeypatch.setattr(B, "cfg_for", lambda job=None: {"intro_overlap": 2.0, "assets": None, "eq": True, "mic_profiles": {}})
    B.S.update({"await": {}, "ep": {"n": 7, "status": "review", "job": str(j), "approvals": {"111": True},
                                    "report": {"duration": 150.0}, "tracks": {}, "cards": {}}})
    B.on_callback({"id": "1", "data": "cut", "message": {"chat": {"id": 111}, "message_id": 5}}, 111)
    assert B.S["await"]["111"]["kind"] == "cut" and "55:39" in sent[-1]
    B.on_message({"chat": {"id": 111}, "text": "кривая запись"}, 111)           # не время — вырез не создан, ждёт снова
    assert not P.jload(j / "plan.json")["cuts"] and "Не получилось" in sent[-1] and started == []
    B.S["await"]["111"] = {"kind": "cut", "n": 7}
    B.on_message({"chat": {"id": 111}, "text": "0:41-1:35 сбой связи"}, 111)
    cuts = P.jload(j / "plan.json")["cuts"]
    assert len(cuts) == 1 and cuts[0]["note"] == "сбой связи" and started == ["render"]
    B.on_callback({"id": "2", "data": "uncut", "message": {"chat": {"id": 111}, "message_id": 5}}, 111)
    assert P.jload(j / "plan.json")["cuts"] == [] and started == ["render", "render"]


def test_tight_gap_beats_distant_long_pause(tmp_path):
    # фраза кончается в 20,0, сбой начинается в 20,34: пауза всего 0,34 с — короче кроссфейда, но она ровно на краю
    j = make_job(tmp_path, [(0, 20.0), (20.34, 40), (45, 60), (70, 90)])
    f2, t2 = C.snap_cut(j, ["Аня", "Боря"], 0, 200, 20.1, 65.0)
    assert f2 == pytest.approx(20.34 - 0.03, abs=0.01)       # окно кроссфейда кончается до начала следующей речи
    assert f2 < 20.34                                                # …и захватывает только хвост конца фразы
    assert t2 == pytest.approx(70 - C.PAD - C.XF - C.LEAD_IN, abs=0.01)
