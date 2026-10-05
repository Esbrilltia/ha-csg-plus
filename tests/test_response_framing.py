"""IR-B1-01 second pass: independent wire counterexamples over real Core refresh."""

import asyncio
from copy import deepcopy
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame
from homeassistant.util import dt as dt_util

from custom_components.csg_plus import sensor
from custom_components.csg_plus.const import (
    CONF_AUTH_TOKEN, CONF_ELE_ACCOUNTS, CONF_SETTINGS, CONF_UPDATE_INTERVAL,
)
from custom_components.csg_plus.csg_client import CSGClient, CSGElectricityAccount
from custom_components.csg_plus.history_helpers import ResponseValidationError
from custom_components.csg_plus.history_store import CSGHistoryStore
from test_retained_future_statistics import observed

BAD = CSGElectricityAccount("audit-fictional-bad", area_code="080000",
                           ele_customer_id="audit-bad", metering_point_id="meter-bad")
GOOD = CSGElectricityAccount("audit-fictional-good", area_code="080000",
                            ele_customer_id="audit-good", metering_point_id="meter-good")
ENDPOINTS = ["daily", "year", "balance"]
DAMAGE = ["truncated-array", "unmatched-array-end", "trailing-comma"]


def success_body(endpoint):
    data = {
        "daily": {"totalPower": 0, "result": []},
        "year": {"totalBillingElectricity": 0, "totalActualAmount": 0,
                 "electricAndChargeList": []},
        "balance": [{"balance": 0, "arrears": 0}],
    }[endpoint]
    return json.dumps({"sta": "00", "data": data}).encode()


def corrupt(raw, damage):
    return {"truncated-array": b"[" + raw, "unmatched-array-end": raw + b"]",
            "trailing-comma": raw + b","}[damage]


def request(endpoint, wire):
    client = CSGClient()
    client._session.post = lambda *args, **kwargs: SimpleNamespace(
        status_code=200, content=wire, headers={},
    )
    if endpoint == "daily":
        return client.get_month_daily_usage_detail(BAD, (2026, 9))
    if endpoint == "year":
        return client.get_year_month_stats(BAD, 2026)
    return client.get_balance_and_arrears(BAD)


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("damage", DAMAGE)
def test_malformed_json_must_not_become_success(endpoint, damage):
    wire = corrupt(success_body(endpoint), damage)
    with pytest.raises(json.JSONDecodeError):
        json.loads(wire)
    error = None
    result = None
    try:
        result = request(endpoint, wire)
    except ResponseValidationError as err:
        error = err
    observed(f"boundary-{endpoint}-{damage}", {
        "wire": wire.decode(), "whole_body_json": "JSONDecodeError", "result": result,
        "error_type": type(error).__name__ if error else None,
    })
    assert isinstance(error, ResponseValidationError)
    assert str(error) == "Invalid response envelope JSON"
    assert error.__cause__ is None and error.__suppress_context__ is True


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_explicit_legacy_framing_and_complete_json_controls(endpoint):
    raw = success_body(endpoint)
    expected = {"daily": (0, []), "year": (0, 0, []), "balance": (0, 0)}[endpoint]
    assert request(endpoint, raw) == expected
    assert request(endpoint, b"legacy-prefix " + raw + b" legacy-suffix") == expected
    # A complete array must reach the existing container validator, never extraction.
    with pytest.raises(ResponseValidationError, match="^Invalid response envelope container$"):
        request(endpoint, b"[" + raw + b"]")


@pytest.mark.parametrize("prefix,suffix", [
    (b"arbitrary-prefix ", b" arbitrary-suffix"),
    (b"legacy-prefix ", b""), (b"", b" legacy-suffix"),
    (b"legacy-prefix [", b" legacy-suffix"),
    (b"legacy-prefix ", b"] legacy-suffix"),
    (b"legacy-prefix ", b", legacy-suffix"),
    (b"legacy-prefix ", b"} legacy-suffix"),
    (b"legacy-prefix ", b" legacy-suffix trailing-text"),
    (b"leading-text legacy-prefix ", b" legacy-suffix"),
    (b"legacy-prefix ", b' {"sta":"00"} legacy-suffix'),
])
def test_unknown_or_damaged_framing_is_not_a_success(prefix, suffix):
    with pytest.raises(ResponseValidationError, match="^Invalid response envelope JSON$"):
        request("daily", prefix + success_body("daily") + suffix)


@pytest.mark.parametrize("inner", [b'{"sta":', b'{"sta":"00","data":', b'{"sta":"00",}'])
def test_known_frame_still_requires_complete_inner_json(inner):
    with pytest.raises(ResponseValidationError, match="^Invalid response envelope JSON$"):
        request("daily", b"legacy-prefix " + inner + b" legacy-suffix")


@pytest.mark.parametrize("kind", ["realtime", "billing"])
@pytest.mark.parametrize("damage", DAMAGE)
def test_actual_core_refresh_isolates_malformed_framing(tmp_path, monkeypatch, caplog, kind, damage):
    monkeypatch.setattr(dt_util, "utcnow", lambda: dt.datetime(2026, 9, 3, 4, tzinfo=dt.UTC))

    async def scenario():
        hass = HomeAssistant(str(tmp_path))
        loader.async_setup(hass)
        frame.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        entry = ConfigEntry(
            version=1, minor_version=1, domain="csg_plus", title="Synthetic framing regression",
            data={CONF_AUTH_TOKEN: "audit-fake-token", CONF_SETTINGS: {CONF_UPDATE_INTERVAL: 3600},
                  CONF_ELE_ACCOUNTS: {a.account_number: a.dump() for a in (BAD, GOOD)}},
            options={}, source="user", unique_id=None, discovery_keys={}, subentries_data=None,
            entry_id="audit-two-account",
        )
        store = CSGHistoryStore(hass, entry.entry_id)
        await store.async_load()
        await store.async_upsert_daily_usage(BAD.account_number, (2026, 9), [{"date": "2026-09-02", "kwh": 4}])
        await store.async_upsert_monthly_bill(BAD.account_number, (2026, 8), usage_kwh=4, cost_cny=2)
        before = deepcopy(store._data["accounts"][BAD.account_number])
        calls = []

        def post(_session, url, *, json: dict, headers, timeout):
            assert timeout == (10, 60)
            path = url.rsplit("/", 1)[-1]
            endpoint = {"queryDayElectricByMPoint": "daily", "getAnalyzeFeeDetails": "year",
                        "queryUserAccountNumberSurplus": "balance"}.get(path)
            if path in ("queryAuthenticationResult", "getUserInfo"):
                value = {"sta": "00", "data": {"custNumber": "audit-fictional-session"}}
            elif json["eleCustId"] == BAD.ele_customer_id:
                value = corrupt(success_body(endpoint), damage)
            elif endpoint == "balance":
                value = {"sta": "00", "data": [{"balance": 0, "arrears": 0}]}
            elif endpoint == "daily":
                value = {"sta": "00", "data": {"totalPower": 2, "result": [
                    {"date": "2026-09-02", "power": 2}] if json["yearMonth"] == "202609" else []}}
            else:
                value = {"sta": "00", "data": {"totalBillingElectricity": 5, "totalActualAmount": 3,
                    "electricAndChargeList": [{"yearMonth": f"{json['electricityBillYear']}08",
                                              "billingElectricity": 5, "actualTotalAmount": 3}]}}
            content = value if isinstance(value, bytes) else __import__("json").dumps(value).encode()
            calls.append({"path": path, "payload": deepcopy(json), "wire": content.decode()})
            return SimpleNamespace(status_code=200, content=content, headers={})

        # Real load/verify/initialize, executor, Core async_refresh and native Store.
        # Replace only HTTP transport and notification UI; no live service access.
        monkeypatch.setattr(requests.Session, "post", post)
        coordinator = (sensor.RealtimeCoordinator if kind == "realtime" else sensor.BillingCoordinator)(hass, entry, store)
        coordinator._notify_failure = Mock()
        coordinator._clear_failure = Mock()
        try:
            await coordinator.async_refresh()
            errors = [call.args[-1] for call in coordinator._notify_failure.call_args_list]
            after = deepcopy(store._data["accounts"][BAD.account_number])
            observed(f"core-{kind}-{damage}", {
                "requests": calls, "last_update_success": coordinator.last_update_success,
                "last_exception": coordinator.last_exception, "result": coordinator.data,
                "bad_before": before, "bad_after": after,
                "error_types": [type(err).__name__ for err in errors],
                "good_facts": store._data["accounts"][GOOD.account_number],
            })
            assert coordinator.last_update_success is True and coordinator.last_exception is None
            assert len(coordinator.data) == 2
            assert errors and all(isinstance(err, ResponseValidationError) for err in errors)
            assert all(call.args[0] == BAD.account_number for call in coordinator._notify_failure.call_args_list)
            assert all(str(err) == "Invalid response envelope JSON" for err in errors)
            assert after["daily_usage"] == before["daily_usage"]
            assert after["monthly_bills"] == before["monthly_bills"]
            assert store.daily_usage(GOOD.account_number, "2026-09-02")["kwh"] == 2
            customers = [call["payload"]["eleCustId"] for call in calls
                         if call["payload"] and "eleCustId" in call["payload"]]
            assert customers[0] == BAD.ele_customer_id and customers[-1] == GOOD.ele_customer_id
            assert all(customer == GOOD.ele_customer_id for customer in customers[customers.index(GOOD.ele_customer_id):])
            if kind == "realtime":
                affected = [sensor.SUFFIX_BAL, sensor.SUFFIX_ARR, sensor.SUFFIX_YESTERDAY_KWH]
                assert coordinator.data[GOOD.account_number][sensor.SUFFIX_BAL] == 0
                assert coordinator.data[GOOD.account_number][sensor.SUFFIX_YESTERDAY_KWH] == 2
            else:
                affected = [sensor.SUFFIX_THIS_MONTH_KWH, sensor.SUFFIX_THIS_YEAR_KWH,
                            sensor.SUFFIX_THIS_YEAR_COST, sensor.SUFFIX_LAST_YEAR_KWH, sensor.SUFFIX_LAST_YEAR_COST]
                assert coordinator.data[GOOD.account_number][sensor.SUFFIX_THIS_YEAR_KWH] == 5
                assert coordinator.data[GOOD.account_number][sensor.SUFFIX_LAST_MONTH_KWH] == 5
                assert store.monthly_bill(GOOD.account_number, (2026, 8))["usage_kwh"] == 5
            assert all(coordinator.data[BAD.account_number][suffix] == STATE_UNAVAILABLE for suffix in affected)
            assert await store.async_ensure_persisted()
            reloaded = CSGHistoryStore(hass, entry.entry_id)
            await reloaded.async_load()
            assert reloaded._data["accounts"][BAD.account_number]["daily_usage"] == before["daily_usage"]
            assert reloaded._data["accounts"][BAD.account_number]["monthly_bills"] == before["monthly_bills"]
            assert reloaded.daily_usage(GOOD.account_number, "2026-09-02")["kwh"] == 2
            assert "audit-fake-token" not in caplog.text
            assert not any(call["wire"] in caplog.text for call in calls)
        finally:
            await coordinator.async_shutdown()
            await entry._async_process_on_unload(hass)
            await hass.async_stop(force=True)

    asyncio.run(scenario())
