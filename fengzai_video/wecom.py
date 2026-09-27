"""企业微信群机器人：把成片、封面和发布文案推送到门店群。

在企业微信群里：右上角「…」→「消息推送」（或「群机器人」）→ 添加，复制它的 Webhook 地址，
填到飞书「门店资料」的「企微群机器人」字段即可。
"""
from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

import requests

UPLOAD_LIMIT = 20 * 1024 * 1024  # 群机器人发文件的上限
IMAGE_LIMIT = 2 * 1024 * 1024  # 群机器人发图片的上限


class WecomError(RuntimeError):
    pass


def _key(webhook: str) -> str:
    m = re.search(r"key=([\w-]+)", webhook)
    if not m:
        raise WecomError(f"企微群机器人地址不对：{webhook[:60]}")
    return m.group(1)


def _post(url: str, **kw) -> dict:
    resp = requests.post(url, timeout=120, **kw)
    data = resp.json()
    if data.get("errcode") != 0:
        raise WecomError(f"企微推送失败：{data}")
    return data


def send_text(webhook: str, text: str) -> None:
    _post(webhook, json={"msgtype": "text", "text": {"content": text}})


def send_image(webhook: str, image: Path) -> None:
    raw = image.read_bytes()
    if len(raw) > IMAGE_LIMIT:
        raise WecomError("封面图超过 2MB")
    _post(
        webhook,
        json={"msgtype": "image", "image": {"base64": base64.b64encode(raw).decode(), "md5": hashlib.md5(raw).hexdigest()}},
    )


def send_file(webhook: str, path: Path) -> None:
    if path.stat().st_size > UPLOAD_LIMIT:
        raise WecomError("文件超过 20MB，企微群机器人发不了")
    upload = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key={_key(webhook)}&type=file"
    with open(path, "rb") as f:
        media = _post(upload, files={"media": (path.name, f, "application/octet-stream")})["media_id"]
    _post(webhook, json={"msgtype": "file", "file": {"media_id": media}})


def push_video(webhook: str, store: str, video: Path, cover: Path, publish_text: str) -> None:
    send_text(webhook, f"【{store}】今日视频已生成，请下载后发布到视频号👇\n\n{publish_text}".strip())
    send_image(webhook, cover)
    send_file(webhook, video)
