"""G3 business schema through real Core Store wrappers and temporary files."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import traceback
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import storage as ha_storage

from custom_components.csg_plus import history_store as module
from custom_components.csg_plus.history_store import (
    CSGHistoryStore, HistoryStoreSchemaError, HistoryStoreVersionError,
)
from test_history_store import real_storage_io

ACCOUNT = "fictional-sensitive-schema-account"
DAY, MONTH = "2026-08-01", "2026-08"


def payload(collection, rows):
    return {"accounts": {ACCOUNT: {collection: rows}}}


BAD_PAYLOADS = [
    ("business-list", ["bad"]),
    ("accounts-null", {"accounts": None}),
    ("account-list", {"accounts": {ACCOUNT: []}}),
    ("daily-null", payload("daily_usage", None)),
    ("business-null", None),
    ("business-zero", 0),
    ("accounts-list", {"accounts": []}),
    ("daily-row-null", payload("daily_usage", {DAY: None})),
    ("daily-missing-value", payload("daily_usage", {DAY: {}})),
    ("daily-invalid-date", payload("daily_usage", {"2026-02-30": {"kwh": 1}})),
    ("daily-private-date-marker", payload("daily_usage", {"date-marker-DO_NOT_LOG": {"kwh": 1}})),
    ("daily-noncanonical-date", payload("daily_usage", {"20260801": {"kwh": 1}})),
    ("daily-year10000", payload("daily_usage", {"10000-01-01": {"kwh": 1}})),
    ("daily-negative", payload("daily_usage", {DAY: {"kwh": -1}})),
    ("daily-bool", payload("daily_usage", {DAY: {"kwh": True}})),
    ("daily-nan", payload("daily_usage", {DAY: {"kwh": "NaN"}})),
    ("daily-infinite", payload("daily_usage", {DAY: {"kwh": "inf"}})),
    ("daily-huge", payload("daily_usage", {DAY: {"kwh": 10 ** 1000}})),
    ("daily-container-value", payload("daily_usage", {DAY: {"kwh": []}})),
    ("daily-invalid-source", payload("daily_usage", {DAY: {"kwh": 1, "source": []}})),
    ("daily-invalid-time", payload("daily_usage", {DAY: {"kwh": 1, "updated_at": 7}})),
    ("monthly-null", payload("monthly_bills", None)),
    ("monthly-row-null", payload("monthly_bills", {MONTH: None})),
    ("monthly-missing-value", payload("monthly_bills", {MONTH: {}})),
    ("monthly-year10000", payload("monthly_bills", {"10000-01": {"usage_kwh": 1}})),
    ("monthly-invalid-month", payload("monthly_bills", {"2026-13": {"usage_kwh": 1}})),
    ("monthly-negative-usage", payload("monthly_bills", {MONTH: {"usage_kwh": -1}})),
    ("monthly-negative-cost", payload("monthly_bills", {MONTH: {"cost_cny": -1}})),
    ("monthly-bool", payload("monthly_bills", {MONTH: {"cost_cny": False}})),
    ("coverage-null", payload("daily_coverage", None)),
    ("coverage-row-list", payload("daily_coverage", {MONTH: []})),
    ("coverage-state", payload("daily_coverage", {MONTH: {"state": "other"}})),
    ("coverage-count-bool", payload("daily_coverage", {MONTH: {"valid_days": True}})),
    ("coverage-count-range", payload("daily_coverage", {MONTH: {"valid_days": 32}})),
    ("coverage-missing-list", payload("daily_coverage", {MONTH: {"missing_days": None}})),
    ("coverage-missing-date", payload("daily_coverage", {MONTH: {"missing_days": ["2026-09-01"]}})),
    ("coverage-first-date", payload("daily_coverage", {MONTH: {"first_valid_date": 7}})),
    ("coverage-last-date", payload("daily_coverage", {MONTH: {"last_valid_date": "2026-09-01"}})),
    ("reconciliation-null", payload("monthly_reconciliation", None)),
    ("reconciliation-row-null", payload("monthly_reconciliation", {MONTH: None})),
    ("reconciliation-negative-sum", payload("monthly_reconciliation", {MONTH: {"daily_sum_kwh": -1}})),
    ("reconciliation-nan-difference", payload("monthly_reconciliation", {MONTH: {"difference_kwh": "NaN"}})),
    ("reconciliation-state", payload("monthly_reconciliation", {MONTH: {"usage_state": "other"}})),
    ("reconciliation-time", payload("monthly_reconciliation", {MONTH: {"checked_at": None}})),
]


def envelope(store, data):
    return {"version": 1, "minor_version": 1, "key": store._store.key, "data": data}


def snapshot(path):
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "mtime_ns": path.stat().st_mtime_ns}


def observe(name, data):
    if directory := os.environ.get("CSG_SCHEMA_EVIDENCE_DIR"):
        output = Path(directory)
        output.mkdir(parents=True, exist_ok=True)
        (output / f"schema-{name}.json").write_text(json.dumps(data, indent=2, default=str, allow_nan=False) + "\n", encoding="utf-8")


async def assert_rejected(tmp_path, monkeypatch, raw_factory, expected=HistoryStoreSchemaError, *, name):
    hass = HomeAssistant(str(tmp_path))
    store = CSGHistoryStore(hass, "synthetic-schema-entry")
    path = Path(store._store.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = raw_factory(store)
    path.write_bytes(raw if isinstance(raw, bytes) else json.dumps(raw).encode())
    before = snapshot(path)
    prior = {"accounts": {"fictional-prior": {"daily_usage": {DAY: {"kwh": 3}}}}}
    store._data = deepcopy(prior)
    save = AsyncMock(side_effect=AssertionError("Rejected file must not be saved"))
    native_load = AsyncMock(wraps=store._store.async_load)
    rename = Mock(side_effect=AssertionError("Rejected file must not be renamed"))
    remove = Mock(side_effect=AssertionError("Rejected file must not be removed"))
    monkeypatch.setattr(store._store, "async_save", save)
    monkeypatch.setattr(store._store, "async_load", native_load)
    monkeypatch.setattr(ha_storage.os, "rename", rename)
    monkeypatch.setattr(ha_storage.os, "remove", remove)
    try:
        with pytest.raises(expected) as error:
            await store.async_load()
        message = str(error.value)
        assert ACCOUNT not in message and DAY not in message and str(path) not in message
        rendered = "".join(traceback.format_exception(error.value))
        assert ACCOUNT not in rendered and DAY not in rendered
        assert "date-marker-DO_NOT_LOG" not in rendered and "Invalid isoformat string" not in rendered
        assert error.value.__suppress_context__
        assert store._data == prior
        assert snapshot(path) == before
        save.assert_not_awaited()
        native_load.assert_not_awaited()
        rename.assert_not_called()
        remove.assert_not_called()
        # Even rejected reads use the unchanged physical path-owned I/O lane.
        assert store._store.hass._lane is not None
        observe(name, {"before": before, "after": snapshot(path), "error_class": type(error.value).__name__,
                       "safe_error": message, "save_calls": 0, "native_load_calls": 0,
                       "rename_calls": 0, "remove_calls": 0, "prior_runtime_preserved": True,
                       "existing_io_lane_used": True})
    finally:
        await hass.async_stop(force=True)


@pytest.mark.parametrize("name,data", BAD_PAYLOADS, ids=[item[0] for item in BAD_PAYLOADS])
def test_real_v1_invalid_business_payload_is_rejected_without_changing_original_bytes(tmp_path, monkeypatch, name, data):
    asyncio.run(assert_rejected(tmp_path, monkeypatch, lambda store: envelope(store, data), name=name))


@pytest.mark.parametrize("kind", ["empty-bytes", "syntax", "raw-nan", "empty-wrapper", "null-wrapper", "list-wrapper",
                                 "missing-data", "missing-version", "bool-version", "wrong-key", "missing-key",
                                 "major2", "minor2", "minor-null"])
def test_real_invalid_wrapper_is_rejected_before_native_recovery_or_migration(tmp_path, monkeypatch, kind):
    def raw(store):
        wrapper = envelope(store, {})
        if kind == "empty-bytes":
            return b""
        if kind == "syntax":
            return b"{invalid JSON"
        if kind == "raw-nan":
            return json.dumps(envelope(store, {"bad_extension": float("nan")})).encode()
        if kind == "empty-wrapper":
            return {}
        if kind == "null-wrapper":
            return None
        if kind == "list-wrapper":
            return []
        if kind == "missing-data":
            wrapper.pop("data")
        elif kind == "missing-version":
            wrapper.pop("version")
        elif kind == "bool-version":
            wrapper["version"] = True
        elif kind == "wrong-key":
            wrapper["key"] = "fictional-wrong-key"
        elif kind == "missing-key":
            wrapper.pop("key")
        elif kind == "major2":
            wrapper["version"] = 2
        elif kind == "minor2":
            wrapper["minor_version"] = 2
        elif kind == "minor-null":
            wrapper["minor_version"] = None
        return wrapper
    expected = HistoryStoreVersionError if kind in ("major2", "minor2") else HistoryStoreSchemaError
    asyncio.run(assert_rejected(tmp_path, monkeypatch, raw, expected, name=kind))


@pytest.mark.parametrize("data", [
    {}, {"accounts": {}}, {"accounts": {ACCOUNT: {}}},
    payload("daily_usage", {DAY: {"kwh": 0}}),
    payload("daily_usage", {DAY: {"kwh": "0", "updated_at": "first", "source": "daily_usage_api"}}),
    payload("monthly_bills", {MONTH: {"usage_kwh": 0}}),
    payload("monthly_bills", {MONTH: {"cost_cny": 0}}),
    payload("daily_coverage", {MONTH: {"state": "empty", "expected_days": 31, "valid_days": 0,
                                       "missing_days": [], "first_valid_date": None, "last_valid_date": None}}),
    payload("monthly_reconciliation", {MONTH: {"daily_sum_kwh": 0, "billed_usage_kwh": None,
                                               "difference_kwh": None, "usage_state": "pending", "checked_at": "first"}}),
    payload("monthly_reconciliation", {MONTH: {"daily_sum_kwh": 0, "billed_usage_kwh": 1,
                                               "difference_kwh": -1, "usage_state": "mismatch"}}),
    {"unknown_extension": {"nested": [None, True, "keep"]}, "accounts": {ACCOUNT: {"unknown_field": "keep"}}},
], ids=["empty-data", "empty-accounts", "missing-collections", "daily-zero", "numeric-string-time",
        "usage-only-zero", "cost-only-zero", "coverage-null-dates", "pending-null-values",
        "signed-difference", "unknown-extensions"])
@pytest.mark.parametrize("minor_present", [True, False], ids=["explicit-minor", "legacy-missing-minor"])
def test_compatible_v1_files_reload_without_save_or_byte_mtime_change(tmp_path, monkeypatch, data, minor_present):
    async def scenario():
        hass = HomeAssistant(str(tmp_path))
        store = CSGHistoryStore(hass, "synthetic-schema-entry")
        path = Path(store._store.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        wrapper = envelope(store, data)
        if not minor_present:
            wrapper.pop("minor_version")
        path.write_text(json.dumps(wrapper), encoding="utf-8")
        before = snapshot(path)
        expected = deepcopy(data)
        expected.setdefault("accounts", {})
        try:
            for _ in range(2):
                fresh = CSGHistoryStore(hass, "synthetic-schema-entry")
                save = AsyncMock(side_effect=AssertionError("Valid V1 load must not save"))
                monkeypatch.setattr(fresh._store, "async_save", save)
                await fresh.async_load()
                assert fresh._data == expected
                assert snapshot(path) == before
                assert fresh._store.hass._lane is not None
                save.assert_not_awaited()
        finally:
            await hass.async_stop(force=True)
    asyncio.run(scenario())


@pytest.mark.parametrize("returned", [{"accounts": None}, {"accounts": {ACCOUNT: {"daily_usage": None}}}, None])
def test_native_load_result_is_validated_before_runtime_publication(tmp_path, monkeypatch, returned):
    async def scenario():
        hass = HomeAssistant(str(tmp_path))
        store = CSGHistoryStore(hass, "synthetic-schema-entry")
        path = Path(store._store.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(envelope(store, {})), encoding="utf-8")
        before = snapshot(path)
        prior = {"accounts": {"fictional-prior": {}}}
        store._data = deepcopy(prior)
        native = AsyncMock(return_value=returned)
        monkeypatch.setattr(store._store, "async_load", native)
        try:
            with pytest.raises(HistoryStoreSchemaError):
                await store.async_load()
            native.assert_awaited_once()
            assert store._data == prior and snapshot(path) == before
        finally:
            await hass.async_stop(force=True)
    asyncio.run(scenario())


def test_only_missing_real_file_initializes_an_empty_store(tmp_path):
    async def scenario():
        hass = HomeAssistant(str(tmp_path))
        store = CSGHistoryStore(hass, "synthetic-missing-entry")
        path = Path(store._store.path)
        try:
            assert not path.exists()
            await store.async_load()
            assert store._data == {"accounts": {}} and not path.exists()
            assert store._store.hass._lane is not None
        finally:
            await hass.async_stop(force=True)
    asyncio.run(scenario())


def test_real_generated_v1_facts_coverage_reconciliation_progress_and_extensions_survive_reload(tmp_path, monkeypatch, real_storage_io):
    async def scenario():
        hass = HomeAssistant(str(tmp_path))
        store = CSGHistoryStore(hass, "synthetic-generated-entry")
        monkeypatch.setattr(module, "_utcnow_iso", lambda: "first")
        try:
            await store.async_load()
            await store.async_upsert_daily_usage(ACCOUNT, (2026, 8), [{"date": DAY, "kwh": 0}])
            await store.async_upsert_monthly_bill(ACCOUNT, (2026, 8), usage_kwh=None, cost_cny=0)
            await store.async_reconcile_month(ACCOUNT, (2026, 8))
            assert await store.async_complete_history_unit(ACCOUNT, daily_month=(2026, 8))
            path = Path(store._store.path)
            wrapped = json.loads(path.read_text(encoding="utf-8"))
            row = wrapped["data"]["accounts"][ACCOUNT]
            row["unknown_extension"] = {"preserve": [None, True, "keep"]}
            row["sync"]["history_backfill"]["completed_daily_months"].append("bad-member")
            row["sync"]["history_backfill"]["bill_year_scopes"] = None
            path.write_text(json.dumps(wrapped), encoding="utf-8")
            before = snapshot(path)
            fresh = CSGHistoryStore(hass, "synthetic-generated-entry")
            writes_before = real_storage_io.writes
            await fresh.async_load()
            assert fresh._data == wrapped["data"]
            assert fresh.daily_usage(ACCOUNT, DAY) == {"kwh": 0, "source": "daily_usage_api", "updated_at": "first"}
            assert fresh.monthly_bill(ACCOUNT, (2026, 8))["cost_cny"] == 0
            assert fresh.daily_coverage(ACCOUNT, (2026, 8)) == row["daily_coverage"][MONTH]
            assert fresh.monthly_reconciliation(ACCOUNT, (2026, 8)) == row["monthly_reconciliation"][MONTH]
            assert fresh.history_progress(ACCOUNT)["completed_daily_months"] == [MONTH]
            assert fresh.history_progress(ACCOUNT)["bill_year_scopes"] == {}
            assert snapshot(path) == before and real_storage_io.writes == writes_before
            observe("legitimate-v1-reload", {"before": before, "after": snapshot(path),
                    "facts_progress_extensions_preserved": True, "save_calls_during_reload": 0,
                    "progress_invalid_members_remain_retryable": True})
        finally:
            await hass.async_stop(force=True)
    asyncio.run(scenario())
