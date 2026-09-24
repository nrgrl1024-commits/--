"""初始化飞书多维表格：建「门店资料」「母版」两页，并给每家门店自己的那一页补齐需要的字段。

可以反复运行：已经存在的页面和字段不会重复创建，也不会改动你们原有的数据。
"""
from __future__ import annotations

import logging

from .config import MASTERS_TABLE, STATUS, STORES_TABLE, Config
from .feishu import Feishu, text_of

TEXT, SELECT, DATE, CHECKBOX, ATTACHMENT, LINK = 1, 3, 5, 7, 17, 18
log = logging.getLogger("fengzai")


def _select(options: list[str]) -> dict:
    return {"options": [{"name": o} for o in options]}


def store_page_fields(cfg: Config, masters_tid: str) -> list[dict]:
    """门店页需要的字段；第一个是主字段。"""
    V = cfg.fields["videos"]
    status = [STATUS[k] for k in ("to_rewrite", "to_generate", "editing", "done", "failed")]
    return [
        {"field_name": V["title"], "type": TEXT},
        {"field_name": V["date"], "type": DATE},
        {"field_name": V["status"], "type": SELECT, "property": _select(status)},
        {"field_name": V["master"], "type": LINK, "property": {"table_id": masters_tid, "multiple": False}},
        {"field_name": V["script"], "type": TEXT},
        {"field_name": V["rewritten"], "type": TEXT},
        {"field_name": V["cover"], "type": TEXT},
        {"field_name": V["subtitle"], "type": TEXT},
        {"field_name": V["highlights"], "type": TEXT},
        {"field_name": V["reference"], "type": ATTACHMENT},
        {"field_name": V["prompt"], "type": TEXT},
        {"field_name": V["generated"], "type": ATTACHMENT},
        {"field_name": V["final"], "type": ATTACHMENT},
        {"field_name": V["cover_image"], "type": ATTACHMENT},
        {"field_name": V["bgm"], "type": TEXT},
        {"field_name": V["note"], "type": TEXT},
    ]


def ensure_base(cfg: Config, fs: Feishu) -> dict[str, str]:
    """确保「母版」「门店资料」两页存在；门店资料第一次创建时，按现有页面名预填门店。"""
    tables = fs.list_tables()
    F = cfg.fields
    if MASTERS_TABLE not in tables:
        tables[MASTERS_TABLE] = fs.create_table(
            MASTERS_TABLE,
            [
                {"field_name": F["masters"]["title"], "type": TEXT},
                {"field_name": F["masters"]["reference"], "type": ATTACHMENT},
                {"field_name": F["masters"]["script"], "type": TEXT},
                {"field_name": F["masters"]["cover"], "type": TEXT},
                {"field_name": F["masters"]["prompt_ref"], "type": TEXT},
                {"field_name": F["masters"]["stores"], "type": TEXT},
                {
                    "field_name": F["masters"]["status"],
                    "type": SELECT,
                    "property": _select([STATUS["pending_dist"], STATUS["distributed"]]),
                },
            ],
        )
        log.info("已创建「%s」页", MASTERS_TABLE)
    if STORES_TABLE not in tables:
        S = F["stores"]
        tables[STORES_TABLE] = fs.create_table(
            STORES_TABLE,
            [
                {"field_name": S["name"], "type": TEXT},
                {"field_name": S["table"], "type": TEXT},
                {"field_name": S["enabled"], "type": CHECKBOX},
                {"field_name": S["city"], "type": TEXT},
                {"field_name": S["district"], "type": TEXT},
                {"field_name": S["top_title"], "type": TEXT},
                {"field_name": S["brand"], "type": TEXT},
                {"field_name": S["address_term"], "type": TEXT},
                {"field_name": S["dialect"], "type": TEXT},
                {"field_name": S["roles"], "type": TEXT},
                {"field_name": S["style"], "type": TEXT},
            ],
        )
        pages = [n for n in tables if n not in (MASTERS_TABLE, STORES_TABLE)]
        if pages:
            fs.batch_create(
                tables[STORES_TABLE],
                [{S["name"]: n, S["table"]: n, S["enabled"]: True, S["brand"]: "蜂仔翻新"} for n in pages],
            )
        log.info("已创建「%s」页，并按现有 %d 个页面预填了门店，请补充城市、称呼、方言等信息", STORES_TABLE, len(pages))
    return tables


def ensure_store_page(cfg: Config, fs: Feishu, tables: dict[str, str], page: str) -> list[str]:
    """门店页不存在就新建；存在就只补缺少的字段。返回新增的字段名。"""
    fields = store_page_fields(cfg, tables[MASTERS_TABLE])
    if page not in tables:
        tables[page] = fs.create_table(page, fields)
        return [f["field_name"] for f in fields]
    existing = fs.list_fields(tables[page])
    added = []
    for field in fields[1:]:  # 主字段不动
        if field["field_name"] not in existing:
            fs.create_field(tables[page], field)
            added.append(field["field_name"])
    return added


def setup(cfg: Config, fs: Feishu) -> dict[str, list[str]]:
    tables = ensure_base(cfg, fs)
    S = cfg.fields["stores"]
    report = {}
    for rec in fs.list_records(tables[STORES_TABLE]):
        fields = rec.get("fields", {})
        page = text_of(fields.get(S["table"])).strip() or text_of(fields.get(S["name"])).strip()
        if page and page not in (MASTERS_TABLE, STORES_TABLE):
            report[page] = ensure_store_page(cfg, fs, tables, page)
    return report


def add_store(cfg: Config, fs: Feishu, name: str, city: str = "") -> None:
    """新开一家门店：建好它的页面，并在门店资料里加一行。"""
    tables = ensure_base(cfg, fs)
    ensure_store_page(cfg, fs, tables, name)
    S = cfg.fields["stores"]
    fs.batch_create(
        tables[STORES_TABLE],
        [{S["name"]: name, S["table"]: name, S["enabled"]: True, S["city"]: city, S["brand"]: "蜂仔翻新"}],
    )
