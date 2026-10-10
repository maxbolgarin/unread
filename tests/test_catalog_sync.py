"""Model catalog auto-update: validation, overlay, remote refresh, price sync script.

The failure this guards against is a running bot pricing every run with
the catalog of its release day — and the opposite risk, a bad remote
file (or a stale cached one) silently replacing good prices.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from unread.ai import catalog_sync
from unread.ai import models as M

BUNDLED = json.loads(M._BUNDLED_CATALOG.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _clean_overlay():
    M.reset_overlay()
    M._cache_checked = True  # tests opt in to the cache explicitly
    yield
    # The cache lives in the session-wide test UNREAD_HOME; leaving it
    # behind would reprice every later test's catalog lookups.
    catalog_sync.cache_path().unlink(missing_ok=True)
    M.reset_overlay()
    M._cache_checked = False


def _doc(updated: str = "2099-01-01", **row_overrides) -> dict:
    doc = copy.deepcopy(BUNDLED)
    doc["updated"] = updated
    doc["providers"]["openai"][0].update(row_overrides)
    return doc


def test_bundled_catalog_ids_are_unique_per_provider() -> None:
    for provider, rows in BUNDLED["providers"].items():
        ids = [r["id"] for r in rows]
        assert len(ids) == len(set(ids)), provider


@pytest.mark.parametrize(
    "bad",
    [
        {"schema": 99},
        {"updated": "yesterday"},
        {"providers": []},
    ],
)
def test_parse_rejects_bad_document(bad) -> None:
    doc = _doc()
    doc.update(bad)
    with pytest.raises(M.CatalogError):
        M.parse_catalog(doc)


@pytest.mark.parametrize(
    "row",
    [
        {"role": "embedding"},
        {"input": -1},
        {"input": "2.0"},
        {"output": True},
        {"context_window": 1.5},
        {"id": ""},
    ],
)
def test_parse_rejects_bad_row(row) -> None:
    with pytest.raises(M.CatalogError):
        M.parse_catalog(_doc(**row))


def test_parse_ignores_unknown_keys_and_providers() -> None:
    doc = _doc(future_field=1)
    doc["providers"]["mistral"] = [{"id": "x", "role": "chat"}]
    _updated, registry = M.parse_catalog(doc)
    assert "mistral" not in registry


def test_overlay_updates_prices_and_keeps_rows_it_dropped() -> None:
    doc = _doc(input=123.0)
    first = doc["providers"]["openai"][0]["id"]
    dropped = doc["providers"]["openai"].pop()["id"]
    assert M.apply_overlay(*M.parse_catalog(doc))
    assert M.find_model(first).input_price == 123.0
    assert M.find_model(dropped) is not None
    assert M.catalog_updated() == "2099-01-01"


def test_overlay_older_than_bundled_is_ignored() -> None:
    first = BUNDLED["providers"]["openai"][0]["id"]
    assert not M.apply_overlay(*M.parse_catalog(_doc(updated="2000-01-01", input=123.0)))
    assert M.find_model(first).input_price != 123.0


def test_overlay_can_add_a_model() -> None:
    doc = _doc()
    doc["providers"]["openai"].insert(0, {"id": "gpt-7", "label": "GPT-7", "role": "chat", "input": 1})
    M.apply_overlay(*M.parse_catalog(doc))
    assert M.find_model("gpt-7").input_price == 1.0
    assert M.models_for_provider("openai")[0].id == "gpt-7"


def _serve(monkeypatch, handler) -> None:
    real = httpx.AsyncClient

    def client(**kwargs):
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(catalog_sync.httpx, "AsyncClient", client)


def test_refresh_applies_and_caches(monkeypatch) -> None:
    body = json.dumps(_doc(input=77.0)).encode()
    _serve(monkeypatch, lambda req: httpx.Response(200, content=body))
    catalog_sync.cache_path().unlink(missing_ok=True)

    assert asyncio.run(catalog_sync.refresh("https://example.test/catalog.json")) == "2099-01-01"
    assert catalog_sync.cache_path().read_bytes() == body

    # A fresh process picks the cached copy up on first lookup.
    M.reset_overlay()
    M._cache_checked = False
    assert M.find_model(BUNDLED["providers"]["openai"][0]["id"]).input_price == 77.0


def test_bad_download_keeps_previous_cache(monkeypatch) -> None:
    good = json.dumps(_doc(input=77.0)).encode()
    catalog_sync.cache_path().parent.mkdir(parents=True, exist_ok=True)
    catalog_sync.cache_path().write_bytes(good)
    _serve(monkeypatch, lambda req: httpx.Response(200, content=json.dumps(_doc(input=-5)).encode()))

    assert asyncio.run(catalog_sync.refresh("https://example.test/catalog.json")) is None
    assert catalog_sync.cache_path().read_bytes() == good


def test_http_error_is_quiet(monkeypatch) -> None:
    _serve(monkeypatch, lambda req: httpx.Response(404))
    assert asyncio.run(catalog_sync.refresh("https://example.test/catalog.json")) is None


def test_refresh_refuses_plain_http() -> None:
    assert asyncio.run(catalog_sync.refresh("http://example.test/catalog.json")) is None


# ----------------------------- scripts/sync_model_prices.py -----------------


def _load_script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "sync_model_prices.py"
    spec = importlib.util.spec_from_file_location("sync_model_prices", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    ("provider", "model_id", "expected"),
    [
        ("openai", "gpt-6-sol", "openai/gpt-6-sol"),
        ("anthropic", "claude-opus-5-5", "anthropic/claude-opus-5.5"),
        ("anthropic", "claude-haiku-4-5", "anthropic/claude-haiku-4.5"),
        ("google", "gemini-3.8-flash", "google/gemini-3.8-flash"),
        ("openrouter", "anthropic/claude-opus-5.5", "anthropic/claude-opus-5.5"),
    ],
)
def test_openrouter_id_mapping(provider, model_id, expected) -> None:
    assert _load_script().openrouter_id(provider, model_id) == expected


def _priced(prompt: float, completion: float) -> dict:
    return {"pricing": {"prompt": str(prompt / 1e6), "completion": str(completion / 1e6)}}


def test_sync_applies_holds_and_guards() -> None:
    script = _load_script()
    catalog = {
        "providers": {
            "openai": [
                {"id": "a", "role": "chat", "input": 1.0, "output": 2.0},
                {"id": "b", "role": "chat", "input": 1.0, "output": 2.0, "manual_until": "2099-01-01"},
                {"id": "c", "role": "chat", "input": 1.0, "output": 2.0},
                {"id": "w", "role": "audio", "input": 0.006},
            ]
        }
    }
    remote = {"openai/a": _priced(1.5, 2.0), "openai/b": _priced(0.5, 1.0), "openai/c": _priced(50.0, 2.0)}
    changed, skipped, missing = script.sync(catalog, remote, "2026-10-10")
    rows = {r["id"]: r for r in catalog["providers"]["openai"]}
    assert rows["a"]["input"] == 1.5
    assert rows["b"]["input"] == 1.0  # held by manual_until
    assert rows["c"]["input"] == 1.0  # 50x jump: suspicious, not applied
    assert len(changed) == 1 and len(skipped) == 3 and missing == []
