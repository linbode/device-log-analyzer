from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RuleConfig:
    implicit_close_seconds: int = 5
    cross_day_rollback_seconds: int = 12 * 3600
    orphan_close_max_seconds: int = 12 * 3600
    ghost_segment_seconds: int = 24 * 3600
    silence_segment_seconds: int = 4 * 3600


@dataclass(frozen=True)
class DeviceConfig:
    id: str
    display_name: str
    folder: str
    channels: tuple[str, ...]
    start_recording: bool = False
    expected_start: date | None = None
    expected_end: date | None = None
    notes: tuple[str, ...] = ()
    pre_erase_duration_seconds: int | None = None
    source_pattern: str = "{device}/操作日志/*"
    reset_active_at_day_start: bool = False
    low_voltage_stops_recording: bool = True
    exclude_day_end_from_current: bool = False
    compensate_clock_rollbacks: bool = True


@dataclass(frozen=True)
class SiteConfig:
    site_name: str
    report_name: str
    progress_name: str
    input_glob: str
    devices: tuple[DeviceConfig, ...]
    rules: RuleConfig


@dataclass(frozen=True)
class LogSource:
    name: str
    member_name: str
    data: bytes
    archive: Path | None = None


@dataclass
class Event:
    day: date
    raw_second: int
    raw_display: str
    message: str
    source_name: str
    line_number: int
    day_index: int = 0
    global_second: int = 0
    kind: str = "other"
    channel: str | None = None
    erase_value: int | None = None
    is_time_adjustment: bool = False


@dataclass(frozen=True)
class ClockJump:
    day: date
    raw_display: str
    rollback_seconds: int


@dataclass(frozen=True)
class EraseObservation:
    event_index: int
    global_second: int
    value: int


@dataclass(frozen=True)
class EraseChange:
    event_index: int
    global_second: int
    before: int
    after: int


@dataclass(frozen=True)
class SamplingDrop:
    event_index: int
    global_second: int
    before: int
    observed: int


@dataclass(frozen=True)
class Segment:
    channel: str
    start: int
    end: int
    dangling: bool = False
    carry_over: bool = False

    @property
    def duration(self) -> int:
        return max(0, self.end - self.start)


@dataclass(frozen=True)
class TimelineResult:
    events: tuple[Event, ...]
    clock_jumps: tuple[ClockJump, ...]
    observations: tuple[EraseObservation, ...]
    erasures: tuple[EraseChange, ...]
    sampling_drops: tuple[SamplingDrop, ...]
    segments: tuple[Segment, ...]
    first_value: int | None
    total_end: int


@dataclass(frozen=True)
class Anomaly:
    kind: str
    severity: str
    start: int | None
    end: int | None
    channel: str | None
    message: str


@dataclass
class DeviceResult:
    config: DeviceConfig
    actual_days: tuple[date, ...]
    missing_days: tuple[date, ...]
    timeline: TimelineResult
    channel_windows: dict[str, tuple[int, ...]] = field(default_factory=dict)
    channel_current: dict[str, int] = field(default_factory=dict)
    channel_full: dict[str, int] = field(default_factory=dict)
    daily_segments: dict[date, dict[str, tuple[Segment, ...]]] = field(
        default_factory=dict
    )
    anomalies: tuple[Anomaly, ...] = ()
    config_time_second: int | None = None
    config_time_label: str | None = None
    first_erase_after_config: int | None = None
    acc_off_count: int = 0
    low_voltage_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "device": self.config.display_name,
            "device_id": self.config.id,
            "folder": self.config.folder,
            "actual_days": [d.isoformat() for d in self.actual_days],
            "missing_days": [d.isoformat() for d in self.missing_days],
            "first_avg_erase_count": self.timeline.first_value,
            "erase_count": len(self.timeline.erasures),
            "erasures": [
                {
                    "before": e.before,
                    "after": e.after,
                    "global_second": e.global_second,
                }
                for e in self.timeline.erasures
            ],
            "channel_windows": {
                channel: list(values)
                for channel, values in self.channel_windows.items()
            },
            "channel_current": self.channel_current,
            "channel_full": self.channel_full,
            "config_time_label": self.config_time_label,
            "first_erase_after_config": self.first_erase_after_config,
            "acc_off_count": self.acc_off_count,
            "low_voltage_count": self.low_voltage_count,
            "anomalies": [
                {
                    "kind": a.kind,
                    "severity": a.severity,
                    "channel": a.channel,
                    "start": a.start,
                    "end": a.end,
                    "message": a.message,
                }
                for a in self.anomalies
            ],
            "segments": [
                {
                    "channel": s.channel,
                    "start": s.start,
                    "end": s.end,
                    "duration": s.duration,
                    "dangling": s.dangling,
                    "carry_over": s.carry_over,
                }
                for s in self.timeline.segments
            ],
        }

