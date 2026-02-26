"""
main.py — CLI entry point for the Automaton Auditor Swarm.

Usage:
    uv run python main.py <repo_url> [--pdf <path>] [--output-dir <dir>]

Examples:
    # Audit a repo (no PDF)
    uv run python main.py https://github.com/user/repo

    # Audit with PDF report
    uv run python main.py https://github.com/user/repo --pdf reports/final_report.pdf

    # Custom output directory
    uv run python main.py https://github.com/user/repo --pdf report.pdf --output-dir audit/report_onpeer_generated
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys

from dotenv import load_dotenv

# Load environment variables before any LLM imports
load_dotenv()


# ---------------------------------------------------------------------------
# SIGINT handler — forcefully terminate even when threads are blocked on I/O.
# sys.exit() only raises SystemExit which background threads can ignore;
# os._exit() kills the process at the OS level immediately.
# ---------------------------------------------------------------------------
def _sigint_handler(signum, frame):
    """Handle Ctrl+C by forcefully killing the process."""
    print("\n" + "!" * 60, file=sys.stderr)
    print("AUDIT INTERRUPTED BY USER (Ctrl+C). Force-killing process...", file=sys.stderr)
    print("!" * 60, file=sys.stderr)
    os._exit(130)  # 128 + SIGINT(2) = 130


signal.signal(signal.SIGINT, _sigint_handler)


def setup_logging(verbose: bool = False) -> None:
    """Configure logging for the auditor."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s │ %(name)-30s │ %(levelname)-7s │ %(message)s",
        datefmt="%H:%M:%S",
    )
    # Reduce noise from HTTP libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Automaton Auditor -- Digital Courtroom for Code Governance",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  uv run python main.py https://github.com/user/repo\n"
            "  uv run python main.py https://github.com/user/repo --pdf report.pdf\n"
            "  uv run python main.py https://github.com/user/repo --pdf report.pdf "
            "--output-dir audit/report_onpeer_generated\n"
        ),
    )

    parser.add_argument(
        "repo_url",
        help="HTTPS URL of the GitHub repository to audit",
    )
    parser.add_argument(
        "--pdf",
        dest="pdf_path",
        default=None,
        help="Path to the PDF report for cross-referencing (optional)",
    )
    parser.add_argument(
        "--output-dir",
        dest="output_dir",
        default=os.getenv("OUTPUT_DIR", "audit/report_onself_generated"),
        help="Directory for the generated audit report",
    )
    parser.add_argument(
        "--rubric",
        dest="rubric_path",
        default=os.getenv("RUBRIC_PATH"),
        help="Path to rubric.json",
    )
    parser.add_argument(
        "--thread-id",
        dest="thread_id",
        default="audit_session_1",
        help="Thread ID for LangGraph checkpointing",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    logger = logging.getLogger("main")

    # Ensure output directory exists
    os.makedirs(args.output_dir, exist_ok=True)

    logger.info("=" * 60)
    logger.info("AUTOMATON AUDITOR -- Digital Courtroom")
    logger.info("=" * 60)
    logger.info("Target repo:  %s", args.repo_url)
    logger.info("PDF report:   %s", args.pdf_path or "(none)")
    logger.info("Output dir:   %s", args.output_dir)
    logger.info("=" * 60)

    # Import here to avoid circular imports and ensure .env is loaded first
    from src.graph import run_auditor_graph
    from src.report_generator import render_audit_report

    # Run the full auditor graph
    try:
        final_state = run_auditor_graph(
            repo_url=args.repo_url,
            pdf_path=args.pdf_path,
            rubric_path=args.rubric_path,
            thread_id=args.thread_id,
        )
    except KeyboardInterrupt:
        logger.warning("\n" + "!" * 60)
        logger.warning("AUDIT INTERRUPTED BY USER (Ctrl+C). Shutting down...")
        logger.warning("!" * 60)
        sys.exit(130)  # Standard exit code for SIGINT

    # Render the report if we got one
    report = final_state.get("final_report")
    if report:
        try:
            # Save Markdown Report (the task doc requires Markdown output)
            output_path = os.path.join(args.output_dir, "audit_report.md")
            render_audit_report(report, output_path)

            logger.info("=" * 60)
            logger.info("AUDIT COMPLETE")
            logger.info("   Overall Score: %.1f / 5.0", report.overall_score)
            logger.info("   Report:        %s", output_path)
            logger.info("=" * 60)
        except KeyboardInterrupt:
            logger.warning(
                "User interrupted while saving report; cleaning up and exiting."
            )
            sys.exit(130)
        except Exception as exc:
            logger.exception("Failed to write final report: %s", exc)
            sys.exit(1)
    else:
        error = final_state.get("error", "Unknown error")
        logger.error("=" * 60)
        logger.error("AUDIT FAILED: %s", error)
        logger.error("=" * 60)
        sys.exit(1)


if __name__ == "__main__":
    main()
