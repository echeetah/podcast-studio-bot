"""Тесты бота без сети: подпись мини-приложения и отрисовка карточки."""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import bot as B


def init_data(uid, token="123456:TEST-TOKEN", age=0):
    d = {"auth_date": str(int(time.time() - age)), "user": json.dumps({"id": uid})}
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    d["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(d)


def test_init_data_valid():
    assert B.check_init_data(init_data(111)) == 111


def test_init_data_rejects_wrong_token_old_and_empty():
    assert B.check_init_data(init_data(111, token="999:OTHER")) is None
    assert B.check_init_data(init_data(111, age=7200)) is None          # старше часа
    assert B.check_init_data(init_data(111, age=-600)) is None          # из будущего
    assert B.check_init_data("") is None


def test_init_data_tampered_user():
    s = init_data(111).replace("111", "222")
    assert B.check_init_data(s) is None


def test_card_states(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "DATA", tmp_path)
    B.S.update({"last_n": 11, "ep": {"n": 12, "status": "waiting", "opened_at": time.time(), "tracks": {
        "111": {"path": "x", "file": "a.wav", "dur": 3600, "ok": True}}, "cards": {}, "approvals": {}, "stages": {}}})
    text, kb = B.card(222)
    assert "№12" in text and "Боря · ⏳" in text
    assert kb["inline_keyboard"][0][0].get("disabled") == {}            # «Начать» неактивна без второй дорожки
    assert B.status_text().startswith("🎙 №12")
    B.S["ep"].update(status="processing", mode="run", stage_keys=["sync", "transcribe"],
                     stages={"sync": {"st": "done", "info": "сдвиг +0.50 с"}, "transcribe": {"st": "run", "frac": 0.5}},
                     eta=time.time() + 60)
    text, kb = B.card(111)
    assert "✅ синхронизация" in text and "50%" in text and kb is None


def test_topics_flow(tmp_path, monkeypatch):
    """Тема из бота → второму ведущему; голоса, вид, «обсудили» с номером выпуска; удалить может только автор."""
    monkeypatch.setattr(B, "STATE_F", tmp_path / "state.json")
    sent = []
    monkeypatch.setattr(B, "send", lambda chat, text, kb=None, **kw: sent.append((chat, text)))
    B.S.update({"last_n": 31, "ep": {"n": 31, "status": "review"}, "topics": [], "topic_seq": 0, "topics_v": 0, "await": {}})
    B.on_message({"chat": {"id": 111}, "text": "💡 Тема"}, 111)
    assert B.S["await"]["111"]["kind"] == "topic"
    B.on_message({"chat": {"id": 111}, "text": "Летающие <машины>"}, 111)
    t = B.S["topics"][0]
    assert t["by"] == 111 and t["text"] == "Летающие <машины>" and "111" not in B.S["await"]
    assert any(c == 222 and "Аня" in x and "&lt;машины&gt;" in x for c, x in sent)     # второй видит автора, текст экранирован
    assert B.topic_op(222, {"op": "vote", "id": 1, "v": "fire"}) is None and t["votes"] == {"222": "fire"}
    assert B.topic_op(222, {"op": "vote", "id": 1, "v": "лайк"}) and B.topic_op(222, {"op": "vote", "id": 1, "v": None}) is None
    assert t["votes"] == {}
    assert B.topic_op(111, {"op": "kind", "id": 1, "kind": "note"}) is None and t["kind"] == "note"
    assert B.topic_op(111, {"op": "status", "id": 1, "status": "done"}) is None and t["n"] == 31
    assert B.topic_op(222, {"op": "tdel", "id": 1}) == "удалить может только автор"
    assert B.topic_op(111, {"op": "tdel", "id": 1}) is None and B.S["topics"] == []
    assert B.S["topics_v"] == 6                    # добавление + 5 правок


def test_session_token():
    tok = B.session_token(111)
    assert B.check_session(tok) == 111
    uid, exp, sig = tok.split(".")
    assert B.check_session(f"222.{exp}.{sig}") is None                       # чужой ID с той же подписью
    assert B.check_session(f"{uid}.{int(exp) + 10}.{sig}") is None           # продлить срок нельзя
    assert B.check_session(f"{uid}.{int(time.time()) - 5}.{sig}") is None
    assert B.check_session("") is None and B.check_session("a.b") is None


def test_rec_payload(tmp_path, monkeypatch):
    B.S.update({"last_n": 31, "ep": None, "marks": [
        {"uid": 111, "n": 32, "t": 1000.0, "at": time.time()}, {"uid": 222, "n": 32, "t": 1000.0, "at": time.time()},
        {"uid": 111, "n": 32, "t": 4900.0, "at": time.time()}]})
    r = B.rec_payload(32)
    assert r == {"n": 32, "start": 1000000.0, "end": 4900000.0}
    assert B.rec_payload(33)["start"] is None


def test_noise_label_and_toggle(tmp_path):
    model = tmp_path / "bd.rnnn"
    model.write_text("rnnoise-nu model file version 1\n")
    base = {"denoise": True, "denoise_nr": 12, "rnnoise_model": ""}
    assert B.noise_label(base) == "12 дБ" and not B.nn_denoise(base)
    assert B.noise_label({**base, "denoise": False}) == "выкл"
    assert B.noise_label({**base, "denoise_nr": 0}) == "выкл"
    nn = {**base, "rnnoise_model": str(model)}
    assert B.nn_denoise(nn) and B.noise_label(nn) == "нейро"
    assert B.noise_label({**nn, "denoise": False}) == "выкл"
    assert not B.nn_denoise({**base, "rnnoise_model": str(tmp_path / "нет.rnnn")})     # файла нет — обычный шумодав
