"""Retry, backoff and completeness behaviour of the retrieval layer, fully offline."""
from __future__ import annotations

import logging

import pytest
import requests

from pipeline import extract
from tests.conftest import MONTH, FakeResponse, FakeSession

LOG = logging.getLogger("test")


def _client(cfg, session, sleeps):
    return extract.HttpClient(cfg["http"], LOG, session=session, sleep=sleeps.append)


def test_retries_500_and_honours_retry_after_on_429(cfg):
    session, sleeps = FakeSession(), []
    session.script["/ok"] = [FakeResponse(500), FakeResponse(429, headers={"Retry-After": "3"}),
                             FakeResponse(payload={"fine": True})]
    cfg.settings["http"]["backoff_base_seconds"] = 1.0
    response = _client(cfg, session, sleeps).request("GET", "https://example.test/ok")
    assert response.json() == {"fine": True}
    assert sleeps == [1.0, 3.0]  # exponential backoff first, then the server's Retry-After


def test_non_retryable_status_fails_immediately(cfg):
    session, sleeps = FakeSession(), []
    session.script["/missing"] = [FakeResponse(404)]
    with pytest.raises(extract.NonRetryableHTTPError):
        _client(cfg, session, sleeps).request("GET", "https://example.test/missing")
    assert len(session.calls) == 1 and sleeps == []


def test_retries_are_bounded(cfg):
    session, sleeps = FakeSession(), []
    session.script["/down"] = [requests.ConnectionError("refused")] * 10
    with pytest.raises(extract.RetrievalError, match="gave up after 4 attempts"):
        _client(cfg, session, sleeps).request("GET", "https://example.test/down")
    assert len(session.calls) == cfg["http"]["max_retries"]


def test_unpublished_month_gives_a_clear_message(cfg):
    session, sleeps = FakeSession(), []
    session.script["2026-09"] = [FakeResponse(403)]
    with pytest.raises(extract.NotPublishedError, match="has not published"):
        extract.fetch_trip_file("2026-09", cfg, _client(cfg, session, sleeps), LOG)


def test_truncated_download_is_retried_then_rejected_without_leaving_a_file(cfg):
    session, sleeps = FakeSession(), []
    client = _client(cfg, session, sleeps)
    with pytest.raises(extract.IncompleteRetrievalError):
        extract.fetch_trip_file(MONTH, cfg, client, LOG, truncate_after=1024)
    folder = cfg.path("raw") / "tlc" / "fhvhv" / f"month={MONTH}"
    assert not any(folder.glob("*.parquet")) and not any(folder.glob("*.part"))


def test_download_is_reused_only_when_it_provably_matches(cfg):
    session, sleeps = FakeSession(), []
    client = _client(cfg, session, sleeps)
    first = extract.fetch_trip_file(MONTH, cfg, client, LOG)
    second = extract.fetch_trip_file(MONTH, cfg, client, LOG)
    assert first["action"] == "downloaded" and second["action"] == "cached"
    assert first["sha256"] == second["sha256"] and second["bytes_match"]
    # corrupt the local copy: same size, different bytes -> must download again
    path = cfg.path("raw") / "tlc" / "fhvhv" / f"month={MONTH}" / f"fhvhv_tripdata_{MONTH}.parquet"
    data = bytearray(path.read_bytes())
    data[100] ^= 0xFF
    path.write_bytes(bytes(data))
    third = extract.fetch_trip_file(MONTH, cfg, client, LOG)
    assert third["action"] == "downloaded" and third["sha256"] == first["sha256"]


def test_socrata_refuses_a_partial_result(cfg):
    session, sleeps = FakeSession(), []
    session.script["c5iv"] = [FakeResponse(payload=[{"n": "5"}]), FakeResponse(payload=[{"a": 1}] * 3)]
    with pytest.raises(extract.IncompleteRetrievalError, match="reported 5 rows, received 3"):
        extract.fetch_socrata("c5iv-bn4s", "1=1", "a", cfg.path("raw") / "api", cfg, _client(cfg, session, sleeps),
                              LOG)
