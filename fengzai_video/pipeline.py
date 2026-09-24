"""每日流程：分发母版 → AI 改写 → （门店在即梦生成并上传）→ 自动剪辑回写。"""
from __future__ import annotations

import datetime as dt
import logging
import traceback

from . import editor
from .config import STATUS, Config
from .feishu import Feishu, attachments, link_ids, text_of
from .rewrite import Source, Store, rewrite

log = logging.getLogger("fengzai")


class Pipeline:
    def __init__(self, cfg: Config, feishu: Feishu | None = None):
        self.cfg = cfg
        self.fs = feishu or Feishu(cfg.feishu.get("app_id"), cfg.feishu.get("app_secret"), cfg.feishu.get("app_token"))
        self.tables = cfg.feishu.get("tables") or {}

    def f(self, table: str, key: str) -> str:
        return self.cfg.field(table, key)

    def _table(self, name: str) -> str:
        tid = self.tables.get(name)
        if not tid:
            raise RuntimeError(f"config.yaml 里缺少 feishu.tables.{name}（先运行 setup 建表）")
        return tid

    # ---------- 读取门店 ----------
    def load_stores(self) -> dict[str, tuple[Store, bool]]:
        stores = {}
        for rec in self.fs.list_records(self._table("stores")):
            fields = rec.get("fields", {})
            g = lambda k: text_of(fields.get(self.f("stores", k))).strip()  # noqa: E731
            enabled_raw = fields.get(self.f("stores", "enabled"))
            store = Store(
                name=g("name"),
                city=g("city"),
                district=g("district"),
                top_title=g("top_title"),
                brand=g("brand") or "蜂仔翻新",
                address_term=g("address_term"),
                dialect=g("dialect"),
                roles=g("roles"),
                style=g("style"),
            )
            stores[rec["record_id"]] = (store, enabled_raw is None or bool(enabled_raw))
        return stores

    # ---------- 1. 分发母版 ----------
    def distribute(self) -> int:
        masters_t, videos_t = self._table("masters"), self._table("videos")
        stores = self.load_stores()
        today = int(dt.datetime.combine(dt.date.today(), dt.time()).timestamp() * 1000)
        created = 0
        for rec in self.fs.list_records(masters_t):
            fields = rec.get("fields", {})
            if text_of(fields.get(self.f("masters", "status"))) != STATUS["pending_dist"]:
                continue
            chosen = link_ids(fields.get(self.f("masters", "stores")))
            targets = [sid for sid, (_, on) in stores.items() if on and (not chosen or sid in chosen)]
            topic = text_of(fields.get(self.f("masters", "title"))) or "母版"
            rows = [
                {
                    self.f("videos", "title"): f"{stores[sid][0].name}-{topic}",
                    self.f("videos", "store"): [sid],
                    self.f("videos", "master"): [rec["record_id"]],
                    self.f("videos", "date"): today,
                    self.f("videos", "script"): text_of(fields.get(self.f("masters", "script"))),
                    self.f("videos", "status"): STATUS["to_rewrite"],
                }
                for sid in targets
            ]
            if rows:
                self.fs.batch_create(videos_t, rows)
            self.fs.update_record(masters_t, rec["record_id"], {self.f("masters", "status"): STATUS["distributed"]})
            log.info("母版「%s」已分发到 %d 家门店", topic, len(rows))
            created += len(rows)
        return created

    # ---------- 2. AI 改写 ----------
    def rewrite_pending(self) -> int:
        videos_t = self._table("videos")
        stores = self.load_stores()
        masters = {r["record_id"]: r.get("fields", {}) for r in self.fs.list_records(self._table("masters"))}
        records = self.fs.list_records(videos_t)

        # 同一母版下已用过的开头，交给模型避开，保证各门店不雷同
        used: dict[str, list[str]] = {}
        for rec in records:
            fields = rec.get("fields", {})
            done = text_of(fields.get(self.f("videos", "rewritten")))
            mid = next(iter(link_ids(fields.get(self.f("videos", "master")))), "")
            if done and mid:
                used.setdefault(mid, []).append(_opening(done))

        count = 0
        for rec in records:
            fields = rec.get("fields", {})
            if text_of(fields.get(self.f("videos", "status"))) != STATUS["to_rewrite"]:
                continue
            rid = rec["record_id"]
            try:
                sid = next(iter(link_ids(fields.get(self.f("videos", "store")))), "")
                if sid not in stores:
                    raise RuntimeError("没有关联门店")
                store = stores[sid][0]
                mid = next(iter(link_ids(fields.get(self.f("videos", "master")))), "")
                source = self._source(fields, masters.get(mid, {}))
                result = rewrite(self.cfg.llm, store, source, used.get(mid, [])[-10:])
                self.fs.update_record(
                    videos_t,
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
                log.info("已改写：%s", store.name)
            except Exception as e:  # 单条失败不影响其他门店
                log.exception("改写失败 %s", rid)
                self._fail(videos_t, rid, f"改写失败：{e}")
        return count

    def _source(self, video_fields: dict, master_fields: dict) -> Source:
        script = text_of(master_fields.get(self.f("masters", "script"))) or text_of(
            video_fields.get(self.f("videos", "script"))
        )
        if not script.strip():
            raise RuntimeError("没有母版脚本")
        return Source(
            script=script,
            cover=text_of(master_fields.get(self.f("masters", "cover"))),
            prompt_ref=text_of(master_fields.get(self.f("masters", "prompt_ref"))),
        )

    # ---------- 3. 自动剪辑 ----------
    def edit_pending(self) -> int:
        videos_t = self._table("videos")
        stores = self.load_stores()
        count = 0
        for rec in self.fs.list_records(videos_t):
            fields = rec.get("fields", {})
            status = text_of(fields.get(self.f("videos", "status")))
            generated = attachments(fields.get(self.f("videos", "generated")))
            finished = attachments(fields.get(self.f("videos", "final")))
            if not generated or finished or status in (STATUS["failed"], STATUS["editing"], STATUS["done"]):
                continue
            rid = rec["record_id"]
            try:
                self.fs.update_record(videos_t, rid, {self.f("videos", "status"): STATUS["editing"]})
                sid = next(iter(link_ids(fields.get(self.f("videos", "store")))), "")
                store = stores[sid][0] if sid in stores else Store(name="")
                script = text_of(fields.get(self.f("videos", "rewritten"))) or text_of(
                    fields.get(self.f("videos", "script"))
                )
                work = self.cfg.work_dir / rid
                src = self.fs.download(generated[-1], work / "input.mp4")
                stamp = dt.date.today().strftime("%m%d")
                out_video = work / f"{store.name or '成片'}-{stamp}.mp4"
                out_cover = work / f"{store.name or '封面'}-{stamp}-封面.jpg"
                cover_title = text_of(fields.get(self.f("videos", "cover")))
                subtitle = text_of(fields.get(self.f("videos", "subtitle"))) or _strip_city(cover_title, store.city)
                highlights = _split_words(text_of(fields.get(self.f("videos", "highlights"))))
                music = editor.pick_music(
                    self.cfg.render["music_dir"], rid, text_of(fields.get(self.f("videos", "bgm")))
                )
                editor.render(
                    src, out_video, out_cover, script, store.title() if store.name else "", subtitle,
                    cover_title, highlights, self.cfg.render, music,
                )  # fmt: skip
                video_token = self.fs.upload(out_video, "bitable_file")
                cover_token = self.fs.upload(out_cover, "bitable_image")
                self.fs.update_record(
                    videos_t,
                    rid,
                    {
                        self.f("videos", "final"): [{"file_token": video_token}],
                        self.f("videos", "cover_image"): [{"file_token": cover_token}],
                        self.f("videos", "status"): STATUS["done"],
                        self.f("videos", "note"): "",
                    },
                )
                count += 1
                log.info("已出成片：%s", store.name)
            except Exception as e:
                log.error("剪辑失败 %s：%s", rid, traceback.format_exc())
                self._fail(videos_t, rid, f"剪辑失败：{e}")
        return count

    def _fail(self, table: str, rid: str, msg: str) -> None:
        try:
            self.fs.update_record(
                table, rid, {self.f("videos", "status"): STATUS["failed"], self.f("videos", "note"): msg[:500]}
            )
        except Exception:
            log.exception("回写失败状态也失败了 %s", rid)

    def run_once(self) -> dict:
        return {"分发": self.distribute(), "改写": self.rewrite_pending(), "剪辑": self.edit_pending()}


def _opening(script: str) -> str:
    first = script.splitlines()[0] if script else ""
    first = first.split("：", 1)[-1]
    for p in "！!。，,？?":
        first = first.split(p)[0]
    return first[:20]


def _split_words(text: str) -> list[str]:
    import re

    return [w for w in re.split(r"[，,、;；\s]+", text) if w]


def _strip_city(cover: str, city: str) -> str:
    return cover.replace(city, "", 1).strip() if city else cover
