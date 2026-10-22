from __future__ import annotations

from datetime import date, datetime, timedelta

from .models import (
    Anomaly,
    DeviceConfig,
    DeviceResult,
    RuleConfig,
    Segment,
)
from .rules import DAY, build_timeline


def format_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "—"
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}h{minutes}m{secs}s"


def clock_from_second(seconds: int) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def datetime_label(origin: date, seconds: int) -> str:
    value = datetime.combine(origin, datetime.min.time()) + timedelta(seconds=seconds)
    return value.strftime("%Y-%m-%d %H:%M:%S")


def analyze_device(
    events,
    device: DeviceConfig,
    rules: RuleConfig,
) -> DeviceResult:
    timeline = build_timeline(events, device, rules)
    actual_days = tuple(sorted({event.day for event in timeline.events}))
    origin = actual_days[0] if actual_days else date.today()
    expected_start = device.expected_start or (actual_days[0] if actual_days else origin)
    expected_end = device.expected_end or (actual_days[-1] if actual_days else origin)
    missing_days = _missing_days(expected_start, expected_end, set(actual_days))

    boundaries = tuple(change.global_second for change in timeline.erasures)
    window_count = len(boundaries) + 1
    channel_windows: dict[str, tuple[int, ...]] = {}
    channel_current: dict[str, int] = {}
    channel_full: dict[str, int] = {}

    for channel in device.channels:
        values = [0] * window_count
        for segment in _segments_for(timeline.segments, channel):
            for start, end in _split_at_boundaries(segment, boundaries):
                index = _window_index(start, end, boundaries)
                values[index] += end - start
        channel_windows[channel] = tuple(values)
        channel_full[channel] = sum(values)
        if window_count > 0:
            current_total = 0
            for segment in _segments_for(timeline.segments, channel):
                for start, end in _split_at_boundaries(segment, boundaries):
                    if (
                        _window_index(start, end, boundaries)
                        == window_count - 1
                        and end < timeline.total_end
                        and not (
                            device.exclude_day_end_from_current
                            and end % DAY == 0
                        )
                    ):
                        current_total += end - start
            channel_current[channel] = current_total
        else:
            channel_current[channel] = 0

    if device.pre_erase_duration_seconds is not None and window_count:
        for channel in device.channels:
            values = list(channel_windows[channel])
            values[0] = device.pre_erase_duration_seconds
            channel_windows[channel] = tuple(values)
            channel_full[channel] = sum(values)

    daily_segments: dict[date, dict[str, tuple[Segment, ...]]] = {}
    current = expected_start
    while current <= expected_end:
        day_start = (current - origin).days * DAY
        day_end = day_start + DAY
        per_channel: dict[str, tuple[Segment, ...]] = {}
        for channel in device.channels:
            pieces: list[Segment] = []
            for segment in _segments_for(timeline.segments, channel):
                start = max(segment.start, day_start)
                end = min(segment.end, day_end)
                if end <= start:
                    continue
                pieces.append(
                    Segment(
                        channel=channel,
                        start=start - day_start,
                        end=end - day_start,
                        dangling=segment.dangling and end == timeline.total_end,
                        carry_over=segment.carry_over,
                    )
                )
            per_channel[channel] = tuple(pieces)
        daily_segments[current] = per_channel
        current += timedelta(days=1)

    anomalies = _build_anomalies(
        timeline.segments,
        timeline.events,
        missing_days,
        timeline.clock_jumps,
        timeline.sampling_drops,
        timeline.erasures,
        rules,
        origin,
    )

    config_events = [
        event
        for event in timeline.events
        if event.kind == "config" and "时间设置" not in event.message
    ]
    if not config_events:
        config_events = [event for event in timeline.events if event.kind == "config"]
    config_time_second: int | None = None
    config_time_label: str | None = None
    if config_events:
        config_event = config_events[-1]
        config_time_second = config_event.global_second
        config_time_label = datetime_label(origin, config_time_second)

    first_erase_after_config = None
    if config_time_second is not None:
        for change in timeline.erasures:
            if change.global_second >= config_time_second:
                first_erase_after_config = change.global_second
                break

    return DeviceResult(
        config=device,
        actual_days=actual_days,
        missing_days=missing_days,
        timeline=timeline,
        channel_windows=channel_windows,
        channel_current=channel_current,
        channel_full=channel_full,
        daily_segments=daily_segments,
        anomalies=tuple(anomalies),
        config_time_second=config_time_second,
        config_time_label=config_time_label,
        first_erase_after_config=first_erase_after_config,
        acc_off_count=sum(
            1 for event in timeline.events if event.kind == "power_off"
        ),
        low_voltage_count=sum(
            1 for event in timeline.events if event.kind == "power_off_low"
        ),
    )


def _segments_for(segments: tuple[Segment, ...], channel: str):
    return (segment for segment in segments if segment.channel == channel)


def _missing_days(start: date, end: date, present: set[date]) -> tuple[date, ...]:
    result: list[date] = []
    current = start
    while current <= end:
        if current not in present:
            result.append(current)
        current += timedelta(days=1)
    return tuple(result)


def _split_at_boundaries(
    segment: Segment, boundaries: tuple[int, ...]
) -> list[tuple[int, int]]:
    cuts = [segment.start]
    cuts.extend(boundary for boundary in boundaries if segment.start < boundary < segment.end)
    cuts.append(segment.end)
    return list(zip(cuts, cuts[1:]))


def _window_index(start: int, end: int, boundaries: tuple[int, ...]) -> int:
    midpoint = (start + end) / 2
    return sum(1 for boundary in boundaries if midpoint >= boundary)


def _build_anomalies(
    segments: tuple[Segment, ...],
    events: tuple,
    missing_days: tuple[date, ...],
    clock_jumps: tuple,
    sampling_drops: tuple,
    erasures: tuple,
    rules: RuleConfig,
    origin: date,
) -> list[Anomaly]:
    anomalies: list[Anomaly] = []
    for jump in clock_jumps:
        severity = "中" if jump.rollback_seconds >= rules.cross_day_rollback_seconds else "低"
        anomalies.append(
            Anomaly(
                kind="时钟回退",
                severity=severity,
                start=None,
                end=None,
                channel=None,
                message=(
                    f"{jump.day.isoformat()} {jump.raw_display} 检出时间回退 "
                    f"{format_duration(jump.rollback_seconds)}，已保持时间轴单调。"
                ),
            )
        )
    for drop in sampling_drops:
        anomalies.append(
            Anomaly(
                kind="擦写计数回落",
                severity="低",
                start=drop.global_second,
                end=drop.global_second,
                channel=None,
                message=(
                    f"{datetime_label(origin, drop.global_second)} 观测到 "
                    f"avgEraseCount {drop.before}→{drop.observed}，"
                    "按采样异常处理，不计作新擦写。"
                ),
            )
        )
    for change in erasures:
        if change.after - change.before > 1:
            anomalies.append(
                Anomaly(
                    kind="擦写异常跳变",
                    severity="中",
                    start=change.global_second,
                    end=change.global_second,
                    channel=None,
                    message=(
                        f"{datetime_label(origin, change.global_second)} "
                        f"avgEraseCount {change.before}→{change.after}，"
                        f"单次增加 {change.after - change.before}。"
                    ),
                )
            )

    for segment in segments:
        start_time = origin + timedelta(days=segment.start // DAY)
        end_time = origin + timedelta(days=(segment.end - 1) // DAY)
        crosses_missing = any(
            start_time < missing < end_time for missing in missing_days
        )
        if segment.duration >= rules.ghost_segment_seconds and crosses_missing:
            anomalies.append(
                Anomaly(
                    kind="幽灵段",
                    severity="中",
                    start=segment.start,
                    end=segment.end,
                    channel=segment.channel,
                    message=(
                        f"{segment.channel} 连续录像 {format_duration(segment.duration)}，"
                        "跨过缺失日志日，时长真实性存疑。"
                    ),
                )
            )
        inside = [
            event.global_second
            for event in events
            if segment.start <= event.global_second <= segment.end
        ]
        if len(inside) >= 2:
            max_gap = max(
                inside[index] - inside[index - 1]
                for index in range(1, len(inside))
            )
            if max_gap >= rules.silence_segment_seconds:
                anomalies.append(
                    Anomaly(
                        kind="疑似挂机/空转",
                        severity="中",
                        start=segment.start,
                        end=segment.end,
                        channel=segment.channel,
                        message=(
                            f"{segment.channel} 段内最大静默 "
                            f"{format_duration(max_gap)}，可能未实际录像。"
                        ),
                    )
                )
        if segment.dangling:
            anomalies.append(
                Anomaly(
                    kind="悬空段",
                    severity="低",
                    start=segment.start,
                    end=segment.end,
                    channel=segment.channel,
                    message=(
                        f"{segment.channel} 在日志末尾仍未关闭，"
                        "计入全程但不计入当前已录制时长。"
                    ),
                )
            )
    return anomalies

