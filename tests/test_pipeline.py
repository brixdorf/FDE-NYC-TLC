"""End-to-end runs on the real-row fixture: idempotency, conservation, failure safety."""
from __future__ import annotations

import json
import logging

import pytest

from pipeline import runner
from pipeline.extract import RetrievalError
from pipeline.validate import ValidationError
from tests.conftest import MONTH, FakeSession

LOG = logging.getLogger("test")


def _run(cfg, session, run_id, chaos=None):
    return runner.run_month(MONTH, cfg, run_id, LOG, chaos=chaos, session=session)


def test_full_run_conserves_rows_and_passes_reconciliation(cfg, session):
    manifest = _run(cfg, session, "run-a")
    rows = manifest["row_counts"]
    assert rows["parquet_footer"] == rows["scanned"] == rows["silver"]
    assert rows["kpi_measurable"] + rows["held_out_of_kpi"] == rows["scanned"]
    recon = json.loads((cfg.path("outputs") / f"month={MONTH}" / "reconciliation.json").read_text())
    assert [c["status"] for c in recon["checks"]] == ["PASS", "PASS"]


def test_rerun_reproduces_identical_outputs(cfg, session):
    first = _run(cfg, session, "run-a")["outputs"]
    second = _run(cfg, session, "run-b")["outputs"]
    assert first == second


def test_missing_trips_in_file_put_the_month_on_hold_with_bounds(cfg):
    manifest = _run(cfg, FakeSession(published_delta=500), "run-a")
    assert manifest["publish_decision"] == "HOLD"
    impact = manifest["kpi_impact_of_missing_trips"]
    assert impact["missing_trips"] == 500
    assert impact["lower_bound"] <= impact["reported"] <= impact["upper_bound"]


def test_api_down_degrades_instead_of_failing(cfg, session):
    manifest = _run(cfg, session, "run-a", chaos="api_down")
    assert manifest["degraded"] and manifest["publish_decision"] == "PUBLISH WITH CAVEATS"
    assert (cfg.path("outputs") / "chaos" / "api_down" / f"month={MONTH}").exists()


@pytest.mark.parametrize("chaos, error", [("missing_column", ValidationError),
                                          ("truncated_download", RetrievalError)])
def test_failed_run_leaves_previous_outputs_untouched(cfg, session, chaos, error):
    good = _run(cfg, session, "run-good")["outputs"]
    out = cfg.path("outputs") / f"month={MONTH}"
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    with pytest.raises(error):
        _run(cfg, FakeSession(), "run-bad", chaos=chaos)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before
    assert good
