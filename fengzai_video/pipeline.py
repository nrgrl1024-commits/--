"""每日流程：

1. 母版：总部上传对标视频 → 自动识别文案、封面、镜头 → 分发到每家门店的页面
2. 改写：按门店生成定向文案、封面、即梦提示词、发布文案，并汇总到「今日任务」页
3. （人工）同事在「今日任务」页逐行复制提示词到即梦，生成后把视频拖回同一行
4. 剪辑：自动加标题、字幕、音乐、封面 → 回写门店页 → 推送到门店企业微信群

每家门店是多维表格里单独的一页，视频都记录在自己那一页里，方便长期翻看。
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import traceback
from dataclasses import dataclass

from . import analyze, editor, wecom
from .config import HQ_TABLES, MASTERS_TABLE, STATUS, STORES_TABLE, TASKS_TABLE, Config
from .feishu import Feishu, attachments, link_ids, text_of
from .rewrite import Source, Store, rewrite
from .setup_tables import ensure_base, ensure_store_page, store_page_name

log = logging.getLogger("fengzai")


@dataclass
class StorePage:
    store: Store
    table_id: str
    page: str


def _today_ms() -> int:
    return int(dt.datetime.combine(dt.date.today(), dt.time()).timestamp() * 1000)


class Pipeline:
    def __init__(self, cfg: Config, feishu: Feishu | None = None):
        self.cfg = cfg
        self.fs = feishu or Feishu(cfg.feishu.get("app_id"), cfg.feishu.get("app_secret"), cfg.feishu.get("app_token"))

    def f(self, table: str, key: str) -> str:
        return self.cfg.field(table, key)

    # ---------- 读取页面和门店；新门店自动建页 ----------
    def load(self) -> tuple[dict[str, str], list[StorePage]]:
        tables = ensure_base(self.cfg, self.fs)
        pages = []
        for rec in self.fs.list_records(tables[STORES_TABLE]):
            fields = rec.get("fields", {})
            g = lambda k: text_of(fields.get(self.f("stores", k))).strip()  # noqa: E731
            enabled = fields.get(self.f("stores", "enabled"))
            page = store_page_name(self.cfg, fields)
            if not page or page in HQ_TABLES or (enabled is not None and not enabled):
                continue
            if page not in tables:
                ensure_store_page(self.cfg, self.fs, tables, page)
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
                anchor=g("anchor"),
                webhook=g("webhook"),
            )
            pages.append(StorePage(store, tables[page], page))
        return tables, pages

    # ---------- 1. 母版：识别对标视频 + 分发 ----------
    def distribute(self) -> int:
        tables, pages = self.load()
        masters_t = tables[MASTERS_TABLE]
        M = lambda k: self.f("masters", k)  # noqa: E731
        created = 0
        for rec in self.fs.list_records(masters_t):
            fields, mid = rec.get("fields", {}), rec["record_id"]
            if text_of(fields.get(M("status"))) != STATUS["pending_dist"]:
                continue
            topic = text_of(fields.get(M("title"))) or "母版"
            try:
                fields = self._analyze_master(masters_t, mid, fields)
                script = text_of(fields.get(M("script")))
                if not script.strip():
                    raise RuntimeError("没有脚本内容：请上传对标视频，或手动填写「脚本内容」")
                chosen = _split_words(text_of(fields.get(M("stores"))))
                targets = [p for p in pages if not chosen or p.store.name in chosen or p.page in chosen]
                for p in targets:
                    self.fs.batch_create(
                        p.table_id,
                        [
                            {
                                self.f("videos", "title"): f"{dt.date.today():%m-%d} {topic}",
                                self.f("videos", "master"): [mid],
                                self.f("videos", "date"): _today_ms(),
                                self.f("videos", "script"): script,
                                self.f("videos", "status"): STATUS["to_rewrite"],
                            }
                        ],
                    )
                self.fs.update_record(masters_t, mid, {M("status"): STATUS["distributed"], M("note"): ""})
                log.info("母版「%s」已分发到 %d 家门店", topic, len(targets))
                created += len(targets)
            except Exception as e:
                log.exception("母版「%s」处理失败", topic)
                self.fs.update_record(masters_t, mid, {M("status"): STATUS["failed"], M("note"): str(e)[:500]})
        return created

    def _analyze_master(self, table_id: str, mid: str, fields: dict) -> dict:
        """有对标视频、且文案或镜头分析还空着时，自动识别补上（人工已填的内容不覆盖）。"""
        M = lambda k: self.f("masters", k)  # noqa: E731
        refs = attachments(fields.get(M("reference")))
        if not refs or (text_of(fields.get(M("script"))) and text_of(fields.get(M("prompt_ref")))):
            return fields
        self.fs.update_record(table_id, mid, {M("status"): STATUS["analyzing"]})
        work = self.cfg.work_dir / f"master-{mid}"
        video = self.fs.download(refs[-1], work / "reference.mp4")
        result = analyze.analyze(self.cfg.llm, video, work)
        updates = {}
        if not text_of(fields.get(M("script"))):
            updates[M("script")] = result.script
        if not text_of(fields.get(M("cover"))) and result.cover:
            updates[M("cover")] = result.cover
        if not text_of(fields.get(M("prompt_ref"))):
            updates[M("prompt_ref")] = result.shots + (f"\n\n爆点：{result.hook}" if result.hook else "")
        if updates:
            self.fs.update_record(table_id, mid, updates)
        log.info("已识别对标视频：%s", result.cover or mid)
        return {**fields, **updates}

    # ---------- 2. AI 改写，并汇总到「今日任务」 ----------
    def rewrite_pending(self) -> int:
        tables, pages = self.load()
        masters = {r["record_id"]: r.get("fields", {}) for r in self.fs.list_records(tables[MASTERS_TABLE])}
        tasks_t = tables[TASKS_TABLE]
        tasks_by_ref = {
            text_of(r.get("fields", {}).get(self.f("tasks", "ref"))): r["record_id"] for r in self.fs.list_records(tasks_t)
        }
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
                        self.f("videos", "publish"): result.publish_text,
                        self.f("videos", "status"): STATUS["to_generate"],
                        self.f("videos", "note"): "",
                    },
                )
                ref = f"{p.table_id}/{rid}"
                task = {
                    self.f("tasks", "title"): p.store.name,
                    self.f("tasks", "anchor"): p.store.anchor,
                    self.f("tasks", "prompt"): result.jimeng_prompt,
                    self.f("tasks", "status"): STATUS["to_generate"],
                    self.f("tasks", "date"): _today_ms(),
                    self.f("tasks", "ref"): ref,
                }
                if ref in tasks_by_ref:  # 重新改写时更新原来那行，不重复加
                    self.fs.update_record(tasks_t, tasks_by_ref[ref], task)
                else:
                    self.fs.batch_create(tasks_t, [task])
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

    # ---------- 3. 自动剪辑 + 推送 ----------
    def edit_pending(self) -> int:
        tables, pages = self.load()
        by_table = {p.table_id: p for p in pages}
        tasks_t = tables[TASKS_TABLE]
        store_rows: dict[str, dict[str, dict]] = {}

        def rows_of(p: StorePage) -> dict[str, dict]:
            if p.table_id not in store_rows:
                store_rows[p.table_id] = {r["record_id"]: r.get("fields", {}) for r in self.fs.list_records(p.table_id)}
            return store_rows[p.table_id]

        count = 0
        # 「今日任务」页上传的视频
        for task in self.fs.list_records(tasks_t):
            tf = task.get("fields", {})
            if text_of(tf.get(self.f("tasks", "status"))) != STATUS["to_generate"]:
                continue
            generated = attachments(tf.get(self.f("tasks", "generated")))
            if not generated:
                continue
            table_id, _, rid = text_of(tf.get(self.f("tasks", "ref"))).partition("/")
            p = by_table.get(table_id)
            if not p or rid not in rows_of(p):
                self._set(tasks_t, task["record_id"], {self.f("tasks", "status"): STATUS["failed"]})
                continue
            if self._edit_one(p, rid, rows_of(p)[rid], generated[-1], (tasks_t, task["record_id"])):
                count += 1

        # 门店自己在门店页上传的视频
        for p in pages:
            for rid, fields in rows_of(p).items():
                if text_of(fields.get(self.f("videos", "status"))) != STATUS["to_generate"]:
                    continue
                generated = attachments(fields.get(self.f("videos", "generated")))
                if generated and not attachments(fields.get(self.f("videos", "final"))):
                    if self._edit_one(p, rid, fields, generated[-1], None):
                        count += 1
        return count

    def _edit_one(self, p: StorePage, rid: str, fields: dict, video: dict, task: tuple[str, str] | None) -> bool:
        V = lambda k: self.f("videos", k)  # noqa: E731
        store = p.store
        try:
            self._set(p.table_id, rid, {V("status"): STATUS["editing"]})
            if task:
                self._set(task[0], task[1], {self.f("tasks", "status"): STATUS["editing"]})
            script = text_of(fields.get(V("rewritten"))) or text_of(fields.get(V("script")))
            work = self.cfg.work_dir / rid
            src = self.fs.download(video, work / "input.mp4")
            stamp = dt.date.today().strftime("%m%d")
            out_video = work / f"{store.name}-{stamp}.mp4"
            out_cover = work / f"{store.name}-{stamp}-封面.jpg"
            cover_title = text_of(fields.get(V("cover")))
            subtitle = text_of(fields.get(V("subtitle"))) or _strip_city(cover_title, store.city)
            highlights = _split_words(text_of(fields.get(V("highlights"))))
            music = editor.pick_music(self.cfg.render["music_dir"], rid, text_of(fields.get(V("bgm"))))
            editor.render(
                src, out_video, out_cover, script, store.title(), subtitle,
                cover_title, highlights, self.cfg.render, music,
            )  # fmt: skip
            video_token = self.fs.upload(out_video, "bitable_file")
            cover_token = self.fs.upload(out_cover, "bitable_image")
            self._set(
                p.table_id,
                rid,
                {
                    V("final"): [{"file_token": video_token}],
                    V("cover_image"): [{"file_token": cover_token}],
                    V("status"): STATUS["done"],
                    V("note"): "",
                },
            )
            if task:
                self._set(
                    task[0],
                    task[1],
                    {self.f("tasks", "final"): [{"file_token": video_token}], self.f("tasks", "status"): STATUS["done"]},
                )
            log.info("已出成片：%s", store.name)
        except Exception as e:
            log.error("剪辑失败 %s/%s：%s", p.page, rid, traceback.format_exc())
            self._fail(p.table_id, rid, f"剪辑失败：{e}")
            if task:
                self._set(task[0], task[1], {self.f("tasks", "status"): STATUS["failed"]})
            return False

        if store.webhook and not fields.get(V("pushed")):
            try:
                upload = editor.shrink_for_upload(out_video, float(self.cfg.render["wecom_max_mb"]))
                wecom.push_video(store.webhook, store.name, upload, out_cover, text_of(fields.get(V("publish"))))
                self._set(p.table_id, rid, {V("pushed"): True})
                log.info("已推送到门店群：%s", store.name)
            except Exception as e:  # 推送失败不影响成片，在备注里提示
                log.exception("推送失败 %s", store.name)
                self._set(p.table_id, rid, {V("note"): f"推送企微群失败：{e}"[:500]})
        return True

    def _set(self, table_id: str, rid: str, fields: dict) -> None:
        self.fs.update_record(table_id, rid, fields)

    def _fail(self, table_id: str, rid: str, msg: str) -> None:
        try:
            self._set(table_id, rid, {self.f("videos", "status"): STATUS["failed"], self.f("videos", "note"): msg[:500]})
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
