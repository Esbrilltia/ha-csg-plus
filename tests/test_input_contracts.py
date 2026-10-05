"""B1: raw synthetic response contracts through real snapshot/fact consumers."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import datetime as dt
from itertools import permutations
from types import MethodType
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.const import STATE_UNAVAILABLE

from custom_components.csg_plus import history_helpers as helpers, history_store as store_module, sensor
from custom_components.csg_plus.const import (
    CONF_SETTINGS, CONF_UPDATE_INTERVAL, SUFFIX_LAST_MONTH_COST, SUFFIX_LAST_MONTH_KWH,
    SUFFIX_LATEST_DAY_KWH, SUFFIX_THIS_MONTH_KWH, SUFFIX_THIS_YEAR_COST,
    SUFFIX_THIS_YEAR_KWH, SUFFIX_YESTERDAY_KWH,
)
from custom_components.csg_plus.csg_client import CSGClient, CSGElectricityAccount
from custom_components.csg_plus.history_helpers import ResponseValidationError
from custom_components.csg_plus.history_store import CSGHistoryStore
from test_daily_usage import ACCOUNT, make_coordinator
from test_energy_statistics_recorder import recorder_world, ACCOUNT as RECORDER_ACCOUNT
from test_history_store import make_store

INVALID_NUMBERS = [None, True, False, "bad", "", "NaN", float("nan"),
                   "inf", float("inf"), float("-inf"), 10**1000]


@pytest.fixture(autouse=True)
def shanghai_day(monkeypatch):
    monkeypatch.setattr(sensor.dt_util, "utcnow", lambda: dt.datetime(2026, 9, 3, 4, tzinfo=dt.UTC))


def raw_client(rows=(), total="0", bills=(), year_usage="0", year_cost="0"):
    """Replace cloud response methods; run every production high-level wrapper."""
    client = CSGClient.__new__(CSGClient)
    client.api_query_day_electric_by_m_point = lambda year, month, *args: {
        "totalPower": total, "result": deepcopy(list(rows)) if (year, month) == (2026, 9) else []}
    client.api_query_account_surplus = lambda *args: [{"balance": "1", "arrears": "0"}]
    client.api_get_fee_analyze_details = lambda year, *args: {
        "totalBillingElectricity": year_usage, "totalActualAmount": year_cost,
        "electricAndChargeList": deepcopy(list(bills)) if year == 2026 else []}
    return client


@pytest.mark.parametrize("total", INVALID_NUMBERS + [-1])
def test_invalid_daily_total_keeps_valid_zero_rows_and_unavailable_total(total):
    async def scenario():
        client = raw_client([{"date": "2026-09-02", "power": "0"}], total=total)
        assert client.get_month_daily_usage_detail(ACCOUNT, (2026, 9)) == (
            None, [{"date": "2026-09-02", "kwh": 0}])
        history = make_store()
        coordinator = make_coordinator(sensor.BillingCoordinator, client, history)
        data = await coordinator._update_account(client, ACCOUNT, [(2026, 9), (2026, 8)])
        assert data[SUFFIX_THIS_MONTH_KWH] == STATE_UNAVAILABLE
        assert data[SUFFIX_LATEST_DAY_KWH] == 0
        assert history.daily_usage(ACCOUNT.account_number, "2026-09-02")["kwh"] == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("field", ["totalBillingElectricity", "totalActualAmount"])
@pytest.mark.parametrize("invalid", INVALID_NUMBERS)
def test_year_totals_fail_independently_of_each_other_and_official_rows(field, invalid):
    async def scenario():
        client = raw_client(bills=[{"yearMonth": "202608", "billingElectricity": "0",
                                    "actualTotalAmount": "3"}], year_usage="5", year_cost="7")
        response = client.api_get_fee_analyze_details(2026)
        response[field] = invalid
        client.api_get_fee_analyze_details = lambda year, *args: deepcopy(response) if year == 2026 else {
            "totalBillingElectricity": "0", "totalActualAmount": "0", "electricAndChargeList": []}
        history = make_store()
        coordinator = make_coordinator(sensor.BillingCoordinator, client, history)
        data = {SUFFIX_LAST_MONTH_KWH: STATE_UNAVAILABLE, SUFFIX_LAST_MONTH_COST: STATE_UNAVAILABLE}
        await sensor.BillingCoordinator._add_year_data(coordinator, client, ACCOUNT, data)
        assert data[SUFFIX_THIS_YEAR_KWH] == (None if field == "totalBillingElectricity" else 5)
        assert data[SUFFIX_THIS_YEAR_COST] == (None if field == "totalActualAmount" else 7)
        assert data[SUFFIX_LAST_MONTH_KWH] == 0
        assert data[SUFFIX_LAST_MONTH_COST] == 3
        assert history.monthly_bill(ACCOUNT.account_number, (2026, 8))["usage_kwh"] == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("response", [None, [], "payload-marker", {}, {"result": None},
                                      {"result": {}}, {"result": "payload-marker"}])
def test_invalid_daily_batch_is_safe_error_not_successful_empty(response):
    client = raw_client()
    client.api_query_day_electric_by_m_point = lambda *args: response
    with pytest.raises(ResponseValidationError) as raised:
        client.get_month_daily_usage_detail(ACCOUNT, (2026, 9))
    assert "payload-marker" not in str(raised.value)


@pytest.mark.parametrize("response", [None, [], "payload-marker", {},
                                      {"electricAndChargeList": None}, {"electricAndChargeList": {}}])
def test_invalid_bill_batch_is_safe_error_not_successful_empty(response):
    client = raw_client()
    client.api_get_fee_analyze_details = lambda *args: response
    with pytest.raises(ResponseValidationError) as raised:
        client.get_year_month_stats(ACCOUNT, 2026)
    assert "payload-marker" not in str(raised.value)


def test_successful_empty_and_missing_fields_are_not_fabricated_zero():
    client = raw_client()
    client.api_query_day_electric_by_m_point = lambda *args: {"result": []}
    client.api_get_fee_analyze_details = lambda *args: {"electricAndChargeList": []}
    client.api_query_account_surplus = lambda *args: []
    assert client.get_month_daily_usage_detail(ACCOUNT, (2026, 9)) == (None, [])
    assert client.get_year_month_stats(ACCOUNT, 2026) == (None, None, [])
    assert client.get_balance_and_arrears(ACCOUNT) == (None, None)


def test_signed_account_money_and_year_charge_keep_their_own_domain():
    client = raw_client(year_usage="-1", year_cost="-7", bills=[
        {"yearMonth": "202608", "billingElectricity": "2", "actualTotalAmount": "-3"}])
    client.api_query_account_surplus = lambda *args: [{"balance": "-2", "arrears": "-1"}]
    assert client.get_balance_and_arrears(ACCOUNT) == (-2, -1)
    cost, usage, rows = client.get_year_month_stats(ACCOUNT, 2026)
    assert (cost, usage) == (-7, None)
    assert helpers.collect_monthly_bill_candidates(rows, ACCOUNT.account_number, 2026) == {
        (2026, 8): (2, None)}  # Existing official bill cost semantics are unchanged.


@pytest.mark.parametrize("field", ["balance", "arrears"])
@pytest.mark.parametrize("invalid", INVALID_NUMBERS)
def test_account_numeric_failure_does_not_erase_other_money_field(field, invalid):
    client = raw_client()
    response = {"balance": "-2", "arrears": "3"}
    response[field] = invalid
    client.api_query_account_surplus = lambda *args: [response]
    assert client.get_balance_and_arrears(ACCOUNT) == (
        (None, 3) if field == "balance" else (-2, None))


@pytest.mark.parametrize("response", [None, {}, "payload-marker", [None], ["payload-marker"]])
def test_account_container_failure_is_a_safe_typed_error(response):
    client = raw_client()
    client.api_query_account_surplus = lambda *args: response
    with pytest.raises(ResponseValidationError) as raised:
        client.get_balance_and_arrears(ACCOUNT)
    assert "payload-marker" not in str(raised.value)


@pytest.mark.parametrize("values", [(5, 7), (7, 5)] + list(permutations((5, 5.00000000075, 5.0000000015))))
def test_conflicting_daily_batch_preserves_old_fact_without_display_substitution(values):
    async def scenario():
        history = make_store()
        await history.async_upsert_daily_usage(ACCOUNT.account_number, (2026, 9),
                                              [{"date": "2026-09-02", "kwh": 4}])
        old = history.daily_usage(ACCOUNT.account_number, "2026-09-02")
        client = raw_client([{"date": "2026-09-02", "power": value} for value in values])
        realtime = await make_coordinator(sensor.RealtimeCoordinator, client, history)._async_update_data()
        billing = await make_coordinator(sensor.BillingCoordinator, client, history)._update_account(
            client, ACCOUNT, [(2026, 9), (2026, 8)])
        assert history.daily_usage(ACCOUNT.account_number, "2026-09-02") == old
        assert realtime[ACCOUNT.account_number][SUFFIX_YESTERDAY_KWH] == STATE_UNAVAILABLE
        assert billing[SUFFIX_LATEST_DAY_KWH] == STATE_UNAVAILABLE
    asyncio.run(scenario())


@pytest.mark.parametrize("values", [(5, 5), (5, 5.00000000075), (5.00000000075, 5)])
def test_agreeing_daily_candidates_share_stable_source_representative(values):
    async def scenario():
        history = make_store()
        client = raw_client([{"date": "2026-09-02", "power": value} for value in values])
        realtime = await make_coordinator(sensor.RealtimeCoordinator, client, history)._async_update_data()
        billing = await make_coordinator(sensor.BillingCoordinator, client, history)._update_account(
            client, ACCOUNT, [(2026, 9), (2026, 8)])
        assert realtime[ACCOUNT.account_number][SUFFIX_YESTERDAY_KWH] == 5
        assert billing[SUFFIX_LATEST_DAY_KWH] == 5
        assert history.daily_usage(ACCOUNT.account_number, "2026-09-02")["kwh"] == 5
    asyncio.run(scenario())


@pytest.mark.parametrize("day", ["2026-10-01", "2026-09-30", "2026-08-31", "2026-09-31"])
def test_wrong_month_future_or_impossible_day_cannot_publish_latest(day):
    async def scenario():
        history = make_store()
        client = raw_client([{"date": day, "power": 9}])
        assert client.get_month_daily_usage_detail(ACCOUNT, (2026, 9))[1] == []
        billing = await make_coordinator(sensor.BillingCoordinator, client, history)._update_account(
            client, ACCOUNT, [(2026, 9), (2026, 8)])
        assert history.daily_usage(ACCOUNT.account_number, day) is None
        assert billing[SUFFIX_LATEST_DAY_KWH] == STATE_UNAVAILABLE
    asyncio.run(scenario())


@pytest.mark.parametrize("instant,month,accepted,future", [
    (dt.datetime(2026, 9, 2, 15, 59, 59, tzinfo=dt.UTC), (2026, 9), "2026-09-02", "2026-09-03"),
    (dt.datetime(2026, 9, 2, 16, tzinfo=dt.UTC), (2026, 9), "2026-09-03", "2026-09-04"),
    (dt.datetime(2026, 12, 31, 16, tzinfo=dt.UTC), (2027, 1), "2027-01-01", "2027-01-02"),
    (dt.datetime(2024, 2, 28, 16, tzinfo=dt.UTC), (2024, 2), "2024-02-29", "2024-03-01"),
])
def test_today_is_valid_across_shanghai_midnight_month_year_and_leap_boundaries(
    monkeypatch, instant, month, accepted, future,
):
    monkeypatch.setattr(sensor.dt_util, "utcnow", lambda: instant)
    client = raw_client()
    client.api_query_day_electric_by_m_point = lambda *args: {"totalPower": "0", "result": [
        {"date": accepted, "power": 0}, {"date": future, "power": 9}]}
    assert client.get_month_daily_usage_detail(ACCOUNT, month) == (0, [{"date": accepted, "kwh": 0}])
    history = make_store()
    asyncio.run(history.async_upsert_daily_usage(ACCOUNT.account_number, month, [
        {"date": accepted, "kwh": 0}, {"date": future, "kwh": 9}]))
    assert history.daily_usage(ACCOUNT.account_number, accepted)["kwh"] == 0
    assert history.daily_usage(ACCOUNT.account_number, future) is None


@pytest.mark.parametrize("values", [((10, 5), (20, 9)), ((20, 9), (10, 5))])
def test_conflicting_official_bill_is_rejected_by_both_fact_and_display(values):
    async def scenario():
        history = make_store()
        await history.async_upsert_monthly_bill(ACCOUNT.account_number, (2026, 8), usage_kwh=4, cost_cny=2)
        old = history.monthly_bill(ACCOUNT.account_number, (2026, 8))
        client = raw_client(bills=[{"yearMonth": "202608", "billingElectricity": usage,
                                    "actualTotalAmount": cost} for usage, cost in values] + [
                                        {"yearMonth": "202607", "billingElectricity": 3, "actualTotalAmount": 1}])
        coordinator = make_coordinator(sensor.BillingCoordinator, client, history)
        data = {SUFFIX_LAST_MONTH_KWH: STATE_UNAVAILABLE, SUFFIX_LAST_MONTH_COST: STATE_UNAVAILABLE}
        for _ in range(2):
            await sensor.BillingCoordinator._add_year_data(coordinator, client, ACCOUNT, data)
            assert history.monthly_bill(ACCOUNT.account_number, (2026, 8)) == old
            assert data[SUFFIX_LAST_MONTH_KWH] == STATE_UNAVAILABLE
            assert data[SUFFIX_LAST_MONTH_COST] == STATE_UNAVAILABLE
            assert history.monthly_bill(ACCOUNT.account_number, (2026, 7))["usage_kwh"] == 3
    asyncio.run(scenario())


def test_wrong_year_bill_cannot_populate_previous_month_display(monkeypatch):
    monkeypatch.setattr(sensor.dt_util, "utcnow", lambda: dt.datetime(2027, 1, 3, 4, tzinfo=dt.UTC))
    client = raw_client()
    client.api_get_fee_analyze_details = lambda year, *args: {
        "totalBillingElectricity": 0, "totalActualAmount": 0,
        "electricAndChargeList": [{"yearMonth": "202612", "billingElectricity": 9, "actualTotalAmount": 3}]
        if year == 2027 else []}
    history = make_store()
    coordinator = make_coordinator(sensor.BillingCoordinator, client, history)
    data = {SUFFIX_LAST_MONTH_KWH: STATE_UNAVAILABLE, SUFFIX_LAST_MONTH_COST: STATE_UNAVAILABLE}
    asyncio.run(sensor.BillingCoordinator._add_year_data(coordinator, client, ACCOUNT, data))
    assert data[SUFFIX_LAST_MONTH_KWH] == STATE_UNAVAILABLE
    assert data[SUFFIX_LAST_MONTH_COST] == STATE_UNAVAILABLE
    assert history.monthly_bill(ACCOUNT.account_number, (2026, 12)) is None


@pytest.mark.parametrize("month", [(10000, 1), (0, 1), (2026, 0), (2026, 13), (True, 1),
                                   (2026, False), (2026.0, 1), (2026, 1.0), (2026,), None, "2026-09"])
def test_internal_invalid_month_is_safe_before_any_fact_write(month):
    history = make_store()
    with pytest.raises(ValueError):
        asyncio.run(history.async_upsert_monthly_bill(ACCOUNT.account_number, month,
                                                     usage_kwh=1, cost_cny=1))
    assert history._data == {"accounts": {}}
    assert history._store.save_count == 0


def test_internal_nonmapping_huge_or_invalid_rows_do_not_poison_valid_fact():
    history = make_store()
    result = asyncio.run(history.async_upsert_daily_usage(ACCOUNT.account_number, (2026, 9), [
        None, "bad", [], {"date": "2026-09-01", "kwh": 10**1000},
        {"date": "2026-09-02", "kwh": 0}, {"date": "2026-09-03", "kwh": True}]))
    assert result.inserted_dates == ("2026-09-02",)
    assert history.daily_usage(ACCOUNT.account_number, "2026-09-01") is None
    assert history.daily_usage(ACCOUNT.account_number, "2026-09-02")["kwh"] == 0
    assert history.daily_usage(ACCOUNT.account_number, "2026-09-03") is None


@pytest.mark.parametrize("rows", [None, {}, "payload-marker", b"payload-marker", 123])
@pytest.mark.parametrize("kind", ["daily", "monthly"])
def test_internal_candidate_batch_container_is_safe_typed_error(rows, kind):
    with pytest.raises(ResponseValidationError) as raised:
        if kind == "daily":
            helpers.collect_daily_usage_candidates(rows, ACCOUNT.account_number, (2026, 9))
        else:
            helpers.collect_monthly_bill_candidates(rows, ACCOUNT.account_number, 2026)
    assert "payload-marker" not in str(raised.value)


def test_huge_month_and_nonmapping_raw_rows_do_not_erase_other_official_month():
    client = raw_client(bills=[None, "bad", [], {"yearMonth": 10**5000, "billingElectricity": 9},
                               {"yearMonth": "2026-08", "billingElectricity": 0, "actualTotalAmount": 3}])
    cost, usage, rows = client.get_year_month_stats(ACCOUNT, 2026)
    assert (cost, usage) == (0, 0)
    assert helpers.collect_monthly_bill_candidates(rows, ACCOUNT.account_number, 2026) == {(2026, 8): (0, 3)}


@pytest.mark.parametrize("consumer", ["realtime", "billing"])
def test_malformed_batch_keeps_other_account_year_and_old_facts_independent(consumer):
    async def scenario():
        bad = CSGElectricityAccount("fictional-bad", area_code="080000", ele_customer_id="bad-customer")
        good = CSGElectricityAccount("fictional-good", area_code="080000", ele_customer_id="good-customer")
        history = make_store()
        await history.async_upsert_daily_usage(bad.account_number, (2026, 9),
                                              [{"date": "2026-09-02", "kwh": 4}])
        await history.async_upsert_monthly_bill(bad.account_number, (2026, 8), usage_kwh=4, cost_cny=2)
        old_daily = history.daily_usage(bad.account_number, "2026-09-02")
        old_bill = history.monthly_bill(bad.account_number, (2026, 8))
        client = raw_client()

        def daily(year, month, area, customer, meter):
            if customer == "bad-customer" and (year, month) == (2026, 9):
                return {"totalPower": "secret-payload-marker", "result": None}
            return {"totalPower": 2, "result": [{"date": "2026-09-02", "power": 2}]
                    if (year, month) == (2026, 9) else []}

        def bills(year, area, customer):
            if customer == "bad-customer" and year == 2026:
                return {"electricAndChargeList": None}
            return {"totalBillingElectricity": 5, "totalActualAmount": 3,
                    "electricAndChargeList": [{"yearMonth": f"{year}08", "billingElectricity": 5,
                                                "actualTotalAmount": 3}]}

        client.api_query_day_electric_by_m_point = daily
        client.api_get_fee_analyze_details = bills
        kind = sensor.RealtimeCoordinator if consumer == "realtime" else sensor.BillingCoordinator
        coordinator = make_coordinator(kind, client, history)
        coordinator._accounts = lambda: [bad, good]
        coordinator._notify_failure = Mock()
        if consumer == "billing":
            coordinator._add_year_data = MethodType(sensor.BillingCoordinator._add_year_data, coordinator)
        data = await coordinator._async_update_data()
        assert history.daily_usage(bad.account_number, "2026-09-02") == old_daily
        assert history.monthly_bill(bad.account_number, (2026, 8)) == old_bill
        assert history.daily_usage(good.account_number, "2026-09-02")["kwh"] == 2
        if consumer == "realtime":
            assert data[bad.account_number][SUFFIX_YESTERDAY_KWH] == STATE_UNAVAILABLE
            assert data[good.account_number][SUFFIX_YESTERDAY_KWH] == 2
        else:
            assert data[bad.account_number][SUFFIX_THIS_YEAR_KWH] == STATE_UNAVAILABLE
            assert data[bad.account_number][sensor.SUFFIX_LAST_YEAR_KWH] == 5
            assert data[good.account_number][SUFFIX_THIS_YEAR_KWH] == 5
            assert data[good.account_number][SUFFIX_LAST_MONTH_KWH] == 5
    asyncio.run(scenario())


@pytest.mark.parametrize("consumer", ["realtime", "billing"])
def test_future_raw_api_row_never_reaches_real_store_bridge_or_recorder(recorder_world, monkeypatch, caplog, consumer):
    """New evidence exceeds original Store/build_statistics D2-F observations."""
    async def scenario():
        async with recorder_world() as world:
            account = CSGElectricityAccount(RECORDER_ACCOUNT, area_code="080000")
            world.entry.data[CONF_SETTINGS][CONF_UPDATE_INTERVAL] = 3600
            client = raw_client([{"date": "2026-09-01", "power": 2},
                                 {"date": "2026-09-03", "power": 0},
                                 {"date": "2026-09-30", "power": 9}], total="11")
            kind = sensor.RealtimeCoordinator if consumer == "realtime" else sensor.BillingCoordinator
            coordinator = kind(world.hass, world.entry, world.store, world.bridge)
            monkeypatch.setattr(coordinator, "_client", AsyncMock(return_value=client))
            monkeypatch.setattr(coordinator, "_accounts", lambda: [account])
            monkeypatch.setattr(coordinator, "_notify_failure", Mock())
            monkeypatch.setattr(coordinator, "_clear_failure", Mock())
            try:
                data = await coordinator._async_update_data()
                await world.sync()
                facts = await world.store.async_daily_usage_snapshot(RECORDER_ACCOUNT)
                assert set(facts) == {"2026-09-01", "2026-09-03"}
                assert facts["2026-09-03"]["kwh"] == 0
                restored = CSGHistoryStore(world.hass, world.entry.entry_id)
                await restored.async_load()
                assert await restored.async_daily_usage_snapshot(RECORDER_ACCOUNT) == facts
                assert [(row["state"], row["sum"]) for row in await world.query()] == [(2, 2), (0, 2)]
                assert all(row["start"].date() <= dt.date(2026, 9, 3)
                           for _, imported in world.imports for row in imported)
                if consumer == "billing":
                    assert data[RECORDER_ACCOUNT][SUFFIX_LATEST_DAY_KWH] == 0
                    assert data[RECORDER_ACCOUNT]["settlement_date"]["settlement_date"] == "2026-09-03"
                ha_warnings = [record for record in caplog.records
                               if record.name.startswith("homeassistant.") and record.levelno >= 30]
                # Core emits its expected custom-integration trust notice;
                # preserve it and reject unexpected Recorder/sensor warnings.
                assert all(record.name == "homeassistant.loader"
                           and "custom integration csg_plus" in record.getMessage()
                           for record in ha_warnings)
            finally:
                await coordinator.async_shutdown()
    asyncio.run(scenario())
