"""用 ffmpeg 生成一段测试视频，跑一遍完整剪辑。"""
import subprocess

import pytest

from fengzai_video import editor
from fengzai_video.config import DEFAULT_RENDER

try:
    FFMPEG = editor.ffmpeg_exe()
except RuntimeError:
    FFMPEG = None


@pytest.mark.skipif(FFMPEG is None, reason="没有 ffmpeg")
def test_render_outputs_video_and_cover(tmp_path):
    src = tmp_path / "in.mp4"
    subprocess.run(
        [FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=540x940:rate=30:duration=3",
         "-f", "lavfi", "-i", "sine=frequency=300:duration=3", "-shortest", "-pix_fmt", "yuv420p", str(src)],
        check=True,
    )  # fmt: skip
    cfg = {**DEFAULT_RENDER, "fonts_dir": str(tmp_path / "nofonts"), "output_width": 540, "preset": "ultrafast"}
    out, cover = tmp_path / "out" / "成片.mp4", tmp_path / "out" / "封面.jpg"
    info = editor.render(src, out, cover, "女业主：珠海的街坊们，翻新不用搬家。", "珠海蜂仔翻新团队", "厨房翻新",
                         "珠海厨房翻新 不用搬家", ["珠海"], cfg)  # fmt: skip
    assert out.stat().st_size > 10_000
    assert cover.stat().st_size > 1_000
    assert info["captions"] == 2
    assert editor.probe(out).has_audio


def test_pick_music_is_stable(tmp_path):
    for name in ("a.mp3", "b.mp3", "c.mp3"):
        (tmp_path / name).write_bytes(b"x")
    first = editor.pick_music(str(tmp_path), "rec123")
    assert first == editor.pick_music(str(tmp_path), "rec123")
    assert editor.pick_music(str(tmp_path), "rec123", preferred="b").name == "b.mp3"
    assert editor.pick_music(str(tmp_path / "missing"), "x") is None
