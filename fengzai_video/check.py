"""检查配置：飞书、豆包、剪辑工具是否都能用。有问题时直接告诉你改哪里。"""
from __future__ import annotations

import tempfile
from pathlib import Path

import requests

from . import editor, llm
from .config import HQ_TABLES, Config
from .feishu import Feishu


def run_checks(cfg: Config) -> bool:
    ok = True

    def report(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        print(f"{'✅' if passed else '❌'} {name}" + (f"：{detail}" if detail else ""))

    # 1. 飞书
    fs_cfg = cfg.feishu
    missing = [k for k in ("app_id", "app_secret", "app_token") if not fs_cfg.get(k)]
    if missing:
        report("飞书配置", False, f".env 里缺少 {', '.join('FEISHU_' + m.upper() for m in missing)}")
    else:
        fs = Feishu(fs_cfg["app_id"], fs_cfg["app_secret"], fs_cfg["app_token"])
        try:
            fs.token()
            report("飞书 App ID / App Secret", True)
            try:
                tables = fs.list_tables()
                stores = [n for n in tables if n not in HQ_TABLES]
                hq = [n for n in HQ_TABLES if n in tables]
                report("飞书多维表格权限", True, f"能看到 {len(stores)} 个门店页，总部页：{'、'.join(hq) or '还没建（运行安装程序会自动建）'}")
            except Exception as e:
                report("飞书多维表格权限", False, f"{e}。请确认表格链接那一串填对了，并在表格「分享」里给应用「可管理」权限")
        except requests.RequestException as e:
            report("飞书 App ID / App Secret", False, f"连不上飞书，请检查这台电脑的网络：{str(e)[:120]}")
        except Exception as e:
            report("飞书 App ID / App Secret", False, f"{e}。请核对 .env 里的 FEISHU_APP_ID 和 FEISHU_APP_SECRET")

    # 2. 豆包：文字 + 看图
    try:
        reply = llm.chat(cfg.llm, "你是助手", "只回复两个字：收到", temperature=0)
        report("豆包文字模型", True, f"模型回复「{reply.strip()[:10]}」")
    except Exception as e:
        report("豆包文字模型", False, str(e)[:300])
    try:
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "test.jpg"
            editor._run(["-y", "-f", "lavfi", "-i", "color=c=yellow:s=64x64", "-frames:v", "1", str(img)])
            reply = llm.vision(cfg.llm, "这张图片是什么颜色？只回答颜色", [img])
        report("豆包看图（识别对标视频要用）", True, f"模型回复「{reply.strip()[:10]}」")
    except Exception as e:
        hint = "" if llm.KEY_HINT in str(e) else "。请换一个支持图片理解的豆包模型，填到 ARK_MODEL"
        report("豆包看图（识别对标视频要用）", False, f"{str(e)[:200]}{hint}")

    # 3. 剪辑工具
    try:
        import subprocess

        filters = subprocess.run(
            [editor.ffmpeg_exe(), "-hide_banner", "-filters"], capture_output=True, text=True, encoding="utf-8", errors="replace"
        ).stdout
        report("剪辑工具 ffmpeg", "subtitles" in filters, "" if "subtitles" in filters else "当前 ffmpeg 不支持字幕，请联系开发者")
    except Exception as e:
        report("剪辑工具 ffmpeg", False, str(e)[:200])

    print("\n全部正常，可以开始使用。" if ok else "\n有 ❌ 的项目需要处理，改好后再运行一次检查。")
    return ok
