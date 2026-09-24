"""大模型调用：火山方舟（豆包），OpenAI 兼容接口。换 DeepSeek / 通义只需改 base_url、model、api_key。"""
from __future__ import annotations

import json
import re

import requests


class LLMError(RuntimeError):
    pass


def chat(llm_cfg: dict, system: str, user: str, temperature: float = 0.9) -> str:
    base_url = (llm_cfg.get("base_url") or "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
    api_key, model = llm_cfg.get("api_key"), llm_cfg.get("model")
    if not (api_key and model):
        raise LLMError("缺少大模型配置：llm.api_key / llm.model")
    resp = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "temperature": temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        },
        timeout=int(llm_cfg.get("timeout", 180)),
    )
    if resp.status_code != 200:
        raise LLMError(f"大模型调用失败 HTTP {resp.status_code}：{resp.text[:300]}")
    return resp.json()["choices"][0]["message"]["content"]


def parse_json(text: str) -> dict:
    """从模型回复中取出 JSON（兼容 ```json 代码块和前后多余文字）。"""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text[text.find("{") : text.rfind("}") + 1]
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, ValueError) as e:
        raise LLMError(f"模型返回的不是合法 JSON：{e}\n{text[:500]}")
