"""IR-B1-01: real protocol calls on fake HTTP, typed failures and account isolation."""

import asyncio
from copy import deepcopy
import datetime as dt
import json
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.const import STATE_UNAVAILABLE

from custom_components.csg_plus import sensor
from custom_components.csg_plus.csg_client import (
    CSGClient, CSGElectricityAccount, InvalidCredentials, NotLoggedIn, CSGAPIError,
)
from custom_components.csg_plus.csg_client.const import HEADER_X_AUTH_TOKEN
from custom_components.csg_plus.history_helpers import ResponseValidationError
from test_daily_usage import make_coordinator
from test_history_store import make_store
from test_retained_future_statistics import observed

BAD = CSGElectricityAccount("fictional-bad", area_code="080000", ele_customer_id="bad-customer")
GOOD = CSGElectricityAccount("fictional-good", area_code="080000", ele_customer_id="good-customer")
MARKER = "payload-marker-DO-NOT-LOG"
MALFORMED = [
    pytest.param(None, id="null"), pytest.param([], id="list"),
    pytest.param([{"sta": "00", "data": {}}], id="wrapped-success"),
    pytest.param(MARKER, id="string"), pytest.param(0, id="number"),
    pytest.param({}, id="missing-status"), pytest.param({"data": MARKER}, id="data-only"),
    pytest.param({"sta": None}, id="null-status"), pytest.param({"sta": True}, id="bool-status"),
    pytest.param({"sta": 0}, id="numeric-status"),
    pytest.param({"sta": ""}, id="empty-status"), pytest.param({"sta": "  "}, id="blank-status"),
    pytest.param({"sta": "00"}, id="missing-success-data"),
    pytest.param({"sta": "00", "data": None}, id="null-data"),
    pytest.param({"sta": "00", "data": MARKER}, id="string-data"),
    pytest.param({"sta": "00", "data": True}, id="bool-data"),
]


def http_client(respond):
    client = CSGClient()
    requests = []

    def post(url, *, json: dict, headers, timeout):
        assert timeout == (10, 60)
        path = url.rsplit("/", 1)[-1]
        requests.append((path, deepcopy(json)))
        value = respond(path, json)
        body = value if isinstance(value, bytes) else __import__("json").dumps(value).encode()
        return SimpleNamespace(status_code=200, content=body,
                               headers={HEADER_X_AUTH_TOKEN: "synthetic-session"})

    client._session.post = post
    return client, requests


def call_active(client, endpoint):
    if endpoint == "daily":
        return client.api_query_day_electric_by_m_point(2026, 9, "080000", "synthetic-customer", "synthetic-meter")
    if endpoint == "billing":
        return client.api_get_fee_analyze_details(2026, "080000", "synthetic-customer")
    return client.api_query_account_surplus("080000", "synthetic-customer")


@pytest.mark.parametrize("endpoint", ["daily", "billing", "balance"])
@pytest.mark.parametrize("transport", ["http", "raw-boundary"])
@pytest.mark.parametrize("envelope", MALFORMED)
def test_malformed_active_envelopes_are_safe_typed_errors(endpoint, transport, envelope):
    client, requests = http_client(lambda *args: envelope)
    if transport == "raw-boundary":
        client._make_request = lambda *args, **kwargs: ({}, deepcopy(envelope))
    with pytest.raises(ResponseValidationError) as raised:
        call_active(client, endpoint)
    assert MARKER not in str(raised.value)
    assert raised.value.__cause__ is None
    assert len(requests) == (transport == "http")


@pytest.mark.parametrize("endpoint", ["daily", "billing", "balance"])
def test_container_type_is_endpoint_specific(endpoint):
    wrong = {} if endpoint == "balance" else []
    client, _ = http_client(lambda *args: {"sta": "00", "data": wrong})
    with pytest.raises(ResponseValidationError):
        call_active(client, endpoint)


@pytest.mark.parametrize("body", [b"", b"not-json", b'{"sta":', b'{"sta":"00","data":'])
def test_malformed_wire_json_is_safe(body):
    client, _ = http_client(lambda *args: body)
    with pytest.raises(ResponseValidationError) as raised:
        call_active(client, "daily")
    assert raised.value.__cause__ is None
    assert str(raised.value) == "Invalid response envelope JSON"


def test_legacy_framing_and_valid_empty_and_zero_data_remain_valid():
    daily, _ = http_client(lambda *args: b'legacy-prefix {"sta":"00","data":{"result":[],"totalPower":0}} legacy-suffix')
    assert daily.get_month_daily_usage_detail(GOOD, (2026, 9)) == (0, [])
    billing, _ = http_client(lambda *args: {"sta": "00", "data": {
        "totalBillingElectricity": 0, "totalActualAmount": 0, "electricAndChargeList": []}})
    assert billing.get_year_month_stats(GOOD, 2026) == (0, 0, [])
    balance, _ = http_client(lambda *args: {"sta": "00", "data": []})
    assert balance.get_balance_and_arrears(GOOD) == (None, None)


@pytest.mark.parametrize("endpoint", ["daily", "billing", "balance"])
def test_auth_expiry_keeps_existing_type_without_success_data(endpoint):
    client, _ = http_client(lambda *args: {"sta": "04"})
    with pytest.raises(NotLoggedIn) as raised:
        call_active(client, endpoint)
    assert raised.value.sta == "04"


def test_login_sms_qr_and_api_error_status_semantics_are_preserved():
    success, _ = http_client(lambda *args: {"sta": "00"})
    assert success.api_send_login_sms("synthetic-phone") is True
    assert success.api_login_with_sms_code("synthetic-phone", "000000") == "synthetic-session"
    assert success.api_login_with_password_and_sms_code("synthetic-phone", "synthetic-password", "000000") == "synthetic-session"
    assert success.api_get_qr_login_status("synthetic-login-id") == (True, "synthetic-session")
    waiting, _ = http_client(lambda *args: {"sta": "09"})
    assert waiting.api_get_qr_login_status("synthetic-login-id") == (False, "")
    wrong, _ = http_client(lambda *args: {"sta": "00010002"})
    with pytest.raises(InvalidCredentials):
        wrong.api_login_with_password_and_sms_code("synthetic-phone", "synthetic-password", "000000")
    error, _ = http_client(lambda *args: {"sta": "02"})
    with pytest.raises(CSGAPIError) as raised:
        call_active(error, "daily")
    assert type(raised.value) is CSGAPIError


@pytest.mark.parametrize("consumer", ["realtime", "billing"])
@pytest.mark.parametrize("envelope", MALFORMED)
def test_bad_first_good_second_http_refresh_is_isolated(consumer, envelope, monkeypatch, caplog):
    monkeypatch.setattr(sensor.dt_util, "utcnow", lambda: dt.datetime(2026, 9, 3, 4, tzinfo=dt.UTC))

    async def scenario():
        history = make_store()
        await history.async_upsert_daily_usage(BAD.account_number, (2026, 9), [{"date": "2026-09-02", "kwh": 4}])
        await history.async_upsert_monthly_bill(BAD.account_number, (2026, 8), usage_kwh=4, cost_cny=2)
        prior = deepcopy(history._data["accounts"][BAD.account_number])

        def respond(path, payload):
            if payload["eleCustId"] == "bad-customer":
                return deepcopy(envelope)
            if path == "queryUserAccountNumberSurplus":
                data = [{"balance": 1, "arrears": 0}]
            elif path == "queryDayElectricByMPoint":
                data = {"totalPower": 2, "result": [{"date": "2026-09-02", "power": 2}]
                        if payload["yearMonth"] == "202609" else []}
            else:
                year = payload["electricityBillYear"]
                data = {"totalBillingElectricity": 5, "totalActualAmount": 3,
                        "electricAndChargeList": [{"yearMonth": f"{year}08", "billingElectricity": 5,
                                                    "actualTotalAmount": 3}]}
            return {"sta": "00", "data": data}

        client, requests = http_client(respond)
        kind = sensor.RealtimeCoordinator if consumer == "realtime" else sensor.BillingCoordinator
        coordinator = make_coordinator(kind, client, history)
        coordinator._accounts = lambda: [BAD, GOOD]
        coordinator._notify_failure = Mock()
        if consumer == "billing":
            coordinator._add_year_data = MethodType(sensor.BillingCoordinator._add_year_data, coordinator)
        result = await coordinator._async_update_data()
        bad_after = history._data["accounts"][BAD.account_number]
        assert bad_after["daily_usage"] == prior["daily_usage"]
        assert bad_after["monthly_bills"] == prior["monthly_bills"]
        assert history.daily_usage(GOOD.account_number, "2026-09-02")["kwh"] == 2
        customers = [p["eleCustId"] for _, p in requests]
        assert customers[0] == "bad-customer" and customers[-1] == "good-customer"
        assert customers.index("good-customer") > 0
        assert all(c == "good-customer" for c in customers[customers.index("good-customer"):])
        errors = [call.args[-1] for call in coordinator._notify_failure.call_args_list]
        assert errors and all(isinstance(e, ResponseValidationError) for e in errors)
        assert MARKER not in caplog.text
        if consumer == "realtime":
            assert result[BAD.account_number][sensor.SUFFIX_YESTERDAY_KWH] == STATE_UNAVAILABLE
            assert result[BAD.account_number][sensor.SUFFIX_BAL] == STATE_UNAVAILABLE
            assert result[GOOD.account_number][sensor.SUFFIX_YESTERDAY_KWH] == 2
            assert result[GOOD.account_number][sensor.SUFFIX_BAL] == 1
        else:
            assert result[BAD.account_number][sensor.SUFFIX_THIS_YEAR_KWH] == STATE_UNAVAILABLE
            assert result[BAD.account_number][sensor.SUFFIX_LAST_MONTH_KWH] == STATE_UNAVAILABLE
            assert result[GOOD.account_number][sensor.SUFFIX_THIS_YEAR_KWH] == 5
            assert result[GOOD.account_number][sensor.SUFFIX_LAST_MONTH_KWH] == 5
            assert history.monthly_bill(GOOD.account_number, (2026, 8))["usage_kwh"] == 5
        case = hashlib_id(envelope)
        observed(f"envelope-{consumer}-{case}", {"requests": requests, "result": result,
                 "bad_before": prior, "bad_after": history._data["accounts"][BAD.account_number],
                 "error_types": [type(e).__name__ for e in errors]})
    asyncio.run(scenario())


def hashlib_id(value):
    import hashlib
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:12]
