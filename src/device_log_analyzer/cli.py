from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config
from .engine import AnalysisError, analyze_site
from .metrics import datetime_label, format_duration
from .report import generate_outputs
from .verification import compare_results_to_baseline


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "analyze":
            return _analyze(args)
        if args.command == "inspect":
            return _inspect(args)
        if args.command == "verify":
            return _verify(args)
    except (ConfigError, AnalysisError, FileExistsError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    parser.print_help()
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="device-log-analyzer",
        description="Configurable vehicle operation-log analysis and reporting.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze", help="Analyze logs and generate reports.")
    _add_common_arguments(analyze)
    analyze.add_argument("--force", action="store_true", help="Replace existing output files.")
    analyze.add_argument(
        "--baseline",
        type=Path,
        help="Optional existing workbook for immediate metric regression.",
    )
    analyze.add_argument(
        "--generated-at",
        help="Deterministic ISO timestamp used by report metadata.",
    )

    inspect = subparsers.add_parser("inspect", help="Print parsed metrics without Excel output.")
    _add_common_arguments(inspect)
    inspect.add_argument("--events", action="store_true", help="Include parsed event summary.")
    inspect.add_argument("--json", action="store_true", help="Print full JSON result.")

    verify = subparsers.add_parser("verify", help="Compare computed metrics to an existing workbook.")
    _add_common_arguments(verify)
    verify.add_argument("--baseline", type=Path, required=True)

    return parser


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--device", help="Process one configured device by id, folder, or name.")
    parser.add_argument("--output", type=Path, default=Path("output"))


def _load_run(args):
    config = load_config(args.config)
    run = analyze_site(config, args.input, device_filter=args.device)
    return config, run


def _analyze(args) -> int:
    config, run = _load_run(args)
    generated_at = _parse_datetime(args.generated_at)
    report_path, progress_path = generate_outputs(
        run.results,
        config,
        args.output,
        generated_at,
        force=args.force,
    )
    json_path = args.output / "device_metrics.json"
    summary_path = args.output / "run_summary.json"
    log_path = args.output / "run.log"
    payload = [result.as_dict() for result in run.results]
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    diffs = []
    if args.baseline:
        diffs = compare_results_to_baseline(run.results, args.baseline)

    summary = {
        "version": __version__,
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "site": config.site_name,
        "input": str(args.input.resolve()),
        "devices": len(run.results),
        "source_files": run.source_count,
        "warnings": list(run.warnings),
        "regression_differences": len(diffs),
        "report": str(report_path.resolve()),
        "progress": str(progress_path.resolve()),
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log_lines = [
        f"device-log-analyzer {__version__}",
        f"site={config.site_name}",
        f"input={args.input}",
        f"devices={len(run.results)} source_files={run.source_count}",
        "",
    ]
    for result in run.results:
        log_lines.append(_result_line(result))
    log_lines.extend(f"WARNING: {warning}" for warning in run.warnings)
    if args.baseline:
        log_lines.append(f"regression_differences={len(diffs)}")
        log_lines.extend(
            f"DIFF: {item.sheet}/{item.channel}/{item.field}: "
            f"{item.expected!r} -> {item.actual!r}"
            for item in diffs
        )
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    print(f"报告: {report_path}")
    print(f"进度: {progress_path}")
    print(f"结构化结果: {json_path}")
    if args.baseline:
        print(f"金标准差异: {len(diffs)}")
        for diff in diffs:
            print(
                f"  {diff.sheet}/{diff.channel}/{diff.field}: "
                f"{diff.expected} -> {diff.actual}"
            )
    return 2 if diffs else 0


def _inspect(args) -> int:
    _, run = _load_run(args)
    if args.json:
        print(json.dumps([result.as_dict() for result in run.results], ensure_ascii=False, indent=2))
        return 0
    for result in run.results:
        print(_result_line(result))
        if args.events:
            for event in result.timeline.events:
                if event.kind == "other" and event.erase_value is None:
                    continue
                print(
                    f"  {event.day.isoformat()} {event.raw_display} "
                    f"{event.kind} {event.channel or ''} "
                    f"erase={event.erase_value if event.erase_value is not None else ''}"
                )
    return 0


def _verify(args) -> int:
    _, run = _load_run(args)
    diffs = compare_results_to_baseline(run.results, args.baseline)
    if not diffs:
        print("PASS: 所有业务指标与基线一致。")
        return 0
    print(f"FAIL: 发现 {len(diffs)} 个差异。")
    for diff in diffs:
        print(
            f"{diff.sheet}/{diff.channel}/{diff.field}: "
            f"baseline={diff.expected!r} candidate={diff.actual!r}"
        )
    return 2


def _result_line(result) -> str:
    origin = result.actual_days[0]
    erasures = "；".join(
        f"{change.before}->{change.after}@{datetime_label(origin, change.global_second)}"
        for change in result.timeline.erasures
    )
    channel_1 = result.config.channels[0]
    missing = ",".join(day.isoformat() for day in result.missing_days) or "none"
    return (
        f"{result.config.display_name}: "
        f"erase={len(result.timeline.erasures)} "
        f"{channel_1}-current={format_duration(result.channel_current[channel_1])} "
        f"{channel_1}-full={format_duration(result.channel_full[channel_1])} "
        f"missing={missing} "
        f"erasures=[{erasures}]"
    )


def _parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now().replace(microsecond=0)
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(f"--generated-at must be ISO date/time: {value!r}") from exc


if __name__ == "__main__":
    raise SystemExit(main())

