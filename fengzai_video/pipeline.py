"""每日流程：

1. 母版：总部上传对标视频 → 自动识别文案、封面、镜头 → 分发到每家门店的页面
2. 改写：按门店生成定向文案、封面、即梦提示词、发布文案，并汇总到「今日任务」页
3. （人工）同事在「今日任务」页逐行复制提示词到即梦，生成后把视频拖回同一行
4. 剪辑：自动加标题、字幕、音乐、封面 → 回写门店页 → 推送到门店企业微信群
5. 混剪（额外一条）：每个城市每天一条，画面用「素材库」里本城市真人主播 + 通用素材，
   声音用当天即梦视频里已经配好的 AI 口播，记录在「混剪」页

每家门店是多维表格里单独的一页，视频都记录在自己那一页里，方便长期翻看。
"""
from __future__ import annotations

import datetime as dt
import logging
import random
import re
import traceback
from dataclasses import dataclass
from pathlib import Path

from . import analyze, editor, mixer, wecom
from .config import (
    HQ_TABLES, LIBRARY_TABLE, MASTERS_TABLE, MIX_TABLE, SHARED_CITY, STATUS, STORES_TABLE, TASKS_TABLE, Config,
)  # fmt: skip
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


def _is_today(value) -> bool:
    try:
        return dt.date.fromtimestamp(float(value) / 1000) == dt.date.today()
    except (TypeError, ValueError, OSError):
        return False


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

    # ---------- 4. 混剪：每个城市每天额外一条 ----------
    def mix_pending(self) -> int:
        tables, pages = self.load()
        mix_t = tables[MIX_TABLE]
        X = lambda k: self.f("mixes", k)  # noqa: E731
        today_rows: dict[str, tuple[str, dict]] = {}
        for rec in self.fs.list_records(mix_t):
            fields = rec.get("fields", {})
            if _is_today(fields.get(X("date"))):
                today_rows[text_of(fields.get(X("title"))).strip()] = (rec["record_id"], fields)

        cities: dict[str, list[StorePage]] = {}
        for p in pages:
            if p.store.city:
                cities.setdefault(p.store.city, []).append(p)

        library = None
        count = 0
        for city, city_pages in cities.items():
            existing = today_rows.get(city)
            if existing and text_of(existing[1].get(X("status"))) != STATUS["to_mix"]:
                continue  # 今天已经做过（或正在做 / 失败了等人工处理）
            source = self._mix_source(city_pages)
            if not source:
                continue  # 这个城市今天的即梦视频还没剪好，下一轮再看
            p, src_rid, src_fields = source
            row = {
                X("title"): city,
                X("date"): _today_ms(),
                X("store"): p.store.name,
                X("ref"): f"{p.table_id}/{src_rid}",
                X("status"): STATUS["editing"],
                X("note"): "",
            }
            if existing:
                rid = existing[0]
                self._set(mix_t, rid, row)
            else:
                rid = self.fs.batch_create(mix_t, [row])[0]["record_id"]
            if library is None:
                library = self.fs.list_records(tables[LIBRARY_TABLE])
            if self._mix_one(tables[LIBRARY_TABLE], library, mix_t, rid, p, src_rid, src_fields):
                count += 1
        return count

    def _mix_source(self, city_pages: list[StorePage]) -> tuple[StorePage, str, dict] | None:
        """这个城市今天已剪好的门店视频；一个城市有几家门店时按日期轮换。"""
        V = lambda k: self.f("videos", k)  # noqa: E731
        shift = dt.date.today().toordinal() % len(city_pages)
        for p in city_pages[shift:] + city_pages[:shift]:
            for rec in self.fs.list_records(p.table_id):
                fields = rec.get("fields", {})
                if (
                    text_of(fields.get(V("status"))) == STATUS["done"]
                    and _is_today(fields.get(V("date")))
                    and attachments(fields.get(V("generated")))
                ):
                    return p, rec["record_id"], fields
        return None

    def _library_clips(self, library: list[dict], city: str) -> tuple[list, list]:
        """返回 (本城市主播出镜素材, 可用的其他素材)，每项是 (记录 ID, 附件, 使用次数)。"""
        L = lambda k: self.f("library", k)  # noqa: E731
        anchors, brolls = [], []
        for rec in library:
            fields = rec.get("fields", {})
            if fields.get(L("disabled")):
                continue
            tag = text_of(fields.get(L("city"))).strip()
            has_anchor = bool(text_of(fields.get(L("anchor"))).strip())
            try:
                used = int(float(text_of(fields.get(L("used"))) or 0))
            except ValueError:
                used = 0
            for att in attachments(fields.get(L("file"))):
                item = (rec["record_id"], att, used)
                if has_anchor and tag == city:
                    anchors.append(item)
                elif not has_anchor and tag in (city, *SHARED_CITY):
                    brolls.append(item)
                # 其他城市的素材、没写城市的主播素材都不用
        return anchors, brolls

    def _mix_one(
        self, lib_t: str, library: list[dict], mix_t: str, rid: str, p: StorePage, src_rid: str, src: dict
    ) -> bool:
        V = lambda k: self.f("videos", k)  # noqa: E731
        X = lambda k: self.f("mixes", k)  # noqa: E731
        store, city = p.store, p.store.city
        stamp = dt.date.today().strftime("%m%d")
        work = self.cfg.work_dir / f"mix-{rid}"
        try:
            anchors, brolls = self._library_clips(library, city)
            if not anchors:
                raise RuntimeError(
                    f"素材库里没有「{city}」的主播出镜素材：请上传，并把「城市」填 {city}、「主播」填主播名字"
                )
            voice = self.fs.download(attachments(src.get(V("generated")))[-1], work / "voice.mp4")
            duration = editor.probe(voice).duration
            seg = float(self.cfg.mix["segment_seconds"])
            need = int(duration / seg) + 2
            rng = random.Random(f"{city}{stamp}")

            def pick(items):  # 用得少的优先，同样次数的每天随机换顺序
                rng.shuffle(items)
                return sorted(items, key=lambda x: x[2])[:need]

            cache = self.cfg.work_dir / "library"
            local = []
            for anchor, items in ((True, pick(anchors)), (False, pick(brolls))):
                for key, att, _ in items:
                    path = cache / f"{att['file_token']}{Path(att.get('name') or '').suffix or '.mp4'}"
                    if not path.exists():
                        self.fs.download(att, path)
                    local.append((key, path, anchor))
            clips = mixer.load_clips(local)
            segments = mixer.plan(
                duration,
                [c for c in clips if c.anchor],
                [c for c in clips if not c.anchor],
                seed=f"{city}{stamp}",
                segment_seconds=seg,
                opening_seconds=float(self.cfg.mix["opening_seconds"]),
            )
            width = int(self.cfg.render["output_width"])
            montage = mixer.montage(segments, voice, work / "montage.mp4", width, int(round(width * 16 / 9 / 2) * 2))

            out_video = work / f"{city}混剪-{stamp}.mp4"
            out_cover = work / f"{city}混剪-{stamp}-封面.jpg"
            cover_title = text_of(src.get(V("cover")))
            subtitle = text_of(src.get(V("subtitle"))) or _strip_city(cover_title, city)
            music = editor.pick_music(self.cfg.render["music_dir"], f"mix-{rid}", text_of(src.get(V("bgm"))))
            editor.render(
                montage, out_video, out_cover, text_of(src.get(V("rewritten"))) or text_of(src.get(V("script"))),
                store.title(), subtitle, cover_title, _split_words(text_of(src.get(V("highlights")))),
                self.cfg.render, music,
            )  # fmt: skip
            video_token = self.fs.upload(out_video, "bitable_file")
            cover_token = self.fs.upload(out_cover, "bitable_image")
            n_anchor = sum(1 for s in segments if s.clip.anchor)
            summary = f"主播出镜 {n_anchor} 段（占 {mixer.anchor_share(segments):.0%}），其他素材 {len(segments) - n_anchor} 段"
            self._set(
                mix_t,
                rid,
                {
                    X("final"): [{"file_token": video_token}],
                    X("cover_image"): [{"file_token": cover_token}],
                    X("publish"): text_of(src.get(V("publish"))),
                    X("clips"): summary,
                    X("status"): STATUS["done"],
                },
            )
            self._count_usage(lib_t, library, segments)
            log.info("已出混剪：%s（%s）", city, summary)
        except Exception as e:
            log.error("混剪失败 %s：%s", city, traceback.format_exc())
            self._set(mix_t, rid, {X("status"): STATUS["failed"], X("note"): f"混剪失败：{e}"[:500]})
            return False

        if store.webhook:
            try:
                upload = editor.shrink_for_upload(out_video, float(self.cfg.render["wecom_max_mb"]))
                wecom.push_video(store.webhook, f"{store.name}·混剪", upload, out_cover, text_of(src.get(V("publish"))))
                self._set(mix_t, rid, {X("pushed"): True})
            except Exception as e:
                log.exception("混剪推送失败 %s", store.name)
                self._set(mix_t, rid, {X("note"): f"推送企微群失败：{e}"[:500]})
        return True

    def _count_usage(self, lib_t: str, library: list[dict], segments: list) -> None:
        L = lambda k: self.f("library", k)  # noqa: E731
        used: dict[str, int] = {}
        for s in segments:
            used[s.clip.key] = used.get(s.clip.key, 0) + 1
        for rec in library:
            key = rec["record_id"]
            if key not in used:
                continue
            fields = rec.setdefault("fields", {})
            try:
                total = int(float(text_of(fields.get(L("used"))) or 0)) + used[key]
            except ValueError:
                total = used[key]
            fields[L("used")] = total  # 同一轮里后面的城市也能看到最新次数
            try:
                self._set(lib_t, key, {L("used"): total, L("last_used"): _today_ms()})
            except Exception:
                log.exception("更新素材使用次数失败 %s", key)

    def _set(self, table_id: str, rid: str, fields: dict) -> None:
        self.fs.update_record(table_id, rid, fields)

    def _fail(self, table_id: str, rid: str, msg: str) -> None:
        try:
            self._set(table_id, rid, {self.f("videos", "status"): STATUS["failed"], self.f("videos", "note"): msg[:500]})
        except Exception:
            log.exception("回写失败状态也失败了 %s", rid)

    def run_once(self) -> dict:
        result = {"分发": self.distribute(), "改写": self.rewrite_pending(), "剪辑": self.edit_pending()}
        if self.cfg.mix.get("enabled", True):
            result["混剪"] = self.mix_pending()
        return result


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
