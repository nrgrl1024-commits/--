"""每日流程：分发母版 → AI 改写 → （门店在即梦生成并上传）→ 自动剪辑回写。

每家门店是多维表格里单独的一页（数据表），视频都记录在自己那一页里，方便长期翻看。
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import traceback
from dataclasses import dataclass

from . import editor
from .config import MASTERS_TABLE, STATUS, STORES_TABLE, Config
from .feishu import Feishu, attachments, link_ids, text_of
from .rewrite import Source, Store, rewrite

log = logging.getLogger("fengzai")


@dataclass
class StorePage:
    store: Store
    table_id: str
    page: str


class Pipeline:
    def __init__(self, cfg: Config, feishu: Feishu | None = None):
        self.cfg = cfg
        self.fs = feishu or Feishu(cfg.feishu.get("app_id"), cfg.feishu.get("app_secret"), cfg.feishu.get("app_token"))

    def f(self, table: str, key: str) -> str:
        return self.cfg.field(table, key)

    # ---------- 读取页面和门店 ----------
    def load(self) -> tuple[dict[str, str], list[StorePage]]:
        tables = self.fs.list_tables()
        if STORES_TABLE not in tables:
            raise RuntimeError(f"多维表格里没有「{STORES_TABLE}」页，请先运行 setup")
        pages = []
        for rec in self.fs.list_records(tables[STORES_TABLE]):
            fields = rec.get("fields", {})
            g = lambda k: text_of(fields.get(self.f("stores", k))).strip()  # noqa: E731
            enabled = fields.get(self.f("stores", "enabled"))
            if enabled is not None and not enabled:
                continue
            page = g("table") or g("name")
            if page not in tables:
                log.warning("门店「%s」对应的页面「%s」不存在，已跳过", g("name"), page)
                continue
            store = Store(
                name=g("name") or page,
                city=g("city"),
                district=g("district"),
                top_title=g("top_title"),
                brand=g("brand") or "蜂仔翻新",
                address_term=g("address_term"),
                dialect=g("dialect"),
                roles=g("roles"),
                style=g("style"),
            )
            pages.append(StorePage(store, tables[page], page))
        return tables, pages

    # ---------- 1. 分发母版：在每家门店的页面里各加一行 ----------
    def distribute(self) -> int:
        tables, pages = self.load()
        if MASTERS_TABLE not in tables:
            return 0
        today = int(dt.datetime.combine(dt.date.today(), dt.time()).timestamp() * 1000)
        created = 0
        for rec in self.fs.list_records(tables[MASTERS_TABLE]):
            fields = rec.get("fields", {})
            if text_of(fields.get(self.f("masters", "status"))) != STATUS["pending_dist"]:
                continue
            chosen = _split_words(text_of(fields.get(self.f("masters", "stores"))))
            topic = text_of(fields.get(self.f("masters", "title"))) or "母版"
            targets = [p for p in pages if not chosen or p.store.name in chosen or p.page in chosen]
            for p in targets:
                self.fs.batch_create(
                    p.table_id,
                    [
                        {
                            self.f("videos", "title"): f"{dt.date.today():%m-%d} {topic}",
                            self.f("videos", "master"): [rec["record_id"]],
                            self.f("videos", "date"): today,
                            self.f("videos", "script"): text_of(fields.get(self.f("masters", "script"))),
                            self.f("videos", "status"): STATUS["to_rewrite"],
                        }
                    ],
                )
            self.fs.update_record(
                tables[MASTERS_TABLE], rec["record_id"], {self.f("masters", "status"): STATUS["distributed"]}
            )
            log.info("母版「%s」已分发到 %d 家门店", topic, len(targets))
            created += len(targets)
        return created

    # ---------- 2. AI 改写 ----------
    def rewrite_pending(self) -> int:
        tables, pages = self.load()
        masters = {}
        if MASTERS_TABLE in tables:
            masters = {r["record_id"]: r.get("fields", {}) for r in self.fs.list_records(tables[MASTERS_TABLE])}

        rows = [(p, rec) for p in pages for rec in self.fs.list_records(p.table_id)]

        # 同一母版下其他门店已用过的开头，交给模型避开，保证各门店不雷同
        used: dict[str, list[str]] = {}
        for _, rec in rows:
            fields = rec.get("fields", {})
            done = text_of(fields.get(self.f("videos", "rewritten")))
            mid = _first(link_ids(fields.get(self.f("videos", "master"))))
            if done and mid:
                used.setdefault(mid, []).append(_opening(done))

        count = 0
        for p, rec in rows:
            fields = rec.get("fields", {})
            if text_of(fields.get(self.f("videos", "status"))) != STATUS["to_rewrite"]:
                continue
            rid = rec["record_id"]
            try:
                mid = _first(link_ids(fields.get(self.f("videos", "master"))))
                source = self._source(fields, masters.get(mid, {}))
                result = rewrite(self.cfg.llm, p.store, source, used.get(mid, [])[-10:])
                self.fs.update_record(
                    p.table_id,
                    rid,
                    {
                        self.f("videos", "rewritten"): result.script_text(),
                        self.f("videos", "cover"): result.cover_title,
                        self.f("videos", "subtitle"): result.subtitle,
                        self.f("videos", "highlights"): "，".join(result.highlights),
                        self.f("videos", "prompt"): result.jimeng_prompt,
                        self.f("videos", "status"): STATUS["to_generate"],
                        self.f("videos", "note"): "",
                    },
                )
                used.setdefault(mid, []).append(_opening(result.script_text()))
                count += 1
                log.info("已改写：%s", p.store.name)
            except Exception as e:  # 单条失败不影响其他门店
                log.exception("改写失败 %s/%s", p.page, rid)
                self._fail(p.table_id, rid, f"改写失败：{e}")
        return count

    def _source(self, row: dict, master: dict) -> Source:
        """有关联母版就用母版；没有就用这一行自己填的脚本（门店也可以自己加一行来改写）。"""
        script = text_of(master.get(self.f("masters", "script"))) or text_of(row.get(self.f("videos", "script")))
        if not script.strip():
            raise RuntimeError("没有脚本内容")
        return Source(
            script=script,
            cover=text_of(master.get(self.f("masters", "cover"))) or text_of(row.get(self.f("videos", "cover"))),
            prompt_ref=text_of(master.get(self.f("masters", "prompt_ref"))),
        )

    # ---------- 3. 自动剪辑 ----------
    def edit_pending(self) -> int:
        _, pages = self.load()
        count = 0
        for p in pages:
            for rec in self.fs.list_records(p.table_id):
                fields = rec.get("fields", {})
                # 只处理「待生成」且已上传即梦视频、还没有成片的行；你们以前手工做好的旧行不会被动到
                if text_of(fields.get(self.f("videos", "status"))) != STATUS["to_generate"]:
                    continue
                generated = attachments(fields.get(self.f("videos", "generated")))
                if not generated or attachments(fields.get(self.f("videos", "final"))):
                    continue
                if self._edit_one(p, rec["record_id"], fields, generated[-1]):
                    count += 1
        return count

    def _edit_one(self, p: StorePage, rid: str, fields: dict, video: dict) -> bool:
        try:
            self.fs.update_record(p.table_id, rid, {self.f("videos", "status"): STATUS["editing"]})
            store = p.store
            script = text_of(fields.get(self.f("videos", "rewritten"))) or text_of(fields.get(self.f("videos", "script")))
            work = self.cfg.work_dir / rid
            src = self.fs.download(video, work / "input.mp4")
            stamp = dt.date.today().strftime("%m%d")
            out_video = work / f"{store.name}-{stamp}.mp4"
            out_cover = work / f"{store.name}-{stamp}-封面.jpg"
            cover_title = text_of(fields.get(self.f("videos", "cover")))
            subtitle = text_of(fields.get(self.f("videos", "subtitle"))) or _strip_city(cover_title, store.city)
            highlights = _split_words(text_of(fields.get(self.f("videos", "highlights"))))
            music = editor.pick_music(self.cfg.render["music_dir"], rid, text_of(fields.get(self.f("videos", "bgm"))))
            editor.render(
                src, out_video, out_cover, script, store.title(), subtitle,
                cover_title, highlights, self.cfg.render, music,
            )  # fmt: skip
            video_token = self.fs.upload(out_video, "bitable_file")
            cover_token = self.fs.upload(out_cover, "bitable_image")
            self.fs.update_record(
                p.table_id,
                rid,
                {
                    self.f("videos", "final"): [{"file_token": video_token}],
                    self.f("videos", "cover_image"): [{"file_token": cover_token}],
                    self.f("videos", "status"): STATUS["done"],
                    self.f("videos", "note"): "",
                },
            )
            log.info("已出成片：%s", store.name)
            return True
        except Exception as e:
            log.error("剪辑失败 %s/%s：%s", p.page, rid, traceback.format_exc())
            self._fail(p.table_id, rid, f"剪辑失败：{e}")
            return False

    def _fail(self, table_id: str, rid: str, msg: str) -> None:
        try:
            self.fs.update_record(
                table_id, rid, {self.f("videos", "status"): STATUS["failed"], self.f("videos", "note"): msg[:500]}
            )
        except Exception:
            log.exception("回写失败状态也失败了 %s", rid)

    def run_once(self) -> dict:
        return {"分发": self.distribute(), "改写": self.rewrite_pending(), "剪辑": self.edit_pending()}


def _first(items: list[str]) -> str:
    return items[0] if items else ""


def _opening(script: str) -> str:
    first = script.splitlines()[0] if script else ""
    first = first.split("：", 1)[-1]
    return re.split(r"[！!。，,？?]", first)[0][:20]


def _split_words(text: str) -> list[str]:
    return [w for w in re.split(r"[，,、;；\s]+", text) if w]


def _strip_city(cover: str, city: str) -> str:
    return cover.replace(city, "", 1).strip() if city else cover
