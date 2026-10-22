from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .metrics import analyze_device
from .models import DeviceResult, SiteConfig
from .parser import discover_sources, parse_sources


@dataclass
class SiteRun:
    results: tuple[DeviceResult, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)
    source_count: int = 0


class AnalysisError(ValueError):
    pass


def analyze_site(
    config: SiteConfig,
    input_path: Path,
    device_filter: str | None = None,
) -> SiteRun:
    selected = [
        device
        for device in config.devices
        if device_filter is None
        or device_filter in {device.id, device.display_name, device.folder}
    ]
    if not selected:
        raise AnalysisError(f"Device not found in config: {device_filter}")

    results: list[DeviceResult] = []
    warnings: list[str] = []
    source_count = 0
    for device in selected:
        sources = discover_sources(input_path, device)
        source_count += len(sources)
        if not sources:
            raise AnalysisError(
                f"No operation logs found for {device.display_name} under {input_path}"
            )
        events, parse_warnings = parse_sources(sources)
        if not events:
            raise AnalysisError(
                f"Operation logs were found for {device.display_name}, "
                "but no valid event lines could be parsed."
            )
        result = analyze_device(events, device, config.rules)
        results.append(result)
        warnings.extend(
            f"{device.display_name}: {message}" for message in parse_warnings
        )

    return SiteRun(
        results=tuple(results),
        warnings=tuple(warnings),
        source_count=source_count,
    )

