"""Per-provider model catalogue.

Single source of truth for the settings picker, default pricing, and
"is this model supported by this provider" checks. The rows live in
`catalog.json` (checked against the provider docs — OpenAI:
platform.openai.com/docs/pricing, Anthropic:
docs.claude.com/en/docs/about-claude/models, Google: ai.google.dev/pricing).
A running bot re-fetches that file from the repo (see
`unread.ai.catalog_sync`), and a weekly workflow proposes price changes
to it from OpenRouter's public model list
(`scripts/sync_model_prices.py`).

Adding a model to `catalog.json` makes it appear in `unread settings` (under the
matching provider) AND seeds a default pricing row for `unread stats`.
The user can still pick a custom model name at the picker — the
registry is a curated list, not a hard allow-list.

`role`:
  - `chat`   — a "smart" model used for the final reduce / answers.
  - `filter` — a cheap-and-fast model for per-chunk map / rerank.
  - `audio`  — Whisper-style transcription (OpenAI-only today).
  - `vision` — image understanding for `--enrich=image` (OpenAI-only).

Cached-input prices reflect the provider's prompt-cache *read* rate
(Anthropic: 0.1× input, OpenAI: 0.1× input, Google: ~0.25× input). They
are estimates — the orchestrator records the actual cached_tokens count
returned by the API, so cost reports stay accurate even if the multiplier
moves.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ModelInfo:
    id: str
    label: str
    role: str  # "chat" | "filter" | "audio" | "vision"
    input_price: float = 0.0  # $ / 1M tokens (or $ / minute for audio)
    cached_price: float = 0.0  # $ / 1M tokens
    output_price: float = 0.0  # $ / 1M tokens
    # Effective input-context window in tokens. 0 means "unknown — use the
    # 128k fallback". Wired through `model_context_window()` so the
    # chunker sizes prompts correctly for Claude / Gemini, not just
    # OpenAI.
    context_window: int = 0
    # Hard cap on a single completion's `max_tokens` (output tokens).
    # 0 means "unknown — use the orchestrator's 16k fallback". Used by
    # `analyzer.openai_client.chat_complete` to bound the truncation
    # retry: bumping above the per-model cap just guarantees a 4xx after
    # the user already paid for the prompt (e.g. Gemini Flash caps at
    # 8192 — doubling 4000→8000 is fine, doubling 5000→10000 is not).
    # Audio / vision-only entries leave this at 0; they're never used
    # for chat completions.
    max_output_tokens: int = 0
    # OpenAI reasoning-class models (o-series, gpt-5 family, including
    # the mini / nano variants) reject any `temperature` other than the
    # default `1.0` with a 400. The OpenAI adapter drops `temperature`
    # from the request when this flag is True. Anthropic / Google models
    # leave this False; their reasoning toggles are different shapes
    # (extended thinking, etc.) and don't constrain `temperature`.
    reasoning: bool = False
    # Accepts image input — folds the model into the vision picker
    # alongside the explicit `role="vision"` rows.
    vision: bool = False


# ----------------------- Catalog data ------------------------------------
#
# The rows live in `catalog.json` next to this file, so the same file can
# be shipped in the wheel AND fetched from the repo by a running bot
# (`unread.ai.catalog_sync`) — a price change or a new model reaches a
# long-lived deployment without a release. Per-row `note` fields carry
# the caveats a flat row can't express (promo vs list rate, tiered
# pricing past a prompt-size threshold).
#
# `reasoning=True` means "rejects a custom `temperature`" (o-series, the
# gpt-5+ family, Claude Opus 4.7 and every Claude 5.x). `max_output_tokens`
# stays at 16k on Claude even where the model allows 128k: the adapter
# doesn't stream, and the SDK refuses a non-streaming request with a very
# large `max_tokens`. OpenRouter spells Claude versions with a DOT
# (`claude-opus-5.5`), unlike Anthropic's own API (`claude-opus-5-5`).

# Bumped only on a breaking change to the file's shape. New optional
# fields don't bump it — older clients ignore keys they don't know.
CATALOG_SCHEMA = 1

_ROLES: frozenset[str] = frozenset({"chat", "filter", "audio", "vision"})
# Ordered for UI consistency; see `supported_providers()`.
_PROVIDERS: tuple[str, ...] = ("openai", "anthropic", "google", "openrouter", "local")
_BUNDLED_CATALOG = Path(__file__).with_name("catalog.json")


class CatalogError(ValueError):
    """A catalog document that doesn't match the expected shape."""


def _num(row: dict, key: str, kind: type, ceiling: float) -> float | int:
    value = row.get(key, 0)
    # bool is an int subclass; `"reasoning": true` in a price slot is a typo.
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise CatalogError(f"{row.get('id')!r}: {key} must be a number")
    if kind is int and not float(value).is_integer():
        raise CatalogError(f"{row.get('id')!r}: {key} must be an integer")
    if not 0 <= value <= ceiling:
        raise CatalogError(f"{row.get('id')!r}: {key}={value} out of range")
    return kind(value)


def _parse_row(row: object) -> ModelInfo:
    if not isinstance(row, dict):
        raise CatalogError("model row must be an object")
    model_id, label, role = row.get("id"), row.get("label", ""), row.get("role")
    if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 200:
        raise CatalogError(f"bad model id: {model_id!r}")
    if not isinstance(label, str) or len(label) > 200:
        raise CatalogError(f"{model_id!r}: bad label")
    if role not in _ROLES:
        raise CatalogError(f"{model_id!r}: unknown role {role!r}")
    return ModelInfo(
        model_id,
        label or model_id,
        role,
        _num(row, "input", float, 10_000),
        _num(row, "cached", float, 10_000),
        _num(row, "output", float, 10_000),
        context_window=_num(row, "context_window", int, 100_000_000),
        max_output_tokens=_num(row, "max_output_tokens", int, 100_000_000),
        reasoning=row.get("reasoning") is True,
        vision=row.get("vision") is True,
    )


def parse_catalog(data: object) -> tuple[str, dict[str, tuple[ModelInfo, ...]]]:
    """Validate a catalog document → `(updated, {provider: rows})`.

    Strict on purpose: a remote file that fails any check is dropped
    whole, and the previous catalog stays in force. Half-applying a
    broken file could leave a model priced at $0.
    """
    if not isinstance(data, dict):
        raise CatalogError("catalog must be a JSON object")
    if data.get("schema") != CATALOG_SCHEMA:
        raise CatalogError(f"unsupported catalog schema {data.get('schema')!r}")
    updated = data.get("updated")
    if not isinstance(updated, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", updated):
        raise CatalogError(f"bad 'updated' date: {updated!r}")
    providers = data.get("providers")
    if not isinstance(providers, dict):
        raise CatalogError("'providers' must be an object")
    registry: dict[str, tuple[ModelInfo, ...]] = {}
    for provider, rows in providers.items():
        if provider not in _PROVIDERS:
            continue  # a provider this version can't route to anyway
        if not isinstance(rows, list):
            raise CatalogError(f"{provider}: rows must be a list")
        registry[provider] = tuple(_parse_row(r) for r in rows)
    return updated, registry


def _load_bundled() -> tuple[str, dict[str, tuple[ModelInfo, ...]]]:
    updated, registry = parse_catalog(json.loads(_BUNDLED_CATALOG.read_text(encoding="utf-8")))
    for provider in _PROVIDERS:
        registry.setdefault(provider, ())
    return updated, registry


_BUNDLED_UPDATED, _BUNDLED = _load_bundled()
# Live view: the bundled catalog, possibly overlaid by a newer remote one.
# Replaced wholesale (never mutated) so a concurrent reader sees either
# the old or the new catalog, not a mix.
_REGISTRY: dict[str, tuple[ModelInfo, ...]] = _BUNDLED
_overlay_updated: str = ""


def apply_overlay(updated: str, overlay: dict[str, tuple[ModelInfo, ...]]) -> bool:
    """Layer a fetched catalog over the bundled one. Returns True if applied.

    Ignored when it's older than what this install shipped with — a
    cached download from before an upgrade must not roll prices back.
    Overlay rows win by id and set the picker order; bundled rows the
    overlay dropped are kept, so a config still pinning a retired model
    keeps its pricing.
    """
    global _REGISTRY, _overlay_updated
    if updated < _BUNDLED_UPDATED:
        return False
    merged: dict[str, tuple[ModelInfo, ...]] = {}
    for provider, bundled in _BUNDLED.items():
        rows = overlay.get(provider, ())
        ids = {m.id for m in rows}
        merged[provider] = tuple(rows) + tuple(m for m in bundled if m.id not in ids)
    _REGISTRY = merged
    _overlay_updated = updated
    return True


def reset_overlay() -> None:
    """Drop any applied overlay (tests, or a disabled refresh)."""
    global _REGISTRY, _overlay_updated
    _REGISTRY = _BUNDLED
    _overlay_updated = ""


_cache_checked = False


def _pools() -> dict[str, tuple[ModelInfo, ...]]:
    """The catalog in force; picks up the last downloaded copy on first use.

    Reading the cache here rather than at import keeps importing this
    module free of disk access, and covers every entry point (CLI, bot,
    tests) without each one remembering to load it.
    """
    global _cache_checked
    if not _cache_checked:
        _cache_checked = True
        try:
            from unread.ai.catalog_sync import load_cached

            load_cached()
        except Exception:  # a broken cache must never break a lookup
            pass
    return _REGISTRY


def catalog_updated() -> str:
    """Date of the catalog currently in force (overlay if applied)."""
    _pools()
    return _overlay_updated or _BUNDLED_UPDATED


def models_for_provider(provider: str, *, role: str | None = None) -> list[ModelInfo]:
    """Return the catalog for `provider`, optionally filtered by role.

    Unknown providers return an empty list — the caller falls back to a
    Custom-only picker. Role filtering is order-preserving so the picker
    presents models in the listed-here sequence (flagship → cheap).

    For chat / filter the filter is *advisory*: when `role="chat"` we
    include models tagged `filter` too, because users sometimes want to
    pin a budget model to the chat slot. The reverse isn't true — when
    asking for filter models we hide flagships to keep the picker focused
    on cheap options.

    For `role="vision"` we include the explicit `role="vision"` entry
    (e.g. OpenAI's gpt-4o-mini) plus every chat-class model flagged
    `vision` in the catalog. This avoids
    duplicating Anthropic / Google catalog entries just to expose them
    under the vision picker.
    """
    pool = _pools().get(provider.strip().lower(), ())
    if role is None:
        return list(pool)
    if role == "chat":
        return [m for m in pool if m.role in {"chat", "filter"}]
    if role == "filter":
        return [m for m in pool if m.role == "filter"]
    if role == "vision":
        return [m for m in pool if m.role == "vision" or m.vision]
    return [m for m in pool if m.role == role]


def all_known_models() -> list[ModelInfo]:
    """Flat list of every (provider, model) pair we ship pricing for."""
    seen: dict[str, ModelInfo] = {}
    for pool in _pools().values():
        for m in pool:
            # Same id can appear under multiple providers (e.g. OpenRouter
            # mirrors). Keep the first occurrence so vanilla OpenAI rows
            # win over `openai/...` aliases when both exist.
            seen.setdefault(m.id, m)
    return list(seen.values())


def find_model(model_id: str) -> ModelInfo | None:
    """Look up a model by id across every provider's catalog.

    An OpenRouter-style `vendor/model` id the catalog doesn't list falls
    back to the vendor's own row (`openai/gpt-5.5` → `gpt-5.5`), with
    OpenRouter's dotted Claude versions folded to Anthropic's dashes
    (`anthropic/claude-opus-4.7` → `claude-opus-4-7`). That keeps pricing
    and context windows right for ids typed in by hand and for older
    OpenRouter rows the picker no longer offers.
    """
    for pool in _pools().values():
        for m in pool:
            if m.id == model_id:
                return m
    vendor, sep, bare = (model_id or "").partition("/")
    pool = _pools().get(vendor.lower(), ()) if sep and vendor.lower() != "openrouter" else ()
    candidates = (bare, bare.replace(".", "-")) if vendor.lower() == "anthropic" else (bare,)
    for candidate in candidates:
        for m in pool:
            if m.id == candidate:
                return m
    return None


def provider_for_model(model_id: str) -> str | None:
    """Return the canonical provider name for ``model_id`` (or None).

    Resolution order (vendor prefix wins over catalog hit so OpenRouter
    aliases like ``anthropic/claude-opus-4-7`` route to the underlying
    vendor's tokenizer / safety margin, not to OpenRouter's bucket):

      1. ``vendor/...`` prefix (OpenRouter convention) — peel off the vendor.
      2. Exact catalog hit — return the provider whose pool contains it.
      3. Heuristic on the bare id — ``claude*``/``anthropic*`` → anthropic,
         ``gemini*``/``google*`` → google, ``gpt*``/``o1*``/``o3*``/``o4*``
         → openai, otherwise None.

    Used by token counting to apply a per-provider safety margin without
    requiring the user to maintain a registry entry for every Claude /
    Gemini variant they might pass on the CLI.
    """
    raw = (model_id or "").strip()
    if not raw:
        return None
    lower = raw.lower()
    # 1. vendor/...  prefix — OpenRouter style. The semantic provider
    # is the vendor (the model behind the OpenRouter facade), not the
    # router itself, because token counting wants the underlying
    # tokenizer's safety margin.
    if "/" in raw:
        vendor = raw.split("/", 1)[0].lower()
        if vendor in _PROVIDERS or vendor in {"anthropic", "google", "openai"}:
            return vendor
    # 2. Exact catalog match
    for provider, pool in _pools().items():
        if any(m.id.lower() == lower for m in pool):
            return provider
    # 3. Heuristics
    if lower.startswith(("claude", "anthropic")):
        return "anthropic"
    if lower.startswith(("gemini", "google")):
        return "google"
    if lower.startswith(("gpt", "o1", "o3", "o4", "chatgpt")):
        return "openai"
    return None


# Claude families that reject `temperature` (and every other sampling
# parameter) with a 400. Matched against the bare id with dots folded to
# dashes, so OpenRouter's `anthropic/claude-opus-5.5` and Anthropic's
# `claude-opus-5-5` both hit — and so does a custom id the catalog has
# never heard of.
_SAMPLING_LOCKED_CLAUDE_PREFIXES: tuple[str, ...] = (
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-5",
    "claude-fable-",
    "claude-mythos-",
)


def rejects_temperature(model_id: str) -> bool:
    """True when `model_id` 400s on a custom `temperature`.

    The catalog's `reasoning` flag is the source of truth; the name-shape
    fallback covers ids typed in by hand (the bot's "Custom…" model) so a
    newer release doesn't fail every run until someone edits this file.
    Dropping `temperature` for a model that would have accepted it is
    harmless (the server default applies); sending it to one that doesn't
    is a hard failure.
    """
    info = find_model(model_id)
    if info is not None and info.reasoning:
        return True
    name = (model_id or "").rsplit("/", 1)[-1].lower().replace(".", "-")
    if name.startswith(("o1", "o3", "o4")) or re.match(r"gpt-([5-9]|\d{2,})\b", name):
        return True
    return name.startswith(_SAMPLING_LOCKED_CLAUDE_PREFIXES)


def supported_providers() -> tuple[str, ...]:
    """Provider names with a curated catalog (ordered for UI consistency)."""
    return _PROVIDERS


# Per-provider safety multiplier applied to tiktoken counts. tiktoken
# uses OpenAI's BPE encodings; Claude and Gemini tokenize the same
# text into ~10-25% more tokens (different vocab, different merges).
# Bumping the count keeps the chunker on the safe side of provider
# context limits without resorting to network calls in the hot loop.
# OpenAI / OpenRouter / local: 1.0 (tiktoken is exact for OpenAI; for
# OpenRouter we trust the underlying-model heuristic, which already
# routes claude/gemini ids to the anthropic/google bucket).
PROVIDER_TOKEN_SAFETY_MARGIN: dict[str, float] = {
    "openai": 1.0,
    "openrouter": 1.0,
    "local": 1.0,
    "anthropic": 1.25,
    "google": 1.25,
}
