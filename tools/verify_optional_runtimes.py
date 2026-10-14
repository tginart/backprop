"""Require genuine optional libraries and passing integration tests.

Architecture: import installed providers -> load fixed integration targets ->
run real-provider comparisons -> reject failures, errors, empty targets or skips.
Main callers: maintainers closing the remaining Python runtime verification gates.
Why this exists: the ordinary suite intentionally skips unavailable optional
libraries; a successful ordinary run cannot prove these integrations work.
This command installs nothing, uses local synthetic data, and makes no Foundry
requests. It does not verify a hosted Colab/Kaggle session or browser sign-in.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = {
    "numpy": "numpy", "pandas": "pandas", "matplotlib": "matplotlib",
    "plotly": "plotly", "polars": "polars", "re2": "google-re2",
}
TARGETS = (
    "test_sdk_plotly_contract.PlotlyWrapperContracts.test_genuine_plotly_figure_matches_pinned_wrapper",
    "test_public_api.PublicApiTests.test_regex_strict_end_anchor_and_explicit_full_dialect_dependency",
    "test_public_api.PublicApiTests.test_query_operators_match_documented_semantics",
    "test_public_api.PublicApiTests.test_invalid_query_rejected_even_with_empty_or_nonmatching_data",
    "test_genuine_provider_contracts.ProviderReferenceFixtureContracts.test_pinned_typename_preserves_module_dispatch_without_optional_packages",
    "test_genuine_provider_contracts.GenuineMatplotlibPlotlyContracts.test_real_converter_matches_pinned_and_survives_run_log_readback",
    "test_genuine_provider_contracts.GenuinePolarsHistoryContracts.test_real_per_run_frames_schema_errors_and_empty_results_match_pinned",
)


def installed_providers():
    """Load each real provider and record its distribution version and origin.

    Main callers: main. Import failures stop verification before tests run;
    missing distribution metadata is also insufficient runtime evidence.
    """
    available, missing = {}, {}
    for module_name, distribution in PROVIDERS.items():
        try:
            module = importlib.import_module(module_name)
            version = importlib.metadata.version(distribution)
        except Exception as error:
            # A binary/import initialization failure is an unavailable provider,
            # not permission to run only a fallback branch and claim success.
            missing[module_name] = type(error).__name__
        else:
            available[module_name] = {"distribution": distribution, "version": version,
                                      "origin": getattr(module, "__file__", None)}
    return available, missing


def run_selected_tests(targets=TARGETS, stream=None):
    """Return strict results for the fixed integration targets.

    Main callers: main; verifier guard checks may supply tiny local suites.
    Every target must contain a test, and every selected test must run without
    skips or expected failures. W&B source-fixture absence therefore cannot
    become a passing gate.
    """
    loader = unittest.TestLoader()
    suites = [(target, loader.loadTestsFromName(target)) for target in targets]
    empty = [target for target, suite in suites if suite.countTestCases() == 0]
    if not suites or empty or loader.errors:
        return {"status": "test_loading_failed", "empty_targets": empty,
                "loader_errors": loader.errors, "tests_run": 0}
    suite = unittest.TestSuite(value for _, value in suites)
    result = unittest.TextTestRunner(stream=stream or sys.stderr, verbosity=2).run(suite)
    passed = (result.wasSuccessful() and not result.skipped
              and not result.expectedFailures and result.testsRun > 0)
    return {"status": "passed" if passed else "integration_failed",
            "tests_run": result.testsRun, "failures": len(result.failures),
            "errors": len(result.errors),
            "expected_failures": [test.id() for test, _ in result.expectedFailures],
            "skipped": [{"test": test.id(), "reason": reason} for test, reason in result.skipped]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional JSON result file; no credentials are included")
    args = parser.parse_args()
    # Provider imports can initialize Matplotlib configuration. Keep that
    # initialization and every test's font/cache work in an owned temporary dir.
    with tempfile.TemporaryDirectory(prefix="backprop-optional-runtime-") as directory:
        with patch.dict(os.environ, {"MPLCONFIGDIR": directory, "MPLBACKEND": "Agg"}):
            available, missing = installed_providers()
            report = {"python": sys.version.split()[0], "executable": sys.executable,
                      "providers": available, "missing_providers": missing,
                      "targets": list(TARGETS), "foundry_writes": 0}
            if missing:
                report.update(status="missing_dependencies", tests_run=0)
            else:
                sys.path[:0] = [str(ROOT / "sdk"), str(ROOT / "tests")]
                report.update(run_selected_tests())
    payload = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload)
    print(payload, end="")
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
