#!/usr/bin/env python3
"""
API Health Monitor
- Checks a list of HTTP endpoints defined in a JSON config file
- Records status code, latency, and errors
- Writes JSON and CSV reports
- Sends a Slack alert if any endpoint is unhealthy
- Exits with code 1 on failure so CI pipelines can react
"""

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("health-monitor")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    if not cfg.get("endpoints"):
        raise ValueError("Config must contain a non-empty 'endpoints' list.")

    for ep in cfg["endpoints"]:
        if "name" not in ep or "url" not in ep:
            raise ValueError(f"Each endpoint needs 'name' and 'url': {ep}")

    return cfg


def check_endpoint(ep: dict, default_timeout: int) -> dict:
    """Run one health check and return a result dictionary."""
    method = ep.get("method", "GET").upper()
    expected_status = ep.get("expected_status", 200)
    max_latency_ms = ep.get("max_latency_ms")
    timeout = ep.get("timeout", default_timeout)
    headers = ep.get("headers", {})

    result = {
        "name": ep["name"],
        "url": ep["url"],
        "method": method,
        "timestamp": utc_now(),
        "status_code": None,
        "latency_ms": None,
        "healthy": False,
        "error": None,
    }

    start = time.perf_counter()
    try:
        response = requests.request(
            method, ep["url"], headers=headers, timeout=timeout
        )
        latency_ms = (time.perf_counter() - start) * 1000

        result["status_code"] = response.status_code
        result["latency_ms"] = round(latency_ms, 2)

        status_ok = response.status_code == expected_status
        latency_ok = max_latency_ms is None or latency_ms <= max_latency_ms

        result["healthy"] = status_ok and latency_ok

        if not status_ok:
            result["error"] = (
                f"Expected HTTP {expected_status}, got {response.status_code}"
            )
        elif not latency_ok:
            result["error"] = (
                f"Slow response: {latency_ms:.0f}ms (limit {max_latency_ms}ms)"
            )

    except requests.exceptions.Timeout:
        result["error"] = f"Timeout after {timeout}s"
    except requests.exceptions.ConnectionError as exc:
        result["error"] = f"Connection error: {exc}"
    except requests.exceptions.RequestException as exc:
        result["error"] = f"Request failed: {type(exc).__name__}: {exc}"

    return result


def save_reports(results: list[dict], report_dir: str) -> None:
    """Write a JSON report and a CSV report for this run."""
    out = Path(report_dir)
    out.mkdir(parents=True, exist_ok=True)

    healthy_count = sum(1 for r in results if r["healthy"])
    summary = {
        "generated_at": utc_now(),
        "total": len(results),
        "healthy": healthy_count,
        "unhealthy": len(results) - healthy_count,
        "results": results,
    }

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    json_path = out / f"report-{stamp}.json"
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # Always keep a "latest" copy that is easy to find
    (out / "latest.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    csv_path = out / f"report-{stamp}.csv"
    fieldnames = ["timestamp", "name", "url", "method", "status_code",
                  "latency_ms", "healthy", "error"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow({k: r.get(k) for k in fieldnames})

    log.info("Reports written to %s", out)


def send_slack_alert(failures: list[dict], total: int) -> None:
    """Send a Slack message via incoming webhook, if configured."""
    webhook_url = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook_url:
        log.warning("SLACK_WEBHOOK_URL not set, skipping Slack alert.")
        return

    lines = [
        f":rotating_light: *API Health Alert*: {len(failures)}/{total} endpoint(s) failing",
        "",
    ]
    for f in failures:
        lines.append(f"• *{f['name']}* ({f['url']})")
        lines.append(f"   Error: {f['error']}")

    run_url = None
    if os.getenv("GITHUB_SERVER_URL") and os.getenv("GITHUB_REPOSITORY") and os.getenv("GITHUB_RUN_ID"):
        run_url = (
            f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}"
            f"/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        )
        lines.append("")
        lines.append(f"<{run_url}|View workflow run>")

    try:
        resp = requests.post(webhook_url, json={"text": "\n".join(lines)}, timeout=10)
        resp.raise_for_status()
        log.info("Slack alert sent.")
    except requests.exceptions.RequestException as exc:
        log.error("Failed to send Slack alert: %s", exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="API Health Monitor")
    parser.add_argument("--config", default="config/endpoints.json",
                        help="Path to endpoints config file")
    parser.add_argument("--report-dir", default="reports",
                        help="Directory for report output")
    args = parser.parse_args()

    cfg = load_config(args.config)
    default_timeout = cfg.get("default_timeout", 10)

    log.info("Checking %d endpoint(s)...", len(cfg["endpoints"]))
    results = [check_endpoint(ep, default_timeout) for ep in cfg["endpoints"]]

    for r in results:
        if r["healthy"]:
            log.info("OK    | %-25s | %s | %sms",
                     r["name"], r["status_code"], r["latency_ms"])
        else:
            log.error("FAIL  | %-25s | %s", r["name"], r["error"])

    save_reports(results, args.report_dir)

    failures = [r for r in results if not r["healthy"]]
    if failures:
        send_slack_alert(failures, total=len(results))
        log.error("%d endpoint(s) unhealthy.", len(failures))
        return 1

    log.info("All endpoints healthy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())