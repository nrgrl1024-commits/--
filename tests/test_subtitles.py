from fengzai_video import subtitles

SCRIPT = (
    "女业主：珠海的街坊们别再踩坑啦！翻新厨房卫生间真花不了多少。我家刚弄完这套厨房才几千块，"
    "人家全包人工材料，咱连家都不用搬，完工后垃圾也全给收拾走。\n"
    "男师傅：刷到的老乡评论区留个1，我带工具上门给您瞅瞅情况。"
)


def test_role_and_time_prefixes_removed():
    assert subtitles.script_to_lines("女业主：你好。\n男师傅：再见") == ["你好。", "再见"]
    timed = "0-4秒（女业主）：第一句。 4-8秒（男师傅）：第二句。"
    assert subtitles.script_to_lines(timed) == ["第一句。", "第二句。"]


def test_phrases_short_and_not_breaking_words():
    phrases = subtitles.split_phrases(SCRIPT, 9, ["老乡"])
    assert all(len(p) <= 9 for p in phrases)
    assert "".join(phrases) == "".join(
        c for c in "".join(subtitles.script_to_lines(SCRIPT)) if c not in "，。！？"
    )
    assert "评论区留个1" in phrases  # 不会切成"老乡评 / 论区"


def test_allocate_times_follows_voiced_regions():
    phrases = ["一二三四", "五六七八"]
    times = subtitles.allocate_times(phrases, [(0.0, 2.0), (3.0, 5.0)], 5.0)
    assert times[0][0] == 0.0
    assert times[1][0] == 3.0  # 第二句落在第二段人声上
    assert times[1][1] <= 5.0


def test_allocate_times_without_voice_uses_full_duration():
    times = subtitles.allocate_times(["一二", "三四"], [], 4.0)
    assert times[0] == (0.0, 2.0)
    assert times[-1][1] == 4.0


def test_highlight_marks_keywords():
    out = subtitles.highlight("珠海的街坊们", ["珠海", "珠"], "#FFE100")
    assert out.startswith("{\\c&H0000E1FF&}珠海{\\c&H00FFFFFF&}")


def test_ass_color():
    assert subtitles.ass_color("#FFE100") == "&H0000E1FF"


def test_build_ass_contains_layers():
    render = {
        "title_font": "X", "caption_font": "X", "title_color": "#FFE100",
        "disclaimer": "仅供参考", "ai_label": "内容由AI生成", "fonts_dir": "",
    }  # fmt: skip
    ass = subtitles.build_ass(1080, 1920, 12.0, "珠海蜂仔翻新团队", "厨房翻新", [(0, 1, "你好")], render)
    assert "PlayResY: 1920" in ass
    assert "Title,,0,0,0,,珠海蜂仔翻新团队" in ass
    assert "Caption,,0,0,0,,你好" in ass
    assert "内容由AI生成" in ass


def test_em_ratio_reads_fonts_dir_without_fc_match(tmp_path, monkeypatch):
    """Windows 上没有 fc-match，也要能从 assets/fonts 里读到字体尺寸。"""
    import shutil
    import subprocess

    import pytest

    src = "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"
    pytest.importorskip("fontTools")
    if not __import__("os").path.exists(src):
        pytest.skip("没有测试字体")
    shutil.copy(src, tmp_path / "wqy.ttc")

    def no_fc_match(*a, **k):
        raise FileNotFoundError("fc-match")

    monkeypatch.setattr(subprocess, "run", no_fc_match)
    subtitles._RATIO_CACHE.clear()
    assert abs(subtitles.em_ratio("WenQuanYi Zen Hei", str(tmp_path)) - 1024 / 1290) < 0.01
