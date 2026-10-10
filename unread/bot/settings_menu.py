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
#   S_MODELS= show the model prompt (the next message is the model id)
#   S_MODEL = pick a model (arg = model id; empty = preset default)
#   S_KEY   = start the API-key prompt
# Per-chat settings — the plural opens the sub-menu, the singular applies
# the tapped value (empty = back to the default):
#   S_LANGS / S_LANG    = report language
#   S_FMTS  / S_FMT     = report format
#   S_PRSTS / S_PRST    = preset
#   S_WINS  / S_WIN     = TG period
#   S_MEDS  / S_MED     = TG extra media (arg = kind to toggle, or all/none)
#   S_CONF  = flip the confirm panel
#   S_CLOSE = drop the keyboard
CHAT_MENU_ACTIONS = frozenset({"S_LANGS", "S_FMTS", "S_PRSTS", "S_WINS", "S_MEDS"})
CHAT_SET_ACTIONS = frozenset({"S_LANG", "S_FMT", "S_PRST", "S_WIN", "S_MED", "S_CONF"})
AI_ACTIONS = frozenset({"S_PROVS", "S_PROV", "S_MODELS", "S_MODEL", "S_KEY"})
SETTINGS_ACTIONS = frozenset({"S_ROOT", "S_CLOSE"}) | AI_ACTIONS | CHAT_MENU_ACTIONS | CHAT_SET_ACTIONS


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


def _btn(label: str, action: str, panel_msg_id: int, value: str | None = None) -> Any:
    return Button.inline(label, encode_settings_callback(action, panel_msg_id, value))


def _back_row(panel_msg_id: int) -> list:
    return [_btn("⬅ Back", "S_ROOT", panel_msg_id)]


def _grid(buttons: list, per_row: int = 2) -> list[list]:
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


def _mark(label: str, active: bool) -> str:
    return f"✓ {label}" if active else label


def build_settings_menu(*, chat_state: dict, settings: Any, panel_msg_id: int) -> tuple[str, list]:
    """Root menu: what's set now, plus a button for every setting."""
    from unread.bot.runtime import STICKY_CONFIRM_DISABLED, render_settings_overview

    provider = _active_provider(settings)
    model = _active_model(settings) or "preset default"
    key_field = _KEY_FIELD.get(provider, "")
    key_value = ""
    if key_field:
        section, _, field = key_field.partition(".")
        key_value = getattr(getattr(settings, section, None), field, "") or ""

    text = render_settings_overview(chat_state, settings)
    text += (
        "\n\n🤖 **AI** __(whole bot)__\n"
        f"• Provider: `{provider}`\n"
        f"• Model: `{model}`\n"
        f"• API key: {mask_secret(key_value) if key_field else 'not needed'}"
        "\n\nTap a button to change a setting."
    )
    confirm_on = not chat_state.get(STICKY_CONFIRM_DISABLED)
    rows = [
        [_btn("🌐 Language", "S_LANGS", panel_msg_id), _btn("📄 Format", "S_FMTS", panel_msg_id)],
        [_btn("🎛 Preset", "S_PRSTS", panel_msg_id), _btn("📅 Period", "S_WINS", panel_msg_id)],
        [
            _btn("🖼 Media", "S_MEDS", panel_msg_id),
            _btn(f"{'✅' if confirm_on else '⬜'} Confirm", "S_CONF", panel_msg_id),
        ],
        [_btn("🔀 Provider", "S_PROVS", panel_msg_id), _btn("🧠 Model", "S_MODELS", panel_msg_id)],
        [_btn("🔑 API key", "S_KEY", panel_msg_id), _btn("✖ Close", "S_CLOSE", panel_msg_id)],
    ]
    return text, rows


def build_language_menu(*, chat_state: dict, settings: Any, panel_msg_id: int) -> tuple[str, list]:
    from unread.bot.runtime import LANGUAGE_LABELS, STICKY_REPORT_LANGUAGE

    sticky = (chat_state.get(STICKY_REPORT_LANGUAGE) or "").strip()
    cfg = settings.locale.report_language or settings.locale.language or "en"
    buttons = [
        _btn(_mark(name, code == sticky), "S_LANG", panel_msg_id, code)
        for code, name in LANGUAGE_LABELS.items()
    ]
    rows = _grid(buttons)
    rows.append([_btn(_mark(f"Default ({cfg})", not sticky), "S_LANG", panel_msg_id, "")])
    rows.append(_back_row(panel_msg_id))
    text = (
        "🌐 **Language**\n\n"
        "Used for analyses, report headings and transcripts.\n"
        "Not listed? Send `/lang <code>`, e.g. `/lang pl`."
    )
    return text, rows


def build_format_menu(*, chat_state: dict, settings: Any, panel_msg_id: int) -> tuple[str, list]:
    from unread.bot.runtime import FORMAT_LABELS, STICKY_REPORT_FORMAT

    sticky = (chat_state.get(STICKY_REPORT_FORMAT) or "").strip()
    cfg = (getattr(settings.bot, "report_format", "") or "pdf").strip()
    rows = [
        [_btn(_mark(label, value == sticky), "S_FMT", panel_msg_id, value)]
        for value, label in FORMAT_LABELS.items()
    ]
    rows.append(
        [_btn(_mark(f"Default ({FORMAT_LABELS.get(cfg, cfg)})", not sticky), "S_FMT", panel_msg_id, "")]
    )
    rows.append(_back_row(panel_msg_id))
    text = (
        "📄 **Report format**\n\n"
        "• **PDF document** — rendered, best on phones\n"
        "• **Markdown file** — the raw `.md`\n"
        "• **Message in chat** — the report itself, nothing to download"
    )
    return text, rows


def _visible_presets() -> list[tuple[str, str]]:
    """`(name, description)` of the presets a user may pick, sorted.

    Hidden presets are the ones routing picks on its own (single message,
    website, video); offering them as a sticky default would pin every
    run to one content type.
    """
    from unread.analyzer.prompts import get_presets

    try:
        presets = get_presets("en")
    except Exception:
        return []
    # Only the lead of each description: the full ones run to a sentence
    # and turn the menu into a wall.
    return sorted(
        (name, (p.description or "").split(" — ")[0].strip()) for name, p in presets.items() if not p.hidden
    )


def build_preset_menu(*, chat_state: dict, settings: Any, panel_msg_id: int) -> tuple[str, list]:
    from unread.bot.runtime import STICKY_PRESET

    sticky = (chat_state.get(STICKY_PRESET) or "").strip()
    presets = _visible_presets()
    buttons = [_btn(_mark(name, name == sticky), "S_PRST", panel_msg_id, name) for name, _ in presets]
    rows = _grid(buttons)
    rows.append([_btn(_mark("Auto (per content type)", not sticky), "S_PRST", panel_msg_id, "")])
    rows.append(_back_row(panel_msg_id))
    lines = ["🎛 **Preset**", "", "What kind of report to write:"]
    lines += [f"• `{name}` — {desc}" if desc else f"• `{name}`" for name, desc in presets]
    return "\n".join(lines), rows


def build_window_menu(*, chat_state: dict, settings: Any, panel_msg_id: int) -> tuple[str, list]:
    from unread.bot.runtime import STICKY_TG_WINDOW, TG_WINDOW_LABELS

    sticky = (chat_state.get(STICKY_TG_WINDOW) or "").strip()
    rows = [
        [_btn(_mark(label, value == sticky), "S_WIN", panel_msg_id, value)]
        for value, label in TG_WINDOW_LABELS.items()
    ]
    rows.append([_btn(_mark("Ask each time", not sticky), "S_WIN", panel_msg_id, "")])
    rows.append(_back_row(panel_msg_id))
    text = (
        "📅 **Period for Telegram chats**\n\n"
        "Which messages to read when you send a `t.me/...` link or a forward. "
        "With **Ask each time** I show the choice before every run."
    )
    return text, rows


def build_media_menu(*, chat_state: dict, settings: Any, panel_msg_id: int) -> tuple[str, list]:
    from unread.bot.runtime import ENRICH_LABELS, ENRICH_NAMES, STICKY_ENRICH_EXTRAS

    extras = set(chat_state.get(STICKY_ENRICH_EXTRAS) or ())
    buttons = []
    for name in ENRICH_NAMES:
        on = name in extras or bool(getattr(settings.enrich, name, False))
        locked = bool(getattr(settings.enrich, name, False))
        label = f"{'✅' if on else '⬜'} {ENRICH_LABELS[name]}{' 🔒' if locked else ''}"
        buttons.append(_btn(label, "S_MED", panel_msg_id, name))
    rows = _grid(buttons)
    rows.append([_btn("All", "S_MED", panel_msg_id, "all"), _btn("None", "S_MED", panel_msg_id, "none")])
    rows.append(_back_row(panel_msg_id))
    text = (
        "🖼 **Media in Telegram chats**\n\n"
        "Besides text, which attachments to read. Voice and video notes are "
        "always transcribed. Each extra costs more time and tokens.\n\n"
        "🔒 = turned on in the bot config, can't be switched off here."
    )
    return text, rows


CHAT_MENU_BUILDERS = {
    "S_LANGS": build_language_menu,
    "S_FMTS": build_format_menu,
    "S_PRSTS": build_preset_menu,
    "S_WINS": build_window_menu,
    "S_MEDS": build_media_menu,
}


def chat_setting_change(action: str, value: str | None, chat_state: dict) -> tuple[str, Any]:
    """Map a per-chat settings tap to `(sticky_key, new_value)`.

    A falsy `new_value` means "clear the sticky value". Pure, so the
    taps are testable without a Telegram connection; the caller persists.
    Raises ValueError on a value the menu never offers (stale or forged
    callback data).
    """
    from unread.bot.runtime import (
        ENRICH_NAMES,
        FORMAT_LABELS,
        STICKY_CONFIRM_DISABLED,
        STICKY_ENRICH_EXTRAS,
        STICKY_PRESET,
        STICKY_REPORT_FORMAT,
        STICKY_REPORT_LANGUAGE,
        STICKY_TG_WINDOW,
        TG_WINDOW_LABELS,
        parse_lang_value,
    )

    value = (value or "").strip()
    if action == "S_CONF":
        return STICKY_CONFIRM_DISABLED, not chat_state.get(STICKY_CONFIRM_DISABLED)
    if action == "S_LANG":
        if value and parse_lang_value(value)[0] is None:
            raise ValueError(f"bad language {value!r}")
        return STICKY_REPORT_LANGUAGE, value.lower()
    if action == "S_FMT":
        if value and value not in FORMAT_LABELS:
            raise ValueError(f"bad format {value!r}")
        return STICKY_REPORT_FORMAT, value
    if action == "S_WIN":
        if value and value not in TG_WINDOW_LABELS:
            raise ValueError(f"bad window {value!r}")
        return STICKY_TG_WINDOW, value
    if action == "S_PRST":
        if value and value not in {name for name, _ in _visible_presets()}:
            raise ValueError(f"bad preset {value!r}")
        return STICKY_PRESET, value
    if action == "S_MED":
        extras = set(chat_state.get(STICKY_ENRICH_EXTRAS) or ())
        if value == "all":
            extras = set(ENRICH_NAMES)
        elif value == "none":
            extras = set()
        elif value in ENRICH_NAMES:
            extras ^= {value}
        else:
            raise ValueError(f"bad media kind {value!r}")
        return STICKY_ENRICH_EXTRAS, extras
    raise ValueError(f"not a chat setting action: {action!r}")


def build_provider_menu(*, settings: Any, panel_msg_id: int) -> tuple[str, list]:
    """One row per provider, the active one marked."""
    active = _active_provider(settings)
    rows = []
    for name in _PROVIDERS:
        label = f"{'✓ ' if name == active else ''}{name}"
        rows.append([Button.inline(label, encode_settings_callback("S_PROV", panel_msg_id, name))])
    rows.append([Button.inline("⬅ Back", encode_settings_callback("S_ROOT", panel_msg_id))])
    return ("Pick the provider for analysis:", rows)


# Where to look up model ids for each provider. Linked from the model
# prompt instead of listing models as buttons: a hardcoded list goes stale
# within weeks, and OpenRouter alone has hundreds of models.
MODEL_LIST_URL: dict[str, str] = {
    "openai": "https://platform.openai.com/docs/models",
    "openrouter": "https://openrouter.ai/models",
    "anthropic": "https://docs.claude.com/en/docs/about-claude/models/overview",
    "google": "https://ai.google.dev/gemini-api/docs/models",
    "local": "https://ollama.com/library",
}

# Example ids, so the user sees the spelling each provider expects
# (OpenRouter's `vendor/model`, Anthropic's dashes).
_MODEL_ID_EXAMPLE: dict[str, str] = {
    "openai": "gpt-5.6-terra",
    "openrouter": "anthropic/claude-sonnet-5.5",
    "anthropic": "claude-sonnet-5-5",
    "google": "gemini-3.7-flash",
    "local": "llama3.1",
}


def build_model_menu(*, settings: Any, panel_msg_id: int) -> tuple[str, list]:
    """Prompt for a typed model id, with a link to the provider's list.

    The caller arms the prompt, so the next message is taken as the id.
    The only buttons are "preset default" (clears the override) and Back.
    """
    from unread.ai.models import find_model

    provider = _active_provider(settings)
    active_model = _active_model(settings)
    example = _MODEL_ID_EXAMPLE.get(provider, "model-name")
    url = MODEL_LIST_URL.get(provider, "")

    if active_model:
        info = find_model(active_model)
        price = (
            f" (${info.input_price:g}/${info.output_price:g} per 1M in/out)"
            if info and info.output_price
            else ""
        )
        current = f"Current model: `{active_model}`{price}."
    else:
        current = "Current model: **preset default**: each preset uses the model it pins."
    lines = [
        f"🧠 **Model for `{provider}`**",
        "",
        current,
        "",
        f"Send the model id as your next message, e.g. `{example}`.",
    ]
    if url:
        lines.append(f"Available models: {url}")
    lines += [
        "",
        "The id is used exactly as typed. Models I don't have prices for still work, "
        "but their runs show as $0 in cost reports.",
        "",
        "`/cancel` aborts.",
    ]
    rows = [
        [
            Button.inline(
                f"{'✓ ' if not active_model else ''}preset default",
                encode_settings_callback("S_MODEL", panel_msg_id, ""),
            ),
            Button.inline("⬅ Back", encode_settings_callback("S_ROOT", panel_msg_id)),
        ]
    ]
    return "\n".join(lines), rows


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
