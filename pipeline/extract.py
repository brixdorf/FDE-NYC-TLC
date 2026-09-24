"""Stage 1, EXTRACT: retrieve raw inputs and prove the retrieval is complete.

Three sources, two retrieval modes:
  * files over HTTPS: the monthly HVFHV trip Parquet and the Taxi Zone Lookup CSV
  * REST API (Socrata SODA): TLC's own published aggregates, used to reconcile

Raw inputs are preserved unchanged under data/raw/, partitioned by month,
each with a _manifest.json recording what was received and how we know it
is complete (byte count, checksum, Parquet footer row count, API count).
"""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq
import requests

from .config import Config
from .io_utils import read_json, sha256_file, write_json

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# Ask for the file exactly as stored, so bytes received can be compared with Content-Length
IDENTITY = {"Accept-Encoding": "identity"}


class RetrievalError(Exception):
    """A source could not be retrieved completely. Maps to exit code 3."""


class NotPublishedError(RetrievalError):
    """TLC has not published the requested month (the file URL returns 403/404)."""


class IncompleteRetrievalError(RetrievalError):
    """Data arrived but does not match what the source says it sent."""


class NonRetryableHTTPError(RetrievalError):
    def __init__(self, status: int, url: str):
        super().__init__(f"HTTP {status} from {url} (not retryable)")
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class HttpClient:
    """requests wrapper with bounded exponential backoff.

    Retries only failures that can succeed on their own: HTTP 429/5xx,
    connection errors and timeouts. Honours Retry-After when the server sends
    it. Any other 4xx fails immediately, because repeating it changes nothing.
    """

    def __init__(self, http_cfg: dict, logger: logging.Logger, session: requests.Session | None = None,
                 sleep=time.sleep):
        self.cfg = http_cfg
        self.logger = logger
        self.session = session or requests.Session()
        self.sleep = sleep

    def _wait_seconds(self, attempt: int, response: requests.Response | None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after is not None:
                try:
                    return float(retry_after)
                except ValueError:
                    pass
        return self.cfg["backoff_base_seconds"] * (2 ** (attempt - 1))

    def request(self, method: str, url: str, *, params: dict | None = None, stream: bool = False,
                label: str = "", headers: dict | None = None) -> requests.Response:
        max_retries = self.cfg["max_retries"]
        for attempt in range(1, max_retries + 1):
            response = None
            try:
                response = self.session.request(method, url, params=params, stream=stream, headers=headers,
                                                timeout=self.cfg["timeout_seconds"])
            except (requests.ConnectionError, requests.Timeout) as exc:
                reason = type(exc).__name__
            else:
                if response.status_code < 400:
                    return response
                if response.status_code not in RETRYABLE_STATUS:
                    raise NonRetryableHTTPError(response.status_code, url)
                reason = f"HTTP {response.status_code}"
            if attempt == max_retries:
                raise RetrievalError(f"{label or url}: gave up after {max_retries} attempts ({reason})")
            wait = self._wait_seconds(attempt, response)
            self.logger.warning("Retryable failure | source=%s reason=%s attempt=%s/%s wait=%.1fs",
                                label or url, reason, attempt, max_retries, wait)
            self.sleep(wait)
        raise AssertionError("unreachable")


def _head(client: HttpClient, url: str, label: str) -> dict:
    response = client.request("HEAD", url, label=label, headers=IDENTITY)
    return {
        "content_length": int(response.headers["Content-Length"]),
        "etag": response.headers.get("ETag", "").strip('"'),
        "last_modified": response.headers.get("Last-Modified"),
    }


def _stream_to_file(client: HttpClient, url: str, destination: Path, expected_bytes: int, label: str,
                    truncate_after: int | None = None) -> tuple[int, str]:
    """Download to a temp file, verify byte count, then rename into place."""
    chunk = client.cfg["download_chunk_bytes"]
    max_retries = client.cfg["max_retries"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(destination.name + ".part")
    for attempt in range(1, max_retries + 1):
        digest = hashlib.sha256()
        written = 0
        with client.request("GET", url, stream=True, label=label, headers=IDENTITY) as response, open(tmp, "wb") as f:
            for block in response.iter_content(chunk_size=chunk):
                f.write(block)
                digest.update(block)
                written += len(block)
                if truncate_after is not None and written >= truncate_after:
                    break  # chaos: simulate a connection dropped mid-file
        if written == expected_bytes:
            tmp.replace(destination)
            return written, digest.hexdigest()
        tmp.unlink(missing_ok=True)
        client.logger.warning("Truncated download | source=%s received=%s expected=%s attempt=%s/%s",
                              label, written, expected_bytes, attempt, max_retries)
        if attempt < max_retries:
            client.sleep(client.cfg["backoff_base_seconds"] * (2 ** (attempt - 1)))
    raise IncompleteRetrievalError(
        f"{label}: download incomplete after {max_retries} attempts; no file kept (expected {expected_bytes} bytes)")


def _fetch_file(client: HttpClient, url: str, destination: Path, label: str, logger: logging.Logger,
                force: bool = False, truncate_after: int | None = None) -> dict:
    """Retrieve one file, reusing the local copy only if it provably matches the remote."""
    manifest_path = destination.parent / "_manifest.json"
    remote = _head(client, url, label)
    manifest = read_json(manifest_path) if manifest_path.exists() else None

    reuse = (destination.exists() and not force and truncate_after is None
             and destination.stat().st_size == remote["content_length"])
    if reuse and manifest:
        # Same size is not enough: TLC republishes files, so the ETag must match
        # and the local bytes must still hash to what we recorded last time.
        checksum = sha256_file(destination)
        reuse = manifest.get("etag") == remote["etag"] and checksum == manifest.get("sha256")
    elif reuse:
        checksum = sha256_file(destination)  # right size, fetched outside the pipeline: adopt and record it

    if reuse:
        logger.info("Reusing cached file | source=%s bytes=%s sha256=%s", label, remote["content_length"], checksum[:12])
        written, retrieved_at, action = remote["content_length"], (manifest or {}).get("retrieved_at", _now()), "cached"
    else:
        if destination.exists() and not force and truncate_after is None:
            logger.warning("Local copy does not match the remote file | source=%s local_bytes=%s remote_bytes=%s "
                           "| discarding it and downloading again", label, destination.stat().st_size,
                           remote["content_length"])
        logger.info("Downloading | source=%s bytes=%s", label, remote["content_length"])
        started = time.monotonic()
        written, checksum = _stream_to_file(client, url, destination, remote["content_length"], label, truncate_after)
        retrieved_at, action = _now(), "downloaded"
        logger.info("Downloaded | source=%s bytes=%s seconds=%.1f", label, written, time.monotonic() - started)

    return {
        "source": label,
        "url": url,
        "file": destination.name,
        "action": action,
        "retrieved_at": retrieved_at,
        "remote_content_length": remote["content_length"],
        "bytes_on_disk": written,
        "bytes_match": written == remote["content_length"],
        "sha256": checksum,
        "etag": remote["etag"],
        "remote_last_modified": remote["last_modified"],
    }


def fetch_trip_file(month: str, cfg: Config, client: HttpClient, logger: logging.Logger, force: bool = False,
                    truncate_after: int | None = None) -> dict:
    """Download fhvhv_tripdata_<month>.parquet and record its completeness evidence."""
    url = cfg["sources"]["trip_url_template"].format(month=month)
    destination = cfg.path("raw") / "tlc" / cfg["project"]["dataset"] / f"month={month}" / Path(url).name
    try:
        manifest = _fetch_file(client, url, destination, f"tlc_trip_file:{month}", logger, force, truncate_after)
    except NonRetryableHTTPError as exc:
        if exc.status in (403, 404):
            raise NotPublishedError(
                f"TLC has not published {cfg['project']['dataset']} trip data for {month} "
                f"(HTTP {exc.status} at {url}). Files usually appear about "
                f"{cfg['project']['publication_lag_months']} months after month end.") from exc
        raise

    meta = pq.ParquetFile(destination).metadata
    manifest["parquet_footer_rows"] = meta.num_rows
    manifest["parquet_row_groups"] = meta.num_row_groups
    manifest["parquet_columns"] = [meta.schema.column(i).name for i in range(meta.num_columns)]
    write_json(manifest, destination.parent / "_manifest.json")
    logger.info("Trip file ready | month=%s rows_in_footer=%s columns=%s", month, meta.num_rows, meta.num_columns)
    return {**manifest, "path": str(destination)}


def fetch_zone_lookup(cfg: Config, client: HttpClient, logger: logging.Logger, force: bool = False) -> dict:
    url = cfg["sources"]["zone_lookup_url"]
    destination = cfg.path("raw") / "reference" / "taxi_zone_lookup.csv"
    manifest = _fetch_file(client, url, destination, "taxi_zone_lookup", logger, force)
    lines = destination.read_text(encoding="utf-8").strip().splitlines()
    manifest["header"] = lines[0]
    manifest["data_rows"] = len(lines) - 1
    write_json(manifest, destination.parent / "_manifest.json")
    return {**manifest, "path": str(destination)}


def fetch_socrata(dataset: str, where: str, order: str, out_dir: Path, cfg: Config, client: HttpClient,
                  logger: logging.Logger, base_url: str | None = None) -> tuple[list[dict], dict]:
    """Page through a Socrata dataset and prove every row arrived.

    Asks the API for count(*) first, pages with $limit/$offset in a stable
    $order, saves every raw page, then refuses to return a partial result.
    """
    base = (base_url or cfg["sources"]["socrata_base_url"]).rstrip("/")
    url = f"{base}/{dataset}.json"
    label = f"socrata:{dataset}"
    page_size = cfg["http"]["socrata_page_size"]

    expected = int(client.request("GET", url, params={"$select": "count(*) AS n", "$where": where},
                                  label=label).json()[0]["n"])

    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("page_*.json"):
        stale.unlink()  # a rerun replaces the partition instead of mixing old and new pages

    rows: list[dict] = []
    page = 1
    while True:
        params = {"$where": where, "$order": order, "$limit": page_size, "$offset": (page - 1) * page_size}
        batch = client.request("GET", url, params=params, label=label).json()
        write_json(batch, out_dir / f"page_{page:03d}.json")
        rows.extend(batch)
        logger.info("Fetched API page | source=%s page=%s rows=%s cumulative=%s expected=%s",
                    label, page, len(batch), len(rows), expected)
        if len(batch) < page_size:
            break
        page += 1

    meta = {"source": label, "url": url, "where": where, "order": order, "retrieved_at": _now(),
            "api_reported_count": expected, "rows_received": len(rows), "pages": page,
            "complete": len(rows) == expected}
    write_json(meta, out_dir / "_manifest.json")
    if len(rows) != expected:
        raise IncompleteRetrievalError(f"{label}: API reported {expected} rows, received {len(rows)}")
    return rows, meta
