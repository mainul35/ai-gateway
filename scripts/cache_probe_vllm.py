#!/usr/bin/env python3
"""vLLM prefix-cache probe (phase 0b). Same cases as cache_probe.py, read from usage + /metrics.
python3 vllm_probe.py --base http://127.0.0.1:8198 --model qwen3.6-vllm --tokens 2500"""
import argparse, json, re, sys, time, urllib.request, urllib.error

def call(base, path, body=None, timeout=900):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except Exception as e:
        return 0, str(e)

def metric(base, name):
    st, txt = call(base, "/metrics")
    total = 0.0
    for line in txt.splitlines():
        if line.startswith(name) and not line.startswith("#"):
            total += float(line.rsplit(" ", 1)[1])
    return total

def filler(words, tag):
    return " ".join(f"{tag}-{i % 997}" for i in range(words))

def ask(base, model, system, tail):
    q0, h0 = metric(base, "vllm:prefix_cache_queries_total"), metric(base, "vllm:prefix_cache_hits_total")
    body = {"model": model, "max_tokens": 1, "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": tail}]}
    t = time.time()
    st, txt = call(base, "/v1/chat/completions", body)
    wall = round(time.time() - t, 2)
    try:
        usage = json.loads(txt).get("usage", {})
    except Exception:
        return {"status": st, "wall_s": wall, "error": txt[:200]}
    d = usage.get("prompt_tokens_details") or {}
    return {"status": st, "wall_s": wall, "prompt_tokens": usage.get("prompt_tokens"),
            "cached_tokens": d.get("cached_tokens"),
            "metric_queries": metric(base, "vllm:prefix_cache_queries_total") - q0,
            "metric_hits": metric(base, "vllm:prefix_cache_hits_total") - h0}

ap = argparse.ArgumentParser()
ap.add_argument("--base", required=True); ap.add_argument("--model", required=True)
ap.add_argument("--tokens", type=int, default=2500)
a = ap.parse_args()
sysA = "Reference notes. " + filler(a.tokens, "n")
sysB = "Other notes. " + filler(a.tokens, "m")
r = {}
r["A_cold"] = ask(a.base, a.model, sysA, "Reply with one word. A")
r["A_repeat_new_tail"] = ask(a.base, a.model, sysA, "Reply with one word. B")
r["A_repeat_again"] = ask(a.base, a.model, sysA, "Reply with one word. C")
r["B_start_edited"] = ask(a.base, a.model, sysA.replace("Reference notes.", "Reference notes!", 1), "Reply with one word. B")
r["C_other_prefix_cold"] = ask(a.base, a.model, sysB, "Reply with one word. A")
r["C_back_to_first"] = ask(a.base, a.model, sysA, "Reply with one word. D")
r["C_other_again"] = ask(a.base, a.model, sysB, "Reply with one word. B")
print(json.dumps(r, indent=2))
