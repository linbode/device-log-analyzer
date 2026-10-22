from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from device_log_analyzer.config import load_config, parse_duration
from device_log_analyzer.metrics import analyze_device
from device_log_analyzer.models import (
    DeviceConfig,
    Event,
    LogSource,
    RuleConfig,
)
from device_log_analyzer.parser import parse_sources
from device_log_analyzer.rules import DAY


def make_event(
    hms: str,
    message: str,
    *,
    kind: str = "other",
    channel: str | None = None,
    erase_value: int | None = None,
    day: date = date(2026, 10, 1),
) -> Event:
    hour, minute, second = (int(part) for part in hms.split(":"))
    return Event(
        day=day,
        raw_second=hour * 3600 + minute * 60 + second,
        raw_display=hms,
        message=message,
        source_name=f"20261001log",
        line_number=0,
        kind=kind,
        channel=channel,
        erase_value=erase_value,
    )


class ParserTests(unittest.TestCase):
    def test_parses_bilingual_events_and_deduplicates(self) -> None:
        text = "\n".join(
            [
                '00:00:01, ,"Power on Last power-off cause:ACC sleep"',
                '00:00:02, ,"Channel1 Main record on"',
                '00:00:02, ,"Channel2 Main record on"',
                '00:00:02, ,"Channel2 Main record on"',
                '00:00:03, ,"通道3 主存储器主码流录像开启"',
                '00:00:04, ,"Emmc bitrate check: avgEraseCount=2 manufacturer=4"',
                '00:01:00, ,"ACCPower off"',
            ]
        )
        source = LogSource(
            name="20261001000000_20261001235959_pf261001log",
            member_name="20261001000000_20261001235959_pf261001log",
            data=text.encode("utf-8"),
            archive=None,
        )
        events, warnings = parse_sources([source])
        self.assertEqual(warnings, [])
        self.assertEqual(sum(event.kind == "record_on" for event in events), 3)
        self.assertEqual(events[-1].kind, "power_off")
        self.assertEqual(
            next(event.erase_value for event in events if event.erase_value is not None),
            2,
        )


class RuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.device = DeviceConfig(
            id="device-1",
            display_name="设备1",
            folder="device-1",
            channels=("CH1",),
            start_recording=False,
            expected_start=date(2026, 10, 1),
            expected_end=date(2026, 10, 1),
        )
        self.rules = RuleConfig()

    def test_implicit_power_off_close_uses_plus_five_seconds(self) -> None:
        events = [
            make_event("08:00:00", "on", kind="record_on", channel="CH1"),
            make_event("09:00:00", "off", kind="power_off"),
            make_event("10:00:00", "heartbeat"),
        ]
        result = analyze_device(events, self.device, self.rules)
        self.assertEqual(result.channel_full["CH1"], 3605)
        self.assertEqual(result.channel_current["CH1"], 3605)

    def test_explicit_close_within_five_seconds_wins(self) -> None:
        events = [
            make_event("08:00:00", "on", kind="record_on", channel="CH1"),
            make_event("09:00:00", "off", kind="power_off"),
            make_event("09:00:03", "close", kind="record_off", channel="CH1"),
        ]
        result = analyze_device(events, self.device, self.rules)
        self.assertEqual(result.channel_full["CH1"], 3603)

    def test_delayed_close_does_not_extend_recording(self) -> None:
        events = [
            make_event("08:00:00", "on", kind="record_on", channel="CH1"),
            make_event("09:00:00", "off", kind="power_off"),
            make_event("09:20:00", "delayed close", kind="record_off", channel="CH1"),
        ]
        result = analyze_device(events, self.device, self.rules)
        self.assertEqual(result.channel_full["CH1"], 3605)

    def test_erase_count_must_increase(self) -> None:
        events = [
            make_event("00:00:01", "e1", erase_value=1),
            make_event("00:00:02", "e2", erase_value=2),
            make_event("00:00:03", "e2", erase_value=2),
            make_event("00:00:04", "drop", erase_value=1),
            make_event("00:00:05", "e3", erase_value=3),
        ]
        result = analyze_device(events, self.device, self.rules)
        self.assertEqual(
            [(change.before, change.after) for change in result.timeline.erasures],
            [(1, 2), (2, 3)],
        )
        self.assertEqual(len(result.timeline.sampling_drops), 1)

    def test_window_split_does_not_double_count(self) -> None:
        events = [
            make_event("00:00:00", "e0", erase_value=0),
            make_event("00:00:00", "on", kind="record_on", channel="CH1"),
            make_event("00:00:50", "e1", erase_value=1),
            make_event("00:01:40", "close", kind="record_off", channel="CH1"),
        ]
        result = analyze_device(events, self.device, self.rules)
        self.assertEqual(result.channel_windows["CH1"], (50, 50))
        self.assertEqual(result.channel_full["CH1"], 100)

    def test_missing_day_is_reported_without_creating_data(self) -> None:
        device = DeviceConfig(
            id="device-1",
            display_name="设备1",
            folder="device-1",
            channels=("CH1",),
            expected_start=date(2026, 10, 1),
            expected_end=date(2026, 10, 3),
        )
        events = [make_event("00:00:00", "heartbeat")]
        result = analyze_device(events, device, self.rules)
        self.assertEqual(
            result.missing_days,
            (date(2026, 10, 2), date(2026, 10, 3)),
        )


class ConfigTests(unittest.TestCase):
    def test_duration_and_minimal_config(self) -> None:
        self.assertEqual(parse_duration("133h47m42s"), 481662)
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "site.toml"
            config_path.write_text(
                """
[pilot]
name = "测试站点"

[rules]
implicit_close_seconds = 5

[devices."car-1"]
display_name = "车辆一"
folder = "car-1"
channels = ["CH1"]
expected_start = "2026-10-01"
expected_end = "2026-10-01"
""",
                encoding="utf-8",
            )
            config = load_config(config_path)
            self.assertEqual(config.devices[0].display_name, "车辆一")
            self.assertEqual(config.devices[0].channels, ("CH1",))


if __name__ == "__main__":
    unittest.main()

