"""混剪：画面编排规则 + 飞书流程 + 用 ffmpeg 真拼一条。"""
import subprocess
from pathlib import Path

import pytest

from fengzai_video import editor, mixer, wecom
from fengzai_video.config import DEFAULT_RENDER
from fengzai_video.mixer import Clip
from fengzai_video.pipeline import Pipeline, _today_ms

from .test_pipeline import base  # noqa: F401  复用同一个假飞书


def clips(prefix, n, dur, anchor):
    return [Clip(f"{prefix}{i}", Path(f"{prefix}{i}.mp4"), dur, anchor) for i in range(n)]


def test_plan_starts_and_ends_with_anchor_and_fills_duration():
    segs = mixer.plan(31.0, clips("a", 3, 10, True), clips("b", 4, 10, False), seed="x")
    assert segs[0].clip.anchor and segs[-1].clip.anchor
    assert segs[0].length == 3.0
    assert sum(s.length for s in segs) == pytest.approx(31.0, abs=0.01)
    assert mixer.anchor_share(segs) >= 0.5
    assert all(a.clip is not b.clip for a, b in zip(segs, segs[1:]))  # 同一素材不紧挨着
    for s in segs:
        assert 0 <= s.start and s.start + s.length <= s.clip.duration + 1e-6


def test_plan_short_clips_still_fill_and_only_anchor_when_no_broll():
    segs = mixer.plan(20.0, clips("a", 2, 1.2, True), [], seed="x")
    assert all(s.clip.anchor for s in segs)
    assert sum(s.length for s in segs) == pytest.approx(20.0, abs=0.01)


def test_plan_requires_anchor():
    with pytest.raises(RuntimeError, match="主播"):
        mixer.plan(10, [], clips("b", 2, 10, False), seed="x")


def test_plan_is_repeatable_per_seed():
    a, b = clips("a", 3, 10, True), clips("b", 3, 10, False)
    assert mixer.plan(20, a, b, seed="珠海1009") == mixer.plan(20, a, b, seed="珠海1009")


# ---------- 飞书流程 ----------
def _ready_store_video(fs, page):
    fs.batch_create(fs.tables[page], [{
        "状态": "已完成", "日期": _today_ms(), "生成视频": [{"file_token": "jimeng"}],
        "改写脚本内容": "女业主：珠海的街坊们看过来！", "封面标题": "珠海厨房翻新", "发布文案": "珠海厨房翻新 #珠海装修",
    }])  # fmt: skip


def _library(fs, rows):
    for r in rows:
        fs.batch_create(fs.tables["素材库"], [r])


def test_mix_uses_only_local_anchor_and_runs_once_per_day(base, monkeypatch):  # noqa: F811
    fs, cfg = base
    for row in fs.rows[fs.tables["门店资料"]].values():
        row["企微群机器人"] = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    _ready_store_video(fs, "珠海香洲店")
    _library(fs, [
        {"名称": "阿乐开场", "素材": [{"file_token": "zh-anchor"}], "城市": "珠海", "主播": "阿乐师傅"},
        {"名称": "佛山主播", "素材": [{"file_token": "fs-anchor"}], "城市": "佛山", "主播": "阿强"},
        {"名称": "施工", "素材": [{"file_token": "broll"}], "城市": "通用"},
        {"名称": "佛山门头", "素材": [{"file_token": "fs-door"}], "城市": "佛山"},
        {"名称": "停用的", "素材": [{"file_token": "off"}], "城市": "珠海", "主播": "阿乐师傅", "停用": True},
    ])  # fmt: skip

    seen, pushed = {}, []
    monkeypatch.setattr(editor, "probe", lambda p: editor.Probe(12.0, 1080, 1920, True))
    monkeypatch.setattr(mixer, "probe", lambda p: editor.Probe(8.0, 1080, 1920, True))
    monkeypatch.setattr(mixer, "montage", lambda segs, voice, out, w, h: seen.setdefault("segs", segs) and out)
    monkeypatch.setattr(editor, "render", lambda *a, **k: seen.setdefault("title", a[4]) and {})
    monkeypatch.setattr(editor, "shrink_for_upload", lambda v, mb: v)
    monkeypatch.setattr(wecom, "push_video", lambda hook, store, v, c, text: pushed.append((store, text)))

    pipe = Pipeline(cfg, fs)
    assert pipe.mix_pending() == 1  # 佛山今天还没有剪好的即梦视频，先不做
    files = {s.clip.path.name.split(".")[0] for s in seen["segs"]}
    assert files <= {"zh-anchor", "broll"} and "zh-anchor" in files
    assert seen["title"] == "珠海蜂仔翻新团队"
    mix = fs.page("混剪")[0]
    assert mix["城市"] == "珠海" and mix["状态"] == "已完成" and mix["成片"] and mix["已推送群"] is True
    assert "主播出镜" in mix["素材清单"]
    assert pushed == [("珠海香洲店·混剪", "珠海厨房翻新 #珠海装修")]
    lib = {r["名称"]: r for r in fs.page("素材库")}
    assert lib["阿乐开场"]["使用次数"] >= 2 and "使用次数" not in lib["佛山主播"]

    assert pipe.mix_pending() == 0  # 每个城市每天只做一条
    assert len(fs.page("混剪")) == 1


def test_mix_fails_clearly_without_local_anchor_and_can_retry(base, monkeypatch):  # noqa: F811
    fs, cfg = base
    _ready_store_video(fs, "珠海香洲店")
    _library(fs, [{"名称": "施工", "素材": [{"file_token": "broll"}]}])
    pipe = Pipeline(cfg, fs)
    assert pipe.mix_pending() == 0
    mix = fs.page("混剪")[0]
    assert mix["状态"] == "失败" and "珠海" in mix["备注"] and "主播" in mix["备注"]

    # 补上素材后，把状态改成「待混剪」就会重做（不会新增一行）
    _library(fs, [{"名称": "阿乐", "素材": [{"file_token": "zh"}], "城市": "珠海", "主播": "阿乐师傅"}])
    mix["状态"] = "待混剪"
    monkeypatch.setattr(editor, "probe", lambda p: editor.Probe(6.0, 1080, 1920, True))
    monkeypatch.setattr(mixer, "probe", lambda p: editor.Probe(8.0, 1080, 1920, True))
    monkeypatch.setattr(mixer, "montage", lambda segs, voice, out, w, h: out)
    monkeypatch.setattr(editor, "render", lambda *a, **k: {})
    assert pipe.mix_pending() == 1
    assert len(fs.page("混剪")) == 1 and fs.page("混剪")[0]["状态"] == "已完成"


# ---------- 用 ffmpeg 真拼一条 ----------
try:
    FFMPEG = editor.ffmpeg_exe()
except RuntimeError:
    FFMPEG = None


def _make(path, src, dur, size="640x360", audio=False):
    args = [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"{src}=size={size}:rate=25:duration={dur}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=300:duration={dur}", "-shortest"]
    subprocess.run(args + ["-pix_fmt", "yuv420p", str(path)], check=True)
    return path


@pytest.mark.skipif(FFMPEG is None, reason="没有 ffmpeg")
def test_montage_and_render(tmp_path):
    voice = _make(tmp_path / "voice.mp4", "testsrc", 7, "540x960", audio=True)
    items = [
        ("a1", _make(tmp_path / "a1.mp4", "testsrc2", 4), True),  # 横屏素材也会裁成竖屏
        ("a2", _make(tmp_path / "a2.mp4", "smptebars", 3, "540x960"), True),
        ("b1", _make(tmp_path / "b1.mp4", "rgbtestsrc", 4, "360x640"), False),
    ]
    clips_ = mixer.load_clips(items)
    segs = mixer.plan(7.0, [c for c in clips_ if c.anchor], [c for c in clips_ if not c.anchor], seed="t")
    out = mixer.montage(segs, voice, tmp_path / "m.mp4", 540, 960)
    info = editor.probe(out)
    assert (info.width, info.height) == (540, 960) and info.has_audio
    assert info.duration == pytest.approx(7.0, abs=0.2)

    cfg = {**DEFAULT_RENDER, "fonts_dir": str(tmp_path / "nofonts"), "output_width": 540, "preset": "ultrafast"}
    final = tmp_path / "成片.mp4"
    editor.render(out, final, tmp_path / "封面.jpg", "女业主：珠海的街坊们，翻新不用搬家。", "珠海蜂仔翻新团队",
                  "", "珠海厨房翻新", [], cfg)  # fmt: skip
    assert final.stat().st_size > 10_000
