"""Hosted embedding client for semantic tool retrieval (optional, graceful).

find_tool ranks lexically by default. Lexical search has a hard ceiling on
paraphrased/slang queries (an eval on an independent 191-case set showed
recall@8 ~75%), because a query that shares no vocabulary with a tool's name or
description is invisible to keyword matching. True semantic retrieval fixes that,
but the box has no GPU and forbids local ML models (see CLAUDE.md), so we use a
HOSTED embedding API - the same "reach for a hosted API before a local model"
rule the voice transcription follows.

This module is provider-agnostic and entirely optional: with no key configured,
embed() returns None and find_tool stays purely lexical (zero behaviour change).
Add a key and it upgrades to hybrid lexical+semantic ranking automatically.

Providers (auto-detected by which key is present; override with
WHATSAPP_MCP_EMBED_PROVIDER = voyage | openai | gemini | none):
  - Voyage AI   VOYAGE_API_KEY   (default model voyage-3.5-lite) - Anthropic's
                recommended embeddings; generous free tier.
  - OpenAI      OPENAI_API_KEY   (default model text-embedding-3-small)
  - Gemini      GEMINI_API_KEY   (default model text-embedding-004)

All calls are I/O-bound HTTPS (cheap on this box). Query embedding is one small
call per find_tool invocation; the 92 tool docs are embedded once and cached to
disk (see toolsearch.py), so startup cost is a single batch call, amortised.
"""
from __future__ import annotations

import logging
import os
import time

import requests

log = logging.getLogger("whatsapp_mcp.embed")

_TIMEOUT = float(os.environ.get("WHATSAPP_MCP_EMBED_TIMEOUT", "15"))
_RETRIES = 2


def _provider() -> tuple[str, str, str] | None:
    """Resolve (provider, api_key, model) from env, or None if unconfigured."""
    forced = os.environ.get("WHATSAPP_MCP_EMBED_PROVIDER", "auto").strip().lower()
    if forced == "none":
        return None

    voyage = os.environ.get("VOYAGE_API_KEY", "").strip()
    openai = os.environ.get("OPENAI_API_KEY", "").strip()
    gemini = os.environ.get("GEMINI_API_KEY", "").strip() or os.environ.get("GOOGLE_API_KEY", "").strip()

    model_override = os.environ.get("WHATSAPP_MCP_EMBED_MODEL", "").strip()

    def pick(p):
        if p == "voyage" and voyage:
            return ("voyage", voyage, model_override or "voyage-3.5-lite")
        if p == "openai" and openai:
            return ("openai", openai, model_override or "text-embedding-3-small")
        if p == "gemini" and gemini:
            return ("gemini", gemini, model_override or "text-embedding-004")
        return None

    if forced in ("voyage", "openai", "gemini"):
        return pick(forced)
    # auto: first key that exists, in preference order
    for p in ("voyage", "openai", "gemini"):
        got = pick(p)
        if got:
            return got
    return None


def available() -> bool:
    return _provider() is not None


def provider_name() -> str:
    p = _provider()
    return p[0] if p else "none"


def _post(url: str, headers: dict, payload: dict) -> dict:
    last = None
    for attempt in range(_RETRIES + 1):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=_TIMEOUT)
            if r.status_code == 200:
                return r.json()
            # 429/5xx: transient, back off and retry; else fail fast.
            if r.status_code in (429, 500, 502, 503, 504) and attempt < _RETRIES:
                time.sleep(0.5 * (attempt + 1))
                last = f"{r.status_code}: {r.text[:200]}"
                continue
            raise RuntimeError(f"embed HTTP {r.status_code}: {r.text[:200]}")
        except requests.RequestException as e:
            last = str(e)
            if attempt < _RETRIES:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise RuntimeError(f"embed request failed: {last}") from e
    raise RuntimeError(f"embed failed after retries: {last}")


def _embed_voyage(texts, key, model, input_type):
    body = {"model": model, "input": texts}
    if input_type:
        body["input_type"] = input_type  # "query" or "document" sharpens Voyage
    data = _post(
        "https://api.voyageai.com/v1/embeddings",
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        body,
    )
    return [d["embedding"] for d in data["data"]]


def _embed_openai(texts, key, model, _input_type):
    data = _post(
        "https://api.openai.com/v1/embeddings",
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        {"model": model, "input": texts},
    )
    return [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]


def _embed_gemini(texts, key, model, input_type):
    # Gemini batchEmbedContents: one request, N contents.
    task = "RETRIEVAL_QUERY" if input_type == "query" else "RETRIEVAL_DOCUMENT"
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
           f":batchEmbedContents?key={key}")
    reqs = [{"model": f"models/{model}",
             "content": {"parts": [{"text": t}]},
             "taskType": task} for t in texts]
    data = _post(url, {"Content-Type": "application/json"}, {"requests": reqs})
    return [e["values"] for e in data["embeddings"]]


_DISPATCH = {"voyage": _embed_voyage, "openai": _embed_openai, "gemini": _embed_gemini}


def embed(texts: list[str], input_type: str = "document") -> list[list[float]] | None:
    """Embed a batch of texts. Returns a list of vectors, or None if no provider
    is configured or the call fails (caller falls back to lexical). input_type is
    'query' or 'document' - some providers use it to sharpen retrieval."""
    if not texts:
        return []
    prov = _provider()
    if not prov:
        return None
    name, key, model = prov
    try:
        vecs = _DISPATCH[name](texts, key, model, input_type)
        if len(vecs) != len(texts):
            log.warning("embed: provider returned %d vectors for %d texts", len(vecs), len(texts))
            return None
        return vecs
    except Exception as e:
        log.warning("embed via %s failed (%s); falling back to lexical", name, e)
        return None
