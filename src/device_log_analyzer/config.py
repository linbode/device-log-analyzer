from __future__ import annotations

import re
import tomllib
from datetime import date
from pathlib import Path
from typing import Any

from .models import DeviceConfig, RuleConfig, SiteConfig


class ConfigError(ValueError):
    pass


_DURATION_RE = re.compile(
    r"^\s*(?:(?P<h>\d+)\s*h)?\s*(?:(?P<m>\d+)\s*m)?\s*(?:(?P<s>\d+)\s*s)?\s*$",
    re.IGNORECASE,
)


def parse_duration(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    match = _DURATION_RE.fullmatch(text)
    if not match or not any(match.groupdict().values()):
        raise ConfigError(f"Invalid duration: {value!r}")
    return (
        int(match.group("h") or 0) * 3600
        + int(match.group("m") or 0) * 60
        + int(match.group("s") or 0)
    )


def _date(value: Any, field: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ConfigError(f"{field} must use YYYY-MM-DD: {value!r}") from exc


def load_config(path: Path) -> SiteConfig:
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc

    pilot = raw.get("pilot", {})

    rules_raw = raw.get("rules", {})
    rules = RuleConfig(
        implicit_close_seconds=int(rules_raw.get("implicit_close_seconds", 5)),
        cross_day_rollback_seconds=int(
            rules_raw.get("cross_day_rollback_seconds", 12 * 3600)
        ),
        orphan_close_max_seconds=int(
            rules_raw.get("orphan_close_max_seconds", 12 * 3600)
        ),
        ghost_segment_seconds=int(
            rules_raw.get("ghost_segment_seconds", 24 * 3600)
        ),
        silence_segment_seconds=int(
            rules_raw.get("silence_segment_seconds", 4 * 3600)
        ),
    )

    devices_raw = raw.get("devices", {})
    overrides_raw = raw.get("device_overrides", {})
    if not isinstance(devices_raw, dict) or not devices_raw:
        raise ConfigError("At least one entry is required under [devices].")

    devices: list[DeviceConfig] = []
    seen_ids: set[str] = set()
    seen_folders: set[str] = set()
    for device_id, value in devices_raw.items():
        if not isinstance(value, dict):
            raise ConfigError(
                f'Device "{device_id}" must be a table, for example '
                f'[devices."{device_id}"].'
            )
        if device_id in seen_ids:
            raise ConfigError(f"Duplicate device id: {device_id}")
        folder = str(value.get("folder", device_id))
        if folder in seen_folders:
            raise ConfigError(f"Duplicate device folder: {folder}")
        override = overrides_raw.get(device_id, {})
        if not isinstance(override, dict):
            raise ConfigError(f"Device override must be a table: {device_id}")

        merged = {**value, **override}
        channels = tuple(str(ch) for ch in value.get("channels", ["CH1", "CH2", "CH3"]))
        if not channels:
            raise ConfigError(f"Device {device_id} has no channels.")
        device = DeviceConfig(
            id=device_id,
            display_name=str(value.get("display_name", device_id)),
            folder=folder,
            channels=channels,
            start_recording=bool(merged.get("start_recording", False)),
            expected_start=_date(
                merged.get("expected_start"), f"devices.{device_id}.expected_start"
            ),
            expected_end=_date(
                merged.get("expected_end"), f"devices.{device_id}.expected_end"
            ),
            notes=tuple(str(item) for item in merged.get("notes", [])),
            pre_erase_duration_seconds=parse_duration(
                merged.get("pre_erase_duration")
            ),
            source_pattern=str(
                value.get("source_pattern", pilot.get("input_glob", "{device}/操作日志/*"))
            ),
            reset_active_at_day_start=bool(
                merged.get("reset_active_at_day_start", False)
            ),
            low_voltage_stops_recording=bool(
                merged.get("low_voltage_stops_recording", True)
            ),
            exclude_day_end_from_current=bool(
                merged.get("exclude_day_end_from_current", False)
            ),
            compensate_clock_rollbacks=bool(
                merged.get("compensate_clock_rollbacks", True)
            ),
        )
        if (
            device.expected_start
            and device.expected_end
            and device.expected_end < device.expected_start
        ):
            raise ConfigError(f"Device {device_id}: expected_end is before expected_start")
        devices.append(device)
        seen_ids.add(device_id)
        seen_folders.add(folder)

    order = pilot.get("report_order", [])
    if order:
        position = {name: index for index, name in enumerate(order)}
        devices.sort(key=lambda d: position.get(d.id, len(position)))

    return SiteConfig(
        site_name=str(pilot.get("name", "设备日志核算")),
        report_name=str(pilot.get("report_name", "设备日志分析报告.xlsx")),
        progress_name=str(pilot.get("progress_name", "统计进度.xlsx")),
        input_glob=str(pilot.get("input_glob", "{device}/操作日志/*")),
        devices=tuple(devices),
        rules=rules,
    )

