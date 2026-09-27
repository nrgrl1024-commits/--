"""飞书开放平台：多维表格读写 + 附件上传下载。"""
from __future__ import annotations

import os
import time
from pathlib import Path

import requests

BASE = "https://open.feishu.cn/open-apis"
SMALL_UPLOAD_LIMIT = 20 * 1024 * 1024  # 超过 20MB 走分片上传
RATE_LIMIT_CODES = {99991400, 1254290}  # 飞书"请求过于频繁"
RETRIES = 5
DOWNLOAD_RETRIES = 6


class FeishuError(RuntimeError):
    pass


class Feishu:
    def __init__(self, app_id: str, app_secret: str, app_token: str, timeout: int = 60):
        if not (app_id and app_secret and app_token):
            raise FeishuError("缺少飞书配置：app_id / app_secret / app_token")
        self.app_id = app_id
        self.app_secret = app_secret
        self.app_token = app_token
        self.timeout = timeout
        self._token = ""
        self._token_expire = 0.0

    # ---------- 基础 ----------
    def token(self) -> str:
        if self._token and time.time() < self._token_expire - 60:
            return self._token
        resp = requests.post(
            f"{BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=self.timeout,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise FeishuError(f"获取 tenant_access_token 失败：{data}")
        self._token = data["tenant_access_token"]
        self._token_expire = time.time() + int(data.get("expire", 7200))
        return self._token

    def request(self, method: str, path: str, **kw) -> dict:
        url = path if path.startswith("http") else f"{BASE}{path}"
        headers = kw.pop("headers", {})
        for attempt in range(RETRIES + 1):
            headers["Authorization"] = f"Bearer {self.token()}"
            resp = requests.request(method, url, headers=headers, timeout=self.timeout, **kw)
            try:
                data = resp.json()
            except ValueError:
                if resp.status_code != 429:
                    raise FeishuError(f"{method} {path} 返回非 JSON（HTTP {resp.status_code}）：{resp.text[:200]}")
                data = {}
            # 请求太快被飞书限流时，等一下再试（上传文件的请求体已读完，不能重发，所以不重试）
            limited = resp.status_code == 429 or data.get("code") in RATE_LIMIT_CODES
            if limited and attempt < RETRIES and "files" not in kw:
                time.sleep(2 ** attempt)
                continue
            if data.get("code") != 0:
                raise FeishuError(f"{method} {path} 失败：code={data.get('code')} msg={data.get('msg')}")
            return data.get("data") or {}
        raise FeishuError(f"{method} {path} 多次被限流，请稍后再试")

    def _records_path(self, table_id: str) -> str:
        return f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records"

    # ---------- 记录 ----------
    def list_records(self, table_id: str) -> list[dict]:
        records, page_token = [], None
        while True:
            params = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            data = self.request("GET", self._records_path(table_id), params=params)
            records.extend(data.get("items") or [])
            if not data.get("has_more"):
                return records
            page_token = data.get("page_token")

    def batch_create(self, table_id: str, rows: list[dict]) -> list[dict]:
        created = []
        for i in range(0, len(rows), 500):
            chunk = [{"fields": r} for r in rows[i : i + 500]]
            data = self.request("POST", f"{self._records_path(table_id)}/batch_create", json={"records": chunk})
            created.extend(data.get("records") or [])
        return created

    def update_record(self, table_id: str, record_id: str, fields: dict) -> None:
        self.request("PUT", f"{self._records_path(table_id)}/{record_id}", json={"fields": fields})

    # ---------- 数据表 / 字段 ----------
    def list_tables(self) -> dict[str, str]:
        """返回 {数据表名称: table_id}，即多维表格左侧的每一页。"""
        tables, page_token = {}, None
        while True:
            params = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            data = self.request("GET", f"/bitable/v1/apps/{self.app_token}/tables", params=params)
            for t in data.get("items") or []:
                tables[t["name"]] = t["table_id"]
            if not data.get("has_more"):
                return tables
            page_token = data.get("page_token")

    def list_fields(self, table_id: str) -> set[str]:
        data = self.request(
            "GET", f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/fields", params={"page_size": 100}
        )
        return {f["field_name"] for f in data.get("items") or []}

    def create_field(self, table_id: str, field: dict) -> None:
        self.request("POST", f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/fields", json=field)

    def create_table(self, name: str, fields: list[dict]) -> str:
        data = self.request(
            "POST",
            f"/bitable/v1/apps/{self.app_token}/tables",
            json={"table": {"name": name, "default_view_name": "表格", "fields": fields}},
        )
        return data["table_id"]

    # ---------- 附件 ----------
    def download(self, attachment: dict, dest: Path) -> Path:
        """下载附件。网络中途断开时自动重试，并尽量从断开的位置接着下载。"""
        url = attachment.get("url") or f"{BASE}/drive/v1/medias/{attachment['file_token']}/download"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.unlink(missing_ok=True)
        total = None
        last_err: Exception | None = None
        for attempt in range(DOWNLOAD_RETRIES + 1):
            done = dest.stat().st_size if dest.exists() else 0
            if total is not None and done >= total:
                return dest
            headers = {"Authorization": f"Bearer {self.token()}"}
            if done:
                headers["Range"] = f"bytes={done}-"
            try:
                with requests.get(url, headers=headers, stream=True, timeout=(15, 120)) as r:
                    if r.status_code not in (200, 206):
                        raise FeishuError(f"下载附件失败 HTTP {r.status_code}：{r.text[:200]}")
                    if r.status_code == 200:  # 服务器不支持续传，从头下载
                        done = 0
                        total = int(r.headers["Content-Length"]) if r.headers.get("Content-Length") else None
                    elif total is None and "/" in r.headers.get("Content-Range", ""):
                        size = r.headers["Content-Range"].rsplit("/", 1)[1]
                        total = int(size) if size.isdigit() else None
                    with open(dest, "ab" if done else "wb") as f:
                        for chunk in r.iter_content(1 << 20):
                            f.write(chunk)
                if total is None or dest.stat().st_size >= total:
                    return dest
                last_err = FeishuError(f"只下载了 {dest.stat().st_size}/{total} 字节")
            except FeishuError:
                raise
            except requests.RequestException as e:
                last_err = e
            time.sleep(min(2 ** attempt, 30))
        raise FeishuError(f"下载附件多次中断，请检查网络：{last_err}")

    def upload(self, path: Path, parent_type: str = "bitable_file") -> str:
        """上传到本多维表格，返回 file_token，可直接写入附件字段。"""
        size = os.path.getsize(path)
        if size <= SMALL_UPLOAD_LIMIT:
            with open(path, "rb") as f:
                data = self.request(
                    "POST",
                    "/drive/v1/medias/upload_all",
                    data={
                        "file_name": path.name,
                        "parent_type": parent_type,
                        "parent_node": self.app_token,
                        "size": str(size),
                    },
                    files={"file": (path.name, f)},
                )
            return data["file_token"]

        prep = self.request(
            "POST",
            "/drive/v1/medias/upload_prepare",
            json={"file_name": path.name, "parent_type": parent_type, "parent_node": self.app_token, "size": size},
        )
        upload_id, block_size, block_num = prep["upload_id"], int(prep["block_size"]), int(prep["block_num"])
        with open(path, "rb") as f:
            for seq in range(block_num):
                chunk = f.read(block_size)
                self.request(
                    "POST",
                    "/drive/v1/medias/upload_part",
                    data={"upload_id": upload_id, "seq": str(seq), "size": str(len(chunk))},
                    files={"file": (path.name, chunk)},
                )
        done = self.request(
            "POST", "/drive/v1/medias/upload_finish", json={"upload_id": upload_id, "block_num": block_num}
        )
        return done["file_token"]


# ---------- 字段值解析（飞书返回的格式因字段类型而异） ----------
def text_of(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or "")
    if isinstance(value, list):
        return "".join(text_of(v) for v in value)
    return str(value)


def link_ids(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, dict):
        return list(value.get("link_record_ids") or value.get("record_ids") or [])
    ids: list[str] = []
    for v in value:
        if isinstance(v, str):
            ids.append(v)
        elif isinstance(v, dict):
            ids.extend(v.get("record_ids") or v.get("link_record_ids") or [])
    return ids


def attachments(value) -> list[dict]:
    return [v for v in (value or []) if isinstance(v, dict) and v.get("file_token")]
