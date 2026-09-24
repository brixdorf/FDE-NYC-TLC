"""Shared fixtures: an offline copy of the sources, served by a fake HTTP session.

The trip file is a sample of real June 2026 rows (see make_fixture.py). The
fake session answers the same URLs the pipeline calls, so the whole pipeline
can run end to end in a temporary folder without network access.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

import duckdb
import pytest
import requests

from pipeline.config import Config, load_config

FIXTURES = Path(__file__).parent / "fixtures"
TRIP_FIXTURE = FIXTURES / "fhvhv_sample_2026-06.parquet"
ZONE_FIXTURE = FIXTURES / "taxi_zone_lookup.csv"
MONTH = "2026-06"


class FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b"", headers: dict | None = None, payload=None):
        self.status_code = status
        self._body = body if payload is None else json.dumps(payload).encode()
        self.headers = requests.structures.CaseInsensitiveDict(headers or {})
        if "Content-Length" not in self.headers:
            self.headers["Content-Length"] = str(len(self._body))

    def json(self):
        return json.loads(self._body)

    def iter_content(self, chunk_size: int = 1024):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSession:
    """Serves the fixture files and a Socrata API derived from them.

    `published_delta` lets a test make TLC's published total differ from the file.
    `script` maps a URL substring to a list of responses returned in order first.
    """

    def __init__(self, published_delta: int = 0):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.script: dict[str, list] = {}
        zone_counts = duckdb.sql(
            f"SELECT PULocationID, count(*) FROM '{TRIP_FIXTURE.as_posix()}' GROUP BY 1 ORDER BY 1").fetchall()
        self.zone_rows = [{"locationid": str(z), "trip_count": str(n)} for z, n in zone_counts]
        self.zone_rows[0]["trip_count"] = str(int(self.zone_rows[0]["trip_count"]) + published_delta)
        brands = dict(duckdb.sql(
            f"SELECT hvfhs_license_num, count(*) FROM '{TRIP_FIXTURE.as_posix()}' GROUP BY 1").fetchall())
        self.base_rows = [{"base_license_number": "LYFT", "total_dispatched_trips": str(brands.get("HV0005", 0))},
                          {"base_license_number": "UBER", "total_dispatched_trips": str(brands.get("HV0003", 0))}]

    def request(self, method, url, params=None, stream=False, headers=None, timeout=None):
        self.calls.append((method, url, params))
        for key, queue in self.script.items():
            if key in url and queue:
                item = queue.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item
        if urlparse(url).netloc == "127.0.0.1:9":
            raise requests.ConnectionError("connection refused (simulated)")
        path = urlparse(url).path
        if path.endswith(".parquet"):
            body = TRIP_FIXTURE.read_bytes()
            etag = hashlib.md5(body).hexdigest()
            return FakeResponse(200, b"" if method == "HEAD" else body,
                                {"Content-Length": str(len(body)), "ETag": f'"{etag}"'})
        if path.endswith(".csv"):
            body = ZONE_FIXTURE.read_bytes()
            return FakeResponse(200, b"" if method == "HEAD" else body, {"Content-Length": str(len(body))})
        if path.endswith(".json"):
            rows = self.zone_rows if "c5iv" in path else self.base_rows
            if params and params.get("$select", "").startswith("count"):
                return FakeResponse(payload=[{"n": str(len(rows))}])
            offset, limit = int(params["$offset"]), int(params["$limit"])
            return FakeResponse(payload=rows[offset:offset + limit])
        return FakeResponse(404)


@pytest.fixture
def cfg(tmp_path) -> Config:
    """Real project config, with every path redirected into a temp folder and fast retries."""
    settings = copy.deepcopy(load_config().settings)
    settings["http"]["backoff_base_seconds"] = 0.0
    settings["http"]["socrata_page_size"] = 50  # force several API pages
    return Config(settings=settings, root=tmp_path)


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()
