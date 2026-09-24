"""命令行入口：python -m fengzai_video <命令>"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from . import editor
from .config import load_config


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fengzai_video", description="蜂仔翻新 · 门店短视频流水线")
    p.add_argument("--config", help="配置文件路径，默认 ./config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("setup", help="建「门店资料」「母版」两页，并给各门店页补齐字段（可反复运行）")
    ad = sub.add_parser("add-store", help="新开一家门店：新建它的页面并加入门店资料")
    ad.add_argument("name", help="门店名称，同时作为页面名")
    ad.add_argument("--city", default="")
    sub.add_parser("distribute", help="把状态为「待分发」的母版分发给各门店")
    sub.add_parser("rewrite", help="AI 改写所有「待改写」的行")
    sub.add_parser("edit", help="给已上传即梦视频的行自动剪辑并回写成片")
    sub.add_parser("run", help="依次执行 distribute → rewrite → edit 一次")
    w = sub.add_parser("watch", help="常驻运行，每隔一段时间执行一次 run")
    w.add_argument("--interval", type=int, default=120, help="间隔秒数，默认 120")

    lr = sub.add_parser("local-rewrite", help="不连飞书，本地测试 AI 改写")
    lr.add_argument("--script", required=True, help="母版脚本文本或 .txt 文件")
    lr.add_argument("--cover", default="")
    lr.add_argument("--city", required=True)
    lr.add_argument("--store", default="")
    lr.add_argument("--dialect", default="")
    lr.add_argument("--term", default="", help="对当地人的称呼，如 街坊")

    le = sub.add_parser("local-edit", help="不连飞书，本地测试自动剪辑")
    le.add_argument("--video", required=True)
    le.add_argument("--script", required=True, help="改写后的脚本文本或 .txt 文件")
    le.add_argument("--title", required=True, help="顶部大标题，如 珠海蜂仔翻新团队")
    le.add_argument("--subtitle", default="", help="顶部第二行")
    le.add_argument("--cover", default="", help="封面标题")
    le.add_argument("--highlights", default="", help="高亮词，逗号分隔")
    le.add_argument("--music", default="", help="背景音乐文件；不填则从音乐库自动选")
    le.add_argument("--out", default="out/成片.mp4")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)

    if args.cmd == "local-rewrite":
        from .rewrite import Source, Store, rewrite

        store = Store(name=args.store or args.city, city=args.city, dialect=args.dialect, address_term=args.term)
        result = rewrite(cfg.llm, store, Source(script=_read(args.script), cover=args.cover))
        print(result.script_text(), "\n")
        print("封面标题：", result.cover_title)
        print("副标题：", result.subtitle)
        print("高亮词：", "，".join(result.highlights), "\n")
        print(result.jimeng_prompt)
        return 0

    if args.cmd == "local-edit":
        out = Path(args.out)
        music = Path(args.music) if args.music else editor.pick_music(cfg.render["music_dir"], args.video)
        info = editor.render(
            Path(args.video), out, out.with_name(out.stem + "-封面.jpg"), _read(args.script), args.title,
            args.subtitle, args.cover, [w for w in args.highlights.replace("，", ",").split(",") if w],
            cfg.render, music,
        )  # fmt: skip
        print(json.dumps(info, ensure_ascii=False))
        return 0

    from .feishu import Feishu
    from .pipeline import Pipeline

    if args.cmd in ("setup", "add-store"):
        from . import setup_tables

        fs = Feishu(cfg.feishu.get("app_id"), cfg.feishu.get("app_secret"), cfg.feishu.get("app_token"))
        if args.cmd == "add-store":
            setup_tables.add_store(cfg, fs, args.name, args.city)
            print(f"已新建门店页「{args.name}」，并加入门店资料")
            return 0
        report = setup_tables.setup(cfg, fs)
        print(f"检查了 {len(report)} 家门店的页面：")
        for page, added in report.items():
            print(f"  {page}：" + (f"新增字段 {'、'.join(added)}" if added else "字段齐全"))
        return 0

    pipe = Pipeline(cfg)
    if args.cmd == "distribute":
        print("分发", pipe.distribute(), "条")
    elif args.cmd == "rewrite":
        print("改写", pipe.rewrite_pending(), "条")
    elif args.cmd == "edit":
        print("剪辑", pipe.edit_pending(), "条")
    elif args.cmd == "run":
        print(pipe.run_once())
    elif args.cmd == "watch":
        while True:
            try:
                logging.info("本轮结果：%s", pipe.run_once())
            except Exception:
                logging.exception("本轮出错，稍后重试")
            time.sleep(args.interval)
    return 0


def _read(value: str) -> str:
    path = Path(value)
    if path.suffix == ".txt" and path.exists():
        return path.read_text(encoding="utf-8")
    return value.replace("\\n", "\n")


if __name__ == "__main__":
    sys.exit(main())
