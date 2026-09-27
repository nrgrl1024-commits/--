import json
import subprocess

import pytest

from fengzai_video import analyze, editor, llm, wecom


class Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def test_push_video_sends_text_image_file(tmp_path, monkeypatch):
    video, cover = tmp_path / "v.mp4", tmp_path / "c.jpg"
    video.write_bytes(b"v" * 100)
    cover.write_bytes(b"c" * 100)
    calls = []

    def fake_post(url, timeout, **kw):
        calls.append((url, kw))
        return Resp({"errcode": 0, "media_id": "m1"})

    monkeypatch.setattr(wecom.requests, "post", fake_post)
    hook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc-123"
    wecom.push_video(hook, "佛山禅城店", video, cover, "标题 #装修")
    kinds = [kw.get("json", {}).get("msgtype", "upload") for _, kw in calls]
    assert kinds == ["text", "image", "upload", "file"]
    assert "key=abc-123&type=file" in calls[2][0]
    assert "佛山禅城店" in calls[0][1]["json"]["text"]["content"]


def test_wecom_error_raised(monkeypatch):
    monkeypatch.setattr(wecom.requests, "post", lambda *a, **k: Resp({"errcode": 93000, "errmsg": "invalid"}))
    with pytest.raises(wecom.WecomError):
        wecom.send_text("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x", "hi")


def _make_video(path, seconds=4):
    subprocess.run(
        [editor.ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i", f"testsrc=size=540x960:rate=30:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=300:duration={seconds}", "-shortest", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )  # fmt: skip


def test_analyze_sends_frames_and_parses(tmp_path, monkeypatch):
    src = tmp_path / "ref.mp4"
    _make_video(src)
    seen = {}

    def fake_vision(cfg, prompt, images):
        seen["n"], seen["prompt"] = len(images), prompt
        return json.dumps({"script": "女业主：你好", "cover": "封面", "shots": [{"time": "0-4秒", "desc": "厨房"}], "hook": "痛点"})

    monkeypatch.setattr(llm, "vision", fake_vision)
    result = analyze.analyze({"vision_max_frames": 8}, src, tmp_path / "work")
    assert 1 <= seen["n"] <= 8 and "{{" not in seen["prompt"]
    assert result.script == "女业主：你好" and result.shots == "0-4秒：厨房"


def test_shrink_for_upload(tmp_path):
    src = tmp_path / "big.mp4"
    _make_video(src, 3)
    assert editor.shrink_for_upload(src, 50) == src  # 不超就原样
    small = editor.shrink_for_upload(src, 0.05)
    assert small != src and small.exists()
