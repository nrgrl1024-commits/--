"""自动剪辑：即梦原片 → 加顶部标题、口播字幕、角标、背景音乐 → 1080p 成片 + 封面图。"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import subtitles

MUSIC_EXT = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg"}


def ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        raise RuntimeError("找不到 ffmpeg：请安装 ffmpeg，或 pip install imageio-ffmpeg")


def _run(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败：{proc.stderr[-1500:]}")
    return proc.stderr


@dataclass
class Probe:
    duration: float
    width: int
    height: int
    has_audio: bool


def probe(path: Path) -> Probe:
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    info = proc.stderr
    dur = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", info)
    size = re.search(r"Stream #.*Video:.*?, (\d{2,5})x(\d{2,5})", info)
    if not (dur and size):
        raise RuntimeError(f"无法识别视频：{path}")
    h, m, s = dur.groups()
    return Probe(
        duration=int(h) * 3600 + int(m) * 60 + float(s),
        width=int(size.group(1)),
        height=int(size.group(2)),
        has_audio=bool(re.search(r"Stream #.*Audio:", info)),
    )


def detect_voiced(path: Path, duration: float, noise_db: int = -32, min_silence: float = 0.25) -> list[tuple[float, float]]:
    """用静音检测找出有人声的时间段（即梦原片不带背景音乐时最准）。"""
    log = _run(["-i", str(path), "-vn", "-af", f"silencedetect=n={noise_db}dB:d={min_silence}", "-f", "null", "-"])
    starts = [float(x) for x in re.findall(r"silence_start: (-?\d+(?:\.\d+)?)", log)]
    ends = [float(x) for x in re.findall(r"silence_end: (\d+(?:\.\d+)?)", log)]
    voiced, cursor = [], 0.0
    for i, s in enumerate(starts):
        if s - cursor > 0.15:
            voiced.append((cursor, s))
        cursor = ends[i] if i < len(ends) else duration
    if duration - cursor > 0.15:
        voiced.append((cursor, duration))
    return voiced


def pick_music(music_dir: str, key: str, preferred: str = "") -> Path | None:
    folder = Path(music_dir)
    if not folder.is_dir():
        return None
    tracks = sorted(p for p in folder.iterdir() if p.suffix.lower() in MUSIC_EXT)
    if not tracks:
        return None
    if preferred:
        for t in tracks:
            if preferred in t.stem:
                return t
    idx = int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16) % len(tracks)
    return tracks[idx]


def _filter_path(p: Path) -> str:
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def render(
    video_in: Path,
    out_video: Path,
    out_cover: Path,
    script: str,
    top_title: str,
    subtitle: str,
    cover_title: str,
    highlights: list[str],
    render_cfg: dict,
    music: Path | None = None,
) -> dict:
    info = probe(video_in)
    out_w = int(render_cfg["output_width"])
    out_h = int(round(info.height * out_w / info.width / 2) * 2)

    phrases = subtitles.split_phrases(script, int(render_cfg["caption_max_chars"]), highlights)
    voiced = detect_voiced(video_in, info.duration) if info.has_audio else []
    times = subtitles.allocate_times(phrases, voiced, info.duration)
    captions = [
        (s, e, subtitles.highlight(p, highlights, render_cfg["highlight_color"])) for p, (s, e) in zip(phrases, times)
    ]

    work = out_video.parent
    work.mkdir(parents=True, exist_ok=True)
    ass = work / "captions.ass"
    ass.write_text(
        subtitles.build_ass(out_w, out_h, info.duration, top_title, subtitle, captions, render_cfg), encoding="utf-8"
    )
    fonts = Path(render_cfg["fonts_dir"])
    sub_filter = f"subtitles=filename={ass.name}" + (f":fontsdir={_filter_path(fonts)}" if fonts.is_dir() else "")
    vf = f"scale={out_w}:{out_h}:flags=lanczos,{sub_filter}"

    args = ["-y", "-i", str(video_in.resolve())]
    if music:
        args += ["-stream_loop", "-1", "-i", str(music.resolve())]
        fade_at = max(info.duration - 1.0, 0)
        bgm = f"[1:a]volume={render_cfg['bgm_volume']},afade=t=out:st={fade_at:.2f}:d=1[bgm]"
        if info.has_audio:
            audio = f"{bgm};[0:a][bgm]amix=inputs=2:duration=first:normalize=0[aout]"
        else:
            audio = f"{bgm};[bgm]anull[aout]"
        args += ["-filter_complex", f"[0:v]{vf}[vout];{audio}", "-map", "[vout]", "-map", "[aout]"]
    else:
        args += ["-vf", vf]
    args += [
        "-t", f"{info.duration:.3f}",
        "-c:v", "libx264", "-crf", str(render_cfg["crf"]), "-preset", render_cfg["preset"],
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
        str(out_video.resolve()),
    ]  # fmt: skip
    _run(args, cwd=work)

    # 封面：取一帧 + 顶部标题 + 中间封面大字
    cover_ass = work / "cover.ass"
    cover_cfg = {**render_cfg, "disclaimer": "", "ai_label": render_cfg.get("ai_label", "")}
    cover_ass.write_text(
        subtitles.build_ass(out_w, out_h, 1.0, top_title, "", [], cover_cfg, center_text=cover_title),
        encoding="utf-8",
    )
    cover_filter = f"subtitles=filename={cover_ass.name}" + (
        f":fontsdir={_filter_path(fonts)}" if fonts.is_dir() else ""
    )
    t = min(float(render_cfg["cover_time"]), max(info.duration - 0.1, 0))
    _run(
        ["-y", "-ss", f"{t:.2f}", "-i", str(video_in.resolve()), "-frames:v", "1",
         "-vf", f"scale={out_w}:{out_h}:flags=lanczos,{cover_filter}", "-q:v", "2", str(out_cover.resolve())],
        cwd=work,
    )  # fmt: skip

    return {"duration": info.duration, "size": (out_w, out_h), "captions": len(captions), "music": music.name if music else ""}


def shrink_for_upload(video: Path, max_mb: float) -> Path:
    """企微群机器人最多发 20MB 的文件；超了就另存一份压缩版（原成片不变）。"""
    if video.stat().st_size <= max_mb * 1024 * 1024:
        return video
    info = probe(video)
    total_kbps = max_mb * 8 * 1024 * 0.92 / max(info.duration, 1)
    video_kbps = int(max(total_kbps - 160, 500))
    out = video.with_name(video.stem + "-群发版.mp4")
    _run(
        ["-y", "-i", str(video), "-c:v", "libx264", "-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps}k",
         "-bufsize", f"{video_kbps * 2}k", "-preset", "medium", "-c:a", "aac", "-b:a", "128k",
         "-movflags", "+faststart", str(out)]
    )  # fmt: skip
    return out
