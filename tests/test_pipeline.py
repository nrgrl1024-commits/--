"""用一个假的飞书，验证完整流程：识别对标视频 → 分发 → 改写 + 今日任务 → 剪辑 → 推送企微群。"""
import itertools
import json

import pytest

from fengzai_video import analyze, editor, llm, setup_tables, wecom
from fengzai_video.analyze import Analysis
from fengzai_video.config import DEFAULT_FIELDS, DEFAULT_RENDER, Config
from fengzai_video.pipeline import Pipeline


class FakeFeishu:
    def __init__(self, pages):
        self._ids = itertools.count()
        self.tables, self.rows, self.fields = {}, {}, {}
        for name, (fields, rows) in pages.items():
            tid = self.create_table(name, [{"field_name": f} for f in fields])
            for r in rows:
                self.batch_create(tid, [r])

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
        return f"tok-{path.name}"

    def page(self, name):
        return list(self.rows[self.tables[name]].values())


OLD_PAGE_FIELDS = ["文本", "对标脚本内容", "改写脚本内容", "封面标题", "对标视频", "即梦AI提示词", "生成视频"]
OLD_ROW = {"文本": "珠海", "对标脚本内容": "旧脚本", "生成视频": [{"file_token": "old"}]}  # 以前手工做的


def make_cfg(tmp_path):
    return Config(feishu={}, llm={}, render=dict(DEFAULT_RENDER), fields=DEFAULT_FIELDS, work_dir=tmp_path)


def fake_rewrite_reply(cfg, system, user, **k):
    city = "佛山" if "城市/区域：佛山" in system else "珠海"
    return json.dumps({
        "segments": [{"start": 0, "end": 4, "role": "女业主", "text": f"{city}的街坊们看过来！"}],
        "cover_title": f"{city}厨房翻新 全包", "subtitle": "厨房翻新 全包", "highlights": [city],
        "publish_text": f"{city}厨房翻新 #{city}装修", "prompt_main": "实拍", "prompt_negative": "无", "prompt_spec": "无",
    }, ensure_ascii=False)  # fmt: skip


@pytest.fixture
def base(tmp_path):
    fs = FakeFeishu({"珠海香洲店": (OLD_PAGE_FIELDS, [OLD_ROW]), "佛山禅城店": (OLD_PAGE_FIELDS, [])})
    cfg = make_cfg(tmp_path)
    setup_tables.setup(cfg, fs)
    for row in fs.rows[fs.tables["门店资料"]].values():
        row["城市"] = row["门店名称"][:2]
        row["即梦主播"] = f"{row['城市']}阿乐师傅"
    return fs, cfg


def test_setup_keeps_existing_pages_and_adds_fields(base):
    fs, cfg = base
    assert {"门店资料", "母版", "今日任务"} <= set(fs.tables)
    assert {"状态", "成片", "发布文案", "已推送群"} <= fs.fields[fs.tables["珠海香洲店"]]
    assert fs.page("珠海香洲店") == [OLD_ROW]  # 旧数据原样保留
    assert setup_tables.setup(cfg, fs)["珠海香洲店"] == []  # 反复运行不出问题


def test_new_store_row_creates_page_automatically(base):
    fs, cfg = base
    fs.batch_create(fs.tables["门店资料"], [{"门店名称": "中山石岐店", "城市": "中山", "启用": True}])
    _, pages = Pipeline(cfg, fs).load()
    assert "中山石岐店" in fs.tables and "中山石岐店" in [p.page for p in pages]


def test_disabled_store_skipped(base):
    fs, cfg = base
    for row in fs.rows[fs.tables["门店资料"]].values():
        row["启用"] = row["门店名称"] != "佛山禅城店"
    _, pages = Pipeline(cfg, fs).load()
    assert [p.page for p in pages] == ["珠海香洲店"]


def test_full_flow(base, monkeypatch):
    fs, cfg = base
    for row in fs.rows[fs.tables["门店资料"]].values():
        if row["门店名称"] == "佛山禅城店":
            row["企微群机器人"] = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"

    # 1. 总部只上传对标视频
    fs.batch_create(fs.tables["母版"], [{"选题": "厨房翻新", "对标视频": [{"file_token": "ref"}], "状态": "待分发"}])
    monkeypatch.setattr(
        analyze, "analyze", lambda *a: Analysis("女业主：珠海的街坊们别再踩坑啦！", "珠海厨房翻新", "0-4秒：厨房", "痛点开头")
    )
    pipe = Pipeline(cfg, fs)
    assert pipe.distribute() == 2
    master = fs.page("母版")[0]
    assert master["状态"] == "已分发" and master["脚本内容"].startswith("女业主") and "爆点" in master["镜头分析"]

    # 2. 改写 + 汇总到今日任务
    monkeypatch.setattr(llm, "chat", fake_rewrite_reply)
    assert pipe.rewrite_pending() == 2
    foshan = fs.page("佛山禅城店")[0]
    assert foshan["改写脚本内容"] == "女业主：佛山的街坊们看过来！" and foshan["发布文案"].startswith("佛山")
    tasks = fs.page("今日任务")
    assert [t["门店"] for t in tasks] == ["珠海香洲店", "佛山禅城店"]
    assert tasks[1]["即梦主播"] == "佛山阿乐师傅" and tasks[1]["状态"] == "待生成"

    # 重新改写不会重复加任务
    foshan["状态"] = "待改写"
    pipe.rewrite_pending()
    assert len(fs.page("今日任务")) == 2

    # 3. 同事在今日任务页上传即梦视频 → 剪辑 → 推送
    rendered, pushed = [], []
    monkeypatch.setattr(editor, "render", lambda *a, **k: rendered.append(a[4]) or {})
    monkeypatch.setattr(editor, "shrink_for_upload", lambda v, mb: v)
    monkeypatch.setattr(wecom, "push_video", lambda hook, store, v, c, text: pushed.append((store, text)))
    tasks[1]["生成视频"] = [{"file_token": "jimeng"}]
    assert pipe.edit_pending() == 1
    assert rendered == ["佛山蜂仔翻新团队"]
    assert foshan["状态"] == "已完成" and foshan["成片"] and foshan["已推送群"] is True
    assert tasks[1]["状态"] == "已完成" and tasks[1]["成片"]
    assert pushed == [("佛山禅城店", "佛山厨房翻新 #佛山装修")]
    assert fs.page("珠海香洲店")[0] == OLD_ROW  # 旧行没有状态，不会被动


def test_master_without_video_or_script_fails_clearly(base):
    fs, cfg = base
    fs.batch_create(fs.tables["母版"], [{"选题": "空母版", "状态": "待分发"}])
    assert Pipeline(cfg, fs).distribute() == 0
    master = fs.page("母版")[0]
    assert master["状态"] == "失败" and "脚本内容" in master["备注"]


def test_push_failure_keeps_video(base, monkeypatch):
    fs, cfg = base
    for row in fs.rows[fs.tables["门店资料"]].values():
        row["企微群机器人"] = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    tid = fs.tables["珠海香洲店"]
    fs.batch_create(tid, [{"状态": "待生成", "改写脚本内容": "女业主：你好", "生成视频": [{"file_token": "v"}]}])
    monkeypatch.setattr(editor, "render", lambda *a, **k: {})
    monkeypatch.setattr(editor, "shrink_for_upload", lambda v, mb: v)

    def boom(*a):
        raise wecom.WecomError("网络错误")

    monkeypatch.setattr(wecom, "push_video", boom)
    assert Pipeline(cfg, fs).edit_pending() == 1
    row = fs.page("珠海香洲店")[-1]
    assert row["状态"] == "已完成" and "推送企微群失败" in row["备注"] and not row.get("已推送群")



def test_prefill_from_numbered_page_names(tmp_path):
    fs = FakeFeishu({"1安徽合肥大铺头(男)": (OLD_PAGE_FIELDS, []), "4广东东莞南城（女）": (OLD_PAGE_FIELDS, [])})
    cfg = make_cfg(tmp_path)
    report = setup_tables.setup(cfg, fs)
    stores = {r["数据表名称"]: r for r in fs.page("门店资料")}
    assert stores["1安徽合肥大铺头(男)"]["门店名称"] == "安徽合肥大铺头"
    assert stores["1安徽合肥大铺头(男)"]["城市"] == "合肥" and stores["1安徽合肥大铺头(男)"]["区域"] == "大铺头"
    assert stores["4广东东莞南城（女）"]["出镜角色"] == "门店主播为女性"
    assert "对标脚本内容" not in report["1安徽合肥大铺头(男)"]  # 用你们原有的字段，不重复建
    _, pages = Pipeline(cfg, fs).load()
    assert {p.store.name for p in pages} == {"安徽合肥大铺头", "广东东莞南城"}
    assert all(p.store.title().endswith("蜂仔翻新团队") for p in pages)
