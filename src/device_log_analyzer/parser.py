from __future__ import annotations

import gzip
import re
import zipfile
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path, PurePosixPath

from .models import DeviceConfig, Event, LogSource


LINE_RE = re.compile(r"^\s*(\d{2}:\d{2}:\d{2})\s*,")
DATE_PREFIX_RE = re.compile(r"^(?P<date>\d{8})")
INLINE_DATE_RE = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})")
CHANNEL_OPEN_RE = re.compile(
    r"(?:通道\s*(?P<cn>\d+)\s+主存储器主码流录像开启)"
    r"|(?:Channel\s*(?P<en>\d+)\s+Main record on)",
    re.IGNORECASE,
)
CHANNEL_CLOSE_RE = re.compile(
    r"(?:通道\s*(?P<cn>\d+)\s+主存储器主码流录像关闭)"
    r"|(?:Channel\s*(?P<en>\d+)\s+Main record off)",
    re.IGNORECASE,
)
POWER_ON_RE = re.compile(
    r"(?:开机\s+上次关机原因)|(?:Power on Last power-off cause)",
    re.IGNORECASE,
)
ACC_OFF_RE = re.compile(
    r"(?:车钥匙关机)|(?:ACC\s*Power off)",
    re.IGNORECASE,
)
LOW_VOLTAGE_RE = re.compile(
    r"(?:低电压关机)|(?:低压关机)|(?:Low[- ]voltage\s*Power off)",
    re.IGNORECASE,
)
ERASE_RE = re.compile(r"avgEraseCount\s*=\s*(\d+)", re.IGNORECASE)
ADJUST_TIME_RE = re.compile(r"Adjust time", re.IGNORECASE)
CONFIG_RE = re.compile(r"(?:保存配置|修改录像设置)")


class InputError(ValueError):
    pass


def _decode(data: bytes) -> str:
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data.decode("utf-8-sig", errors="replace")


def _date_from_name(name: str) -> date | None:
    match = DATE_PREFIX_RE.match(PurePosixPath(name).name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group("date"), "%Y%m%d").date()
    except ValueError:
        return None


def _date_from_content(text: str) -> date | None:
    match = INLINE_DATE_RE.search(text)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group("date"))
    except ValueError:
        return None


def _is_log_member(name: str) -> bool:
    basename = PurePosixPath(name).name
    if not basename or name.endswith("/"):
        return False
    if basename.startswith("~$") or basename.startswith("."):
        return False
    return bool(DATE_PREFIX_RE.match(basename))


def discover_sources(input_path: Path, device: DeviceConfig) -> list[LogSource]:
    if not input_path.exists():
        raise InputError(f"Input path does not exist: {input_path}")

    sources: list[LogSource] = []
    if input_path.is_file():
        if input_path.suffix.lower() != ".zip":
            raise InputError("Input file must be a ZIP archive.")
        _collect_zip_sources(input_path, device, sources)
        return sources

    candidates = [
        input_path / device.folder / "操作日志",
        input_path / device.folder,
        input_path,
    ]
    for folder in candidates:
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            if not path.is_file() or path.name.startswith(("~$", ".")):
                continue
            if path.suffix.lower() == ".zip":
                _collect_zip_sources(path, device, sources)
            elif _is_log_member(path.name):
                try:
                    data = path.read_bytes()
                    _decode(data)
                except (OSError, EOFError, gzip.BadGzipFile):
                    continue
                sources.append(
                    LogSource(
                        name=path.name,
                        member_name=path.name,
                        data=data,
                        archive=None,
                    )
                )
        if sources:
            break

    if not sources:
        pattern = device.source_pattern.format(device=device.folder)
        for path in sorted(input_path.glob(pattern)):
            if not path.is_file() or path.name.startswith(("~$", ".")):
                continue
            if path.suffix.lower() == ".zip":
                _collect_zip_sources(path, device, sources)
            elif _is_log_member(path.name):
                data = path.read_bytes()
                try:
                    _decode(data)
                except (OSError, EOFError, gzip.BadGzipFile):
                    continue
                sources.append(
                    LogSource(
                        name=path.name,
                        member_name=path.name,
                        data=data,
                        archive=None,
                    )
                )

    return _deduplicate_sources(sources)


def _collect_zip_sources(
    path: Path, device: DeviceConfig, sources: list[LogSource]
) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                if member.is_dir() or not _is_log_member(member.filename):
                    continue
                normalized = member.filename.replace("\\", "/")
                if (
                    device.folder not in normalized
                    and device.folder not in path.name
                    and not _zip_is_single_device(archive, device)
                ):
                    continue
                data = archive.read(member)
                try:
                    _decode(data)
                except (EOFError, gzip.BadGzipFile):
                    continue
                sources.append(
                    LogSource(
                        name=PurePosixPath(normalized).name,
                        member_name=normalized,
                        data=data,
                        archive=path,
                    )
                )
    except zipfile.BadZipFile:
        raise InputError(f"Invalid ZIP archive: {path}") from None


def _zip_is_single_device(archive: zipfile.ZipFile, device: DeviceConfig) -> bool:
    roots = {
        PurePosixPath(name.replace("\\", "/")).parts[0]
        for name in archive.namelist()
        if name and not name.endswith("/")
    }
    return len(roots) == 1 and device.folder in next(iter(roots), "")


def _deduplicate_sources(sources: Iterable[LogSource]) -> list[LogSource]:
    seen: set[tuple[str, bytes]] = set()
    result: list[LogSource] = []
    for source in sources:
        key = (PurePosixPath(source.member_name).name, source.data)
        if key in seen:
            continue
        seen.add(key)
        result.append(source)
    return result


def parse_sources(sources: Iterable[LogSource]) -> tuple[list[Event], list[str]]:
    events: list[Event] = []
    warnings: list[str] = []
    seen: set[tuple[date, str, str]] = set()

    for source in sources:
        text = _decode(source.data)
        log_day = _date_from_name(source.member_name) or _date_from_content(text)
        if log_day is None:
            warnings.append(
                f"Skipped {source.member_name}: no valid date in filename or content"
            )
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            match = LINE_RE.match(line)
            if not match:
                continue
            hms = match.group(1)
            parts = line.split(",", 2)
            message = parts[2].strip() if len(parts) > 2 else ""
            if message.startswith('"'):
                message = message[1:]
            if message.endswith('"'):
                message = message[:-1]
            key = (log_day, hms, message)
            if key in seen:
                continue
            seen.add(key)
            hour, minute, second = (int(part) for part in hms.split(":"))
            event = Event(
                day=log_day,
                raw_second=hour * 3600 + minute * 60 + second,
                raw_display=hms,
                message=message,
                source_name=source.member_name,
                line_number=line_number,
            )
            _classify(event)
            events.append(event)

    return events, warnings


def _classify(event: Event) -> None:
    message = event.message
    open_match = CHANNEL_OPEN_RE.search(message)
    close_match = CHANNEL_CLOSE_RE.search(message)
    if open_match:
        event.kind = "record_on"
        event.channel = "CH" + (open_match.group("cn") or open_match.group("en"))
    elif close_match:
        event.kind = "record_off"
        event.channel = "CH" + (close_match.group("cn") or close_match.group("en"))
    elif ACC_OFF_RE.search(message):
        event.kind = "power_off"
    elif LOW_VOLTAGE_RE.search(message):
        event.kind = "power_off_low"
    elif POWER_ON_RE.search(message):
        event.kind = "power_on"
    elif CONFIG_RE.search(message):
        event.kind = "config"
    elif ADJUST_TIME_RE.search(message):
        event.kind = "time_adjust"

    erase_match = ERASE_RE.search(message)
    if erase_match:
        event.erase_value = int(erase_match.group(1))
    event.is_time_adjustment = event.kind in {"config", "time_adjust"}

