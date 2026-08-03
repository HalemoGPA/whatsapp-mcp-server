#!/usr/bin/env python3
"""Voice-transcription regression / provider-comparison harness.

A fixed set of ~20 real voice notes (audio/ - gitignored, they are people's
actual voices) with which to sanity-check the current transcription provider and,
crucially, to compare a CANDIDATE provider against the current one before
switching. There is no human ground-truth here, so this does NOT compute a true
WER. What it does:

  1. Transcribe every clip via the chosen provider.
  2. Flag PROBLEMS mechanically: empty output, an error, output that is
     implausibly short for the clip's duration, or output that is not mostly
     Arabic script (a wrong-language / encoding smell).
  3. If a baseline (the current provider's results) exists, report how close the
     candidate is to it per clip (character-level similarity), so a regression
     or a materially different transcription is visible at a glance.

Usage:
    SPEECHMATICS_API_KEYS=... python3 run.py speechmatics      # make the baseline
    GROQ_API_KEY=...          python3 run.py groq              # a candidate
    OPENAI_API_KEY=...        python3 run.py openai            # a candidate
    DEEPGRAM_API_KEY=...      python3 run.py deepgram          # a candidate
    python3 run.py --compare groq                              # groq vs baseline

Results land in results/<provider>.json (gitignored - they contain private
conversation content). The baseline is results/speechmatics.json.
"""
import difflib
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
AUDIO = HERE / "audio"
RESULTS = HERE / "results"
RESULTS.mkdir(exist_ok=True)
BASELINE_PROVIDER = "speechmatics"
LANG = os.environ.get("TRANSCRIBE_LANGUAGE", "ar")

try:
    import requests
except ImportError:
    sys.exit("pip install requests")


# --- providers: each takes a Path, returns (text, duration_s) or raises -------
def _speechmatics(path: Path):
    keys = [k.strip() for k in os.environ.get("SPEECHMATICS_API_KEYS", "").split(",") if k.strip()]
    if not keys:
        raise RuntimeError("set SPEECHMATICS_API_KEYS")
    cfg = ('{"type":"transcription","transcription_config":'
           f'{{"language":"{LANG}","operating_point":"enhanced"}}}}')
    root = "https://asr.api.speechmatics.com/v2"
    last = ""
    for key in keys:
        h = {"Authorization": f"Bearer {key}"}
        with open(path, "rb") as f:
            r = requests.post(f"{root}/jobs/", headers=h, files={"data_file": f},
                              data={"config": cfg}, timeout=(10, 60))
        if r.status_code >= 400:
            last = f"{r.status_code}:{r.text[:80]}"; continue
        job = r.json()["id"]
        for _ in range(60):
            j = requests.get(f"{root}/jobs/{job}", headers=h, timeout=(10, 30)).json()["job"]
            if j["status"] == "done":
                tr = requests.get(f"{root}/jobs/{job}/transcript?format=txt", headers=h, timeout=(10, 30))
                return tr.content.decode("utf-8", "replace").strip(), float(j.get("duration", 0) or 0)
            if j["status"] in ("rejected", "expired"):
                last = f"job {j['status']}"; break
            time.sleep(4)
        else:
            last = "poll timeout"
    raise RuntimeError(last or "speechmatics failed")


def _groq(path: Path):
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        raise RuntimeError("set GROQ_API_KEY")
    with open(path, "rb") as f:
        r = requests.post("https://api.groq.com/openai/v1/audio/transcriptions",
                          headers={"Authorization": f"Bearer {key}"},
                          files={"file": (path.name, f)},
                          data={"model": "whisper-large-v3", "language": LANG},
                          timeout=(10, 120))
    r.raise_for_status()
    return r.json().get("text", "").strip(), 0.0


def _openai(path: Path):
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("set OPENAI_API_KEY")
    with open(path, "rb") as f:
        r = requests.post("https://api.openai.com/v1/audio/transcriptions",
                          headers={"Authorization": f"Bearer {key}"},
                          files={"file": (path.name, f)},
                          data={"model": "gpt-4o-transcribe", "language": LANG},
                          timeout=(10, 120))
    r.raise_for_status()
    return r.json().get("text", "").strip(), 0.0


def _deepgram(path: Path):
    key = os.environ.get("DEEPGRAM_API_KEY", "").strip()
    if not key:
        raise RuntimeError("set DEEPGRAM_API_KEY")
    with open(path, "rb") as f:
        r = requests.post("https://api.deepgram.com/v1/listen?model=nova-3&language=multi",
                          headers={"Authorization": f"Token {key}", "Content-Type": "audio/ogg"},
                          data=f.read(), timeout=(10, 120))
    r.raise_for_status()
    alt = r.json()["results"]["channels"][0]["alternatives"][0]
    return alt.get("transcript", "").strip(), 0.0


PROVIDERS = {"speechmatics": _speechmatics, "groq": _groq,
             "openai": _openai, "deepgram": _deepgram}


def _arabic_ratio(s: str) -> float:
    letters = [ch for ch in s if ch.isalpha()]
    if not letters:
        return 0.0
    ar = sum(1 for ch in letters if "؀" <= ch <= "ۿ")
    return ar / len(letters)


def _problems(text: str, dur: float, err: str) -> list:
    p = []
    if err:
        p.append(f"error: {err}")
        return p
    if not text.strip():
        p.append("empty transcript")
        return p
    if dur and len(text) < dur * 1.5:  # ~1.5 chars/sec is implausibly sparse speech
        p.append(f"suspiciously short ({len(text)} chars for {dur:.0f}s)")
    if _arabic_ratio(text) < 0.5 and LANG == "ar":
        p.append(f"mostly non-Arabic ({_arabic_ratio(text):.0%} Arabic) - wrong lang / encoding?")
    return p


def run(provider: str):
    fn = PROVIDERS[provider]
    files = sorted(AUDIO.glob("*.ogg"))
    if not files:
        sys.exit(f"no audio in {AUDIO}")
    out = []
    print(f"== {provider}: {len(files)} clips ==")
    for f in files:
        t0 = time.time()
        text, dur, err = "", 0.0, ""
        try:
            text, dur = fn(f)
        except Exception as e:
            err = str(e)[:120]
        latency = round(time.time() - t0, 1)
        probs = _problems(text, dur, err)
        out.append({"id": f.stem, "provider": provider, "text": text,
                    "chars": len(text), "duration_s": dur, "latency_s": latency,
                    "ok": not err and bool(text.strip()), "error": err, "problems": probs})
        flag = " ".join(probs) if probs else "ok"
        print(f"  {f.stem[:18]:18s} {len(text):>4}ch {dur:>4.0f}s {latency:>5.1f}s  {flag}")
    (RESULTS / f"{provider}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    n_ok = sum(1 for r in out if r["ok"])
    n_prob = sum(1 for r in out if r["problems"])
    print(f"== {n_ok}/{len(out)} succeeded, {n_prob} flagged -> results/{provider}.json ==")
    return out


def compare(provider: str):
    base_f = RESULTS / f"{BASELINE_PROVIDER}.json"
    cand_f = RESULTS / f"{provider}.json"
    if not base_f.exists() or not cand_f.exists():
        sys.exit(f"need both {base_f.name} and {cand_f.name} (run each first)")
    base = {r["id"]: r for r in json.loads(base_f.read_text())}
    cand = {r["id"]: r for r in json.loads(cand_f.read_text())}
    print(f"== {provider} vs {BASELINE_PROVIDER} (char similarity; low = big divergence) ==")
    sims = []
    for cid, cr in cand.items():
        br = base.get(cid)
        if not br:
            continue
        sim = difflib.SequenceMatcher(None, br["text"], cr["text"]).ratio()
        sims.append(sim)
        mark = "  <-- divergent" if sim < 0.6 else ""
        print(f"  {cid[:18]:18s} sim={sim:.0%}{mark}")
    if sims:
        print(f"== mean similarity to baseline: {sum(sims)/len(sims):.0%} ==")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    if args[0] == "--compare":
        compare(args[1])
    elif args[0] in PROVIDERS:
        run(args[0])
    else:
        sys.exit(f"unknown provider {args[0]!r}; one of {list(PROVIDERS)} or --compare <p>")
