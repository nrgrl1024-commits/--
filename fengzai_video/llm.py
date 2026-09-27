"""大模型调用：火山方舟（豆包），OpenAI 兼容接口。换 DeepSeek / 通义只需改 base_url、model、api_key。"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import requests


class LLMError(RuntimeError):
    pass


KEY_HINT = "豆包 API Key 不对：请到火山方舟控制台左侧「API Key 管理」复制 Key，填到 .env 的 ARK_API_KEY（不是 AKLT 开头的 Access Key）"


def complete(llm_cfg: dict, messages: list[dict], temperature: float = 0.9, model: str | None = None) -> str:
    base_url = (llm_cfg.get("base_url") or "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
    api_key, model = llm_cfg.get("api_key"), model or llm_cfg.get("model")
    if not (api_key and model):
        raise LLMError("缺少大模型配置：llm.api_key / llm.model")
    if api_key.startswith("AKLT"):
        raise LLMError(KEY_HINT + "（现在填的是 AKLT 开头的 Access Key）")
    resp = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "temperature": temperature, "messages": messages},
        timeout=int(llm_cfg.get("timeout", 300)),
    )
    if resp.status_code == 401:
        raise LLMError(f"{KEY_HINT}。原始报错：{resp.text[:200]}")
    if resp.status_code != 200:
        raise LLMError(f"大模型调用失败 HTTP {resp.status_code}：{resp.text[:300]}")
    return resp.json()["choices"][0]["message"]["content"]


def chat(llm_cfg: dict, system: str, user: str, temperature: float = 0.9) -> str:
    return complete(llm_cfg, [{"role": "system", "content": system}, {"role": "user", "content": user}], temperature)


def vision(llm_cfg: dict, prompt: str, images: list[Path], temperature: float = 0.3) -> str:
    """把几张图片连同文字一起发给视觉模型（豆包带图片理解能力的版本）。"""
    content: list[dict] = []
    for img in images:
        b64 = base64.b64encode(img.read_bytes()).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    content.append({"type": "text", "text": prompt})
    model = llm_cfg.get("vision_model") or llm_cfg.get("model")
    return complete(llm_cfg, [{"role": "user", "content": content}], temperature, model=model)


def parse_json(text: str) -> dict:
    """从模型回复中取出 JSON（兼容 ```json 代码块和前后多余文字）。"""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text[text.find("{") : text.rfind("}") + 1]
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, ValueError) as e:
        raise LLMError(f"模型返回的不是合法 JSON：{e}\n{text[:500]}")
