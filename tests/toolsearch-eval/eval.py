"""Tool-selection eval for the progressive-disclosure toolset (toolsearch.py).

Methodology follows the 2026 agent-eval consensus (Confident AI; arXiv
"How Many Tools Should an LLM Agent See?"): compare the three tool-selection
strategies the literature names and measure retrieval recall SEPARATELY from
routing, because a retriever that can't surface the right tool (low context
recall) forces the agent into wrong calls.

Strategies (ours), mapped to the literature's terms:
  - full          = "all": every tool loaded directly (92).
  - minimal-core  = "domain/curated": 28 hot-core tools only, no meta-tools.
  - minimal+search = "retrieval": 28 core + find_tool/call_tool over the full 92.

Metrics (deterministic; gold labels in cases.json):
  - Coverage (context recall): is the gold tool REACHABLE at all in this
    strategy? core-direct, or (for minimal+search) present in the library and so
    reachable via call_tool. This is where minimal-core regresses: long-tail
    tasks are simply unreachable.
  - find_tool recall@k (k=1,3,5): for tasks whose gold tool is NOT in the core,
    does find_tool return it within the top k? This is the retrieval-quality
    metric the optimization actually moves.
  - Routing precision@1: fraction of ALL tasks where the correct tool is the top
    available choice - a core tool is directly loaded (correct by availability),
    a long-tail tool must be find_tool's top-1. This is the end-to-end
    "did progressive disclosure put the right tool in front of the model" number.

Run (against the live registry, in-process, NO server restart):
    docker exec whatsapp-mcp python /path/to/eval.py
or copy this dir in and point PYTHONPATH at the server package.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# Allow running from the repo checkout by pointing at the server package.
# APPEND (not insert): a caller may prepend a candidate build dir to sys.path to
# test uncommitted code without restarting the container, and that must win.
for cand in ("/srv", os.path.join(HERE, "..", "..", "whatsapp-mcp-server")):
    if os.path.isdir(cand):
        sys.path.append(os.path.abspath(cand))
        break


def _load_cases():
    # WA_EVAL_CASES lets a run point at a different labeled set (e.g. the larger
    # independently-generated set) without editing this file.
    path = os.environ.get("WA_EVAL_CASES") or os.path.join(HERE, "cases.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)["cases"]


def main():
    import server  # triggers capture()+register() in minimal mode
    import toolsearch

    core = set(server._CORE_TOOLS)
    lib = dict(toolsearch._LIBRARY)
    cases = _load_cases()

    # Sanity: every gold/alt label must be a real tool.
    allnames = set(lib)
    bad = [c for c in cases if c["gold"] not in allnames]
    if bad:
        print("FATAL: unknown gold tools:", [c["gold"] for c in bad])
        return 1

    def accept(name, c):
        return name == c["gold"] or name in c.get("alts", [])

    def rank_in_find(c, k):
        """1-based rank of an accepted tool in find_tool(query), or None."""
        res = toolsearch._search(c["q"], k)
        for i, t in enumerate(res, 1):
            if accept(t["name"], c):
                return i
        return None

    def best_rank(c, kmax=25):
        """Best (lowest) rank of an accepted tool within a deep result set, for
        MRR. None if not found at all in the top kmax."""
        res = toolsearch._search(c["q"], kmax)
        for i, t in enumerate(res, 1):
            if accept(t["name"], c):
                return i
        return None

    def run_with_aliases(alias_map):
        """Re-index the library with a chosen alias map, then score."""
        toolsearch._ALIASES = alias_map
        toolsearch.capture(list(lib.values()))  # rebuilds _INDEX with these aliases

        n = len(cases)
        core_cases = [c for c in cases if c["gold"] in core or any(a in core for a in c.get("alts", []))]
        tail_cases = [c for c in cases if c not in core_cases]

        # Coverage per strategy
        cov_full = n  # everything loaded
        cov_core = sum(1 for c in cases if c["gold"] in core or any(a in core for a in c.get("alts", [])))
        cov_search = sum(1 for c in cases if c["gold"] in lib or any(a in lib for a in c.get("alts", [])))

        # find_tool recall@k over the long-tail cases. recall@8/@10 is the PARITY
        # metric: find_tool returns a candidate set and the model does the final
        # pick from it - exactly as it would from the full 92, but with fewer
        # distractors. If the gold tool is in that set, minimal+search routes at
        # least as well as full.
        rec = {}
        for k in (1, 3, 5, 8, 10):
            hit = sum(1 for c in tail_cases if (rank_in_find(c, k) is not None))
            rec[k] = (hit, len(tail_cases))

        # MRR over long-tail: mean of 1/rank of the gold tool (0 if not found in
        # top 25). Rewards ranking the right tool HIGH, not just present - the
        # standard retrieval-quality single number.
        rr = []
        for c in tail_cases:
            br = best_rank(c)
            rr.append(1.0 / br if br else 0.0)
        mrr = sum(rr) / len(rr) if rr else 0.0

        # Routing precision@1 (minimal+search): core -> available directly;
        # tail -> must be find_tool top-1.
        routed = 0
        misses = []
        for c in cases:
            if c["gold"] in core or any(a in core for a in c.get("alts", [])):
                routed += 1
            else:
                r = rank_in_find(c, 1)
                if r == 1:
                    routed += 1
                else:
                    misses.append(c)
        return {
            "n": n, "n_core": len(core_cases), "n_tail": len(tail_cases),
            "cov_full": cov_full, "cov_core": cov_core, "cov_search": cov_search,
            "recall": rec, "mrr": mrr, "routed": routed, "misses": misses,
        }

    snap_aliases = dict(toolsearch._ALIASES)

    print("=" * 68)
    print(f"Tool-selection eval  |  {len(cases)} cases  |  core={len(core)}  library={len(lib)}")
    print("=" * 68)

    for label, amap in (("BASELINE (no synonym aliases)", {}),
                        ("SHIPPED  (with synonym aliases)", snap_aliases)):
        r = run_with_aliases(amap)
        print(f"\n### {label}")
        print(f"  cases: {r['n']}  (core-covered={r['n_core']}, long-tail={r['n_tail']})")
        print("  COVERAGE (context recall):")
        print(f"    full (all 92 loaded)         : {r['cov_full']}/{r['n']}  ({100*r['cov_full']//r['n']}%)")
        print(f"    minimal-core (no meta-tools) : {r['cov_core']}/{r['n']}  ({100*r['cov_core']//r['n']}%)   <- old minimal regressed here")
        print(f"    minimal+search (shipped)     : {r['cov_search']}/{r['n']}  ({100*r['cov_search']//r['n']}%)")
        print(f"  find_tool RECALL@k (long-tail, n={r['n_tail']}):")
        for k in (1, 3, 5, 8, 10):
            hit, tot = r["recall"][k]
            tag = "  <- model-visible set (parity metric)" if k in (8, 10) else ""
            print(f"    recall@{k:<2}: {hit}/{tot}  ({100*hit//tot if tot else 0}%){tag}")
        print(f"    MRR      : {r['mrr']:.3f}   (mean 1/rank of gold; 1.0 = always rank 1)")
        print(f"  ROUTING precision@1 (all cases): {r['routed']}/{r['n']}  ({100*r['routed']//r['n']}%)")
        if r["misses"]:
            print(f"  routing misses ({len(r['misses'])}):")
            for c in r["misses"]:
                top = [t["name"] for t in toolsearch._search(c["q"], 5)]
                print(f"    - {c['gold']:24s} q={c['q']!r}")
                print(f"      top5={top}")

    # restore
    toolsearch._ALIASES = snap_aliases
    toolsearch.capture(list(lib.values()))
    print("\n" + "=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
