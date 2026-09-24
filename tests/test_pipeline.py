"""用一个假的飞书，验证「每家门店一页」的分发、改写、剪辑流程。"""
import itertools
import json

from fengzai_video import editor, llm, setup_tables
from fengzai_video.config import DEFAULT_FIELDS, DEFAULT_RENDER, Config
from fengzai_video.pipeline import Pipeline


class FakeFeishu:
    def __init__(self, pages):
        self._ids = itertools.count()
        self.tables = {}  # name -> table_id
        self.rows = {}  # table_id -> {record_id: fields}
        self.fields = {}  # table_id -> set
        for name, (fields, rows) in pages.items():
            tid = self.create_table(name, [{"field_name": f} for f in fields])
            for r in rows:
                self.batch_create(tid, [r])
        self.uploads = []

    def list_tables(self):
        return dict(self.tables)

    def create_table(self, name, fields):
        tid = f"tbl{next(self._ids)}"
        self.tables[name], self.rows[tid], self.fields[tid] = tid, {}, {f["field_name"] for f in fields}
        return tid

    def list_fields(self, tid):
        return set(self.fields[tid])

    def create_field(self, tid, field):
        self.fields[tid].add(field["field_name"])

    def list_records(self, tid):
        return [{"record_id": rid, "fields": dict(f)} for rid, f in self.rows[tid].items()]

    def batch_create(self, tid, rows):
        for r in rows:
            self.rows[tid][f"rec{next(self._ids)}"] = dict(r)

    def update_record(self, tid, rid, fields):
        self.rows[tid][rid].update(fields)

    def download(self, att, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"video")
        return dest

    def upload(self, path, parent_type):
        self.uploads.append(path.name)
        return f"tok-{path.name}"

    def page(self, name):
        return list(self.rows[self.tables[name]].values())


OLD_PAGE_FIELDS = ["文本", "脚本内容", "改写脚本内容", "封面标题", "对标视频", "即梦AI提示词", "生成视频"]
OLD_ROW = {"文本": "珠海", "脚本内容": "旧脚本", "生成视频": [{"file_token": "old"}]}  # 以前手工做的


def make_cfg(tmp_path):
    return Config(feishu={}, llm={}, render=dict(DEFAULT_RENDER), fields=DEFAULT_FIELDS, work_dir=tmp_path)


def test_setup_keeps_existing_pages_and_adds_fields(tmp_path):
    fs = FakeFeishu({"珠海香洲店": (OLD_PAGE_FIELDS, [OLD_ROW]), "佛山禅城店": (OLD_PAGE_FIELDS, [])})
    report = setup_tables.setup(make_cfg(tmp_path), fs)
    assert set(report) == {"珠海香洲店", "佛山禅城店"}  # 按现有页面预填了门店资料
    assert {"状态", "成片", "封面图", "高亮词"} <= fs.fields[fs.tables["珠海香洲店"]]
    assert "脚本内容" not in report["珠海香洲店"]  # 已有字段不重复建
    assert fs.page("珠海香洲店") == [OLD_ROW]  # 旧数据原样保留
    assert setup_tables.setup(make_cfg(tmp_path), fs)["珠海香洲店"] == []  # 反复运行不出问题


def test_full_flow_per_store_page(tmp_path, monkeypatch):
    fs = FakeFeishu({"珠海香洲店": (OLD_PAGE_FIELDS, [OLD_ROW]), "佛山禅城店": (OLD_PAGE_FIELDS, [])})
    cfg = make_cfg(tmp_path)
    setup_tables.setup(cfg, fs)
    for row in fs.rows[fs.tables["门店资料"]].values():
        row["城市"] = row["门店名称"][:2]
    fs.batch_create(fs.tables["母版"], [{"选题": "厨房翻新", "脚本内容": "女业主：珠海的街坊们……", "状态": "待分发"}])

    pipe = Pipeline(cfg, fs)
    assert pipe.distribute() == 2
    assert fs.page("母版")[0]["状态"] == "已分发"
    new_row = fs.page("佛山禅城店")[0]
    assert new_row["状态"] == "待改写" and new_row["母版"]

    seen_users = []

    def fake_chat(cfg, system, user, **k):
        seen_users.append(user)
        city = "佛山" if "城市/区域：佛山" in system else "珠海"
        return json.dumps({
            "segments": [{"start": 0, "end": 4, "role": "女业主", "text": f"{city}的街坊们看过来！"}],
            "cover_title": f"{city}厨房翻新 全包", "subtitle": "厨房翻新 全包",
            "highlights": [city], "prompt_main": "实拍", "prompt_negative": "无", "prompt_spec": "无",
        }, ensure_ascii=False)  # fmt: skip

    monkeypatch.setattr(llm, "chat", fake_chat)
    assert pipe.rewrite_pending() == 2
    assert "不要雷同" in seen_users[1]  # 第二家门店会拿到第一家的开头去避开
    foshan = fs.page("佛山禅城店")[0]
    assert foshan["改写脚本内容"] == "女业主：佛山的街坊们看过来！" and foshan["状态"] == "待生成"

    rendered = []
    monkeypatch.setattr(editor, "render", lambda *a, **k: rendered.append(a[4]) or {})
    foshan["生成视频"] = [{"file_token": "jimeng"}]
    assert pipe.edit_pending() == 1  # 只剪新行；珠海那条旧行没有状态，不会被动
    assert rendered == ["佛山蜂仔翻新团队"]
    assert foshan["状态"] == "已完成" and foshan["成片"][0]["file_token"].startswith("tok-")
    assert fs.page("珠海香洲店")[0] == OLD_ROW


def test_disabled_store_skipped(tmp_path):
    fs = FakeFeishu({"珠海香洲店": (OLD_PAGE_FIELDS, []), "佛山禅城店": (OLD_PAGE_FIELDS, [])})
    cfg = make_cfg(tmp_path)
    setup_tables.setup(cfg, fs)
    for row in fs.rows[fs.tables["门店资料"]].values():
        if row["门店名称"] == "佛山禅城店":
            row["启用"] = False
    _, pages = Pipeline(cfg, fs).load()
    assert [p.page for p in pages] == ["珠海香洲店"]
