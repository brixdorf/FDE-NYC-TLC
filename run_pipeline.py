"""Monthly NYC TLC rider-wait pipeline.

Usage:
    python run_pipeline.py                         # latest month TLC should have published
    python run_pipeline.py --month 2026-06
    python run_pipeline.py --from 2026-05 --to 2026-07
    python run_pipeline.py --month 2026-06 --chaos api_down

Exit codes: 0 success (including degraded runs), 1 unexpected error,
2 validation failed, 3 retrieval failed, 4 bad arguments.
Each month runs independently; the exit code is the worst across months.
"""
from __future__ import annotations

import argparse
import re
import sys
import traceback
from datetime import date

from pipeline import report
from pipeline.config import load_config
from pipeline.extract import RetrievalError
from pipeline.logging_utils import get_logger, new_run_id
from pipeline.runner import CHAOS_SCENARIOS, run_month
from pipeline.validate import ValidationError

EXIT_OK, EXIT_UNEXPECTED, EXIT_VALIDATION, EXIT_RETRIEVAL, EXIT_ARGS = 0, 1, 2, 3, 4
MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _shift(month: str, delta: int) -> str:
    year, mon = map(int, month.split("-"))
    idx = year * 12 + (mon - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def resolve_months(args, lag: int) -> list[str]:
    if args.month:
        return [args.month]
    if args.from_month:
        end = args.to_month or args.from_month
        months, m = [], args.from_month
        while m <= end:
            months.append(m)
            m = _shift(m, 1)
        return months
    return [_shift(f"{date.today():%Y-%m}", -lag)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--month", help="single month, YYYY-MM")
    parser.add_argument("--from", dest="from_month", help="first month of a range, YYYY-MM")
    parser.add_argument("--to", dest="to_month", help="last month of a range, YYYY-MM")
    parser.add_argument("--force-download", action="store_true", help="ignore the verified local copy")
    parser.add_argument("--chaos", choices=CHAOS_SCENARIOS, help="inject one failure to demonstrate handling")
    parser.add_argument("--no-report", action="store_true", help="skip regenerating docs/evidence.md")
    args = parser.parse_args(argv)

    cfg = load_config()
    for value in (args.month, args.from_month, args.to_month):
        if value and not MONTH.match(value):
            parser.print_usage(sys.stderr)
            print(f"error: {value!r} is not a month in YYYY-MM form", file=sys.stderr)
            return EXIT_ARGS
    months = resolve_months(args, cfg["project"]["publication_lag_months"])

    run_id = new_run_id()
    worst = EXIT_OK
    for month in months:
        logger = get_logger(run_id, cfg.path("logs"), month)
        logger.info("Pipeline start | month=%s chaos=%s config=%s", month, args.chaos, cfg.fingerprint)
        try:
            run_month(month, cfg, run_id, logger, chaos=args.chaos, force_download=args.force_download)
            logger.info("Pipeline finished | month=%s status=SUCCESS", month)
        except ValidationError as exc:
            logger.error("PIPELINE FAILED | month=%s stage=validate reason=%s | no processed output written; "
                         "previous outputs for this month left untouched", month, exc)
            worst = max(worst, EXIT_VALIDATION)
        except RetrievalError as exc:
            logger.error("PIPELINE FAILED | month=%s stage=extract reason=%s | no processed output written; "
                         "previous outputs for this month left untouched", month, exc)
            worst = max(worst, EXIT_RETRIEVAL)
        except Exception as exc:  # anything unplanned still fails loudly, with the trace in the log
            logger.error("PIPELINE FAILED | month=%s unexpected error: %s\n%s", month, exc, traceback.format_exc())
            worst = max(worst, EXIT_UNEXPECTED)

    if not args.no_report and not args.chaos:
        report.build_evidence(cfg, get_logger(run_id, cfg.path("logs"), "report"))
    return worst


if __name__ == "__main__":
    sys.exit(main())
