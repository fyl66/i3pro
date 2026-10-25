"""Battery power and cumulative energy channels.

The raw signals use opposite sign conventions:

* ``Battery Power`` is positive while the pack is discharging and negative
  while it is being charged.
* ``I Data`` is negative while discharging and positive while charging, so
  the IVT fallback is ``-I Data * U1 Data / 1000``.

The public entry point used by the maths engine is :func:`battery_power`; it
returns kW on the master time base. :func:`cumulative` is kept as a pure
reference implementation for tests and tools.
"""

from __future__ import annotations

import numpy as np

from . import derive, timebase

__all__ = [
    "ACTIVE_POWER_KW",
    "MIN_BUS_VOLTAGE",
    "battery_power",
    "cumulative",
    "power_series",
]

#: Below this the pack power is treated as a placeholder (the C125 template
#: channels are often exactly zero or a tiny negative sentinel).
ACTIVE_POWER_KW = 0.1

#: The IVT fallback only trusts samples above this bus voltage. This mirrors
#: the mask used by ``tools/bus_current.py`` and excludes precharge / off data.
MIN_BUS_VOLTAGE = 100.0


def _channel(session, name: str) -> str | None:
    return derive.resolve_channel(session, name, derive.name_table(session))


def _series(session, name: str) -> np.ndarray:
    values = np.asarray(derive.hold_to_master(session, name), dtype=np.float64)
    return np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0)


def _ivt_power(session) -> tuple[str, np.ndarray] | None:
    current = _channel(session, "I Data")
    voltage = _channel(session, "U1 Data")
    if current is None or voltage is None:
        return None
    amps = _series(session, current)
    volts = _series(session, voltage)
    if volts.size == 0 or float(np.max(volts)) <= MIN_BUS_VOLTAGE:
        return None
    power = np.zeros_like(volts)
    valid = volts > MIN_BUS_VOLTAGE
    power[valid] = -amps[valid] * volts[valid] / 1000.0
    return f"{current} × {voltage}", np.nan_to_num(
        power, nan=0.0, posinf=0.0, neginf=0.0
    )


def power_series(session) -> tuple[str, np.ndarray] | None:
    """Return ``(source, power_kw)`` or ``None`` when no source is usable.

    Priority:

    1. ``Battery Power`` when it has at least one active finite sample.
    2. ``I Data`` + ``U1 Data`` with the logger's sign convention.
    3. ``Battery Power`` when it exists but is entirely a zero placeholder.
    4. ``None`` when neither source exists.
    """
    battery = _channel(session, "Battery Power")
    battery_power_values: np.ndarray | None = None
    if battery is not None:
        battery_power_values = _series(session, battery)
        if (battery_power_values.size
                and float(np.max(np.abs(battery_power_values))) > ACTIVE_POWER_KW):
            return battery, battery_power_values

    ivt = _ivt_power(session)
    if ivt is not None:
        return ivt

    if battery is not None and battery_power_values is not None:
        return battery, battery_power_values
    return None


def battery_power(session) -> np.ndarray | None:
    """Master-time-base pack power in kW; ``None`` when no source exists."""
    got = power_series(session)
    return None if got is None else got[1]


def cumulative(session) -> dict[str, object] | None:
    """Cumulative energy in kWh, using the same trapezoid rule as ``integrate``."""
    got = power_series(session)
    if got is None:
        return None
    source, power = got
    axis = timebase.axis(session)
    size = min(power.size, axis.size)
    power = np.asarray(power[:size], dtype=np.float64)
    axis = np.asarray(axis[:size], dtype=np.float64)

    drive = np.zeros(size, dtype=np.float64)
    regen = np.zeros(size, dtype=np.float64)
    if size > 1:
        step = np.diff(axis)
        drive[1:] = np.cumsum(
            0.5 * (np.maximum(power[1:], 0.0) + np.maximum(power[:-1], 0.0)) * step
        ) / 3600.0
        regen[1:] = np.cumsum(
            0.5 * (np.maximum(-power[1:], 0.0) + np.maximum(-power[:-1], 0.0)) * step
        ) / 3600.0
    return {
        "source": source,
        "Drive Energy": drive,
        "Regen Energy": regen,
        "Net Energy": drive - regen,
    }
