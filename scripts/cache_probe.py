#!/usr/bin/env python3
"""Phase 0 probe for the IDE plugin: does this llama.cpp engine reuse its KV cache the way the design assumes?

Run ON homelabai against one engine's own port, never while the engine is serving anyone:

    python3 scripts/cache_probe.py --base http://127.0.0.1:8120 [--model NAME] [--tokens 6000]

It refuses to start if any slot is busy. It sends only max_tokens=1 requests with generic filler text,
and prints a JSON report: what the engine exposes, how much of a repeated prefix is served from cache,
what a change near the start of the prompt costs, whether id_slot pins a request to a slot, and whether
slot save/restore works. Standard library only.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def call(base, path, body=None, method=None, timeout=900):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method or ("POST" if body is not None else "GET"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            return err.code, json.loads(raw)
        except Exception:
            return err.code, {"raw": raw[:300].decode("utf-8", "replace")}
    except Exception as err:  # connection refused, timeout
        return 0, {"error": str(err)}


def filler(words, tag):
    """Generic, deterministic text: numbered lines, nothing from any user."""
    return " ".join(f"{tag}-{i % 997}" for i in range(words))


def ask(base, model, system, tail, slot=None):
    body = {"model": model, "max_tokens": 1, "temperature": 0, "cache_prompt": True,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": tail}]}
    if slot is not None:
        body["id_slot"] = slot
    started = time.time()
    status, out = call(base, "/v1/chat/completions", body)
    wall = round(time.time() - started, 2)
    timings = out.get("timings", {}) if isinstance(out, dict) else {}
    return {"status": status, "wall_s": wall, "prompt_n": timings.get("prompt_n"),
            "cache_n": timings.get("cache_n"), "prompt_ms": timings.get("prompt_ms"),
            "slot": out.get("id_slot") if isinstance(out, dict) else None,
            "error": out.get("error") if status != 200 else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--model", default="")
    ap.add_argument("--tokens", type=int, default=6000, help="rough size of the shared prefix")
    ap.add_argument("--save-name", default="cache-probe.bin")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    report = {"base": base}

    st, props = call(base, "/props")
    report["props"] = {"status": st, "total_slots": props.get("total_slots"),
                       "n_ctx": (props.get("default_generation_settings") or {}).get("n_ctx"),
                       "build": props.get("build_info")}
    st, slots = call(base, "/slots")
    report["slots_endpoint"] = st
    if st == 200 and isinstance(slots, list):
        busy = [s.get("id") for s in slots if s.get("is_processing")]
        if busy:
            print(json.dumps({"refused": "slots busy, engine is in use", "busy": busy}, indent=2))
            return 2
        report["slot_count"] = len(slots)
    elif st != 200:
        report["slots_note"] = "no /slots; pinning and save/restore cannot be probed"

    system = "Reference notes. " + filler(args.tokens, "n")
    # A: cold, then same prefix with a different tail (the append-only case)
    report["A_cold"] = ask(base, args.model, system, "Reply with one word. A")
    report["A_repeat_new_tail"] = ask(base, args.model, system, "Reply with one word. B")
    # B: change one word near the start (what a timestamp in the prefix would do)
    broken = system.replace("Reference notes.", "Reference notes!", 1)
    report["B_prefix_edited_at_start"] = ask(base, args.model, broken, "Reply with one word. B")
    # C: pinning - needs 2+ slots
    if report.get("slot_count", 1) > 1:
        report["C_slot0_warm"] = ask(base, args.model, system, "Reply with one word. C", slot=0)
        report["C_slot1_cold_expected"] = ask(base, args.model, system, "Reply with one word. C", slot=1)
        report["C_slot1_again"] = ask(base, args.model, system, "Reply with one word. D", slot=1)
    # D: save / erase / restore on slot 0
    ask(base, args.model, system, "Reply with one word. E", slot=0 if report.get("slot_count", 1) > 1 else None)
    st, out = call(base, "/slots/0?action=save", {"filename": args.save_name})
    report["D_save"] = {"status": st, "out": out}
    if st == 200:
        st2, out2 = call(base, "/slots/0?action=erase", {})
        report["D_erase"] = {"status": st2}
        started = time.time()
        st3, out3 = call(base, "/slots/0?action=restore", {"filename": args.save_name})
        report["D_restore"] = {"status": st3, "restore_s": round(time.time() - started, 2),
                               "n_restored": out3.get("n_restored") if isinstance(out3, dict) else None}
        report["D_after_restore"] = ask(base, args.model, system, "Reply with one word. F", slot=0)
    else:
        report["D_note"] = "save refused: engine needs --slot-save-path, or this model/build lacks support"

    a, r, b = report["A_cold"], report["A_repeat_new_tail"], report["B_prefix_edited_at_start"]
    if r.get("prompt_n") is not None and r.get("cache_n") is not None:
        total = r["prompt_n"] + r["cache_n"]
        report["summary"] = {
            "prefix_reuse_ratio": round(r["cache_n"] / total, 3) if total else None,
            "cold_prompt_ms": a.get("prompt_ms"), "warm_prompt_ms": r.get("prompt_ms"),
            "edited_start_reuse_tokens": b.get("cache_n"),
        }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
