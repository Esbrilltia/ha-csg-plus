"""Shared safe input and response-local daily and official bill validation."""

from __future__ import annotations

import datetime as dt
import logging
import math
import re
from collections.abc import Iterable, Mapping
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.util import dt as dt_util

from .csg_client.const import WF_ATTR_CHARGE, WF_ATTR_DATE, WF_ATTR_KWH, WF_ATTR_MONTH
from .utils import account_log_id

_LOGGER = logging.getLogger(__name__)
_CSG_TIME_ZONE = ZoneInfo("Asia/Shanghai")
_DAILY_VALUE_ABS_TOL = 1e-9


class ResponseValidationError(ValueError):
    """A malformed response container, with no runtime payload in its message."""


def finite_number(value: Any, *, nonnegative: bool = False) -> float | None:
    """Keep absent/invalid numbers separate from zero and signed money."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or (nonnegative and number < 0):
        return None
    return number


def validate_history_year(year: Any) -> int:
    """Require a Python-calendar year, without boolean/inexact coercion."""
    if type(year) is not int or not 1 <= year <= 9999:
        raise ValueError("Invalid history year")
    return year


def validate_history_month(month: Any) -> tuple[int, int]:
    """Validate an internal requested month before constructing calendar keys."""
    if not isinstance(month, tuple) or len(month) != 2:
        raise ValueError("Invalid history month")
    year, month_number = month
    validate_history_year(year)
    if type(month_number) is not int or not 1 <= month_number <= 12:
        raise ValueError("Invalid history month")
    return year, month_number


def csg_today() -> dt.date:
    """Return the Shanghai natural day used to bound one validation batch."""
    return dt_util.utcnow().astimezone(_CSG_TIME_ZONE).date()


def validated_daily_date(
    value: Any, month: tuple[int, int], today: dt.date,
) -> str | None:
    """Normalize an in-request calendar day which has actually occurred."""
    if not isinstance(value, str):
        return None
    try:
        day = dt.date.fromisoformat(value)
    except ValueError:
        return None
    if (day.year, day.month) != month or day > today:
        return None
    return day.isoformat()


def response_rows(response: Any, key: str, kind: str) -> tuple[Mapping, list]:
    """Distinguish a malformed batch from its legitimate empty row list."""
    if not isinstance(response, Mapping) or not isinstance(response.get(key), list):
        raise ResponseValidationError(f"Invalid {kind} response container")
    return response, response[key]


def collect_daily_usage_candidates(
    days: Iterable[Any], account: str, month: tuple[int, int],
    *, today: dt.date | None = None,
) -> dict[str, float]:
    """Resolve this batch with the fact layer's existing stable conflict rule.

    Callers retain the observation list separately: a valid date marker is
    coverage evidence, not a candidate or a replacement for an old stored fact.
    """
    month = validate_history_month(month)
    today = today if today is not None else csg_today()
    if not isinstance(days, Iterable) or isinstance(days, (str, bytes, Mapping)):
        raise ResponseValidationError("Invalid daily usage rows container")
    candidate_sets: dict[str, set[float]] = {}
    for item in days:
        if not isinstance(item, Mapping):
            continue
        day_key = validated_daily_date(item.get(WF_ATTR_DATE), month, today)
        value = finite_number(item.get(WF_ATTR_KWH), nonnegative=True)
        if day_key is not None and value is not None:
            candidate_sets.setdefault(day_key, set()).add(value)

    candidates: dict[str, float] = {}
    for day_key, values in sorted(candidate_sets.items()):
        # Approximate equality is not transitive: compare the full range.
        smallest = min(values)
        if not math.isclose(smallest, max(values), rel_tol=0.0, abs_tol=_DAILY_VALUE_ABS_TOL):
            _LOGGER.warning(
                "Conflicting daily usage values for %s on %s; skipped date",
                account_log_id(account), day_key,
            )
            continue
        candidates[day_key] = smallest
    return candidates


def parse_history_start_month(value: Any) -> tuple[int, int]:
    """Parse a locale-independent, strict YYYY-MM calendar month."""
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9]{4}-[0-9]{2}", value) is None
    ):
        raise ValueError("History start month must be YYYY-MM")
    day = dt.date.fromisoformat(f"{value}-01")
    return day.year, day.month


def month_key(month: tuple[int, int]) -> str:
    """Return the canonical month key."""
    return f"{month[0]:04d}-{month[1]:02d}"


def _parse_bill_month(value: Any) -> tuple[int, int] | None:
    """Accept only the API's YYYYMM and YYYY-MM calendar month formats."""
    try:
        text = str(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if re.fullmatch(r"[0-9]{4}-?[0-9]{2}", text) is None:
        return None
    compact = text.replace("-", "")
    year, month = int(compact[:4]), int(compact[4:])
    try:
        dt.date(year, month, 1)
    except ValueError:
        return None
    return year, month


def collect_monthly_bill_candidates(
    by_month: Iterable[Any],
    account: str,
    year: int,
) -> dict[tuple[int, int], tuple[float | None, float | None]]:
    """Accept one candidate per requested-year month without response conflicts."""
    year = validate_history_year(year)
    if not isinstance(by_month, Iterable) or isinstance(by_month, (str, bytes, Mapping)):
        raise ResponseValidationError("Invalid monthly bill rows container")
    candidates: dict[tuple[int, int], dict[str, set[float]]] = {}
    for row in by_month:
        month = _parse_bill_month(
            row.get(WF_ATTR_MONTH) if isinstance(row, Mapping) else None
        )
        if month is None:
            _LOGGER.warning("Skipped malformed monthly bill month for %s/%s", account_log_id(account), year)
            continue
        if month[0] != year:
            _LOGGER.warning(
                "Skipped monthly bill %04d-%02d outside requested year %s for %s",
                month[0], month[1], year, account_log_id(account),
            )
            continue
        fields = candidates.setdefault(
            month, {WF_ATTR_KWH: set(), WF_ATTR_CHARGE: set()}
        )
        for key in fields:
            number = finite_number(row.get(key), nonnegative=True)
            if number is not None:
                fields[key].add(number)

    accepted: dict[tuple[int, int], tuple[float | None, float | None]] = {}
    for month, fields in sorted(candidates.items()):
        if any(len(values) > 1 for values in fields.values()):
            _LOGGER.warning(
                "Skipped monthly bill conflict for %s/%04d-%02d", account_log_id(account), month[0], month[1]
            )
            continue
        accepted[month] = (
            next(iter(fields[WF_ATTR_KWH]), None),
            next(iter(fields[WF_ATTR_CHARGE]), None),
        )
    return accepted
