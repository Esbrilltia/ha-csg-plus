"""B3 applicability of explicit reconciliation over synthetic persisted facts.

Memory failure contracts and real temporary Store/Recorder proofs are separate.
No cloud access, user storage, Energy preferences or automatic recomputation.
"""
from __future__ import annotations

import asyncio
import calendar
import datetime as dt
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.csg_plus import history_helpers, history_store as module
from custom_components.csg_plus.energy_statistics import statistic_metadata
from custom_components.csg_plus.history_store import CSGHistoryStore
from test_energy_statistics_recorder import ACCOUNT as RECORDER_ACCOUNT, recorder_world
from test_history_store import real_storage_io

ACCOUNT = 'synthetic-reconciliation'
MONTH = (2026, 2)
DAY = '2026-02-01'


class MemoryStore:
    """Independent saved snapshot, including known save/verification failures."""

    def __init__(self):
        self.saved = None
        self.mode = 'ok'
        self.save_count = 0
        self.before_save = None
        self.old_saved = None

    async def async_save(self, payload):
        self.save_count += 1
        if self.before_save is not None:
            self.before_save()
        if self.mode == 'raise':
            raise OSError('synthetic failed save')
        if self.mode == 'swallow':
            return
        self.old_saved = deepcopy(self.saved)
        self.saved = deepcopy(payload)

    async def async_load(self):
        if self.mode == 'read_error':
            raise OSError('synthetic failed verification')
        if self.mode == 'read_mismatch':
            return deepcopy(self.old_saved)
        return deepcopy(self.saved)


def memory_store():
    store = CSGHistoryStore.__new__(CSGHistoryStore)
    store._data = {'accounts': {}}
    store._lock = asyncio.Lock()
    store._store = MemoryStore()
    store._verification_store = store._store
    store._persistence_pending = False
    return store


def run(coroutine):
    return asyncio.run(coroutine)


async def populate(store, account=ACCOUNT, month=MONTH, *, value=1):
    year, number = month
    days = calendar.monthrange(year, number)[1]
    await store.async_upsert_daily_usage(account, month, [
        {'date': dt.date(year, number, day).isoformat(), 'kwh': value}
        for day in range(1, days + 1)
    ])
    await store.async_upsert_monthly_bill(account, month, usage_kwh=days * value, cost_cny=3)
    return await store.async_reconcile_month(account, month)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    value = SimpleNamespace(day=dt.date(2026, 9, 3), stamp='2026-09-03T00:00:00+00:00')
    monkeypatch.setattr(module, '_csg_today', lambda: value.day)
    monkeypatch.setattr(module, '_utcnow_iso', lambda: value.stamp)
    return value


def assert_current(result, day=dt.date(2026, 9, 3)):
    assert result['freshness']['state'] == 'current'
    assert result['freshness']['checked_business_date'] == day.isoformat()
    assert isinstance(result['freshness']['facts_signature'], str)
    assert result['freshness']['facts_signature']
    assert result['persistence_confirmed'] is True


def test_explicit_binding_is_detached_and_transient_confirmation_is_not_persisted():
    async def scenario():
        store = memory_store()
        result = await populate(store)
        assert result['usage_state'] == 'matched'
        assert_current(result)
        persisted = store._store.saved['accounts'][ACCOUNT]['monthly_reconciliation']['2026-02']
        assert 'persistence_confirmed' not in persisted
        assert result == dict(persisted, persistence_confirmed=True)
        result['freshness']['state'] = 'tampered-copy'
        result['daily_sum_kwh'] = 999
        assert_current(store.monthly_reconciliation(ACCOUNT, MONTH))
        assert store.monthly_reconciliation(ACCOUNT, MONTH)['daily_sum_kwh'] == 28
    run(scenario())


@pytest.mark.parametrize('revision', ['daily', 'usage', 'cost', 'missing_usage_cost_revision'])
def test_actual_fact_revision_retains_old_comparison_and_invalidates_before_save(revision, clock):
    async def scenario():
        store = memory_store()
        old = await populate(store)
        clock.stamp = '2026-09-03T01:00:00+00:00'

        def before_save():
            view = store.monthly_reconciliation(ACCOUNT, MONTH)
            assert view['freshness']['state'] == 'stale'
            assert view['persistence_confirmed'] is False
            assert view['checked_at'] == old['checked_at']
            assert view['usage_state'] == 'matched'
        store._store.before_save = before_save
        if revision == 'daily':
            await store.async_upsert_daily_usage(ACCOUNT, MONTH, [{'date': DAY, 'kwh': 2}])
        else:
            await store.async_upsert_monthly_bill(ACCOUNT, MONTH,
                usage_kwh=29 if revision == 'usage' else None if revision == 'missing_usage_cost_revision' else 28,
                cost_cny=4 if revision in ('cost', 'missing_usage_cost_revision') else 3)
        stale = store.monthly_reconciliation(ACCOUNT, MONTH)
        assert stale['usage_state'] == old['usage_state'] == 'matched'
        assert stale['daily_sum_kwh'] == old['daily_sum_kwh'] == 28
        assert stale['checked_at'] == old['checked_at']
        assert stale['freshness']['state'] == 'stale'
        assert stale['persistence_confirmed'] is True
        assert store._store.saved['accounts'][ACCOUNT]['monthly_reconciliation']['2026-02']['freshness']['state'] == 'stale'
    run(scenario())


@pytest.mark.parametrize('mode', ['raise', 'swallow', 'read_error', 'read_mismatch'])
@pytest.mark.parametrize('lane', ['daily', 'monthly'])
def test_failed_revision_is_stale_in_memory_and_same_value_recovery_does_not_recompute(mode, lane, clock):
    async def scenario():
        store = memory_store()
        old = await populate(store)
        clock.stamp = '2026-09-03T01:00:00+00:00'
        store._store.mode = mode
        async def upsert():
            if lane == 'daily':
                return await store.async_upsert_daily_usage(ACCOUNT, MONTH, [{'date': DAY, 'kwh': 2}])
            return await store.async_upsert_monthly_bill(ACCOUNT, MONTH, usage_kwh=29, cost_cny=4)
        if mode == 'raise':
            with pytest.raises(OSError):
                await upsert()
        else:
            await upsert()
        stale = store.monthly_reconciliation(ACCOUNT, MONTH)
        assert stale['freshness']['state'] == 'stale'
        assert stale['persistence_confirmed'] is False
        assert stale['checked_at'] == old['checked_at']
        fact = store.daily_usage(ACCOUNT, DAY) if lane == 'daily' else store.monthly_bill(ACCOUNT, MONTH)
        assert fact['updated_at'] == clock.stamp
        clock.stamp = '2026-09-03T02:00:00+00:00'
        store._store.mode = 'ok'
        retry = await upsert()
        if lane == 'daily':
            assert retry.earliest_changed_date is None
            assert retry.unchanged_dates == (DAY,)
        else:
            assert retry is False
        assert (store.daily_usage(ACCOUNT, DAY) if lane == 'daily' else store.monthly_bill(ACCOUNT, MONTH)) == fact
        assert store.monthly_reconciliation(ACCOUNT, MONTH) == dict(stale, persistence_confirmed=True)
        assert not store._persistence_pending
    run(scenario())


@pytest.mark.parametrize('mode', ['raise', 'swallow', 'read_error', 'read_mismatch'])
def test_unconfirmed_explicit_reconciliation_exposes_confirmation_and_retains_binding_on_retry(mode, clock):
    async def scenario():
        store = memory_store()
        await populate(store)
        await store.async_upsert_daily_usage(ACCOUNT, MONTH, [{'date': DAY, 'kwh': 2}])
        clock.stamp = '2026-09-03T01:00:00+00:00'
        store._store.mode = mode
        if mode == 'raise':
            with pytest.raises(OSError):
                await store.async_reconcile_month(ACCOUNT, MONTH)
        else:
            result = await store.async_reconcile_month(ACCOUNT, MONTH)
            assert result['persistence_confirmed'] is False
        view = store.monthly_reconciliation(ACCOUNT, MONTH)
        assert view['freshness']['state'] == 'current'
        assert view['persistence_confirmed'] is False
        assert view['usage_state'] == 'mismatch'
        assert view['daily_sum_kwh'] == 29
        clock.stamp = '2026-09-03T02:00:00+00:00'
        store._store.mode = 'ok'
        assert await store.async_ensure_persisted()
        assert store.monthly_reconciliation(ACCOUNT, MONTH) == dict(view, persistence_confirmed=True)
    run(scenario())


@pytest.mark.parametrize('lane', ['daily', 'usage', 'cost'])
def test_change_and_reversal_require_explicit_reconciliation(lane, clock):
    async def scenario():
        store = memory_store()
        old = await populate(store)
        for value in (2, 1):
            if lane == 'daily':
                await store.async_upsert_daily_usage(ACCOUNT, MONTH, [{'date': DAY, 'kwh': value}])
            else:
                await store.async_upsert_monthly_bill(ACCOUNT, MONTH,
                    usage_kwh=27 + value if lane == 'usage' else 28,
                    cost_cny=2 + value if lane == 'cost' else 3)
        stale = store.monthly_reconciliation(ACCOUNT, MONTH)
        assert stale['freshness']['state'] == 'stale'
        assert stale['freshness']['facts_signature'] == old['freshness']['facts_signature']
        assert stale['checked_at'] == old['checked_at']
        before_facts = deepcopy({key: store._account(ACCOUNT)[key] for key in ('daily_usage', 'monthly_bills')})
        clock.stamp = '2026-09-03T03:00:00+00:00'
        refreshed = await store.async_reconcile_month(ACCOUNT, MONTH)
        assert_current(refreshed)
        assert refreshed['usage_state'] == 'matched'
        assert refreshed['checked_at'] == clock.stamp
        assert {key: store._account(ACCOUNT)[key] for key in before_facts} == before_facts
    run(scenario())


def test_same_missing_invalid_and_near_equal_refetch_preserve_currentness_timestamps_and_io(clock):
    async def scenario():
        store = memory_store()
        old = await populate(store)
        before = deepcopy(store._data)
        count = store._store.save_count
        clock.stamp = '2026-09-03T05:00:00+00:00'
        for days in ([], [{'date': DAY}], [{'date': DAY, 'kwh': float('nan')}],
                     [{'date': DAY, 'kwh': True}], [{'date': DAY, 'kwh': 1}],
                     [{'date': DAY, 'kwh': 1 + 5e-10}]):
            await store.async_upsert_daily_usage(ACCOUNT, MONTH, days)
        for usage, cost in ((28, 3), (None, None), (float('nan'), False)):
            assert not await store.async_upsert_monthly_bill(ACCOUNT, MONTH, usage_kwh=usage, cost_cny=cost)
        assert store._data == before
        assert store._store.save_count == count
        assert store.monthly_reconciliation(ACCOUNT, MONTH) == old
    run(scenario())


def test_zero_and_missing_usage_keep_comparison_states_without_creating_results():
    async def scenario():
        store = memory_store()
        await store.async_upsert_daily_usage(ACCOUNT, MONTH, [{'date': DAY, 'kwh': 0}])
        await store.async_upsert_monthly_bill(ACCOUNT, MONTH, usage_kwh=None, cost_cny=0)
        assert store.monthly_reconciliation(ACCOUNT, MONTH) is None
        pending = await store.async_reconcile_month(ACCOUNT, MONTH)
        assert pending['usage_state'] == 'pending'
        assert pending['daily_sum_kwh'] == 0
        assert pending['billed_usage_kwh'] is None
        assert_current(pending)
        await populate(store, value=0)
        result = store.monthly_reconciliation(ACCOUNT, MONTH)
        assert result['usage_state'] == 'matched'
        assert result['daily_sum_kwh'] == result['billed_usage_kwh'] == result['difference_kwh'] == 0
        assert_current(result)
    run(scenario())


def test_missing_day_insertion_invalidates_not_comparable_result():
    async def scenario():
        store = memory_store()
        await store.async_upsert_daily_usage(ACCOUNT, MONTH, [{'date': DAY, 'kwh': 1}])
        await store.async_upsert_monthly_bill(ACCOUNT, MONTH, usage_kwh=28, cost_cny=3)
        old = await store.async_reconcile_month(ACCOUNT, MONTH)
        assert old['usage_state'] == 'not_comparable'
        await store.async_upsert_daily_usage(ACCOUNT, MONTH, [{'date': '2026-02-02', 'kwh': 0}])
        result = store.monthly_reconciliation(ACCOUNT, MONTH)
        assert result['usage_state'] == 'not_comparable'
        assert result['daily_sum_kwh'] == old['daily_sum_kwh'] == 1
        assert result['freshness']['state'] == 'stale'
    run(scenario())


def test_accounts_and_months_isolate_freshness_but_confirmation_is_store_wide():
    async def scenario():
        store = memory_store()
        a = await populate(store)
        b = await populate(store, account='synthetic-other')
        jan = await populate(store, month=(2026, 1))
        assert a['freshness']['facts_signature'] == b['freshness']['facts_signature']
        store._store.mode = 'raise'
        with pytest.raises(OSError):
            await store.async_upsert_daily_usage(ACCOUNT, MONTH, [{'date': DAY, 'kwh': 2}])
        assert store.monthly_reconciliation(ACCOUNT, MONTH)['freshness']['state'] == 'stale'
        assert store.monthly_reconciliation('synthetic-other', MONTH) == dict(b, persistence_confirmed=False)
        assert store.monthly_reconciliation(ACCOUNT, (2026, 1)) == dict(jan, persistence_confirmed=False)
        store._store.mode = 'ok'
        assert await store.async_ensure_persisted()
        assert store.monthly_reconciliation('synthetic-other', MONTH) == b
        assert store.monthly_reconciliation(ACCOUNT, (2026, 1)) == jan
    run(scenario())


def test_signature_checks_values_and_getter_does_not_rewrite_metadata():
    async def scenario():
        store = memory_store()
        old = await populate(store)
        store._account(ACCOUNT)['daily_usage'][DAY]['updated_at'] = '2026-09-03T09:00:00+00:00'
        store._account(ACCOUNT)['monthly_bills']['2026-02']['updated_at'] = '2026-09-03T09:00:00+00:00'
        assert store.monthly_reconciliation(ACCOUNT, MONTH) == old
        store._account(ACCOUNT)['daily_usage'][DAY]['kwh'] = 2
        before = deepcopy(store._data)
        count = store._store.save_count
        view = store.monthly_reconciliation(ACCOUNT, MONTH)
        assert view['freshness']['state'] == 'stale'
        assert view['checked_at'] == old['checked_at']
        assert store._data == before
        assert store._store.save_count == count
    run(scenario())


@pytest.mark.parametrize('binding', [None, {}, {'state': 'current'},
    {'state': 'current', 'checked_business_date': '2026-09-03'}])
def test_legacy_incomplete_binding_is_unknown_and_does_not_fabricate_checked_time(binding):
    store = memory_store()
    row = {'usage_state': 'matched', 'daily_sum_kwh': 28, 'billed_usage_kwh': 28,
           'difference_kwh': 0, 'checked_at': '2026-02-28T00:00:00+00:00'}
    if binding is not None:
        row['freshness'] = binding
    store._account(ACCOUNT)['monthly_reconciliation']['2026-02'] = deepcopy(row)
    before = deepcopy(store._data)
    view = store.monthly_reconciliation(ACCOUNT, MONTH)
    assert view['freshness']['state'] == 'unknown'
    assert view['checked_at'] == row['checked_at']
    assert store._data == before
    assert store._store.save_count == 0


@pytest.mark.parametrize('boundary', ['month', 'year'])
def test_shanghai_midnight_changes_applicability_without_recomputing_pending(boundary, monkeypatch):
    month = (2026, 9) if boundary == 'month' else (2026, 12)
    utc = [dt.datetime(2026, month[1], calendar.monthrange(*month)[1], 15, 59, tzinfo=dt.UTC)]
    monkeypatch.setattr(history_helpers.dt_util, 'utcnow', lambda: utc[0])
    monkeypatch.setattr(module, '_csg_today', history_helpers.csg_today)
    async def scenario():
        store = memory_store()
        old = await populate(store, month=month)
        assert old['usage_state'] == 'pending'
        count = store._store.save_count
        before = deepcopy(store._data)
        utc[0] += dt.timedelta(minutes=1)
        view = store.monthly_reconciliation(ACCOUNT, month)
        assert view['freshness']['state'] == 'stale'
        assert view['usage_state'] == 'pending'
        assert store._data == before
        assert store._store.save_count == count
        new = await store.async_reconcile_month(ACCOUNT, month)
        assert new['usage_state'] == 'matched'
        assert_current(new, history_helpers.csg_today())
    run(scenario())


@pytest.mark.parametrize('binding_state', [None, 'unknown'], ids=['missing-state', 'unknown-state'])
def test_complete_signature_and_date_with_unknown_state_are_unknown_without_getter_io(binding_state):
    async def scenario():
        store = memory_store()
        old = await populate(store)
        binding = store._account(ACCOUNT)['monthly_reconciliation']['2026-02']['freshness']
        if binding_state is None:
            binding.pop('state')
        else:
            binding['state'] = binding_state
        before = deepcopy(store._data)
        persisted = deepcopy(store._store.saved)
        before_hash = hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest()
        count = store._store.save_count
        view = store.monthly_reconciliation(ACCOUNT, MONTH)
        assert view['freshness']['state'] == 'unknown'
        assert view['freshness']['facts_signature'] == old['freshness']['facts_signature']
        assert view['freshness']['checked_business_date'] == old['freshness']['checked_business_date']
        assert view['checked_at'] == old['checked_at']
        assert view['usage_state'] == old['usage_state']
        assert view['persistence_confirmed'] is True
        assert store._data == before
        assert hashlib.sha256(json.dumps(store._data, sort_keys=True).encode()).hexdigest() == before_hash
        assert store._store.saved == persisted
        assert store._store.save_count == count
    run(scenario())


@pytest.mark.parametrize('reload_kind', ['same_day', 'next_day', 'legacy', 'stale'])
def test_real_disk_reload_preserves_binding_or_reports_legacy_unknown(tmp_path, real_storage_io, clock, reload_kind):
    async def scenario():
        hass = HomeAssistant(str(tmp_path))
        fresh_hass = None
        try:
            store = CSGHistoryStore(hass, 'synthetic-reconciliation-entry')
            await store.async_load()
            old = await populate(store)
            if reload_kind == 'stale':
                await store.async_upsert_daily_usage(ACCOUNT, MONTH, [{'date': DAY, 'kwh': 2}])
            path = Path(store._store.path)
            payload = json.loads(path.read_text(encoding='utf-8'))
            persisted = payload['data']['accounts'][ACCOUNT]['monthly_reconciliation']['2026-02']
            assert 'persistence_confirmed' not in persisted
            if reload_kind == 'legacy':
                persisted.pop('freshness')
                path.write_text(json.dumps(payload), encoding='utf-8')
            elif reload_kind == 'next_day':
                clock.day += dt.timedelta(days=1)
            await hass.async_stop(force=True)
            fresh_hass = HomeAssistant(str(tmp_path))
            restored = CSGHistoryStore(fresh_hass, 'synthetic-reconciliation-entry')
            before = (path.read_bytes(), path.stat().st_mtime_ns)
            await restored.async_load()
            view = restored.monthly_reconciliation(ACCOUNT, MONTH)
            assert view['freshness']['state'] == {'same_day': 'current', 'next_day': 'stale', 'legacy': 'unknown', 'stale': 'stale'}[reload_kind]
            assert view['persistence_confirmed'] is True
            assert view['checked_at'] == old['checked_at']
            assert view['usage_state'] == old['usage_state']
            assert (path.read_bytes(), path.stat().st_mtime_ns) == before
            assert restored.daily_usage(ACCOUNT, DAY)['kwh'] == (2 if reload_kind == 'stale' else 1)
        finally:
            if fresh_hass is not None:
                await fresh_hass.async_stop(force=True)
            await hass.async_stop(force=True)
    run(scenario())


@pytest.mark.parametrize('freshness', ['unknown', 'current', 'stale'])
def test_real_bridge_and_recorder_use_facts_independently_of_comparison_view(recorder_world, freshness):
    async def scenario():
        async with recorder_world() as world:
            await world.upsert({'2026-09-01': 2, '2026-09-02': 0})
            await world.store.async_upsert_monthly_bill(RECORDER_ACCOUNT, (2026, 9), usage_kwh=99, cost_cny=3)
            await world.store.async_reconcile_month(RECORDER_ACCOUNT, (2026, 9))
            if freshness == 'unknown':
                world.store._account(RECORDER_ACCOUNT)['monthly_reconciliation']['2026-09'].pop('freshness')
                world.store._persistence_pending = True
                assert await world.store.async_ensure_persisted()
            elif freshness == 'stale':
                await world.store.async_upsert_monthly_bill(RECORDER_ACCOUNT, (2026, 9), usage_kwh=100, cost_cny=4)
            assert world.store.monthly_reconciliation(RECORDER_ACCOUNT, (2026, 9))['freshness']['state'] == freshness
            before = deepcopy(world.store._data)
            world.store.monthly_reconciliation = Mock(side_effect=AssertionError('Bridge must use daily facts'))
            world.store.async_reconcile_month = Mock(side_effect=AssertionError('Bridge must not recompute comparison'))
            await world.sync()
            assert [(row['state'], row['sum']) for row in await world.query()] == [(2, 2), (0, 2)]
            assert world.imports[-1][0] == statistic_metadata(RECORDER_ACCOUNT)
            assert world.store._data == before
            await world.upsert({'2026-09-01': 5})
            await world.sync()
            assert [(row['state'], row['sum']) for row in await world.query()] == [(5, 5), (0, 5)]
            world.store.monthly_reconciliation.assert_not_called()
            world.store.async_reconcile_month.assert_not_called()
    run(scenario())


@pytest.mark.parametrize('failure', ['swallowed_write', 'verification_read'])
@pytest.mark.parametrize('lane', ['daily', 'monthly'])
def test_real_store_failed_revision_and_same_value_retry_preserve_stale_binding(
    tmp_path, real_storage_io, clock, caplog, failure, lane,
):
    async def scenario():
        hass = HomeAssistant(str(tmp_path))
        fresh_hass = None
        try:
            store = CSGHistoryStore(hass, 'synthetic-freshness-retry')
            await store.async_load()
            old = await populate(store)
            clock.stamp = '2026-09-03T01:00:00+00:00'
            if failure == 'swallowed_write':
                real_storage_io.failed_writes = 1
            else:
                real_storage_io.fail_next_read = True

            async def revision():
                if lane == 'daily':
                    return await store.async_upsert_daily_usage(ACCOUNT, MONTH, [{'date': DAY, 'kwh': 2}])
                return await store.async_upsert_monthly_bill(ACCOUNT, MONTH, usage_kwh=29, cost_cny=4)

            await revision()
            stale = store.monthly_reconciliation(ACCOUNT, MONTH)
            assert stale['freshness']['state'] == 'stale'
            assert stale['persistence_confirmed'] is False
            assert stale['checked_at'] == old['checked_at']
            assert ('Error writing config' if failure == 'swallowed_write' else 'Could not verify history persistence') in caplog.text
            changed_fact = store.daily_usage(ACCOUNT, DAY) if lane == 'daily' else store.monthly_bill(ACCOUNT, MONTH)
            assert changed_fact['updated_at'] == clock.stamp
            clock.stamp = '2026-09-03T02:00:00+00:00'
            writes = real_storage_io.writes
            result = await revision()
            assert real_storage_io.writes == writes + 1
            if lane == 'daily':
                assert result.earliest_changed_date is None
                assert result.unchanged_dates == (DAY,)
            else:
                assert result is False
            assert store.monthly_reconciliation(ACCOUNT, MONTH) == dict(stale, persistence_confirmed=True)
            assert (store.daily_usage(ACCOUNT, DAY) if lane == 'daily' else store.monthly_bill(ACCOUNT, MONTH)) == changed_fact
            await hass.async_stop(force=True)
            fresh_hass = HomeAssistant(str(tmp_path))
            restored = CSGHistoryStore(fresh_hass, 'synthetic-freshness-retry')
            await restored.async_load()
            assert restored.monthly_reconciliation(ACCOUNT, MONTH) == dict(stale, persistence_confirmed=True)
            assert (restored.daily_usage(ACCOUNT, DAY) if lane == 'daily' else restored.monthly_bill(ACCOUNT, MONTH)) == changed_fact
        finally:
            if fresh_hass is not None:
                await fresh_hass.async_stop(force=True)
            await hass.async_stop(force=True)
    run(scenario())
