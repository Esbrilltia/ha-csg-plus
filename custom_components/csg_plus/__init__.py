# -*- coding: utf-8 -*-
"""The CSG Statistics Plus integration."""
from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady, HomeAssistantError,
)
from requests import RequestException
from homeassistant.helpers import entity_registry
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.entity_platform import async_get_platforms

from .const import (
    CONF_AUTH_TOKEN,
    CONF_ELE_ACCOUNTS,
    CONF_HISTORY_START_MONTH,
    CONF_LOGIN_TYPE,
    CONF_SETTINGS,
    CONF_UPDATED_AT,
    DOMAIN,
)
from .csg_client import (
    CSGAPIError,
    CSGClient,
    CSGElectricityAccount,
    InvalidCredentials,
    NotLoggedIn,
)
from .history_coordinator import HistoryCoordinator
from .history_store import CSGHistoryStore, HistoryStoreSchemaError
from .energy_statistics import EnergyStatisticsBridge

PLATFORMS: list[Platform] = [Platform.SENSOR]
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up CSG Statistics Plus from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    if previous := hass.data[DOMAIN].get(entry.entry_id):
        if not previous.get("setup_cleanup_pending"):
            raise ConfigEntryError("Entry resources are already active")
        try:
            await _async_cleanup_failed_setup(hass, entry, previous)
        except Exception as err:
            raise ConfigEntryError(
                f"Previous setup cleanup is incomplete: {type(err).__name__}"
            ) from err

    # validate session, re-authenticate if needed
    client = CSGClient.load(
        {
            CONF_AUTH_TOKEN: entry.data[CONF_AUTH_TOKEN],
        }
    )
    try:
        logged_in = await hass.async_add_executor_job(client.verify_login)
    except (CSGAPIError, RequestException) as err:
        raise ConfigEntryNotReady(f"Unable to contact China Southern Power Grid: {type(err).__name__}") from err
    if not logged_in:
        raise ConfigEntryAuthFailed("Login expired")

    history_store = CSGHistoryStore(hass, entry.entry_id)
    try:
        await history_store.async_load()
    except HistoryStoreSchemaError as err:
        raise ConfigEntryError(str(err)) from err
    except OSError as err:
        raise ConfigEntryNotReady("HistoryStore could not be read") from err
    except HomeAssistantError as err:
        if isinstance(err.__cause__, OSError):
            raise ConfigEntryNotReady("HistoryStore could not be read") from err
        raise
    bridge = EnergyStatisticsBridge(hass, entry, history_store)
    runtime = hass.data[DOMAIN][entry.entry_id] = {
        "history_store": history_store, "energy_statistics_bridge": bridge,
        "sensor_setup_complete": False, "setup_cleanup_pending": True,
        "sensor_platforms_before": {id(platform) for platform in async_get_platforms(hass, DOMAIN)},
    }

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        current_platforms = [
            platform for platform in async_get_platforms(hass, DOMAIN)
            if platform.config_entry is not None
            and platform.config_entry.entry_id == entry.entry_id
            and platform.domain in PLATFORMS
            and id(platform) not in runtime["sensor_platforms_before"]
        ]
        # Sensor's callback submits entity-add work which Core waits afterwards.
        # Its failure is also swallowed after the sensor coroutine completed.
        if current_platforms and any(not platform._setup_complete for platform in current_platforms):
            runtime["sensor_setup_complete"] = False
            runtime.setdefault("sensor_setup_error", "entity_platform_incomplete")
        # Core catches platform exceptions and can return without a successful
        # sensor setup. The platform's explicit handshake closes that gap.
        if not runtime["sensor_setup_complete"]:
            category = runtime.get("sensor_setup_error", "incomplete")
            if category == "auth":
                raise ConfigEntryAuthFailed("Sensor setup authentication failed")
            if category == "retry":
                raise ConfigEntryNotReady("Sensor platform setup is not ready")
            raise ConfigEntryError(f"Sensor platform setup failed: {category}")

        if entry.data.get(CONF_SETTINGS, {}).get(CONF_HISTORY_START_MONTH):
            history = HistoryCoordinator(hass, entry, history_store, bridge)
            runtime["history_coordinator"] = history
            history.start()

        bridge.request_sync()
    except (Exception, asyncio.CancelledError):
        try:
            await _async_cleanup_failed_setup(hass, entry, runtime)
        except (Exception, asyncio.CancelledError) as cleanup_error:
            # Keep ownership and safe diagnostics for retry admission. In
            # particular a cancelled waiter does not release a physical write.
            runtime["setup_cleanup_error"] = type(cleanup_error).__name__
            _LOGGER.warning("CSG failed setup cleanup is incomplete: %s", type(cleanup_error).__name__)
            if isinstance(cleanup_error, asyncio.CancelledError):
                raise
        raise

    runtime["setup_cleanup_pending"] = False
    runtime.pop("sensor_setup_task", None)
    runtime.pop("sensor_entity_tasks", None)
    return True


async def _async_cleanup_failed_setup(hass: HomeAssistant, entry: ConfigEntry, runtime: dict) -> None:
    """Stop only this failed entry before releasing its resource ownership."""
    runtime["setup_cleanup_pending"] = True
    if hass.is_stopping:
        runtime["setup_cleanup_ha_shutdown"] = True
    bridge = runtime.get("energy_statistics_bridge")
    if bridge is not None:
        bridge.stop_requests()
    runtime["setup_cleanup_phase"] = "sensor_task"
    task = runtime.get("sensor_setup_task")
    if task is not None and task is not asyncio.current_task():
        if not task.done() and not task.cancelling():
            task.cancel()
        # Shield the existing task drain, not a new cleanup task. A second
        # cancellation leaves runtime in place until a later admission retries.
        await asyncio.shield(asyncio.gather(task, return_exceptions=True))

    runtime["setup_cleanup_phase"] = "entity_tasks"
    entity_tasks = runtime.get("sensor_entity_tasks", ())
    for task in entity_tasks:
        if not task.done() and not task.cancelling():
            task.cancel()
    if entity_tasks:
        await asyncio.shield(asyncio.gather(*entity_tasks, return_exceptions=True))

    for key in ("history_coordinator", "billing_coordinator", "realtime_coordinator", "current_coordinator"):
        if (producer := runtime.get(key)) is None:
            continue
        runtime["setup_cleanup_phase"] = key
        try:
            await producer.async_shutdown()
        except Exception:
            if (abort := getattr(producer, "async_abort", None)) is None:
                raise
            await abort()

    runtime["setup_cleanup_phase"] = "bridge"
    if bridge is not None:
        await bridge.async_shutdown()
        # Existing Bridge shutdown can retain a Store-draining task after a
        # timeout/cancellation. Its actual retirement is required here.
        if (bridge_task := getattr(bridge, "_task", None)) is not None:
            await asyncio.shield(asyncio.gather(bridge_task, return_exceptions=True))

    if hass.is_stopping:
        runtime["setup_cleanup_ha_shutdown"] = True
    if runtime.get("setup_cleanup_ha_shutdown"):
        # Core stop intentionally permits a Store coroutine to retire while
        # its path-owned physical worker continues. Retain runtime rather than
        # infer physical quiescence from a completed/cancelled coroutine. This
        # remains conservative after Core reaches not_running; a new HA instance
        # reuses M3's path-owned ordering independently of this old runtime.
        runtime["setup_cleanup_phase"] = "ha_shutdown"
        raise ConfigEntryError("Failed setup resources retained during Home Assistant shutdown")

    runtime["setup_cleanup_phase"] = "platform"
    if not runtime.get("setup_platform_unloaded"):
        platforms = sorted({
            platform.domain for platform in async_get_platforms(hass, DOMAIN)
            if platform.config_entry is not None
            and platform.config_entry.entry_id == entry.entry_id
            and platform.domain in PLATFORMS
            and id(platform) not in runtime.get("sensor_platforms_before", ())
        })
        if platforms and not await hass.config_entries.async_unload_platforms(entry, platforms):
            raise ConfigEntryError("Failed sensor platform could not be unloaded")
        runtime["setup_platform_unloaded"] = True
    runtime["setup_cleanup_phase"] = "complete"
    if hass.data[DOMAIN].get(entry.entry_id) is not runtime:
        raise ConfigEntryError("Failed setup resource ownership changed")
    hass.data[DOMAIN].pop(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading CSG entry %s", entry.entry_id)
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    bridge = runtime.get("energy_statistics_bridge")
    if bridge is not None:
        bridge.stop_requests()
    cleanup_errors: list[Exception] = []
    for key in ("history_coordinator", "billing_coordinator", "realtime_coordinator"):
        producer = runtime.get(key)
        if producer is None:
            continue
        try:
            await producer.async_shutdown()
        except Exception as err:
            _LOGGER.warning("CSG %s cleanup failed; forcing coroutine shutdown", key, exc_info=True)
            if abort := getattr(producer, "async_abort", None):
                await abort()
            else:
                cleanup_errors.append(err)
    if cleanup_errors:
        # Unknown lifecycle failures cannot be treated as proven quiescence.
        raise cleanup_errors[0]
    if bridge is not None:
        try:
            await bridge.async_shutdown()
        except Exception:
            _LOGGER.warning(
                "CSG external statistics final materialization did not complete; "
                "HistoryStore facts remain authoritative and a future enabled Bridge sync can retry",
                exc_info=True,
            )
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    _LOGGER.debug("Unload CSG platforms for entry %s, success: %s", entry.entry_id, unload_ok)
    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Remove device"""
    _LOGGER.info("Removing CSG account device")
    account_num = list(device_entry.identifiers)[0][1]

    # remove entities
    entity_reg = entity_registry.async_get(hass)
    entities = {
        ent.unique_id: ent.entity_id
        for ent in entity_registry.async_entries_for_config_entry(
            entity_reg, config_entry.entry_id
        )
        if account_num in ent.unique_id
    }
    for entity_id in entities.values():
        entity_reg.async_remove(entity_id)

    # update config entry
    new_data = {
        **config_entry.data,
        CONF_ELE_ACCOUNTS: {
            account_number: account_data
            for account_number, account_data in config_entry.data[
                CONF_ELE_ACCOUNTS
            ].items()
            if account_number != account_num
        },
    }
    new_data[CONF_UPDATED_AT] = str(int(time.time() * 1000))
    hass.config_entries.async_update_entry(
        config_entry,
        data=new_data,
    )
    _LOGGER.info("Removed a linked electricity account")
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle removal of an entry."""
    _LOGGER.info("Removing CSG entry %s", entry.entry_id)

    # logout
    def client_logout():
        client = CSGClient.load(
            {
                CONF_AUTH_TOKEN: entry.data[CONF_AUTH_TOKEN],
            }
        )
        if client.verify_login():
            client.logout(entry.data[CONF_LOGIN_TYPE])
            _LOGGER.info("CSG account logged out")

    await hass.async_add_executor_job(client_logout)
