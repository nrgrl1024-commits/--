"""初始化飞书多维表格：建「门店资料」「母版」「今日任务」三页，并给每家门店自己的那一页补齐字段。

可以反复运行：已经存在的页面和字段不会重复创建，也不会改动你们原有的数据。
"""
from __future__ import annotations

import logging

from .config import HQ_TABLES, MASTERS_TABLE, STATUS, STORES_TABLE, TASKS_TABLE, Config
from .feishu import Feishu, text_of

TEXT, SELECT, DATE, CHECKBOX, ATTACHMENT, LINK = 1, 3, 5, 7, 17, 18
log = logging.getLogger("fengzai")

VIDEO_STATUS = ("to_rewrite", "to_generate", "editing", "done", "failed")


def _select(options: list[str]) -> dict:
    return {"options": [{"name": o} for o in options]}


def _status_field(name: str, keys) -> dict:
    return {"field_name": name, "type": SELECT, "property": _select([STATUS[k] for k in keys])}


def store_page_fields(cfg: Config, masters_tid: str) -> list[dict]:
    """门店页需要的字段；第一个是主字段。"""
    V = cfg.fields["videos"]
    return [
        {"field_name": V["title"], "type": TEXT},
        {"field_name": V["date"], "type": DATE},
        _status_field(V["status"], VIDEO_STATUS),
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
        {"field_name": V["publish"], "type": TEXT},
        {"field_name": V["pushed"], "type": CHECKBOX},
        {"field_name": V["bgm"], "type": TEXT},
        {"field_name": V["note"], "type": TEXT},
    ]


def ensure_base(cfg: Config, fs: Feishu) -> dict[str, str]:
    """确保三页总部表存在；门店资料第一次创建时，按现有页面名预填门店。"""
    tables = fs.list_tables()
    F = cfg.fields
    if MASTERS_TABLE not in tables:
        M = F["masters"]
        tables[MASTERS_TABLE] = fs.create_table(
            MASTERS_TABLE,
            [
                {"field_name": M["title"], "type": TEXT},
                {"field_name": M["reference"], "type": ATTACHMENT},
                _status_field(M["status"], ("pending_dist", "analyzing", "distributed", "failed")),
                {"field_name": M["script"], "type": TEXT},
                {"field_name": M["cover"], "type": TEXT},
                {"field_name": M["prompt_ref"], "type": TEXT},
                {"field_name": M["stores"], "type": TEXT},
                {"field_name": M["note"], "type": TEXT},
            ],
        )
        log.info("已创建「%s」页", MASTERS_TABLE)
    if TASKS_TABLE not in tables:
        T = F["tasks"]
        tables[TASKS_TABLE] = fs.create_table(
            TASKS_TABLE,
            [
                {"field_name": T["title"], "type": TEXT},
                {"field_name": T["anchor"], "type": TEXT},
                {"field_name": T["prompt"], "type": TEXT},
                {"field_name": T["generated"], "type": ATTACHMENT},
                _status_field(T["status"], ("to_generate", "editing", "done", "failed")),
                {"field_name": T["final"], "type": ATTACHMENT},
                {"field_name": T["date"], "type": DATE},
                {"field_name": T["ref"], "type": TEXT},
            ],
        )
        log.info("已创建「%s」页", TASKS_TABLE)
    if STORES_TABLE not in tables:
        S = F["stores"]
        tables[STORES_TABLE] = fs.create_table(
            STORES_TABLE,
            [{"field_name": S["name"], "type": TEXT}]
            + [
                {"field_name": S[k], "type": CHECKBOX if k == "enabled" else TEXT}
                for k in S
                if k != "name"
            ],
        )
        pages = [n for n in tables if n not in HQ_TABLES]
        if pages:
            fs.batch_create(tables[STORES_TABLE], [{S["name"]: n, S["enabled"]: True} for n in pages])
        log.info("已创建「%s」页，并按现有 %d 个页面预填了门店，请补充城市、即梦主播等信息", STORES_TABLE, len(pages))
    return tables


def ensure_store_page(cfg: Config, fs: Feishu, tables: dict[str, str], page: str) -> list[str]:
    """门店页不存在就新建；存在就只补缺少的字段。返回新增的字段名。"""
    fields = store_page_fields(cfg, tables[MASTERS_TABLE])
    if page not in tables:
        tables[page] = fs.create_table(page, fields)
        log.info("已为新门店建好页面「%s」", page)
        return [f["field_name"] for f in fields]
    existing = fs.list_fields(tables[page])
    added = []
    for field in fields[1:]:  # 主字段不动
        if field["field_name"] not in existing:
            fs.create_field(tables[page], field)
            added.append(field["field_name"])
    return added


def store_page_name(cfg: Config, fields: dict) -> str:
    S = cfg.fields["stores"]
    return text_of(fields.get(S["table"])).strip() or text_of(fields.get(S["name"])).strip()


def setup(cfg: Config, fs: Feishu) -> dict[str, list[str]]:
    tables = ensure_base(cfg, fs)
    report = {}
    for rec in fs.list_records(tables[STORES_TABLE]):
        page = store_page_name(cfg, rec.get("fields", {}))
        if page and page not in HQ_TABLES:
            report[page] = ensure_store_page(cfg, fs, tables, page)
    return report
