"""看对标视频：抽帧交给豆包视觉模型，提取口播文案、封面文案、镜头分析（替代小云雀）。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import editor, llm
from .config import ROOT

PROMPT_FILE = ROOT / "prompts" / "analyze.md"


@dataclass
class Analysis:
    script: str
    cover: str
    shots: str
    hook: str


def extract_frames(video: Path, out_dir: Path, max_frames: int = 16, width: int = 540) -> tuple[list[Path], float, float]:
    info = editor.probe(video)
    interval = max(info.duration / max_frames, 0.5)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("f_*.jpg"):
        old.unlink()
    editor._run(
        ["-y", "-i", str(video), "-vf", f"fps=1/{interval:.3f},scale={width}:-2", "-frames:v", str(max_frames),
         "-q:v", "4", str(out_dir / "f_%03d.jpg")]
    )  # fmt: skip
    return sorted(out_dir.glob("f_*.jpg")), interval, info.duration


def analyze(llm_cfg: dict, video: Path, work: Path) -> Analysis:
    frames, interval, duration = extract_frames(video, work / "frames", int(llm_cfg.get("vision_max_frames", 16)))
    if not frames:
        raise llm.LLMError("对标视频抽不出画面")
    prompt = (
        PROMPT_FILE.read_text(encoding="utf-8")
        .replace("{{interval}}", f"{interval:.1f}")
        .replace("{{count}}", str(len(frames)))
        .replace("{{duration}}", f"{duration:.0f}")
    )
    data = llm.parse_json(llm.vision(llm_cfg, prompt, frames))
    if not data.get("script"):
        raise llm.LLMError("没能从对标视频里识别出口播文案，请手动填写「脚本内容」")
    shots = "\n".join(f"{s.get('time', '')}：{s.get('desc', '')}" for s in data.get("shots") or [])
    return Analysis(
        script=data["script"].strip(),
        cover=(data.get("cover") or "").strip(),
        shots=shots,
        hook=(data.get("hook") or "").strip(),
    )
