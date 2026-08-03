"""Mine per-tool search aliases from a labeled query set, with a train/test split.

Proper hygiene: aliases are the retriever's "training data", so we mine them from
a TRAIN split only and measure recall on a held-out TEST split. Mixing them would
report fantasy numbers. Output:
  - aliases_mined.json : {tool: "extra keywords ..."} from TRAIN queries
  - split_test.json    : the held-out cases, for honest evaluation

A mined alias for a tool is the set of content words that appear in that tool's
TRAIN queries but are not already in the tool's own name/description - i.e. the
user vocabulary the tool text was missing.
"""
from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for cand in ("/srv", os.path.join(HERE, "..", "..", "whatsapp-mcp-server")):
    if os.path.isdir(cand):
        sys.path.append(os.path.abspath(cand))
        break

import toolsearch  # for _stem, _STOPWORDS, _TOKEN_RE, and the tool catalog

TOK = re.compile(r"[^a-z0-9]+")


def content_stems(text: str) -> list[str]:
    out = []
    for w in TOK.split((text or "").lower()):
        if w and w not in toolsearch._STOPWORDS and len(w) > 1:
            out.append(toolsearch._stem(w))
    return out


def main():
    src = os.environ.get("WA_GEN_CASES")
    cases = json.load(open(src, encoding="utf-8"))["cases"]

    # Need the tool text (name+desc) to know which query words are already covered.
    import server  # noqa
    lib = toolsearch._LIBRARY
    tool_text = {}
    for n, t in lib.items():
        tool_text[n] = set(content_stems(n.replace("_", " ") + " " + (t.description or "")))

    # Stratified split by gold tool: first ~40% of each tool's cases -> test.
    by_tool = {}
    for c in cases:
        by_tool.setdefault(c["gold"], []).append(c)
    train, test = [], []
    for _g, cs in by_tool.items():
        k = max(1, round(len(cs) * 0.4)) if len(cs) > 1 else 0
        test.extend(cs[:k])
        train.extend(cs[k:])

    # Mine aliases from TRAIN.
    mined_counts = {}
    for c in train:
        g = c["gold"]
        already = tool_text.get(g, set())
        for s in content_stems(c["q"]):
            if s in already:
                continue
            mined_counts.setdefault(g, {}).setdefault(s, 0)
            mined_counts[g][s] += 1

    # Keep words seen >=1 in train (small data), cap per tool to avoid noise.
    mined = {}
    for g, counts in mined_counts.items():
        words = [w for w, _ in sorted(counts.items(), key=lambda x: -x[1])][:24]
        if words:
            mined[g] = " ".join(words)

    json.dump(mined, open(os.path.join(HERE, "aliases_mined.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=0)
    json.dump({"cases": test}, open(os.path.join(HERE, "split_test.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=0)
    json.dump({"cases": train}, open(os.path.join(HERE, "split_train.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=0)
    print(f"train={len(train)} test={len(test)} tools_mined={len(mined)}")


if __name__ == "__main__":
    raise SystemExit(main())
