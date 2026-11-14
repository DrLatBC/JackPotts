"""Send one recorded request log to two balatrobot-API servers and diff their views after each step.

Usage: python sim/compare_sims.py RECORD.jsonl PORT_A PORT_B [--all] [--keep-text]
Compares every field of the two views except card ids (a session-wide counter)
and, unless --keep-text, the description text the bot no longer reads
(``value.effect`` and the blind/tag effect strings) and the ``earnings``
tracker. Prints the differing paths per step
(only the first few steps unless --all) and a tally of paths that ever differed.
"""
import json
import re
import sys
import urllib.request
from collections import Counter

TEXT_PATHS = re.compile(r"(\.value\.effect$|\.effect$|\.tag_effect$|^\.earnings)")
IDS = re.compile(r"\.id$")


def call(port, method, params):
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}, "id": 1}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.loads(r.read())
    if "error" in out:
        return {"error": out["error"].get("data", {}).get("name"), "msg": out["error"]["message"]}
    return out["result"]


def flatten(v, path="", out=None):
    out = {} if out is None else out
    if isinstance(v, dict):
        if not v:
            out[path] = "{}"
        for k, x in v.items():
            flatten(x, f"{path}.{k}", out)
    elif isinstance(v, list):
        if not v:
            out[path] = "{}"  # balatrobot sends [] for empty objects
        for i, x in enumerate(v):
            flatten(x, f"{path}[{i}]", out)
    elif isinstance(v, float) and v.is_integer():
        out[path] = int(v)
    else:
        out[path] = v
    return out


def main():
    path, pa, pb = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    show_all = "--all" in sys.argv
    keep_text = "--keep-text" in sys.argv
    reqs = [json.loads(l) for l in open(path, encoding="utf8") if l.strip()]
    steps_differing = 0
    ever = Counter()
    for i, rq in enumerate(reqs, 1):
        a = flatten(call(pa, rq["method"], rq.get("params")))
        b = flatten(call(pb, rq["method"], rq.get("params")))
        diff = sorted(p for p in set(a) | set(b)
                      if a.get(p) != b.get(p) and not IDS.search(p)
                      and (keep_text or not TEXT_PATHS.search(p)))
        if not diff:
            continue
        steps_differing += 1
        for p in diff:
            ever[re.sub(r"\[\d+\]", "[]", p)] += 1
        if steps_differing <= 5 or show_all:
            print(f"step {i} {rq['method']} {json.dumps(rq.get('params'))}")
            for p in diff[:25]:
                print(f"   {p}: A={a.get(p)!r}  B={b.get(p)!r}")
            if len(diff) > 25:
                print(f"   ... {len(diff) - 25} more")
    print(f"\n{len(reqs)} requests, {steps_differing} steps differ")
    for p, n in ever.most_common(40):
        print(f"  {n:4d}  {p}")


if __name__ == "__main__":
    main()
