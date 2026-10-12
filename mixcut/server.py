"""门店混剪 · 网页后台。

只做三件事，视频剪辑全部在浏览器里完成：
1. 保存门店资料（所有人打开网址看到同一份）
2. 读取文案 Excel
3. 声音复刻：上传主播的声音样本，之后把文案逐句合成为主播的方言配音（阿里云百炼 CosyVoice）

运行：python server.py         （开发）
      gunicorn -b 0.0.0.0:80 server:app   （服务器，见 install.sh）
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import shutil
import subprocess
import threading
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("MIXCUT_DATA", ROOT / "data"))
STORES_FILE = DATA / "stores.json"
PUBLIC = DATA / "public"      # 声音样本：阿里云要通过网址下载，所以这个目录不设密码
TTS_CACHE = DATA / "tts"

PASSWORD = os.environ.get("ACCESS_PASSWORD", "")
API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")   # 例如 http://1.2.3.4
TTS_MODEL = os.environ.get("TTS_MODEL", "cosyvoice-v3-plus")

# CosyVoice 支持的方言指令（"请用<方言>表达。"），普通话不加指令
DIALECTS = ["普通话", "广东话", "四川话", "东北话", "河南话", "湖南话", "陕西话", "山东话", "安徽话"]

app = Flask(__name__, static_folder=None)
_lock = threading.Lock()


# ---------- 访问密码 ----------
@app.before_request
def check_password():
    if not PASSWORD or request.path.startswith("/public/"):
        return None
    auth = request.authorization
    if auth and secrets.compare_digest(auth.password or "", PASSWORD):
        return None
    return Response("请输入访问密码", 401, {"WWW-Authenticate": 'Basic realm="mixcut", charset="UTF-8"'})


# ---------- 页面 ----------
@app.get("/")
def index():
    return send_from_directory(ROOT / "static", "index.html")


@app.get("/static/<path:name>")
def static_files(name):
    return send_from_directory(ROOT / "static", name)


@app.get("/public/<path:name>")
def public_files(name):
    return send_from_directory(PUBLIC, name)


@app.get("/api/config")
def config():
    return jsonify(voice=bool(API_KEY and PUBLIC_BASE_URL), dialects=DIALECTS)


# ---------- 门店资料 ----------
def load_stores() -> list[dict]:
    if not STORES_FILE.exists():
        return []
    return json.loads(STORES_FILE.read_text(encoding="utf-8"))


def save_stores(stores: list[dict]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = STORES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(stores, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STORES_FILE)


def find_store(stores: list[dict], name: str) -> dict | None:
    return next((s for s in stores if s.get("name") == name), None)


@app.get("/api/stores")
def get_stores():
    return jsonify(load_stores())


@app.put("/api/stores")
def put_stores():
    incoming = request.get_json(force=True)
    if not isinstance(incoming, list):
        return jsonify(error="格式不对"), 400
    with _lock:
        old = {s.get("name"): s for s in load_stores()}
        cleaned = []
        for s in incoming:
            name = str(s.get("name", "")).strip()
            if not name:
                continue
            row = {k: str(s.get(k, "")).strip() for k in ("name", "city", "nickname", "gender", "dialect")}
            # 声音复刻的结果只由后台写，网页提交的资料不覆盖它
            prev = old.get(name, {})
            for k in ("voice_id", "voice_status", "voice_sample"):
                if k in prev:
                    row[k] = prev[k]
            cleaned.append(row)
        save_stores(cleaned)
    return jsonify(cleaned)


# ---------- 文案 Excel ----------
def _pick(headers: list[str], *keywords: str) -> int | None:
    for kw in keywords:
        for i, h in enumerate(headers):
            if kw in h:
                return i
    return None


def parse_excel(data: bytes) -> list[dict]:
    """读取「人物 | … | 改写脚本 | 封面标题」表。列名包含关键词即可，顺序不限。"""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    rows_out = []
    for ws in wb.worksheets:
        rows = [[("" if c is None else str(c).strip()) for c in r] for r in ws.iter_rows(values_only=True)]
        rows = [r for r in rows if any(r)]
        if not rows:
            continue
        head_idx = next((i for i, r in enumerate(rows) if any(("人物" in c or "门店" in c) for c in r)), None)
        if head_idx is None:
            continue
        headers = rows[head_idx]
        who = _pick(headers, "门店", "人物")
        script = _pick(headers, "改写", "文案", "脚本")
        cover = _pick(headers, "封面")
        if who is None or script is None:
            continue
        for r in rows[head_idx + 1:]:
            r = r + [""] * (len(headers) - len(r))
            if not r[who] or not r[script]:
                continue
            rows_out.append({"who": r[who], "script": r[script], "cover": r[cover] if cover is not None else ""})
    return rows_out


def parse_who(who: str) -> dict:
    """「成都温江 李师傅 男」→ 城市/区域、主播昵称、性别。"""
    parts = who.split()
    gender = parts.pop() if parts and parts[-1] in ("男", "女") else ""
    nickname = parts.pop() if len(parts) >= 2 else ""
    return {"place": "".join(parts), "nickname": nickname, "gender": gender}


def match_store(stores: list[dict], who: str) -> str:
    """先按门店名，再按「城市 + 主播昵称」匹配。"""
    for s in stores:
        if s.get("name") and s["name"] in who:
            return s["name"]
    for s in stores:
        if s.get("nickname") and s["nickname"] in who and s.get("city") and s["city"] in who:
            return s["name"]
    return ""


@app.post("/api/import")
def import_excel():
    f = request.files.get("file")
    if not f:
        return jsonify(error="没有收到文件"), 400
    try:
        rows = parse_excel(f.read())
    except Exception:
        return jsonify(error="读不出这个 Excel，请确认是 .xlsx 格式"), 400
    stores = load_stores()
    for r in rows:
        r.update(parse_who(r["who"]))
        r["store"] = match_store(stores, r["who"])
    return jsonify(rows)


# ---------- 声音复刻 ----------
def _to_wav(src: Path) -> Path:
    """转成 16k 单声道 wav，阿里云识别更稳；服务器没装 ffmpeg 就用原文件。"""
    if not shutil.which("ffmpeg"):
        return src
    dst = src.with_suffix(".wav")
    proc = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", "16000", str(dst)],
                          capture_output=True)
    if proc.returncode != 0:
        return src
    src.unlink(missing_ok=True)
    return dst


@app.post("/api/stores/<name>/voice")
def upload_voice(name):
    if not (API_KEY and PUBLIC_BASE_URL):
        return jsonify(error="服务器还没配置阿里云密钥（DASHSCOPE_API_KEY）和服务器网址（PUBLIC_BASE_URL）"), 400
    f = request.files.get("file")
    if not f:
        return jsonify(error="没有收到声音文件"), 400
    with _lock:
        stores = load_stores()
        s = find_store(stores, name)
        if not s:
            return jsonify(error="先保存这家门店"), 404
    PUBLIC.mkdir(parents=True, exist_ok=True)
    ext = Path(f.filename or "a.m4a").suffix.lower() or ".m4a"
    raw = PUBLIC / f"{secrets.token_hex(12)}{ext}"
    f.save(raw)
    path = _to_wav(raw)
    try:
        from dashscope.audio.tts_v2 import VoiceEnrollmentService

        prefix = "s" + hashlib.md5(name.encode()).hexdigest()[:8]
        voice_id = VoiceEnrollmentService(api_key=API_KEY).create_voice(
            target_model=TTS_MODEL, prefix=prefix, url=f"{PUBLIC_BASE_URL}/public/{path.name}")
    except Exception as e:
        return jsonify(error=f"声音复刻失败：{e}"), 502
    with _lock:
        stores = load_stores()
        s = find_store(stores, name)
        if s:
            s.update(voice_id=voice_id, voice_status="处理中", voice_sample=path.name)
            save_stores(stores)
    return jsonify(voice_id=voice_id, voice_status="处理中")


@app.get("/api/stores/<name>/voice")
def voice_status(name):
    stores = load_stores()
    s = find_store(stores, name)
    if not s or not s.get("voice_id"):
        return jsonify(voice_status="未复刻")
    if s.get("voice_status") == "可用" or not API_KEY:
        return jsonify(voice_status=s.get("voice_status", ""))
    try:
        from dashscope.audio.tts_v2 import VoiceEnrollmentService

        info = VoiceEnrollmentService(api_key=API_KEY).query_voice(s["voice_id"])
        raw = str((info or {}).get("status", "")).upper()
    except Exception as e:
        return jsonify(voice_status=s.get("voice_status", ""), error=str(e))
    status = {"OK": "可用", "DEPLOYING": "处理中", "UNDEPLOYED": "失败"}.get(raw, raw or "处理中")
    with _lock:
        stores = load_stores()
        s = find_store(stores, name)
        if s:
            s["voice_status"] = status
            save_stores(stores)
    return jsonify(voice_status=status)


def instruction_for(dialect: str) -> str | None:
    if not dialect or dialect == "普通话":
        return None
    return f"请用{dialect}表达。"


def synthesize(voice_id: str, text: str, rate: float, instruction: str | None) -> bytes:
    from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer

    kwargs = dict(model=TTS_MODEL, voice=voice_id, format=AudioFormat.MP3_24000HZ_MONO_256KBPS, speech_rate=rate)
    if instruction:
        kwargs["instruction"] = instruction
    audio = SpeechSynthesizer(api_key=API_KEY, **kwargs).call(text)
    if not audio:
        raise RuntimeError("没有返回声音")
    return audio


@app.post("/api/tts")
def tts():
    body = request.get_json(force=True)
    name, text = body.get("store", ""), str(body.get("text", "")).strip()
    rate = min(2.0, max(0.5, float(body.get("rate", 1.0))))
    if not text:
        return jsonify(error="文字是空的"), 400
    s = find_store(load_stores(), name)
    if not s or not s.get("voice_id"):
        return jsonify(error="这家门店还没有复刻声音"), 400
    instruction = instruction_for(s.get("dialect", ""))
    key = hashlib.sha1(f"{TTS_MODEL}|{s['voice_id']}|{instruction}|{rate}|{text}".encode()).hexdigest()
    cached = TTS_CACHE / f"{key}.mp3"
    if not cached.exists():
        try:
            audio = synthesize(s["voice_id"], text, rate, instruction)
        except Exception as e:
            return jsonify(error=f"配音失败：{e}"), 502
        TTS_CACHE.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(audio)
    return send_from_directory(TTS_CACHE, cached.name, mimetype="audio/mpeg")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 8000)), debug=False)
