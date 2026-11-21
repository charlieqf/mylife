"""最小的 OpenAI 兼容流式客户端（只用标准库）。密钥从 .secrets/mylife.env 读取，不写进代码。

端点：
  tokenhub   腾讯云 TokenHub（deepseek-v4.1-flash 等），需关思考：extra={"thinking": {"type": "disabled"}}
  dashscope  阿里云百炼（qwen3.8-max 等）
"""
import json
import os
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = os.environ.get("MYLIFE_ENV", os.path.join(ROOT, ".secrets", "mylife.env"))


def _load_env(path):
    env = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v
    return env


_env = _load_env(ENV)
ENDPOINTS = {
    "tokenhub": ("https://tokenhub.tencentmaas.com/v1", _env.get("TENCENT_TOKENHUB_API_KEY")),
    "dashscope": ("https://dashscope.aliyuncs.com/compatible-mode/v1", _env.get("DASHSCOPE_API_KEY")),
}
DEFAULT = {"endpoint": "tokenhub", "model": "deepseek-v4.1-flash",
           "extra": {"thinking": {"type": "disabled"}}}


def chat(messages, endpoint=None, model=None, temperature=0.8, max_tokens=300, extra=None, timeout=60):
    endpoint = endpoint or DEFAULT["endpoint"]
    model = model or DEFAULT["model"]
    if extra is None and endpoint == DEFAULT["endpoint"] and model == DEFAULT["model"]:
        extra = DEFAULT["extra"]
    base, key = ENDPOINTS[endpoint]
    body = {"model": model, "messages": messages, "temperature": temperature,
            "max_tokens": max_tokens, "stream": True}
    body.update(extra or {})
    req = urllib.request.Request(base + "/chat/completions", data=json.dumps(body).encode("utf-8"),
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    t0 = time.time()
    ttft, content, reasoning, usage, finish = None, [], [], None, None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    ev = json.loads(data)
                except ValueError:
                    continue
                if ev.get("usage"):
                    usage = ev["usage"]
                for ch in ev.get("choices") or []:
                    d = ch.get("delta") or {}
                    if d.get("reasoning_content"):
                        reasoning.append(d["reasoning_content"])
                    if d.get("content"):
                        if ttft is None:
                            ttft = time.time() - t0
                        content.append(d["content"])
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
    except urllib.error.HTTPError as e:
        return {"error": "HTTP %d %s" % (e.code, e.read()[:300].decode("utf-8", "replace")), "total": time.time() - t0}
    except Exception as e:  # noqa: BLE001
        return {"error": repr(e), "total": time.time() - t0}
    return {"ttft": ttft, "total": time.time() - t0, "content": "".join(content),
            "reasoning_chars": len("".join(reasoning)), "usage": usage, "finish": finish}
