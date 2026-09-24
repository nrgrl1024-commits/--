"""在飞书多维表格里一键建好三张表：门店资料、母版、每日视频。"""
from __future__ import annotations

from .config import STATUS, Config
from .feishu import Feishu

TEXT, SELECT, DATE, CHECKBOX, ATTACHMENT, LINK = 1, 3, 5, 7, 17, 18


def _select(options: list[str]) -> dict:
    return {"options": [{"name": o} for o in options]}


def create_tables(cfg: Config, fs: Feishu) -> dict[str, str]:
    F = cfg.fields
    stores = fs.create_table(
        "门店资料",
        [
            {"field_name": F["stores"]["name"], "type": TEXT},
            {"field_name": F["stores"]["city"], "type": TEXT},
            {"field_name": F["stores"]["district"], "type": TEXT},
            {"field_name": F["stores"]["top_title"], "type": TEXT},
            {"field_name": F["stores"]["brand"], "type": TEXT},
            {"field_name": F["stores"]["address_term"], "type": TEXT},
            {"field_name": F["stores"]["dialect"], "type": TEXT},
            {"field_name": F["stores"]["roles"], "type": TEXT},
            {"field_name": F["stores"]["style"], "type": TEXT},
            {"field_name": F["stores"]["enabled"], "type": CHECKBOX},
        ],
    )
    masters = fs.create_table(
        "母版",
        [
            {"field_name": F["masters"]["title"], "type": TEXT},
            {"field_name": F["masters"]["reference"], "type": ATTACHMENT},
            {"field_name": F["masters"]["script"], "type": TEXT},
            {"field_name": F["masters"]["cover"], "type": TEXT},
            {"field_name": F["masters"]["prompt_ref"], "type": TEXT},
            {"field_name": F["masters"]["stores"], "type": LINK, "property": {"table_id": stores, "multiple": True}},
            {
                "field_name": F["masters"]["status"],
                "type": SELECT,
                "property": _select([STATUS["pending_dist"], STATUS["distributed"]]),
            },
        ],
    )
    video_status = [STATUS[k] for k in ("to_rewrite", "to_generate", "editing", "done", "failed")]
    videos = fs.create_table(
        "每日视频",
        [
            {"field_name": F["videos"]["title"], "type": TEXT},
            {"field_name": F["videos"]["store"], "type": LINK, "property": {"table_id": stores, "multiple": False}},
            {"field_name": F["videos"]["master"], "type": LINK, "property": {"table_id": masters, "multiple": False}},
            {"field_name": F["videos"]["date"], "type": DATE},
            {"field_name": F["videos"]["status"], "type": SELECT, "property": _select(video_status)},
            {"field_name": F["videos"]["script"], "type": TEXT},
            {"field_name": F["videos"]["rewritten"], "type": TEXT},
            {"field_name": F["videos"]["cover"], "type": TEXT},
            {"field_name": F["videos"]["subtitle"], "type": TEXT},
            {"field_name": F["videos"]["highlights"], "type": TEXT},
            {"field_name": F["videos"]["prompt"], "type": TEXT},
            {"field_name": F["videos"]["generated"], "type": ATTACHMENT},
            {"field_name": F["videos"]["final"], "type": ATTACHMENT},
            {"field_name": F["videos"]["cover_image"], "type": ATTACHMENT},
            {"field_name": F["videos"]["bgm"], "type": TEXT},
            {"field_name": F["videos"]["note"], "type": TEXT},
        ],
    )
    return {"stores": stores, "masters": masters, "videos": videos}
