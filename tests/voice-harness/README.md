# Voice-transcription harness

A fixed set of ~20 real voice notes to (a) sanity-check the current provider and
(b) compare a candidate provider before switching away from Speechmatics.

## Privacy

`audio/` and `results/` are **gitignored** - they are people's actual voices and
private conversation content, and must not be pushed. Only `run.py` and this
README are tracked. The harness therefore lives on the box; a fresh clone has the
runner but not the data (re-populate `audio/` from WhatsApp if needed).

## Use

```bash
# baseline (current provider):
SPEECHMATICS_API_KEYS=$(grep -oP 'SPEECHMATICS_API_KEYS=\K.*' /opt/whatsapp-mcp/.env) \
  python3 run.py speechmatics

# a candidate:
GROQ_API_KEY=...     python3 run.py groq
DEEPGRAM_API_KEY=... python3 run.py deepgram
OPENAI_API_KEY=...   python3 run.py openai

# compare candidate against the Speechmatics baseline:
python3 run.py --compare groq
```

`run.py <provider>` flags problems mechanically (empty output, errors, output too
short for the clip's duration, output that isn't mostly Arabic). `--compare`
reports per-clip character similarity to the baseline so a regression or a
materially different transcription is obvious. There is no human ground-truth, so
this is a coherence + divergence check, not a true WER.

## Baseline (Speechmatics, 2026-07-16)

20/20 clips transcribed, 0 problems flagged, ~5.4s mean latency, 4.6 min of audio
total. All coherent Egyptian; Arabic/English code-switching captured (English
rendered in Arabic script). This is the reference to beat.
