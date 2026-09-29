import json
import os
from pathlib import Path

from django.test.runner import DiscoverRunner

TESTS_DIR = Path(__file__).resolve().parent.parent.parent / "tests"


class Runner(DiscoverRunner):
    """Run the suite in tests/ (beside src/) when no labels are given, from
    whichever directory manage.py is called. Without this, running it from
    inside src/ would quietly find no tests and report OK.

    It also holds the REST API to its OpenAPI document: every answer an
    /api/v1/ endpoint gives during the run is checked against the schema it
    declares (api/core._check_contract), and the run ends with how many
    endpoints the tests exercised. Running the whole suite fails if any
    endpoint was never exercised. API_CONTRACT=record collects mismatches
    into DATA_DIR/contract.json instead of failing on the first one."""

    def build_suite(self, test_labels=None, **kwargs):
        self.whole_suite = not test_labels
        if not test_labels and TESTS_DIR.is_dir():
            test_labels = [str(TESTS_DIR)]
        return super().build_suite(test_labels, **kwargs)

    def run_suite(self, suite, **kwargs):
        from api import core

        core.CONTRACT = {}
        if os.environ.get("API_CONTRACT") == "record":
            core.CONTRACT_PROBLEMS = {}
        result = super().run_suite(suite, **kwargs)
        self.unexercised = self._report(core)
        return result

    def suite_result(self, suite, result, **kwargs):
        failures = super().suite_result(suite, result, **kwargs)
        if getattr(self, "whole_suite", False) and self.unexercised:
            print(f"API contract: FAILED, {len(self.unexercised)} endpoints were never exercised. Every endpoint "
                  "needs a test that gets a successful answer (tests/test_openapi.py has the journeys).")
            for method, path in self.unexercised:
                print(f"  {method} /api/v1/{path}")
            failures += 1
        return failures

    def _report(self, core):
        from django.conf import settings

        seen = core.CONTRACT
        answered = {k for k, codes in seen.items() if any(200 <= c < 300 for c in codes)}
        everything = {(e.method, e.path) for e in core.ENDPOINTS}
        missing = sorted(everything - answered)
        print(f"\nAPI contract: {len(answered & everything)} of {len(everything)} endpoints answered successfully "
              "during the tests, every answer checked against the OpenAPI schema.")
        if self.verbosity > 1 and missing:
            for method, path in missing:
                print(f"  not exercised: {method} /api/v1/{path}")
        if core.CONTRACT_PROBLEMS is not None:
            out = Path(settings.DATA_DIR) / "contract.json"
            out.write_text(json.dumps({"problems": core.CONTRACT_PROBLEMS,
                                       "not_exercised": [f"{m} {p}" for m, p in missing]}, indent=1))
            print(f"API contract (record mode): {len(core.CONTRACT_PROBLEMS)} mismatches, written to {out}")
        return missing
