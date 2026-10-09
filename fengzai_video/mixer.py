"""混剪：用本城市真人主播的出镜素材 + 通用素材拼出画面，配上即梦成片里已经配好的 AI 口播。

画面规则（保证一定有本城市主播）：
- 开头一段、结尾一段一定是主播出镜
- 中间主播镜头和其他素材交替出现，主播至少占一半
- 别的城市的主播素材永远不会被选中（在 pipeline 里按城市筛好再传进来）
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

from .editor import _run, probe

MIN_CLIP = 0.5  # 短于这个秒数的素材不用


@dataclass
class Clip:
    key: str  # 素材库里的记录 ID，用来统计使用次数
    path: Path
    duration: float
    anchor: bool  # 画面里有本城市主播


@dataclass
class Segment:
    clip: Clip
    start: float
    length: float


def plan(
    duration: float,
    anchors: list[Clip],
    brolls: list[Clip],
    seed: str,
    segment_seconds: float = 3.0,
    opening_seconds: float = 3.0,
) -> list[Segment]:
    """把口播时长切成若干段，每段配一个素材片段。素材按传入顺序轮流使用（使用少的排前面）。"""
    anchors = [c for c in anchors if c.duration >= MIN_CLIP]
    brolls = [c for c in brolls if c.duration >= MIN_CLIP]
    if not anchors:
        raise RuntimeError("没有可用的主播出镜素材")
    rng = random.Random(seed)
    cursors = {True: 0, False: 0}
    segments: list[Segment] = []
    t, i = 0.0, 0
    while duration - t > 0.05:
        remaining = duration - t
        target = opening_seconds if i == 0 else segment_seconds
        last = remaining - target < segment_seconds * 0.5
        if last:
            target = remaining  # 最后一段吃掉剩下的时间，避免末尾出现很碎的镜头
        use_anchor = i == 0 or last or not brolls or i % 2 == 0
        pool = anchors if use_anchor else brolls
        clip = pool[cursors[use_anchor] % len(pool)]
        if segments and clip is segments[-1].clip and len(pool) > 1:  # 同一个素材不紧挨着出现
            cursors[use_anchor] += 1
            clip = pool[cursors[use_anchor] % len(pool)]
        cursors[use_anchor] += 1
        length = min(target, clip.duration)
        start = rng.uniform(0, clip.duration - length) if clip.duration > length else 0.0
        segments.append(Segment(clip, round(start, 2), round(length, 3)))
        t += length
        i += 1
    return segments


def anchor_share(segments: list[Segment]) -> float:
    total = sum(s.length for s in segments) or 1
    return sum(s.length for s in segments if s.clip.anchor) / total


def montage(segments: list[Segment], voice_video: Path, out: Path, width: int, height: int, fps: int = 30) -> Path:
    """按 plan 的结果拼接画面（素材原声去掉），音轨用即梦成片里的 AI 口播。"""
    voice = probe(voice_video)
    if not voice.has_audio:
        raise RuntimeError("即梦视频里没有声音，无法做混剪口播")
    args = ["-y"]
    chains = []
    for idx, seg in enumerate(segments):
        args += ["-ss", f"{seg.start:.3f}", "-t", f"{seg.length:.3f}", "-i", str(seg.clip.path.resolve())]
        chains.append(
            f"[{idx}:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
            f"setsar=1,fps={fps},format=yuv420p,trim=duration={seg.length:.3f},setpts=PTS-STARTPTS[v{idx}]"
        )
    args += ["-i", str(voice_video.resolve())]
    concat = "".join(f"[v{i}]" for i in range(len(segments))) + f"concat=n={len(segments)}:v=1:a=0[vout]"
    out.parent.mkdir(parents=True, exist_ok=True)
    args += [
        "-filter_complex", ";".join(chains + [concat]),
        "-map", "[vout]", "-map", f"{len(segments)}:a",
        "-t", f"{voice.duration:.3f}",
        "-c:v", "libx264", "-crf", "16", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", str(out.resolve()),
    ]  # fmt: skip
    _run(args)
    return out


def load_clips(items: list[tuple[str, Path, bool]]) -> list[Clip]:
    """items: (素材记录 ID, 本地文件, 是否主播出镜)。读不出来的文件跳过。"""
    clips = []
    for key, path, anchor in items:
        try:
            clips.append(Clip(key, path, probe(path).duration, anchor))
        except RuntimeError:
            continue
    return clips
