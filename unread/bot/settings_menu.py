"""Inline-keyboard settings menu for `unread bot`.

A container has its own `~/.unread`, so `unread settings` run on a laptop
never reaches a deployed bot. Without this the only way to repoint a
Docker bot at another provider is to SSH in or rebuild `.env.bot`.

Everything here is pure construction — `(text, buttons)` tuples and
callback encoding. The taps land in `BotApp._handle_callback`, which owns
the I/O. Same split as `unread/bot/confirm.py`, for the same reason: the
menu logic stays unit-testable without a Telegram connection.

Model choices are shown WITH prices. Picking blind is how you end up on a
flagship by accident and discover it on the bill.
"""

from __future__ import annotations

from typing import Any

from telethon import Button

# Callback actions. Telegram caps callback data at 64 bytes, and an
# OpenRouter model id (`openai/gpt-5.6-luna-pro:batch`) is 28 of them, so
# the action names stay short.
#   S_ROOT  = back to the root settings menu
#   S_PROVS = show the provider list
#   S_PROV  = pick a provider (arg = provider name)
#   S_MODELS= show the model list for the active provider
#   S_MODEL = pick a model (arg = model id; empty = preset default)
#   S_KEY   = start the API-key prompt
#   S_MODELC= start the custom-model-name prompt
SETTINGS_ACTIONS = frozenset({"S_ROOT", "S_PROVS", "S_PROV", "S_MODELS", "S_MODEL", "S_MODELC", "S_KEY"})


# Providers offered in the menu. Derived from the config allowlist rather
# than retyped: this list previously existed in four places (here,
# `config._VALID_AI_PROVIDERS`, `settings/commands._SLOT_PROVIDERS`, and
# `providers.make_chat_provider`'s dispatch), and a missed edit leaves the
# menu offering a provider whose key it can't store. Sorted for a stable
# button order, with openai/openrouter first since they're the common
# picks. `tests/test_bot_settings_menu.py` pins the two lists together.
def _provider_list() -> tuple[str, ...]:
    from unread.config import _VALID_AI_PROVIDERS

    preferred = ("openai", "openrouter")
    rest = sorted(set(_VALID_AI_PROVIDERS) - set(preferred))
    return tuple(p for p in preferred if p in _VALID_AI_PROVIDERS) + tuple(rest)


_PROVIDERS: tuple[str, ...] = _provider_list()

# Per-provider key field, for the "which key am I setting?" copy.
_KEY_FIELD: dict[str, str] = {
    "openai": "openai.api_key",
    "openrouter": "openrouter.api_key",
    "anthropic": "anthropic.api_key",
    "google": "google.api_key",
    "local": "",
}


def encode_settings_callback(action: str, panel_msg_id: int, value: str | None = None) -> bytes:
    """Pack `(action, panel_msg_id[, value])` into Telegram callback data."""
    if action not in SETTINGS_ACTIONS:
        raise ValueError(f"unknown settings action: {action!r}")
    if value is None:
        return f"{action}:{panel_msg_id}".encode()
    return f"{action}:{panel_msg_id}:{value}".encode()


def parse_settings_callback(data: bytes) -> tuple[str, int, str | None]:
    """Inverse of `encode_settings_callback`. Raises on anything unknown."""
    if not data:
        raise ValueError("empty settings callback")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as e:
        raise ValueError(f"non-ascii settings callback: {data!r}") from e
    parts = text.split(":", 2)
    if len(parts) < 2:
        raise ValueError(f"malformed settings callback: {data!r}")
    action = parts[0]
    if action not in SETTINGS_ACTIONS:
        raise ValueError(f"unknown settings action {action!r} in {data!r}")
    try:
        msg_id = int(parts[1])
    except ValueError as e:
        raise ValueError(f"bad panel id in {data!r}") from e
    return (action, msg_id, parts[2] if len(parts) == 3 else None)


def mask_secret(raw: str) -> str:
    """Render a key for display without showing it.

    Never returns the input unchanged — a short key must not round-trip
    through the mask intact, or "confirming" a key would print it.
    """
    raw = raw or ""
    if not raw:
        return "(not set)"
    if len(raw) <= 8:
        return "•" * len(raw)
    return f"{'•' * 8}{raw[-4:]}"


def _active_provider(settings: Any) -> str:
    return (
        getattr(settings.ai, "chat_provider", "") or getattr(settings.ai, "provider", "") or "openai"
    ).lower()


def _active_model(settings: Any) -> str:
    return getattr(settings.ai, "chat_model", "") or ""


def build_settings_menu(*, chat_state: dict, settings: Any, panel_msg_id: int) -> tuple[str, list]:
    """Root menu: what's set now, plus a way into each sub-menu."""
    from unread.bot.runtime import render_settings_overview

    provider = _active_provider(settings)
    model = _active_model(settings) or "(preset default)"
    key_field = _KEY_FIELD.get(provider, "")
    key_value = ""
    if key_field:
        section, _, field = key_field.partition(".")
        key_value = getattr(getattr(settings, section, None), field, "") or ""

    text = render_settings_overview(chat_state, settings)
    text += (
        "\n\n🤖 **AI**\n"
        f"• **Provider**: `{provider}`\n"
        f"• **Model**: `{model}`\n"
        f"• **API key**: `{mask_secret(key_value) if key_field else '(n/a)'}`"
    )
    rows = [
        [
            Button.inline("🔀 Provider", encode_settings_callback("S_PROVS", panel_msg_id)),
            Button.inline("🧠 Model", encode_settings_callback("S_MODELS", panel_msg_id)),
        ],
        [Button.inline("🔑 API key", encode_settings_callback("S_KEY", panel_msg_id))],
    ]
    return text, rows


def build_provider_menu(*, settings: Any, panel_msg_id: int) -> tuple[str, list]:
    """One row per provider, the active one marked."""
    active = _active_provider(settings)
    rows = []
    for name in _PROVIDERS:
        label = f"{'✓ ' if name == active else ''}{name}"
        rows.append([Button.inline(label, encode_settings_callback("S_PROV", panel_msg_id, name))])
    rows.append([Button.inline("⬅ Back", encode_settings_callback("S_ROOT", panel_msg_id))])
    return ("Pick the provider for analysis:", rows)


def build_model_menu(*, settings: Any, panel_msg_id: int) -> tuple[str, list]:
    """Chat-capable models for the active provider, with prices.

    Prices come from the catalog rather than being typed in, so the menu
    can't advertise a model the cost accounting doesn't know — an unpriced
    model reports $0 and makes a run look free.
    """
    from unread.ai.models import models_for_provider

    active_provider = _active_provider(settings)
    active_model = _active_model(settings)
    try:
        models = models_for_provider(active_provider, role="chat")
    except Exception:  # unknown provider name
        models = []

    rows = [
        [
            Button.inline(
                f"{'✓ ' if not active_model else ''}preset default",
                encode_settings_callback("S_MODEL", panel_msg_id, ""),
            )
        ]
    ]
    for info in models:
        mark = "✓ " if info.id == active_model else ""
        price = f" (${info.input_price:g}/${info.output_price:g})" if info.output_price else ""
        rows.append(
            [
                Button.inline(
                    f"{mark}{info.id}{price}",
                    encode_settings_callback("S_MODEL", panel_msg_id, info.id),
                )
            ]
        )
    # The catalog ages faster than releases ship, and `local` has no
    # catalog at all — a typed-in id is the escape hatch. An active model
    # the catalog doesn't list is shown here so it doesn't look unset.
    custom_active = bool(active_model) and all(info.id != active_model for info in models)
    custom_label = f"✓ ✏️ {active_model}" if custom_active else "✏️ Other model…"
    rows.append([Button.inline(custom_label, encode_settings_callback("S_MODELC", panel_msg_id))])
    rows.append([Button.inline("⬅ Back", encode_settings_callback("S_ROOT", panel_msg_id))])
    text = (
        f"Pick the model for `{active_provider}`.\n"
        "Prices are $ per 1M tokens (input/output). "
        "**preset default** lets each preset use the model it pins. "
        "**Other model…** lets you type any model id."
    )
    return text, rows


# Example ids for the custom-model prompt, so the user sees the spelling
# each provider expects (OpenRouter's `vendor/model`, Anthropic's dashes).
_MODEL_ID_EXAMPLE: dict[str, str] = {
    "openai": "gpt-5.6-terra",
    "openrouter": "anthropic/claude-sonnet-5.5",
    "anthropic": "claude-sonnet-5-5",
    "google": "gemini-3.7-flash",
    "local": "llama3.1",
}


def custom_model_prompt_text(provider: str) -> str:
    """Copy for the "type a model id" prompt."""
    example = _MODEL_ID_EXAMPLE.get(provider, "model-name")
    return (
        f"Send the **{provider}** model id as your next message, "
        f"e.g. `{example}`.\n\n"
        "It's used as-is, so copy it exactly from the provider's model list. "
        "Models I don't have prices for still work, but their runs show as $0 in cost reports.\n\n"
        "`/cancel` aborts."
    )


def looks_like_model_id(raw: str) -> bool:
    """Cheap sanity check on a typed model id.

    Provider ids are short, whitespace-free tokens (`gpt-5.6-luna`,
    `anthropic/claude-opus-5.5`, `qwen2.5:14b`). Anything else (a link,
    a sentence) is a message that landed in the prompt by accident, and
    storing it would break every later run.
    """
    if not raw or len(raw) > 128:
        return False
    if any(ch.isspace() for ch in raw):
        return False
    lowered = raw.lower()
    if lowered.startswith(("http://", "https://", "t.me/", "www.", "/")):
        return False
    return all(ch.isalnum() or ch in "-_.:/@+" for ch in raw)


def key_prompt_text(provider: str) -> str:
    """Copy for the API-key prompt, including the deletion promise."""
    field = _KEY_FIELD.get(provider, "")
    if not field:
        return (
            f"`{provider}` needs no API key — set `local.base_url` instead "
            "(via config or `UNREAD_AI_CHAT_PROVIDER`)."
        )
    return (
        f"Send the **{provider}** API key as your next message.\n\n"
        "⚠️ I delete your message as soon as I've stored the key, so it "
        "doesn't sit in this chat's history. It still passes through "
        "Telegram's servers on the way here — if that's not acceptable, "
        "set it in `.env.bot` on the host instead.\n\n"
        "`/cancel` aborts."
    )


def secret_key_for_provider(provider: str) -> str:
    """`secrets` table key for this provider, or "" when it has none."""
    return _KEY_FIELD.get((provider or "").lower(), "")
