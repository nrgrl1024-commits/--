import io
import json

import openpyxl
import pytest

import server


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DATA", tmp_path)
    monkeypatch.setattr(server, "STORES_FILE", tmp_path / "stores.json")
    monkeypatch.setattr(server, "PUBLIC", tmp_path / "public")
    monkeypatch.setattr(server, "TTS_CACHE", tmp_path / "tts")
    monkeypatch.setattr(server, "PASSWORD", "")
    return server.app.test_client()


def make_xlsx():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([])
    ws.append([None, "人物", "对标脚本", "蜂仔改写脚本", "封面标题"])
    ws.append([None, "合肥 许师傅 男", "原文", "卫生间瓷砖旧了不用砸。", "合肥卫生间翻新 1天焕新"])
    ws.append([None, "成都温江 李师傅 男", "原文", "叔叔阿姨些莫划走噻。", "成都温江翻新"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_parse_excel_picks_rewritten_script():
    rows = server.parse_excel(make_xlsx())
    assert rows[0] == {"who": "合肥 许师傅 男", "script": "卫生间瓷砖旧了不用砸。", "cover": "合肥卫生间翻新 1天焕新"}
    assert len(rows) == 2


def test_parse_who():
    assert server.parse_who("成都温江 李师傅 男") == {"place": "成都温江", "nickname": "李师傅", "gender": "男"}
    assert server.parse_who("合肥 许师傅") == {"place": "合肥", "nickname": "许师傅", "gender": ""}


def test_import_matches_store_by_city_and_nickname(client):
    client.put("/api/stores", json=[{"name": "成都温江店", "city": "成都", "nickname": "李师傅", "dialect": "四川话"}])
    rows = client.post("/api/import", data={"file": (io.BytesIO(make_xlsx()), "a.xlsx")}).get_json()
    assert [r["store"] for r in rows] == ["", "成都温江店"]


def test_put_stores_keeps_voice_fields(client, tmp_path):
    (tmp_path / "stores.json").write_text(json.dumps([{"name": "A", "voice_id": "v1", "voice_status": "可用"}]), encoding="utf-8")
    out = client.put("/api/stores", json=[{"name": "A", "city": "合肥", "voice_id": "hacked"}]).get_json()
    assert out[0]["voice_id"] == "v1" and out[0]["city"] == "合肥"


def test_tts_requires_cloned_voice(client):
    client.put("/api/stores", json=[{"name": "A"}])
    r = client.post("/api/tts", json={"store": "A", "text": "你好"})
    assert r.status_code == 400


def test_tts_caches(client, monkeypatch):
    client.put("/api/stores", json=[{"name": "A", "dialect": "四川话"}])
    data = json.loads(server.STORES_FILE.read_text(encoding="utf-8"))
    data[0]["voice_id"] = "v1"
    server.save_stores(data)
    calls = []
    monkeypatch.setattr(server, "synthesize", lambda v, t, r, i: calls.append((v, t, r, i)) or b"ID3fake")
    for _ in range(2):
        r = client.post("/api/tts", json={"store": "A", "text": "你好", "rate": 1.1})
        assert r.status_code == 200 and r.data == b"ID3fake"
    assert calls == [("v1", "你好", 1.1, "请用四川话表达。")]


def test_password(client, monkeypatch):
    monkeypatch.setattr(server, "PASSWORD", "abc")
    assert client.get("/api/stores").status_code == 401
    import base64
    h = {"Authorization": "Basic " + base64.b64encode(b"x:abc").decode()}
    assert client.get("/api/stores", headers=h).status_code == 200
