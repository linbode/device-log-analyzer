from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from device_log_analyzer.config import load_config
from device_log_analyzer.engine import analyze_site
from device_log_analyzer.verification import compare_results_to_baseline


LOGS = os.environ.get("PILOT2_LOGS")
BASELINE = os.environ.get("PILOT2_BASELINE")
PILOT2_CONFIG = os.environ.get("PILOT2_CONFIG")


@unittest.skipUnless(
    LOGS and BASELINE and Path(LOGS).exists() and Path(BASELINE).exists(),
    "Set PILOT2_LOGS and PILOT2_BASELINE to run the real-data golden test.",
)
class RealDataRegressionTests(unittest.TestCase):
    def test_all_device_metrics_match_baseline(self) -> None:
        config_path = (
            Path(PILOT2_CONFIG)
            if PILOT2_CONFIG
            else ROOT / "config" / "pilot2.local.toml"
        )
        config = load_config(config_path)
        run = analyze_site(config, Path(LOGS))
        differences = compare_results_to_baseline(
            run.results,
            Path(BASELINE),
        )
        self.assertEqual(
            differences,
            [],
            "\n".join(
                f"{item.sheet}/{item.channel}/{item.field}: "
                f"{item.expected} -> {item.actual}"
                for item in differences
            ),
        )


if __name__ == "__main__":
    unittest.main()

