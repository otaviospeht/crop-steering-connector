"""The device info (0.2.13): the manufacturer, the model name, the model code and the firmware of an HA device, from
the device registry, and the cleaning both sides apply to each value (the `device_info_cleaning` vectors of the
contract). Nothing else of the registry is read: never the serial number, the hardware version, the connections (MAC
addresses), the configuration URL, the raw identifiers, the area or the parent device.
"""

from __future__ import annotations

import re
import unicodedata

# The wire keys, in this order, and the registry attribute behind each one: HA's `sw_version` leaves as `firmware`.
FIELDS = ("manufacturer", "model", "model_id", "firmware")
REGISTRY_ATTRIBUTES = ("manufacturer", "model", "model_id", "sw_version")
MAX_LENGTHS = {"manufacturer": 80, "model": 80, "model_id": 80, "firmware": 64}
MAX_DEVICE_INFO_ENTRIES = 200
# Control, format, surrogate, line and paragraph separator characters leave before anything else is checked.
REMOVED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})

_HEX = "[0-9A-Fa-f]"
_OCTET = r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"
_GROUP = f"{_HEX}{{1,4}}"
# A value holding any of these, anywhere, could carry a network address of the house: it leaves as null. The classes
# are ASCII on purpose (Python's `\d` would take any Unicode digit).
_NETWORK = (
    # A MAC address: six hex pairs joined by `:` or `-`.
    re.compile(f"{_HEX}{{2}}(?:[:-]{_HEX}{{2}}){{5}}"),
    # An IPv4 address: four numbers of 0 to 255 joined by dots, not next to another digit or dot.
    re.compile(rf"(?<![0-9.]){_OCTET}(?:\.{_OCTET}){{3}}(?![0-9.])"),
    # An IPv6 address: eight groups, or `::` beside a group.
    re.compile(
        rf"(?<![0-9A-Za-z:])(?:{_GROUP}:){{7}}{_GROUP}(?![0-9A-Za-z:])"
        rf"|(?<![0-9A-Za-z]){_GROUP}::"
        rf"|::{_GROUP}(?![0-9A-Za-z])"
    ),
    # A URL: a scheme and `://`, or `www.`.
    re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://|www\.", re.IGNORECASE),
    # An e-mail address: a name, the at sign and a dotted domain.
    re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+"),
)
# A firmware made only of digits and dots is a version (`1.0.0.12`), never an IPv4.
_VERSION = re.compile(r"[0-9.]+")
# HA's Smart Life integration named a device it pre-registered without support «<product name> (unsupported)» until
# 2026.10; the connector alone drops that suffix from the model, before the shared rule, so the model is the product's
# name on every HA version.
UNSUPPORTED_SUFFIX = " (unsupported)"

DeviceInfo = tuple[str | None, str | None, str | None, str | None]


def clean_device_value(field: str, value: object) -> str | None:
    """One value by the shared rule, in order: not a string is null; the characters of `REMOVED_CATEGORIES` out, then
    trimmed, and empty is null; a value that looks like a MAC, an IP, a URL or an e-mail anywhere is null, save a
    firmware of digits and dots alone; then cut at 80 code points, 64 for the firmware."""
    text = _trimmed(value)
    if not text:
        return None
    if not (field == "firmware" and _VERSION.fullmatch(text)) and any(rule.search(text) for rule in _NETWORK):
        return None
    return text[: MAX_LENGTHS[field]]


def _trimmed(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return "".join(char for char in value if unicodedata.category(char) not in REMOVED_CATEGORIES).strip()


def without_unsupported(value: object) -> object:
    """The model with a trailing « (unsupported)» dropped, once, after the shared removal and trim (a model of the
    suffix alone, as HA wrote for an empty product name, is empty); any other value as it came."""
    text = _trimmed(value)
    if text == UNSUPPORTED_SUFFIX.lstrip():
        return ""
    if text is None or not text.endswith(UNSUPPORTED_SUFFIX):
        return value
    return text[: -len(UNSUPPORTED_SUFFIX)]


def clean_device_info(values: list) -> DeviceInfo:
    """The four registry values of one device, in `REGISTRY_ATTRIBUTES` order, cleaned into `FIELDS` order."""
    manufacturer, model, model_id, firmware = values
    return (
        clean_device_value("manufacturer", manufacturer),
        clean_device_value("model", without_unsupported(model)),
        clean_device_value("model_id", model_id),
        clean_device_value("firmware", firmware),
    )


def wire(info: DeviceInfo) -> dict[str, str | None]:
    """The entry of one device as the report sends it: always the four keys, nulls included."""
    return dict(zip(FIELDS, info))
