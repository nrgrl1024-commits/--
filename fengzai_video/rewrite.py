"""按门店改写母版脚本，并组装即梦提示词。"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import llm
from .config import ROOT

PROMPT_FILE = ROOT / "prompts" / "rewrite.md"
SEGMENT_MAX_CHARS = 30  # 超过就认为模型没按要求写，重试


@dataclass
class Store:
    name: str
    city: str = ""
    district: str = ""
    top_title: str = ""
    brand: str = "蜂仔翻新"
    address_term: str = ""
    dialect: str = ""
    roles: str = ""
    style: str = ""
    anchor: str = ""
    webhook: str = ""

    def title(self) -> str:
        return self.top_title or f"{self.city}{self.brand}团队"


@dataclass
class Source:
    script: str
    cover: str = ""
    prompt_ref: str = ""


@dataclass
class Rewritten:
    segments: list[dict]
    cover_title: str
    subtitle: str
    highlights: list[str]
    jimeng_prompt: str
    publish_text: str = ""
    raw: dict = field(default_factory=dict)

    def script_text(self) -> str:
        """合并连续同一角色的段落，格式与你们现有脚本一致：'女业主：……\n男师傅：……'"""
        lines: list[list[str]] = []
        for seg in self.segments:
            role = seg.get("role", "")
            if lines and lines[-1][0] == role:
                lines[-1][1] += seg["text"]
            else:
                lines.append([role, seg["text"]])
        return "\n".join(f"{r}：{t}" if r else t for r, t in lines)


def build_system_prompt(store: Store) -> str:
    template = PROMPT_FILE.read_text(encoding="utf-8")
    values = {
        "brand": store.brand or "蜂仔翻新",
        "store_name": store.name,
        "city": store.city,
        "district": store.district,
        "address_term": store.address_term or "（按当地习惯）",
        "dialect": store.dialect or "普通话",
        "roles": store.roles or "女业主 + 男师傅（与母版一致）",
        "anchor": store.anchor or "门店主播",
        "style": store.style or "无",
    }
    for k, v in values.items():
        template = template.replace("{{" + k + "}}", v)
    return template


def build_user_prompt(source: Source, avoid_openings: list[str]) -> str:
    parts = [f"## 母版脚本\n{source.script}"]
    if source.cover:
        parts.append(f"## 母版封面标题\n{source.cover}")
    if source.prompt_ref:
        parts.append(f"## 对标视频镜头分析（参考场景和镜头，按本次内容改写）\n{source.prompt_ref}")
    if avoid_openings:
        joined = "\n".join(f"- {o}" for o in avoid_openings)
        parts.append(f"## 以下开头其他门店已经用过，不要雷同\n{joined}")
    return "\n\n".join(parts)


def format_jimeng_prompt(data: dict, store: Store) -> str:
    main = data["prompt_main"].strip()
    if store.dialect and store.dialect not in main:
        main += f" 口播使用{store.dialect}。"
    voice = " ".join(
        f"{_num(s['start'])}-{_num(s['end'])}秒（{s.get('role', '')}）：{s['text']}" for s in data["segments"]
    )
    return "\n".join(
        [
            "生成时关闭自动字幕",
            "即梦主生成提示词",
            main,
            "负面约束防崩坏关键词",
            data.get("prompt_negative", "").strip(),
            "完整口播文案",
            voice,
            "设备、光影、运镜、音效规范",
            data.get("prompt_spec", "").strip(),
        ]
    )


def _num(x) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)


def validate(data: dict) -> None:
    segs = data.get("segments")
    if not isinstance(segs, list) or not segs:
        raise llm.LLMError("缺少 segments")
    for s in segs:
        if not s.get("text"):
            raise llm.LLMError(f"段落缺少 text：{s}")
        if len(s["text"]) > SEGMENT_MAX_CHARS:
            raise llm.LLMError(f"段落过长（{len(s['text'])}字）：{s['text']}")
    for key in ("cover_title", "prompt_main"):
        if not data.get(key):
            raise llm.LLMError(f"缺少 {key}")


def rewrite(llm_cfg: dict, store: Store, source: Source, avoid_openings: list[str] | None = None, retries: int = 2) -> Rewritten:
    system = build_system_prompt(store)
    user = build_user_prompt(source, avoid_openings or [])
    last_err: Exception | None = None
    for _ in range(retries + 1):
        try:
            data = llm.parse_json(llm.chat(llm_cfg, system, user))
            validate(data)
            break
        except llm.LLMError as e:
            last_err = e
    else:
        raise last_err  # type: ignore[misc]

    script = "".join(s["text"] for s in data["segments"])
    highlights = [h for h in (data.get("highlights") or []) if isinstance(h, str) and h and h in script]
    return Rewritten(
        segments=data["segments"],
        cover_title=data["cover_title"].strip(),
        subtitle=(data.get("subtitle") or "").strip(),
        highlights=highlights,
        jimeng_prompt=format_jimeng_prompt(data, store),
        publish_text=(data.get("publish_text") or "").strip(),
        raw=data,
    )
