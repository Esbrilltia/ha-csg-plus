"""B2 entry setup rollback through actual local Core, Store and Recorder."""

import asyncio
from contextlib import asynccontextmanager
import datetime as dt
from pathlib import Path
import threading
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant import loader
from homeassistant.components import sensor as ha_sensor
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_USERNAME, EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers import storage as ha_storage
from homeassistant.helpers.entity_platform import EntityPlatform

import custom_components.csg_plus as integration
from custom_components.csg_plus import sensor
from custom_components.csg_plus.const import (
    CONF_AUTH_TOKEN, CONF_ELE_ACCOUNTS, CONF_ENERGY_STATISTICS_ENABLED,
    CONF_SETTINGS, CONF_UPDATE_INTERVAL, DOMAIN,
)
from custom_components.csg_plus.history_store import HistoryStoreSchemaError, HistoryStoreVersionError
from custom_components.csg_plus.csg_client import CSGElectricityAccount
from test_energy_statistics_pending import blocked_import, wait_entered
from test_energy_statistics_recorder import recorder_world
from test_energy_statistics_unload import Cloud


@pytest.fixture
def setup_world(recorder_world, monkeypatch):
    """Real Core entrance/forward/sensor/unload; fictional cloud and files only."""
    @asynccontextmanager
    async def world():
        async with recorder_world() as base:
            await base.bridge.async_shutdown()
            hass, entry = base.hass, base.entry
            hass.config.skip_pip = True
            await ar.async_load(hass)
            dr.async_setup(hass)
            await dr.async_load(hass)
            await er.async_load(hass)
            hass.config_entries._entries[entry.entry_id] = entry
            hass.config_entries.async_update_entry(entry, data={
                **entry.data, CONF_AUTH_TOKEN: "synthetic", CONF_USERNAME: "synthetic",
                CONF_SETTINGS: {CONF_ENERGY_STATISTICS_ENABLED: True, CONF_UPDATE_INTERVAL: 3600},
            })
            base.cloud = Cloud(value=1)
            monkeypatch.setattr(integration.CSGClient, "load", lambda _: base.cloud)
            monkeypatch.setattr(sensor, "_csg_today", lambda: dt.date(2026, 9, 3))
            await loader.async_get_integration(hass, "sensor")
            assert await ha_sensor.async_setup(hass, {})
            hass.config.components.update({DOMAIN, "sensor"})
            base.component = hass.data[ha_sensor.DATA_COMPONENT]
            try:
                yield base
            finally:
                if entry.state is ConfigEntryState.LOADED:
                    assert await hass.config_entries.async_unload(entry.entry_id)
                elif runtime := hass.data.get(DOMAIN, {}).get(entry.entry_id):
                    if not runtime.get("setup_cleanup_ha_shutdown"):
                        await integration._async_cleanup_failed_setup(hass, entry, runtime)
    return world


def assert_failed_clean(world):
    assert world.entry.state in (ConfigEntryState.SETUP_ERROR, ConfigEntryState.SETUP_RETRY)
    assert world.entry.entry_id not in world.hass.data[DOMAIN]
    assert not tuple(world.component.entities)
    assert world.entry.entry_id not in world.component._platforms
    assert not world.entry._tasks and not world.entry._background_tasks


async def retry_success(world):
    method = (world.hass.config_entries.async_setup if world.entry.state is ConfigEntryState.NOT_LOADED
              else world.hass.config_entries.async_reload)
    assert await method(world.entry.entry_id)
    await world.hass.async_block_till_done()
    assert world.entry.state is ConfigEntryState.LOADED
    assert len(tuple(world.component.entities)) == 16
    runtime = world.hass.data[DOMAIN][world.entry.entry_id]
    assert runtime["sensor_setup_complete"] and not runtime["setup_cleanup_pending"]
    assert "sensor_setup_task" not in runtime
    assert runtime["billing_coordinator"]._unsub_daily_refresh is not None
    return runtime


@pytest.mark.parametrize("kind,expected", [
    ("os", ConfigEntryState.SETUP_RETRY),
    ("wrapped-os", ConfigEntryState.SETUP_RETRY),
    ("schema", ConfigEntryState.SETUP_ERROR),
    ("version", ConfigEntryState.SETUP_ERROR),
    ("wrapped-json", ConfigEntryState.SETUP_ERROR),
    ("other", ConfigEntryState.SETUP_ERROR),
])
def test_store_load_exception_classification_and_retry(setup_world, monkeypatch, kind, expected):
    async def scenario():
        async with setup_world() as world:
            if kind == "os":
                error = OSError("synthetic read failure")
            elif kind == "wrapped-os":
                error = HomeAssistantError("synthetic wrapped read failure")
                error.__cause__ = OSError("synthetic disk failure")
            elif kind == "wrapped-json":
                error = HomeAssistantError("synthetic non-I/O failure")
                error.__cause__ = ValueError("synthetic parse failure")
            else:
                error = {"schema": HistoryStoreSchemaError, "version": HistoryStoreVersionError,
                         "other": RuntimeError}[kind]("synthetic invalid input")
            with monkeypatch.context() as fault:
                fault.setattr(integration.CSGHistoryStore, "async_load", AsyncMock(side_effect=error))
                assert not await world.hass.config_entries.async_setup(world.entry.entry_id)
                assert world.entry.state is expected
                assert (world.entry._async_cancel_retry_setup is not None) is (expected is ConfigEntryState.SETUP_RETRY)
                assert_failed_clean(world)
            await retry_success(world)
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["before-timer", "after-timer", "auth", "retry"])
def test_real_core_swallowed_sensor_failure_rolls_back_then_recovers(setup_world, monkeypatch, caplog, kind):
    async def scenario():
        async with setup_world() as world:
            resources = []
            real_timer = sensor.BillingCoordinator.start_daily_refresh
            def timer(producer):
                resources.append(world.hass.data[DOMAIN][world.entry.entry_id])
                if kind == "after-timer":
                    real_timer(producer)
                error = ConfigEntryAuthFailed if kind == "auth" else ConfigEntryNotReady if kind == "retry" else RuntimeError
                raise error("synthetic sensor setup failure")
            with monkeypatch.context() as fault:
                fault.setattr(sensor.BillingCoordinator, "start_daily_refresh", timer)
                assert not await world.hass.config_entries.async_setup(world.entry.entry_id)
                if kind in ("before-timer", "after-timer"):
                    assert "synthetic sensor setup failure" in caplog.text
                assert_failed_clean(world)
                old = resources[0]
                assert old["sensor_setup_error"] == ("auth" if kind == "auth" else "retry" if kind == "retry" else "RuntimeError")
                assert old["setup_cleanup_phase"] == "complete"
                assert old["energy_statistics_bridge"]._shutdown
                assert not old["billing_coordinator"]._unsub_daily_refresh
                assert not old["realtime_coordinator"]._fact_updates
            await retry_success(world)
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["preload", "after-real-platform", "no-handshake"])
def test_forward_failures_reset_only_created_platform_and_recover(setup_world, monkeypatch, kind):
    async def scenario():
        async with setup_world() as world:
            real_forward = world.hass.config_entries.async_forward_entry_setups
            with monkeypatch.context() as fault:
                if kind == "preload":
                    imported = await loader.async_get_integration(world.hass, DOMAIN)
                    real_import = imported.async_get_platforms
                    async def preload(platforms):
                        if "sensor" in platforms:
                            raise ImportError("synthetic platform preload failure")
                        return await real_import(platforms)
                    fault.setattr(imported, "platforms_are_loaded", lambda _: False)
                    fault.setattr(imported, "async_get_platforms", preload)
                elif kind == "after-real-platform":
                    async def after(entry, platforms):
                        await real_forward(entry, platforms)
                        raise RuntimeError("synthetic forwarding failure after real setup")
                    fault.setattr(world.hass.config_entries, "async_forward_entry_setups", after)
                else:
                    # A forward adapter without the required success signal
                    # must fail exactly as an incomplete real platform would.
                    fault.setattr(world.hass.config_entries, "async_forward_entry_setups", AsyncMock())
                assert not await world.hass.config_entries.async_setup(world.entry.entry_id)
                assert_failed_clean(world)
            await retry_success(world)
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_again", [False, True])
def test_cancelled_setup_drains_real_physical_write_before_release_or_retry(setup_world, monkeypatch, cancel_again):
    async def scenario():
        async with setup_world() as world:
            entered, release, ended = threading.Event(), threading.Event(), threading.Event()
            real_write, first = ha_storage.write_utf8_file, True
            def write(path, *args, **kwargs):
                nonlocal first
                if first and Path(path).name == f"csg_plus.history_store.{world.entry.entry_id}":
                    first = False
                    entered.set()
                    try:
                        assert release.wait(10), "synthetic physical write watchdog"
                        return real_write(path, *args, **kwargs)
                    finally:
                        ended.set()
                return real_write(path, *args, **kwargs)
            monkeypatch.setattr(ha_storage, "write_utf8_file", write)
            setup = asyncio.create_task(world.hass.config_entries.async_setup(world.entry.entry_id))
            try:
                assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 3), 4)
                runtime = world.hass.data[DOMAIN][world.entry.entry_id]
                lane = runtime["history_store"]._store.hass._lane
                setup.cancel()
                await asyncio.sleep(0.03)
                assert runtime["setup_cleanup_phase"] == "sensor_task"
                assert world.hass.data[DOMAIN][world.entry.entry_id] is runtime
                assert not ended.is_set() and lane._issued > lane._finished
                assert not runtime["sensor_setup_complete"]
                assert not runtime["energy_statistics_bridge"]._accepting
                if cancel_again:
                    setup.cancel()
                    await asyncio.sleep(0.03)
                    assert world.hass.data[DOMAIN][world.entry.entry_id] is runtime
                    assert runtime["setup_cleanup_error"] == "CancelledError"
            finally:
                release.set()
            outcome = (await asyncio.wait_for(asyncio.gather(setup, return_exceptions=True), 5))[0]
            assert isinstance(outcome, asyncio.CancelledError)
            await world.hass.async_block_till_done()
            assert ended.is_set() and lane._issued == lane._finished
            assert not tuple(world.component.entities)
            assert not runtime["billing_coordinator"]._unsub_daily_refresh
            assert not runtime["realtime_coordinator"]._fact_updates
            if cancel_again:
                assert world.hass.data[DOMAIN][world.entry.entry_id] is runtime
                assert runtime["setup_cleanup_pending"]
            else:
                assert_failed_clean(world)
            new = await retry_success(world)
            assert new is not runtime
    asyncio.run(scenario())


def test_legal_empty_account_platform_completes_handshake(setup_world):
    async def scenario():
        async with setup_world() as world:
            world.hass.config_entries.async_update_entry(world.entry, data={**world.entry.data, CONF_ELE_ACCOUNTS: {}})
            assert await world.hass.config_entries.async_setup(world.entry.entry_id)
            assert world.entry.state is ConfigEntryState.LOADED
            assert not tuple(world.component.entities)
            runtime = world.hass.data[DOMAIN][world.entry.entry_id]
            assert runtime["sensor_setup_complete"] and "sensor_setup_task" not in runtime
    asyncio.run(scenario())


def test_consecutive_real_platform_failures_then_success_deduplicates_platform_domains(setup_world, monkeypatch):
    async def scenario():
        async with setup_world() as world:
            real_timer = sensor.BillingCoordinator.start_daily_refresh
            attempts = 0
            def timer(producer):
                nonlocal attempts
                attempts += 1
                real_timer(producer)
                if attempts <= 2:
                    raise RuntimeError("synthetic consecutive platform failure")
            monkeypatch.setattr(sensor.BillingCoordinator, "start_daily_refresh", timer)
            assert not await world.hass.config_entries.async_setup(world.entry.entry_id)
            assert_failed_clean(world)
            assert not await world.hass.config_entries.async_reload(world.entry.entry_id)
            assert_failed_clean(world)
            runtime = await retry_success(world)
            assert attempts == 3
            current = runtime["current_coordinator"]
            assert not current._shutdown_requested
            assert await world.hass.config_entries.async_unload(world.entry.entry_id)
            # Core's constructor-registered callback owns Current's shutdown.
            assert current._shutdown_requested and current._unsub_refresh is None
            assert not world.entry._on_unload
    asyncio.run(scenario())


@pytest.mark.parametrize("stop_first", [True, False])
def test_real_core_stop_retains_failed_setup_runtime_while_physical_write_continues(setup_world, monkeypatch, stop_first):
    async def scenario():
        async with setup_world() as world:
            entered, release, ended = threading.Event(), threading.Event(), threading.Event()
            stopping_entered, stopping_release = asyncio.Event(), asyncio.Event()
            original, first = ha_storage.write_utf8_file, True
            def write(path, *args, **kwargs):
                nonlocal first
                if first and Path(path).name == f"csg_plus.history_store.{world.entry.entry_id}":
                    first = False
                    entered.set()
                    try:
                        assert release.wait(10), "synthetic global stop physical write watchdog"
                        return original(path, *args, **kwargs)
                    finally:
                        ended.set()
                return original(path, *args, **kwargs)
            async def hold_stop(_event):
                stopping_entered.set()
                await stopping_release.wait()
            monkeypatch.setattr(ha_storage, "write_utf8_file", write)
            world.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, hold_stop)
            setup = asyncio.create_task(world.hass.config_entries.async_setup(world.entry.entry_id))
            stop = None
            try:
                assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 3), 4)
                runtime = world.hass.data[DOMAIN][world.entry.entry_id]
                lane = runtime["history_store"]._store.hass._lane
                if not stop_first:
                    setup.cancel()
                    await asyncio.sleep(0.03)
                    assert runtime["setup_cleanup_phase"] == "sensor_task"
                    assert not runtime.get("setup_cleanup_ha_shutdown")
                stop = asyncio.create_task(world.hass.async_stop(force=True))
                await asyncio.wait_for(stopping_entered.wait(), 3)
                assert world.hass.is_stopping
                if stop_first:
                    setup.cancel()
                else:
                    # Core stop permits cancelling the Store coroutine even
                    # after an ordinary drain already started. Its actual disk
                    # worker still owns the path after this extra cancellation.
                    runtime["sensor_setup_task"].cancel()
                outcome = (await asyncio.wait_for(asyncio.gather(setup, return_exceptions=True), 3))[0]
                assert isinstance(outcome, asyncio.CancelledError)
                assert not ended.is_set() and lane._issued > lane._finished
                assert runtime["setup_cleanup_ha_shutdown"] and runtime["setup_cleanup_phase"] == "ha_shutdown"
                assert world.hass.data[DOMAIN][world.entry.entry_id] is runtime
                assert runtime["sensor_setup_task"].done()
                assert not runtime["realtime_coordinator"]._fact_updates
                assert not tuple(world.component.entities)
                with pytest.raises(ConfigEntryError, match="Previous setup cleanup is incomplete"):
                    await integration.async_setup_entry(world.hass, world.entry)
                assert world.hass.data[DOMAIN][world.entry.entry_id] is runtime
                # Actual old runtime remains, even though cancelled coroutines
                # have retired. Its physical path lane is still unfinished.
            finally:
                release.set()
                assert await asyncio.wait_for(asyncio.to_thread(ended.wait, 3), 4)
                stopping_release.set()
                if stop is not None:
                    await asyncio.wait_for(stop, 5)
            assert world.hass.data[DOMAIN][world.entry.entry_id] is runtime
            assert runtime["setup_cleanup_pending"]
    asyncio.run(scenario())


def test_retired_platform_from_old_failure_is_not_unloaded_for_later_preload_failure(setup_world, monkeypatch):
    async def scenario():
        async with setup_world() as world:
            with monkeypatch.context() as first:
                first.setattr(sensor.BillingCoordinator, "start_daily_refresh", lambda _: (_ for _ in ()).throw(RuntimeError("synthetic first sensor failure")))
                assert not await world.hass.config_entries.async_setup(world.entry.entry_id)
                assert_failed_clean(world)
            imported = await loader.async_get_integration(world.hass, DOMAIN)
            real_import = imported.async_get_platforms
            async def preload(platforms):
                if "sensor" in platforms:
                    raise ImportError("synthetic second preload failure")
                return await real_import(platforms)
            with monkeypatch.context() as second:
                second.setattr(imported, "platforms_are_loaded", lambda _: False)
                second.setattr(imported, "async_get_platforms", preload)
                assert not await world.hass.config_entries.async_reload(world.entry.entry_id)
                assert_failed_clean(world)
            await retry_success(world)
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["current", "abort", "bridge", "platform-error", "platform-false"])
def test_cleanup_failure_retains_exact_runtime_and_blocks_replacement_until_recovered(setup_world, monkeypatch, kind):
    async def scenario():
        async with setup_world() as world:
            real_forward = world.hass.config_entries.async_forward_entry_setups
            factory = Mock(wraps=integration.CSGHistoryStore)
            old = None
            with monkeypatch.context() as fault:
                fault.setattr(integration, "CSGHistoryStore", factory)
                async def after(entry, platforms):
                    nonlocal old
                    await real_forward(entry, platforms)
                    old = world.hass.data[DOMAIN][entry.entry_id]
                    if kind == "current":
                        fault.setattr(old["current_coordinator"], "async_shutdown", AsyncMock(side_effect=RuntimeError("synthetic current cleanup failure")))
                    elif kind == "abort":
                        producer = old["realtime_coordinator"]
                        fault.setattr(producer, "async_shutdown", AsyncMock(side_effect=RuntimeError("synthetic producer cleanup failure")))
                        fault.setattr(producer, "async_abort", AsyncMock(side_effect=RuntimeError("synthetic abort failure")))
                    elif kind == "bridge":
                        fault.setattr(old["energy_statistics_bridge"], "async_shutdown", AsyncMock(side_effect=RuntimeError("synthetic Bridge cleanup failure")))
                    else:
                        fault.setattr(world.hass.config_entries, "async_unload_platforms", AsyncMock(
                            return_value=False, side_effect=RuntimeError("synthetic platform cleanup failure") if kind == "platform-error" else None,
                        ))
                    raise RuntimeError("synthetic forward failure requiring rollback")
                fault.setattr(world.hass.config_entries, "async_forward_entry_setups", after)
                assert not await world.hass.config_entries.async_setup(world.entry.entry_id)
                assert world.hass.data[DOMAIN][world.entry.entry_id] is old
                assert old["setup_cleanup_pending"] and old["setup_cleanup_error"] in ("RuntimeError", "ConfigEntryError")
                assert not old["energy_statistics_bridge"]._accepting
                assert factory.call_count == 1
                # Core reload of SETUP_ERROR does not invoke the integration's
                # normal unload. Admission itself must retain the old owner.
                assert not await world.hass.config_entries.async_reload(world.entry.entry_id)
                assert factory.call_count == 1
                assert world.hass.data[DOMAIN][world.entry.entry_id] is old
            new = await retry_success(world)
            assert new is not old and new["history_store"] is not old["history_store"]
            assert old["setup_cleanup_phase"] == "complete"
            assert old["energy_statistics_bridge"]._shutdown
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["failure", "cancellation"])
def test_failed_entry_does_not_touch_second_live_entry(setup_world, monkeypatch, kind):
    async def scenario():
        async with setup_world() as world:
            account = "fictional-second-live-account"
            second = ConfigEntry(
                version=1, minor_version=1, domain=DOMAIN, title="Synthetic second entry",
                data={**world.entry.data, CONF_ELE_ACCOUNTS: {account: CSGElectricityAccount(account).dump()}},
                options={}, source="user", unique_id=None, discovery_keys={}, subentries_data=None,
                entry_id="synthetic-second-entry",
            )
            world.hass.config_entries._entries[second.entry_id] = second
            assert await world.hass.config_entries.async_setup(second.entry_id)
            await world.hass.async_block_till_done()
            healthy = world.hass.data[DOMAIN][second.entry_id]
            healthy_entities = {entity.entity_id for entity in world.component.entities}
            assert len(healthy_entities) == 16
            healthy_facts = await healthy["history_store"].async_daily_usage_snapshot(account)
            try:
                if kind == "failure":
                    original = sensor.BillingCoordinator.start_daily_refresh
                    def timer(producer):
                        original(producer)
                        if producer.entry.entry_id == world.entry.entry_id:
                            raise RuntimeError("synthetic entry-local setup failure")
                    with monkeypatch.context() as fault:
                        fault.setattr(sensor.BillingCoordinator, "start_daily_refresh", timer)
                        assert not await world.hass.config_entries.async_setup(world.entry.entry_id)
                else:
                    entered, release = threading.Event(), threading.Event()
                    original = ha_storage.write_utf8_file
                    def write(path, *args, **kwargs):
                        if Path(path).name == f"csg_plus.history_store.{world.entry.entry_id}" and not entered.is_set():
                            entered.set()
                            assert release.wait(10), "synthetic isolated entry write watchdog"
                        return original(path, *args, **kwargs)
                    with monkeypatch.context() as fault:
                        fault.setattr(ha_storage, "write_utf8_file", write)
                        setup = asyncio.create_task(world.hass.config_entries.async_setup(world.entry.entry_id))
                        try:
                            await wait_entered(entered)
                            setup.cancel()
                            await asyncio.sleep(0.03)
                            assert world.hass.data[DOMAIN][second.entry_id] is healthy
                        finally:
                            release.set()
                        assert isinstance((await asyncio.gather(setup, return_exceptions=True))[0], asyncio.CancelledError)
                await world.hass.async_block_till_done()
                assert world.entry.entry_id not in world.hass.data[DOMAIN]
                assert second.state is ConfigEntryState.LOADED
                assert world.hass.data[DOMAIN][second.entry_id] is healthy
                assert {entity.entity_id for entity in world.component.entities} == healthy_entities
                assert healthy["billing_coordinator"]._unsub_daily_refresh is not None
                assert not healthy["realtime_coordinator"]._shutdown_requested
                assert not healthy["current_coordinator"]._shutdown_requested
                assert healthy["energy_statistics_bridge"]._accepting
                assert healthy["energy_statistics_bridge"]._unsubscribe is not None
                assert await healthy["history_store"].async_daily_usage_snapshot(account) == healthy_facts
            finally:
                assert await world.hass.config_entries.async_unload(second.entry_id)
    asyncio.run(scenario())


def test_cancelled_setup_keeps_already_enqueued_recorder_target_until_confirmation(setup_world, monkeypatch):
    async def scenario():
        async with setup_world() as world:
            real_forward = world.hass.config_entries.async_forward_entry_setups
            old = None
            forwarded = asyncio.Event()
            with blocked_import(monkeypatch, world.statistic_id) as (entered, release):
                with monkeypatch.context() as fault:
                    async def after(entry, platforms):
                        nonlocal old
                        await real_forward(entry, platforms)
                        old = world.hass.data[DOMAIN][entry.entry_id]
                        forwarded.set()
                        await asyncio.Event().wait()
                    fault.setattr(world.hass.config_entries, "async_forward_entry_setups", after)
                    setup = asyncio.create_task(world.hass.config_entries.async_setup(world.entry.entry_id))
                    try:
                        await wait_entered(entered)
                        await asyncio.wait_for(forwarded.wait(), 3)
                        assert old is not None
                        bridge = old["energy_statistics_bridge"]
                        target = bridge._lanes[world.statistic_id].target
                        assert target is not None
                        imports = len(world.imports)
                        setup.cancel()
                        await asyncio.sleep(0.03)
                        assert not setup.done()
                        assert world.hass.data[DOMAIN][world.entry.entry_id] is old
                        assert old["setup_cleanup_phase"] == "bridge"
                        assert bridge._lanes[world.statistic_id].target is target
                        assert len(world.imports) == imports
                    finally:
                        release.set()
                    outcome = (await asyncio.wait_for(asyncio.gather(setup, return_exceptions=True), 5))[0]
                    assert isinstance(outcome, asyncio.CancelledError)
                    assert_failed_clean(world)
                    assert bridge._lanes[world.statistic_id].target is None
                    assert [(row["state"], row["sum"]) for row in await world.query()] == [(1, 1)]
            new = await retry_success(world)
            await world.sync(new["energy_statistics_bridge"])
            assert new["energy_statistics_bridge"]._lanes is bridge._lanes
            assert len(world.imports) == imports
            assert [(row["state"], row["sum"]) for row in await world.query()] == [(1, 1)]
    asyncio.run(scenario())


@pytest.mark.parametrize("after_entities", [False, True])
def test_real_core_entity_add_failure_after_sensor_callback_is_not_false_success(setup_world, monkeypatch, after_entities):
    async def scenario():
        async with setup_world() as world:
            real_add = EntityPlatform.async_add_entities
            old = None
            async def add(platform, *args, **kwargs):
                nonlocal old
                old = world.hass.data[DOMAIN][world.entry.entry_id]
                assert old["sensor_setup_complete"] is False  # callback is eager
                if after_entities:
                    await real_add(platform, *args, **kwargs)
                raise RuntimeError("synthetic real entity-add failure")
            with monkeypatch.context() as fault:
                fault.setattr(EntityPlatform, "async_add_entities", add)
                assert not await world.hass.config_entries.async_setup(world.entry.entry_id)
                assert_failed_clean(world)
                assert old["sensor_setup_error"] == "entity_platform_incomplete"
                assert old["setup_cleanup_phase"] == "complete"
                assert all(task.done() for task in old["sensor_entity_tasks"])
                assert not old["billing_coordinator"]._unsub_daily_refresh
            await retry_success(world)
    asyncio.run(scenario())


@pytest.mark.parametrize("after_entities", [False, True])
def test_cancelled_real_core_entity_add_tasks_are_harvested_before_platform_reset(setup_world, monkeypatch, after_entities):
    async def scenario():
        async with setup_world() as world:
            entered, retired = asyncio.Event(), asyncio.Event()
            real_add = EntityPlatform.async_add_entities
            async def add(platform, *args, **kwargs):
                try:
                    if after_entities:
                        await real_add(platform, *args, **kwargs)
                    entered.set()
                    await asyncio.Event().wait()
                finally:
                    retired.set()
            with monkeypatch.context() as fault:
                fault.setattr(EntityPlatform, "async_add_entities", add)
                setup = asyncio.create_task(world.hass.config_entries.async_setup(world.entry.entry_id))
                await asyncio.wait_for(entered.wait(), 3)
                old = world.hass.data[DOMAIN][world.entry.entry_id]
                assert old["sensor_setup_complete"]
                assert old["sensor_entity_tasks"] and any(not task.done() for task in old["sensor_entity_tasks"])
                setup.cancel()
                outcome = (await asyncio.wait_for(asyncio.gather(setup, return_exceptions=True), 5))[0]
                assert isinstance(outcome, asyncio.CancelledError)
                assert retired.is_set()
                assert all(task.done() for task in old["sensor_entity_tasks"])
                assert_failed_clean(world)
                assert not old["billing_coordinator"]._unsub_daily_refresh
            await retry_success(world)
    asyncio.run(scenario())
