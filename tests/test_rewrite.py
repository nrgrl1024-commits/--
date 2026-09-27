import json

import pytest

from fengzai_video import llm, rewrite
from fengzai_video.feishu import attachments, link_ids, text_of

REPLY = {
    "segments": [
        {"start": 0, "end": 4, "role": "女业主", "text": "珠海的街坊们别再踩坑啦！"},
        {"start": 4, "end": 8, "role": "女业主", "text": "我家厨房才几千块。"},
        {"start": 12, "end": 15, "role": "男师傅", "text": "评论区留个1。"},
    ],
    "cover_title": "珠海厨房翻新 人工材料全包",
    "subtitle": "厨房翻新 人工材料全包",
    "highlights": ["珠海", "几千块", "不存在的词"],
    "prompt_main": "9:16竖屏实拍",
    "prompt_negative": "画面自带字幕",
    "prompt_spec": "BGM：无（后期统一添加）",
}


def test_parse_json_from_code_block():
    text = "好的：\n```json\n" + json.dumps(REPLY, ensure_ascii=False) + "\n```"
    assert llm.parse_json(text)["cover_title"] == REPLY["cover_title"]


def test_rewrite_end_to_end(monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps(REPLY, ensure_ascii=False))
    store = rewrite.Store(name="珠海香洲店", city="珠海", dialect="粤语")
    result = rewrite.rewrite({}, store, rewrite.Source(script="母版"))
    assert result.script_text() == "女业主：珠海的街坊们别再踩坑啦！我家厨房才几千块。\n男师傅：评论区留个1。"
    assert result.highlights == ["珠海", "几千块"]  # 不在口播里的词被过滤
    lines = result.jimeng_prompt.splitlines()
    assert lines[0] == "生成时关闭自动字幕"
    assert "口播使用粤语" in result.jimeng_prompt
    assert "0-4秒（女业主）：珠海的街坊们别再踩坑啦！" in result.jimeng_prompt


def test_rewrite_retries_then_fails(monkeypatch):
    bad = dict(REPLY, segments=[{"start": 0, "end": 4, "role": "女业主", "text": "字" * 40}])
    calls = []
    monkeypatch.setattr(llm, "chat", lambda *a, **k: calls.append(1) or json.dumps(bad, ensure_ascii=False))
    with pytest.raises(llm.LLMError):
        rewrite.rewrite({}, rewrite.Store(name="店"), rewrite.Source(script="母版"), retries=1)
    assert len(calls) == 2


def test_system_prompt_fills_store():
    text = rewrite.build_system_prompt(rewrite.Store(name="珠海香洲店", city="珠海", address_term="街坊"))
    assert "珠海香洲店" in text and "街坊" in text and "{{" not in text


def test_feishu_value_helpers():
    assert text_of([{"type": "text", "text": "你"}, {"type": "text", "text": "好"}]) == "你好"
    assert text_of("待改写") == "待改写"
    assert link_ids({"link_record_ids": ["rec1"]}) == ["rec1"]
    assert link_ids([{"record_ids": ["rec2"], "text": "x"}]) == ["rec2"]
    assert attachments([{"file_token": "t", "name": "a.mp4"}, {"name": "no token"}]) == [
        {"file_token": "t", "name": "a.mp4"}
    ]


def test_config_accepts_bom_files(tmp_path, monkeypatch):
    """Windows 记事本保存的文件开头带 BOM，也要能正常读取。"""
    from fengzai_video import config

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_bytes("﻿feishu:\n  app_token: ${FT_TEST}\nwork_dir: work\n".encode("utf-8"))
    monkeypatch.setenv("FT_TEST", "abc")
    cfg = config.load_config(cfg_file)
    assert cfg.feishu["app_token"] == "abc"


def test_feishu_retries_when_rate_limited(monkeypatch):
    from fengzai_video import feishu

    class R:
        def __init__(self, code, status=200):
            self.status_code, self._code = status, code

        def json(self):
            return {"code": self._code, "msg": "x", "data": {"ok": 1}}

    replies = [R(99991400), R(0, 429), R(0)]
    monkeypatch.setattr(feishu.requests, "request", lambda *a, **k: replies.pop(0))
    monkeypatch.setattr(feishu.time, "sleep", lambda s: None)
    fs = feishu.Feishu("id", "secret", "app")
    monkeypatch.setattr(fs, "token", lambda: "t")
    assert fs.request("GET", "/x") == {"ok": 1}
    assert replies == []


def test_download_resumes_after_connection_drop(tmp_path, monkeypatch):
    import requests as rq

    from fengzai_video import feishu

    payload = b"x" * 3000
    calls = []

    class Resp:
        def __init__(self, status, body, headers, fail_after=None):
            self.status_code, self._body, self.headers, self._fail = status, body, headers, fail_after
            self.text = ""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_content(self, n):
            if self._fail is not None:
                yield self._body[: self._fail]
                raise rq.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead")
            yield self._body

    def fake_get(url, headers, stream, timeout):
        calls.append(headers.get("Range"))
        if len(calls) == 1:
            return Resp(200, payload, {"Content-Length": "3000"}, fail_after=1000)
        start = int(headers["Range"].split("=")[1].rstrip("-"))
        return Resp(206, payload[start:], {"Content-Range": f"bytes {start}-2999/3000"})

    monkeypatch.setattr(feishu.requests, "get", fake_get)
    monkeypatch.setattr(feishu.time, "sleep", lambda s: None)
    fs = feishu.Feishu("id", "secret", "app")
    monkeypatch.setattr(fs, "token", lambda: "t")
    out = fs.download({"file_token": "f"}, tmp_path / "v.mp4")
    assert out.read_bytes() == payload
    assert calls == [None, "bytes=1000-"]
