"""Independent A1 purposes, real Core lifecycle and temporary Recorder contracts.

Only fictional coordinator values and temporary SQLite are used. The optional
CSG_METADATA_EVIDENCE_DIR records observations outside the source checkout.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant.components.sensor import SensorStateClass, recorder as sensor_recorder
from homeassistant.components.sensor.const import DEVICE_CLASS_STATE_CLASSES, DEVICE_CLASS_UNITS
from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period
from homeassistant.components.recorder.tasks import StatisticsTask
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.recorder import DATA_RECORDER
from homeassistant.util import dt as dt_util

from custom_components.csg_plus import energy_statistics, sensor
from test_energy_statistics_recorder import ACCOUNT, recorder_world
from test_energy_statistics_recovery import platform_world


@dataclass(frozen=True)
class Purpose:
    suffix: str
    group: str
    role: str
    device_class: str | None
    unit: str | None
    state_class: str | None = None


# Declared independently of production descriptions and their ordering. Sources
# and business meanings are fixed in docs/quality/acceptance-contract.md.
PURPOSES = {
    "yesterday_usage": Purpose("yesterday_kwh", "REALTIME", "snapshot", "energy", "kWh"),
    "balance": Purpose("balance", "REALTIME", "snapshot", "monetary", "CNY"),
    "arrears": Purpose("arrears", "REALTIME", "snapshot", "monetary", "CNY"),
    "current_ladder": Purpose("current_ladder", "CURRENT", "tier", None, None),
    "current_ladder_remaining": Purpose("current_ladder_remaining_kwh", "CURRENT", "snapshot", "energy", "kWh"),
    "current_ladder_tariff": Purpose("current_ladder_tariff", "CURRENT", "unit_price", None, "CNY/kWh", "measurement"),
    "latest_settlement_usage": Purpose("latest_settlement_day_kwh", "BILLING", "snapshot", "energy", "kWh"),
    "latest_settlement_cost": Purpose("latest_settlement_day_cost", "BILLING", "snapshot", "monetary", "CNY"),
    "this_month_usage": Purpose("this_month_total_usage", "BILLING", "snapshot", "energy", "kWh"),
    "this_month_cost": Purpose("this_month_total_cost", "BILLING", "snapshot", "monetary", "CNY"),
    "last_month_usage": Purpose("last_month_total_usage", "BILLING", "snapshot", "energy", "kWh"),
    "last_month_cost": Purpose("last_month_total_cost", "BILLING", "snapshot", "monetary", "CNY"),
    "this_year_usage": Purpose("this_year_total_usage", "BILLING", "snapshot", "energy", "kWh"),
    "this_year_cost": Purpose("this_year_total_cost", "BILLING", "snapshot", "monetary", "CNY"),
    "last_year_usage": Purpose("last_year_total_usage", "BILLING", "snapshot", "energy", "kWh"),
    "last_year_cost": Purpose("last_year_total_cost", "BILLING", "snapshot", "monetary", "CNY"),
}
SNAPSHOTS = {key for key, purpose in PURPOSES.items() if purpose.role == "snapshot"}


def descriptions():
    return {
        name: tuple(getattr(sensor, f"{name}_DESCRIPTIONS"))
        for name in ("REALTIME", "CURRENT", "BILLING")
    }


@pytest.mark.parametrize("key,purpose", PURPOSES.items(), ids=PURPOSES)
def test_independent_purpose_metadata_unit_and_identity(key, purpose):
    groups = descriptions()
    all_descriptions = [description for group in groups.values() for description in group]
    assert len(all_descriptions) == len(PURPOSES) == 16
    assert len(SNAPSHOTS) == 14
    assert {description.translation_key for description in all_descriptions} == set(PURPOSES)
    matches = [description for description in groups[purpose.group] if description.translation_key == key]
    assert len(matches) == 1
    description = matches[0]
    assert (description.suffix, description.device_class, description.unit, description.state_class) == (
        purpose.suffix, purpose.device_class, purpose.unit, purpose.state_class,
    )
    if description.device_class is not None and description.state_class is not None:
        assert description.state_class in DEVICE_CLASS_STATE_CLASSES[description.device_class]
    if description.device_class in DEVICE_CLASS_UNITS:
        assert description.unit in DEVICE_CLASS_UNITS[description.device_class]
    entity = sensor.CSGSensor(SimpleNamespace(data={}, last_update_success=True), ACCOUNT, description)
    assert entity.unique_id == f"csg_plus.{ACCOUNT}.{purpose.suffix}"
    assert entity.translation_key == key
    assert entity.native_unit_of_measurement == purpose.unit
    assert entity.device_class == purpose.device_class
    assert entity.state_class == purpose.state_class
    assert entity.device_info["identifiers"] == {("csg_plus", ACCOUNT)}


def live_entities(world):
    entities = {entity.translation_key: entity for entity in world.component.entities}
    assert len(entities) == 16 and set(entities) == set(PURPOSES)
    registry = er.async_get(world.hass)
    for key, entity in entities.items():
        assert entity.unique_id == f"csg_plus.{ACCOUNT}.{PURPOSES[key].suffix}"
        registered = registry.async_get(entity.entity_id)
        assert registered is not None and registered.unique_id == entity.unique_id
        assert registered.config_entry_id == world.entry.entry_id
    return entities


async def apply_metadata_values(world, monkeypatch, value):
    """Exercise CSGSensor via actual coordinator listeners and Core state writes.

    The two daily cost placeholders get values only in this synthetic metadata
    exercise. Normal setup is separately checked to keep them unavailable.
    """
    entities = live_entities(world)
    current = entities["current_ladder_tariff"].coordinator
    monkeypatch.setattr(current, "current_tariff", lambda _account: value)
    groups = {}
    for key, entity in entities.items():
        groups.setdefault(entity.coordinator, {})[PURPOSES[key].suffix] = value
    for coordinator, values in groups.items():
        values["_yesterday_usage_date"] = "2026-09-02"
        coordinator.async_set_updated_data({ACCOUNT: values})
    await world.hass.async_block_till_done()
    result = {}
    for key, entity in entities.items():
        state = world.hass.states.get(entity.entity_id)
        assert state is not None
        # Force the real SensorEntity metadata validator also when unavailable.
        evaluated = entity.state
        if value == STATE_UNAVAILABLE:
            assert not entity.available and entity.native_value is None
            assert state.state == STATE_UNAVAILABLE and evaluated is None
        else:
            assert entity.available and entity.native_value == value
            assert float(state.state) == value and evaluated == value
        result[key] = {
            "entity_id": entity.entity_id, "unique_id": entity.unique_id,
            "device_class": entity.device_class, "unit": entity.native_unit_of_measurement,
            "state_class": entity.state_class, "available": entity.available,
            "state": state.state, "metadata_warning": entity._invalid_state_class_reported,
        }
    await world.drain()
    return result


def sensor_warnings(caplog, offset=0):
    return [record.getMessage() for record in caplog.records[offset:]
            if record.name.startswith("homeassistant.components.sensor")
            and record.levelno >= logging.WARNING]


def save_observation(name, data):
    if not (directory := os.environ.get("CSG_METADATA_EVIDENCE_DIR")):
        return
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    (target / name).write_text(json.dumps(data, indent=2, default=str, allow_nan=False) + "\n", encoding="utf-8")


def test_all_16_real_core_lifecycles_valid_zero_unavailable_have_no_sensor_warning(platform_world, monkeypatch, caplog):
    async def scenario():
        with caplog.at_level(logging.WARNING, logger="homeassistant.components.sensor"):
            async with platform_world() as world:
                initial = live_entities(world)
                for key in ("latest_settlement_cost", "this_month_cost"):
                    assert not initial[key].available and initial[key].native_value is None
                phases = {}
                for label, value in (("valid", 2), ("zero", 0), ("unavailable", STATE_UNAVAILABLE)):
                    phases[label] = await apply_metadata_values(world, monkeypatch, value)
                    assert all(not item["metadata_warning"] for item in phases[label].values())
                warnings = sensor_warnings(caplog)
                save_observation("lifecycle.json", {"phases": phases, "sensor_logging_warnings": warnings})
                assert warnings == []
    asyncio.run(scenario())


async def compile_interval(world, start):
    # The reused real platform fixture omits discovery of unrelated components.
    # Supply the actual sensor recorder platform, never a replacement compiler.
    world.hass.data[DATA_RECORDER].recorder_platforms["sensor"] = sensor_recorder
    await world.drain()
    world.recorder.queue_task(StatisticsTask(start, False))
    await world.drain()


async def read_rows(world, ids, period="5minute"):
    return await world.recorder.async_add_executor_job(
        statistics_during_period, world.hass, dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        None, ids, period, None, {"mean", "min", "max", "sum", "state"},
    )


async def seed_external(world):
    world.hass.config.currency = "CNY"
    store = world.runtime()["history_store"]
    await store.async_upsert_monthly_bill(ACCOUNT, (2026, 8), usage_kwh=5, cost_cny=4)
    await world.sync(world.runtime()["energy_statistics_bridge"])


async def assert_external(world, metadata, *, expected_energy=((1, 1),), expected_cost=((4, 4),)):
    digest = hashlib.sha256(ACCOUNT.encode("utf-8")).hexdigest()
    energy_id, cost_id = f"csg_plus:energy_{digest}", f"csg_plus:cost_{digest}"
    assert energy_id not in world.entities and cost_id not in world.entities
    assert world.hass.states.get(energy_id) is None and world.hass.states.get(cost_id) is None
    for statistic_id, unit, unit_class in ((energy_id, "kWh", "energy"), (cost_id, None, None)):
        actual = metadata[statistic_id][1]
        assert (actual["source"], actual["statistic_id"], actual["unit_of_measurement"],
                actual["unit_class"], actual["has_sum"], actual["has_mean"]) == (
            "csg_plus", statistic_id, unit, unit_class, True, False,
        )
    rows = await read_rows(world, {energy_id, cost_id}, "hour")
    assert tuple((row["state"], row["sum"]) for row in rows[energy_id]) == expected_energy
    assert tuple((row["state"], row["sum"]) for row in rows[cost_id]) == expected_cost
    return rows


def compilation_start():
    # A synthetic interval with a complete hour aggregation, independent of the
    # wall-clock minute. Real current states are carried forward by Core's own
    # history fallback; no SQL, statistics insert, or fake sum is supplied.
    return dt_util.utcnow().replace(minute=55, second=0, microsecond=0)


def test_clean_real_recorder_only_tariff_has_measurement_statistics(platform_world, monkeypatch, caplog):
    async def scenario():
        with caplog.at_level(logging.WARNING, logger="homeassistant.components.sensor"):
            async with platform_world() as world:
                await apply_metadata_values(world, monkeypatch, 2)
                await seed_external(world)
                start = compilation_start()
                await compile_interval(world, start)
                entities = live_entities(world)
                tariff_id = entities["current_ladder_tariff"].entity_id
                snapshot_ids = {entities[key].entity_id for key in SNAPSHOTS}
                snapshot_ids.add(entities["current_ladder"].entity_id)
                listed = await world.recorder.async_add_executor_job(sensor_recorder.list_statistic_ids, world.hass)
                assert set(listed) == {tariff_id}
                metadata = await world.recorder.async_add_executor_job(get_metadata, world.hass)
                assert snapshot_ids.isdisjoint(metadata)
                tariff = metadata[tariff_id][1]
                assert (tariff["source"], tariff["unit_of_measurement"], tariff["has_mean"], tariff["has_sum"]) == (
                    "recorder", "CNY/kWh", True, False,
                )
                observations = {}
                for period in ("5minute", "hour"):
                    rows = await read_rows(world, set(world.entities), period)
                    assert snapshot_ids.isdisjoint(rows)
                    assert set(rows) == {tariff_id} and rows[tariff_id]
                    assert all(row["mean"] == row["min"] == row["max"] == 2 for row in rows[tariff_id])
                    assert all(row.get("sum") is None for row in rows[tariff_id])
                    observations[period] = rows
                external = await assert_external(world, metadata)
                warnings = sensor_warnings(caplog)
                save_observation("clean-recorder.json", {
                    "compile_start": start, "listed": listed, "metadata": metadata,
                    "sensor_rows": observations, "external_rows": external,
                    "sensor_logging_warnings": warnings,
                })
                assert warnings == []
    asyncio.run(scenario())


def test_synthetic_beta1_upgrade_preserves_old_snapshot_statistics_without_extending_them(platform_world, monkeypatch, caplog):
    async def scenario():
        original = descriptions()
        with caplog.at_level(logging.WARNING, logger="homeassistant.components.sensor"):
            # Reproduce exactly the original 14 metadata combinations; only
            # state_class differs. All source wiring and entity identities stay
            # real. The original beta.1 warning evidence is a separate phase.
            for name, group in original.items():
                monkeypatch.setattr(sensor, f"{name}_DESCRIPTIONS", tuple(
                    replace(description, state_class=SensorStateClass.MEASUREMENT)
                    if description.translation_key in SNAPSHOTS else description
                    for description in group
                ))
            async with platform_world() as world:
                baseline_entities = live_entities(world)
                baseline_ids = {key: entity.entity_id for key, entity in baseline_entities.items()}
                await apply_metadata_values(world, monkeypatch, 2)
                await seed_external(world)
                start = compilation_start()
                await compile_interval(world, start)
                snapshots = {baseline_ids[key] for key in SNAPSHOTS}
                baseline = {period: await read_rows(world, snapshots, period) for period in ("5minute", "hour")}
                assert all(set(rows) == snapshots and all(series for series in rows.values()) for rows in baseline.values())
                old_metadata = await world.recorder.async_add_executor_job(get_metadata, world.hass)
                original_warnings = sensor_warnings(caplog)
                assert len([warning for warning in original_warnings if "is impossible considering device class" in warning]) == 14
                for name, group in original.items():
                    monkeypatch.setattr(sensor, f"{name}_DESCRIPTIONS", group)
                cursor = len(caplog.records)
                assert await world.hass.config_entries.async_reload(world.entry.entry_id)
                await world.hass.async_block_till_done()
                current_entities = live_entities(world)
                assert {key: entity.entity_id for key, entity in current_entities.items()} == baseline_ids
                await apply_metadata_values(world, monkeypatch, 3)
                store = world.runtime()["history_store"]
                await store.async_upsert_daily_usage(ACCOUNT, (2026, 9), [{"date": "2026-09-02", "kwh": 3}])
                await store.async_upsert_monthly_bill(ACCOUNT, (2026, 8), usage_kwh=5, cost_cny=6)
                await world.sync(world.runtime()["energy_statistics_bridge"])
                await compile_interval(world, start + dt.timedelta(hours=1))
                after = {period: await read_rows(world, snapshots, period) for period in ("5minute", "hour")}
                assert after == baseline
                metadata = await world.recorder.async_add_executor_job(get_metadata, world.hass)
                assert {key: metadata[key] for key in snapshots} == {key: old_metadata[key] for key in snapshots}
                listed = await world.recorder.async_add_executor_job(sensor_recorder.list_statistic_ids, world.hass)
                tariff_id = baseline_ids["current_ladder_tariff"]
                assert set(listed) == {tariff_id}
                tariff_rows = await read_rows(world, {tariff_id}, "hour")
                assert len(tariff_rows[tariff_id]) == 2
                assert all(row.get("sum") is None for row in tariff_rows[tariff_id])
                external = await assert_external(world, metadata, expected_energy=((1, 1), (3, 4)), expected_cost=((6, 6),))
                fixed_warnings = sensor_warnings(caplog, cursor)
                save_observation("synthetic-beta1-upgrade.json", {
                    "baseline_sensor_logging_warnings": original_warnings,
                    "fixed_sensor_logging_warnings": fixed_warnings,
                    "entity_ids_before_and_after": baseline_ids,
                    "baseline_snapshot_rows": baseline, "fixed_snapshot_rows": after,
                    "baseline_metadata": old_metadata, "fixed_metadata": metadata,
                    "fixed_tariff_rows": tariff_rows, "fixed_external_rows": external,
                    "synthetic_compile_start": start,
                })
                assert fixed_warnings == []
    asyncio.run(scenario())
