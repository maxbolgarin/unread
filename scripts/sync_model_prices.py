"""Propose price updates for `unread/ai/catalog.json` from OpenRouter's model list.

    uv run python scripts/sync_model_prices.py            # report only
    uv run python scripts/sync_model_prices.py --write    # also edit the catalog

OpenRouter's `GET /api/v1/models` is public (no key) and lists the
upstream list price for every OpenAI / Anthropic / Google model, so it
doubles as a price feed for our direct-provider rows. What this script
does NOT do on its own:

  * add new models — their role, label and vision / temperature flags
    need a human; they're listed in the report instead;
  * touch context windows or output caps — ours are deliberately lower
    in places (Claude's 16k non-streaming cap);
  * apply a row with `"manual_until": "YYYY-MM-DD"` before that date —
    used where we record a list rate while the provider runs a promo;
  * apply a jump of more than 5x either way, or a $0 price — those are
    far likelier a feed glitch or a `:free` variant than a real change.

The weekly `model-prices` workflow runs this with `--write` and opens a
PR with the report as its body. Merging it is what ships the change: a
running bot fetches `catalog.json` from `main` (see
`unread/ai/catalog_sync.py`).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

CATALOG = Path(__file__).resolve().parent.parent / "unread" / "ai" / "catalog.json"
OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
VENDORS = ("openai", "anthropic", "google")
NEW_MODEL_WINDOW_DAYS = 45
MAX_RATIO = 5.0

# (catalog key, OpenRouter pricing key)
PRICE_FIELDS = (("input", "prompt"), ("output", "completion"), ("cached", "input_cache_read"))


def openrouter_id(provider: str, model_id: str) -> str:
    """Our catalog id → OpenRouter's id for the same model."""
    if provider == "openrouter":
        return model_id
    if provider == "anthropic":
        # Anthropic dashes the version (claude-opus-5-5); OpenRouter dots it.
        model_id = re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", model_id)
    return f"{provider}/{model_id}"


def per_million(value: object) -> float | None:
    try:
        price = float(value) * 1_000_000  # OpenRouter quotes $ / token
    except (TypeError, ValueError):
        return None
    return round(price, 6)


def fetch_openrouter() -> dict[str, dict]:
    resp = httpx.get(OPENROUTER_MODELS, timeout=30)
    resp.raise_for_status()
    return {m["id"]: m for m in resp.json()["data"] if isinstance(m, dict) and "id" in m}


def sync(catalog: dict, remote: dict[str, dict], today: str) -> tuple[list[str], list[str], list[str]]:
    """Apply price changes in place. Returns (changed, skipped, missing) report lines."""
    changed: list[str] = []
    skipped: list[str] = []
    missing: list[str] = []
    for provider, rows in catalog["providers"].items():
        for row in rows:
            if row["role"] == "audio":
                continue  # $/minute — OpenRouter doesn't price these
            or_id = openrouter_id(provider, row["id"])
            model = remote.get(or_id)
            if model is None:
                missing.append(f"`{row['id']}` ({provider}) — no `{or_id}` on OpenRouter")
                continue
            pricing = model.get("pricing") or {}
            for ours, theirs in PRICE_FIELDS:
                new = per_million(pricing.get(theirs))
                old = float(row.get(ours, 0))
                if new is None or abs(new - old) < 1e-9:
                    continue
                line = f"`{row['id']}` ({provider}) {ours}: ${old:g} → ${new:g} / 1M"
                manual = row.get("manual_until", "")
                if manual and today <= manual:
                    skipped.append(f"{line} — held until {manual}: {row.get('note', 'manual price')}")
                elif new <= 0:
                    skipped.append(f"{line} — free/zero price, not applied")
                elif old > 0 and not (1 / MAX_RATIO <= new / old <= MAX_RATIO):
                    skipped.append(f"{line} — more than {MAX_RATIO:g}x, check by hand")
                else:
                    row[ours] = new
                    changed.append(line)
    return changed, skipped, missing


def new_models(catalog: dict, remote: dict[str, dict], now: float) -> list[str]:
    """Recent vendor models on OpenRouter that the catalog doesn't list."""
    known = {
        openrouter_id(provider, row["id"]) for provider, rows in catalog["providers"].items() for row in rows
    }
    cutoff = now - NEW_MODEL_WINDOW_DAYS * 86400
    lines = []
    for or_id, model in sorted(remote.items()):
        vendor = or_id.split("/", 1)[0]
        if vendor not in VENDORS or or_id in known or ":" in or_id:
            continue
        if float(model.get("created") or 0) < cutoff:
            continue
        pricing = model.get("pricing") or {}
        lines.append(
            f"`{or_id}` — {model.get('name', '')}: "
            f"${per_million(pricing.get('prompt'))} in / ${per_million(pricing.get('completion'))} out, "
            f"{model.get('context_length', '?')} ctx"
        )
    return lines


def render(changed: list[str], skipped: list[str], missing: list[str], fresh: list[str]) -> str:
    out = ["## Model catalog sync", ""]
    sections = (
        ("Price changes applied", changed),
        ("Not applied — review by hand", skipped),
        ("New models on OpenRouter (not added — need a role, label and flags)", fresh),
        ("Catalog rows OpenRouter doesn't list", missing),
    )
    for title, lines in sections:
        if lines:
            out += [f"### {title}", "", *(f"- {line}" for line in lines), ""]
    if not (changed or skipped or fresh):
        out.append("No changes.")
    out.append("Source: `GET https://openrouter.ai/api/v1/models`. Verify against the vendor pricing page.")
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--write", action="store_true", help="write applied changes to catalog.json")
    parser.add_argument("--report", type=Path, help="also write the Markdown report here")
    args = parser.parse_args()

    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    remote = fetch_openrouter()
    today = datetime.now(UTC).date().isoformat()
    changed, skipped, missing = sync(catalog, remote, today)
    report = render(changed, skipped, missing, new_models(catalog, remote, time.time()))
    print(report)
    if args.report:
        args.report.write_text(report, encoding="utf-8")

    if args.write and changed:
        catalog["updated"] = today
        # Fail before writing rather than ship a file the app would reject.
        sys.path.insert(0, str(CATALOG.parents[2]))
        from unread.ai.models import parse_catalog

        parse_catalog(catalog)
        CATALOG.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
