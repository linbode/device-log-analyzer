from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .models import (
    ClockJump,
    DeviceConfig,
    EraseChange,
    EraseObservation,
    Event,
    RuleConfig,
    SamplingDrop,
    Segment,
    TimelineResult,
)


DAY = 86_400


@dataclass
class _ChannelState:
    active: bool
    last_on: int
    pending_off: int | None = None


@dataclass
class _BuildState:
    channels: dict[str, _ChannelState]
    segments: list[Segment] = field(default_factory=list)
    shift: int = 0
    previous_effective: int | None = None
    previous_raw: int | None = None
    clock_jumps: list[ClockJump] = field(default_factory=list)
    current_day_index: int | None = None
    day_first_effective: int | None = None
    seen_on_today: set[str] = field(default_factory=set)
    carry_today: set[str] = field(default_factory=set)


def build_timeline(
    events: list[Event],
    device: DeviceConfig,
    rules: RuleConfig,
) -> TimelineResult:
    if not events:
        return TimelineResult(
            events=(),
            clock_jumps=(),
            observations=(),
            erasures=(),
            sampling_drops=(),
            segments=(),
            first_value=None,
            total_end=0,
        )

    first_day = min(event.day for event in events)
    last_day = max(event.day for event in events)
    for event in events:
        event.day_index = (event.day - first_day).days

    ordered = sorted(
        enumerate(events),
        key=lambda item: (item[1].day, item[1].line_number, item[0]),
    )
    ordered_events = [event for _, event in ordered]
    total_end = (last_day - first_day).days * DAY + DAY

    states = {
        channel: _ChannelState(
            active=device.start_recording,
            last_on=0,
        )
        for channel in device.channels
    }
    state = _BuildState(channels=states)
    observations: list[EraseObservation] = []
    erasures: list[EraseChange] = []
    sampling_drops: list[SamplingDrop] = []
    first_value: int | None = None
    current_value: int | None = None

    for index, event in enumerate(ordered_events):
        raw_global = event.day_index * DAY + event.raw_second
        effective = raw_global + state.shift
        if (
            not event.is_time_adjustment
            and device.compensate_clock_rollbacks
            and state.previous_effective is not None
            and effective < state.previous_effective
        ):
            rollback = state.previous_effective - effective
            state.shift += rollback
            effective += rollback
            state.clock_jumps.append(
                ClockJump(
                    day=event.day,
                    raw_display=event.raw_display,
                    rollback_seconds=rollback,
                )
            )
        if not event.is_time_adjustment:
            state.previous_effective = effective
            state.previous_raw = raw_global
        event.global_second = effective

        if state.current_day_index != event.day_index:
            state.current_day_index = event.day_index
            state.day_first_effective = effective
            state.seen_on_today.clear()
            state.carry_today.clear()
            if device.reset_active_at_day_start:
                day_start = event.day_index * DAY
                for channel, channel_state in state.channels.items():
                    if not channel_state.active:
                        continue
                    if day_start > channel_state.last_on:
                        state.segments.append(
                            Segment(
                                channel=channel,
                                start=channel_state.last_on,
                                end=day_start,
                            )
                        )
                    channel_state.last_on = day_start

        _settle_expired_pending(state, effective, rules.implicit_close_seconds)

        if event.kind == "power_off" or (
            event.kind == "power_off_low"
            and device.low_voltage_stops_recording
        ):
            for channel in device.channels:
                channel_state = state.channels[channel]
                if channel_state.active and channel_state.pending_off is None:
                    channel_state.pending_off = effective

        elif event.kind == "record_off" and event.channel in state.channels:
            channel_state = state.channels[event.channel]
            if channel_state.active:
                _close_channel(
                    state,
                    event.channel,
                    end=effective,
                    close_at_pending=False,
                    implicit_seconds=rules.implicit_close_seconds,
                )
            else:
                day_start = event.day_index * DAY
                if (
                    (
                        not device.reset_active_at_day_start
                        and channel_state.last_on < day_start
                    )
                    or (
                        device.reset_active_at_day_start
                        and event.channel not in state.seen_on_today
                        and event.channel not in state.carry_today
                    )
                ) and effective <= day_start + rules.orphan_close_max_seconds:
                    start = (
                        day_start
                        if device.reset_active_at_day_start
                        else (state.day_first_effective or day_start)
                    )
                    if effective > start:
                        state.segments.append(
                            Segment(
                                channel=event.channel,
                                start=start,
                                end=effective,
                                carry_over=True,
                            )
                        )
                        channel_state.last_on = effective
                        if device.reset_active_at_day_start:
                            state.carry_today.add(event.channel)

        elif event.kind == "record_on" and event.channel in state.channels:
            channel_state = state.channels[event.channel]
            if channel_state.pending_off is not None:
                implicit_end = (
                    channel_state.pending_off + rules.implicit_close_seconds
                )
                if channel_state.active and implicit_end > channel_state.last_on:
                    state.segments.append(
                        Segment(
                            channel=event.channel,
                            start=channel_state.last_on,
                            end=implicit_end,
                        )
                    )
                channel_state.active = False
                channel_state.pending_off = None
                channel_state.last_on = max(effective, implicit_end)
            if not channel_state.active:
                channel_state.active = True
                channel_state.last_on = effective
                state.seen_on_today.add(event.channel)

        if event.erase_value is not None:
            observations.append(
                EraseObservation(
                    event_index=index,
                    global_second=effective,
                    value=event.erase_value,
                )
            )
            if first_value is None:
                first_value = event.erase_value
                current_value = event.erase_value
            elif event.erase_value > current_value:
                erasures.append(
                    EraseChange(
                        event_index=index,
                        global_second=effective,
                        before=current_value,
                        after=event.erase_value,
                    )
                )
                current_value = event.erase_value
            elif event.erase_value < current_value:
                sampling_drops.append(
                    SamplingDrop(
                        event_index=index,
                        global_second=effective,
                        before=current_value,
                        observed=event.erase_value,
                    )
                )

    for channel in device.channels:
        channel_state = state.channels[channel]
        if not channel_state.active:
            continue
        if channel_state.pending_off is not None:
            end = min(
                channel_state.pending_off + rules.implicit_close_seconds,
                total_end,
            )
            dangling = False
        else:
            end = total_end
            dangling = True
        if end > channel_state.last_on:
            state.segments.append(
                Segment(
                    channel=channel,
                    start=channel_state.last_on,
                    end=end,
                    dangling=dangling,
                )
            )

    filtered_segments: list[Segment] = []
    by_channel: dict[str, list[Segment]] = {channel: [] for channel in device.channels}
    for segment in sorted(state.segments, key=lambda item: (item.channel, item.start, item.end)):
        if segment.duration <= 0:
            continue
        by_channel[segment.channel].append(segment)
        filtered_segments.append(segment)

    # Resolve rare overlaps caused by repeated open events without treating the
    # overlap as double recording time.
    deduped: list[Segment] = []
    for channel, segments in by_channel.items():
        last_end = 0
        for segment in segments:
            start = max(segment.start, last_end)
            if segment.end <= start:
                continue
            deduped.append(
                Segment(
                    channel=channel,
                    start=start,
                    end=segment.end,
                    dangling=segment.dangling,
                    carry_over=segment.carry_over,
                )
            )
            last_end = segment.end

    return TimelineResult(
        events=tuple(ordered_events),
        clock_jumps=tuple(state.clock_jumps),
        observations=tuple(observations),
        erasures=tuple(erasures),
        sampling_drops=tuple(sampling_drops),
        segments=tuple(sorted(deduped, key=lambda s: (s.start, s.channel))),
        first_value=first_value,
        total_end=total_end,
    )


def _settle_expired_pending(
    state: _BuildState, current: int, implicit_seconds: int
) -> None:
    for channel, channel_state in state.channels.items():
        pending = channel_state.pending_off
        if pending is None or current <= pending + implicit_seconds:
            continue
        end = pending + implicit_seconds
        if channel_state.active and end > channel_state.last_on:
            state.segments.append(
                Segment(channel=channel, start=channel_state.last_on, end=end)
            )
        channel_state.active = False
        channel_state.pending_off = None
        channel_state.last_on = end


def _close_channel(
    state: _BuildState,
    channel: str,
    end: int,
    close_at_pending: bool,
    implicit_seconds: int,
) -> None:
    channel_state = state.channels[channel]
    actual_end = end
    if close_at_pending and channel_state.pending_off is not None:
        actual_end = min(
            end, channel_state.pending_off + implicit_seconds
        )
    if actual_end > channel_state.last_on:
        state.segments.append(
            Segment(
                channel=channel,
                start=channel_state.last_on,
                end=actual_end,
            )
        )
    channel_state.active = False
    channel_state.pending_off = None
    channel_state.last_on = actual_end


def event_datetimes(events: tuple[Event, ...], first_day: date) -> dict[int, date]:
    return {
        index: first_day
        for index, _ in enumerate(events)
    }

