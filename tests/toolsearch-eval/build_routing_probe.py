"""Build the input for an end-to-end LLM routing test.

The lexical recall metric measures the RETRIEVER. What we actually care about is
whether the MODEL routes correctly given our retrieval UX (find_tool top-k +
the ability to browse the full catalog). This script samples cases - weighting
the ones where lexical recall@8 MISSED (the hard cases) - and for each records
exactly what the model would see: find_tool's top-8 for that query. It also
dumps the full catalog once (what a browse returns). No gold labels go in the
model-facing file; scoring happens separately.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for cand in ("/srv", os.path.join(HERE, "..", "..", "whatsapp-mcp-server")):
    if os.path.isdir(cand):
        sys.path.append(os.path.abspath(cand))
        break


def main():
    import server
    import toolsearch
    core = set(server._CORE_TOOLS)
    cases = json.load(open(os.environ["WA_GEN_CASES"], encoding="utf-8"))["cases"]

    def accept(n, c):
        return n == c["gold"] or n in c.get("alts", [])

    tail = [c for c in cases if c["gold"] not in core and not any(a in core for a in c.get("alts", []))]

    misses, hits = [], []
    for c in tail:
        res = [t["name"] for t in toolsearch._search(c["q"], 8)]
        (hits if any(accept(n, c) for n in res) else misses).append(c)

    # Deterministic sample: 14 misses (the hard ones) + 8 hits, evenly strided.
    def stride(lst, k):
        if len(lst) <= k:
            return lst
        step = len(lst) / k
        return [lst[int(i * step)] for i in range(k)]

    sample = stride(misses, 14) + stride(hits, 8)

    probes = []
    for c in sample:
        found = toolsearch._search(c["q"], 8)
        probes.append({
            "q": c["q"],
            "gold": c["gold"],            # for scoring only, stripped from model view
            "alts": c.get("alts", []),
            "find_tool_top8": [{"name": t["name"], "description": t["description"]} for t in found],
        })

    catalog = [{"name": t["name"], "description": t["description"]}
               for t in toolsearch._catalog(10_000)]
    core_list = sorted(core)

    json.dump({"probes": probes, "catalog": catalog, "core_tools": core_list},
              open(os.path.join(HERE, "routing_probe.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"tail={len(tail)} misses={len(misses)} hits={len(hits)} sampled={len(sample)}")


if __name__ == "__main__":
    raise SystemExit(main())
