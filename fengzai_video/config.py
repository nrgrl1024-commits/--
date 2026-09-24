"""配置加载：config.yaml + .env，支持 ${环境变量} 替换。"""
from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

# 飞书多维表格字段名。如果你们表格里的字段名不同，在 config.yaml 的 fields 下覆盖即可。
DEFAULT_FIELDS = {
    "stores": {
        "name": "门店名称",
        "city": "城市",
        "district": "区域",
        "top_title": "顶部标题",
        "brand": "品牌名",
        "address_term": "地方称呼",
        "dialect": "方言",
        "roles": "出镜角色",
        "style": "风格备注",
        "enabled": "启用",
    },
    "masters": {
        "title": "选题",
        "reference": "对标视频",
        "script": "脚本内容",
        "cover": "封面标题",
        "prompt_ref": "即梦提示词参考",
        "stores": "适用门店",
        "status": "状态",
    },
    "videos": {
        "title": "标题",
        "store": "门店",
        "master": "母版",
        "date": "日期",
        "script": "脚本内容",
        "rewritten": "改写脚本内容",
        "cover": "封面标题",
        "subtitle": "副标题",
        "highlights": "高亮词",
        "prompt": "即梦AI提示词",
        "generated": "生成视频",
        "final": "成片",
        "cover_image": "封面图",
        "bgm": "背景音乐",
        "status": "状态",
        "note": "备注",
    },
}

STATUS = {
    "pending_dist": "待分发",
    "distributed": "已分发",
    "to_rewrite": "待改写",
    "to_generate": "待生成",
    "editing": "剪辑中",
    "done": "已完成",
    "failed": "失败",
}

DEFAULT_RENDER = {
    "fonts_dir": "assets/fonts",
    "title_font": "WenQuanYi Zen Hei",
    "caption_font": "WenQuanYi Zen Hei",
    "music_dir": "assets/music",
    "bgm_volume": 0.12,
    "disclaimer": "视频内容仅供参考，具体以实物效果为准~",
    "ai_label": "内容由AI生成",
    "output_width": 1080,
    "crf": 18,
    "preset": "medium",
    "cover_time": 1.0,
    "caption_max_chars": 9,
    "title_color": "#FFE100",
    "highlight_color": "#FFE100",
}


@dataclass
class Config:
    feishu: dict
    llm: dict
    render: dict
    fields: dict
    work_dir: Path

    def field(self, table: str, key: str) -> str:
        return self.fields[table][key]


_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _expand(obj):
    if isinstance(obj, str):
        return _VAR.sub(lambda m: os.environ.get(m.group(1), ""), obj)
    if isinstance(obj, dict):
        return {k: _expand(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand(v) for v in obj]
    return obj


def load_config(path: str | Path | None = None) -> Config:
    _load_dotenv(ROOT / ".env")
    path = Path(path) if path else ROOT / "config.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    raw = _expand(raw or {})

    fields = copy.deepcopy(DEFAULT_FIELDS)
    for table, overrides in (raw.get("fields") or {}).items():
        fields.setdefault(table, {}).update(overrides or {})

    render = {**DEFAULT_RENDER, **(raw.get("render") or {})}
    for key in ("fonts_dir", "music_dir"):
        render[key] = str((ROOT / render[key]).resolve()) if not os.path.isabs(render[key]) else render[key]

    work_dir = Path(raw.get("work_dir") or ROOT / "work")
    if not work_dir.is_absolute():
        work_dir = ROOT / work_dir
    work_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        feishu=raw.get("feishu") or {},
        llm=raw.get("llm") or {},
        render=render,
        fields=fields,
        work_dir=work_dir,
    )
