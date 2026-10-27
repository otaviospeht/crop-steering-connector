"""The closed action table and the parsing of a claimed command.

The server names the intent; the connector alone picks the HA service and sends only `entity_id` plus the
keys below. `power_on` on a climate is `climate.set_hvac_mode` with `cool`, a constant of this file, and
`climate.turn_on` is never called: without a custom `turn_on` HA picks HEAT_COOL, then HEAT, then COOL.
Heat is its own fixed action, `set_heat_mode`, so no free string from the server reaches it. It is one
`climate.set_temperature` with the constant `hvac_mode: heat` and the server's `temperature`, so the unit enters heat
already at that setpoint: an LG keeps a setpoint per mode, and a mode alone would resume its old heat setpoint, above
the crop's limit. The temperature must sit in the entity's live range and, when it reports `target_temp_step`, on that
step from `min_temp`.
A pump's `pulse_on` carries `max_on_seconds` for the pump watch only: a connector key, checked
here and never copied into the HA body.
A dehumidifier's `set_humidity` is `humidifier.set_humidity` with an integer `humidity` that must sit
in the entity's live `min_humidity` and `max_humidity` and, when it reports `target_humidity_step`, on that step
from the minimum. It runs only on a `device_class` of `dehumidifier`: the platform never commands a humidifier.
The table has no `number` action: the pulse a plug holds is only read, so no server answer can make the connector
write it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from .entities import ENTITY_ID_RE

COMMAND_ID_RE = re.compile(r"^[0-9A-Za-z]{26}$")
WRITABLE_HVAC_MODES = frozenset({"cool", "dry", "fan_only"})
POWER_ON_HVAC_MODE = "cool"
HEAT_HVAC_MODE = "heat"
# HA's own defaults when a climate does not publish its range (climate/const.py).
DEFAULT_MIN_TEMP = 7.0
DEFAULT_MAX_TEMP = 35.0
NEEDS_POWER_ON = frozenset({"set_hvac_mode", "set_heat_mode", "set_temperature", "set_humidity"})
TEMPERATURE_ACTIONS = frozenset({"set_temperature", "set_heat_mode"})
HUMIDITY_RANGE = (0, 100)
DEHUMIDIFIER_DEVICE_CLASS = "dehumidifier"
# P + 15 s with the pulse P from 5 to 900 s, a flush pulse included.
MAX_ON_SECONDS_RANGE = (20, 915)
STEP_TOLERANCE = 1e-6


@dataclass(frozen=True)
class ServiceSpec:
    domain: str
    service: str
    data_keys: tuple[str, ...] = ()
    fixed: tuple[tuple[str, str], ...] = ()
    connector_keys: tuple[str, ...] = ()


ACTIONS: dict[tuple[str, str], ServiceSpec] = {
    ("climate", "power_on"): ServiceSpec("climate", "set_hvac_mode", fixed=(("hvac_mode", POWER_ON_HVAC_MODE),)),
    ("climate", "power_off"): ServiceSpec("climate", "turn_off"),
    ("climate", "set_hvac_mode"): ServiceSpec("climate", "set_hvac_mode", data_keys=("hvac_mode",)),
    ("climate", "set_heat_mode"): ServiceSpec(
        "climate", "set_temperature", data_keys=("temperature",), fixed=(("hvac_mode", HEAT_HVAC_MODE),)
    ),
    ("climate", "set_temperature"): ServiceSpec("climate", "set_temperature", data_keys=("temperature",)),
    ("humidifier", "power_on"): ServiceSpec("humidifier", "turn_on"),
    ("humidifier", "power_off"): ServiceSpec("humidifier", "turn_off"),
    ("humidifier", "set_humidity"): ServiceSpec("humidifier", "set_humidity", data_keys=("humidity",)),
    ("switch", "power_on"): ServiceSpec("switch", "turn_on"),
    ("switch", "power_off"): ServiceSpec("switch", "turn_off"),
    ("switch", "pulse_on"): ServiceSpec("switch", "turn_on", connector_keys=("max_on_seconds",)),
}


class InvalidCommand(Exception):
    """A command outside the table: unknown domain, action or key, or a value outside the table."""


@dataclass(frozen=True)
class Command:
    id: str
    entity_id: Any
    action: Any
    data: Any
    expires_in_seconds: Any
    received_at: float

    @property
    def domain(self) -> str:
        if isinstance(self.entity_id, str) and "." in self.entity_id:
            return self.entity_id.split(".", 1)[0]
        return ""

    @property
    def label_entity(self) -> str:
        return self.entity_id if isinstance(self.entity_id, str) and ENTITY_ID_RE.fullmatch(self.entity_id) else "?"

    @property
    def label_action(self) -> str:
        return self.action if isinstance(self.action, str) and self.action.isidentifier() else "?"


def parse_command(raw: Any, received_at: float) -> Command | None:
    """None when the item has no usable id: such an item can get no result, so it is only logged."""
    if not isinstance(raw, dict):
        return None
    command_id = raw.get("id")
    if not isinstance(command_id, str) or not COMMAND_ID_RE.fullmatch(command_id):
        return None
    return Command(
        id=command_id,
        entity_id=raw.get("entity_id"),
        action=raw.get("action"),
        data=raw.get("data"),
        expires_in_seconds=raw.get("expires_in_seconds"),
        received_at=received_at,
    )


def deadline_of(command: Command) -> float | None:
    """The local pre-filter deadline on the monotonic clock, or None when `expires_in_seconds` is unusable."""
    value = command.expires_in_seconds
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return command.received_at + float(value)


def service_spec(command: Command) -> ServiceSpec:
    """The HA service of a command, after the checks of the table that need no state."""
    if not isinstance(command.entity_id, str) or not ENTITY_ID_RE.fullmatch(command.entity_id):
        raise InvalidCommand("entity_id")
    if not isinstance(command.action, str):
        raise InvalidCommand("action")
    spec = ACTIONS.get((command.domain, command.action))
    if spec is None:
        raise InvalidCommand("action")
    data = command.data
    if not isinstance(data, dict) or set(data) != {*spec.data_keys, *spec.connector_keys}:
        raise InvalidCommand("data")
    if command.action == "set_hvac_mode" and data["hvac_mode"] not in WRITABLE_HVAC_MODES:
        raise InvalidCommand("hvac_mode")
    if command.action in TEMPERATURE_ACTIONS and not _is_number(data["temperature"]):
        raise InvalidCommand("temperature")
    if command.action == "set_humidity" and not _is_humidity(data["humidity"]):
        raise InvalidCommand("humidity")
    if command.action == "pulse_on" and max_on_seconds(command) is None:
        raise InvalidCommand("max_on_seconds")
    return spec


def max_on_seconds(command: Command) -> int | None:
    """The pump watch's limit for a `pulse_on`, or None when it is not a whole number of seconds in range."""
    value = command.data.get("max_on_seconds") if isinstance(command.data, dict) else None
    low, high = MAX_ON_SECONDS_RANGE
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        return None
    return value


def _is_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _is_humidity(value: Any) -> bool:
    """HA coerces `humidity` to an int; a float or a bool from the server is refused, never rounded."""
    low, high = HUMIDITY_RANGE
    return not isinstance(value, bool) and isinstance(value, int) and low <= value <= high


def _number(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return float(value)


def service_body(command: Command, spec: ServiceSpec, live_state: dict) -> dict:
    """The body of the HA call; a temperature or a humidity must sit in the entity's own range."""
    body: dict[str, Any] = {"entity_id": command.entity_id}
    for key, value in spec.fixed:
        body[key] = value
    for key in spec.data_keys:
        body[key] = command.data[key]
    attributes = live_state.get("attributes") if isinstance(live_state.get("attributes"), dict) else {}
    if command.action in TEMPERATURE_ACTIONS:
        low = _number(attributes.get("min_temp"), DEFAULT_MIN_TEMP)
        high = _number(attributes.get("max_temp"), DEFAULT_MAX_TEMP)
        if not low <= float(body["temperature"]) <= high:
            raise InvalidCommand("temperature range")
        if command.action == "set_heat_mode":
            _check_step(body["temperature"], low, attributes, "target_temp_step")
    if command.action == "set_humidity":
        _check_humidity_target(body["humidity"], attributes)
    return body


def _check_humidity_target(value: int, attributes: dict) -> None:
    """Fail closed: missing bounds refuse, and a present step that is not a positive number is never
    read as absent."""
    if attributes.get("device_class") != DEHUMIDIFIER_DEVICE_CLASS:
        raise InvalidCommand("device_class")
    low, high = attributes.get("min_humidity"), attributes.get("max_humidity")
    if not (_is_number(low) and _is_number(high)) or not low <= value <= high:
        raise InvalidCommand("humidity range")
    _check_step(value, low, attributes, "target_humidity_step")


def _check_step(value: float, low: float, attributes: dict, key: str) -> None:
    """No step reported means none to check; a reported step that is not a positive number refuses."""
    if key not in attributes:
        return
    step = attributes[key]
    if not _is_number(step) or step <= 0:
        raise InvalidCommand("step")
    steps = (value - low) / step
    if abs(steps - round(steps)) > STEP_TOLERANCE:
        raise InvalidCommand("step")
