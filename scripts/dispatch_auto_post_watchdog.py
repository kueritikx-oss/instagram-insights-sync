#!/usr/bin/env python3
"""Dispatch the Instagram auto-post workflow if it has gone stale.

This is a cloud-side safety net for GitHub Actions schedule delays. It does
not post directly; it only asks the normal auto-post workflow to run, so the
same idempotency checks and posting limits remain in one place.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone


REPO = os.environ.get("AUTO_POST_REPO") or os.environ.get("GITHUB_REPOSITORY") or "kueritikx-oss/instagram-insights-sync"
WORKFLOW = os.environ.get("AUTO_POST_WORKFLOW", "auto-post-instagram.yml")
STALE_MINUTES = int(os.environ.get("AUTO_POST_STALE_MINUTES", "28"))
FAILURE_RETRY_MINUTES = int(os.environ.get("AUTO_POST_FAILURE_RETRY_MINUTES", str(STALE_MINUTES)))
ENABLE_DISABLED_WORKFLOW = os.environ.get("AUTO_POST_ENABLE_DISABLED_WORKFLOW", "true").lower() not in {
    "0",
    "false",
    "no",
}


def run(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return res.returncode, (res.stdout or "") + (res.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def age_minutes(value: str | None) -> float | None:
    dt = parse_time(value)
    if not dt:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60


def sanitize(text: str) -> str:
    return text.replace("\n", " / ").strip()[:500]


def workflow_api_path(suffix: str = "") -> str:
    return f"repos/{REPO}/actions/workflows/{WORKFLOW}{suffix}"


def ensure_workflow_enabled() -> int:
    code, out = run(["gh", "api", workflow_api_path()], timeout=60)
    if code != 0:
        print(f"workflow state check failed: {sanitize(out)}", file=sys.stderr)
        return code
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        print(f"invalid workflow state JSON: {exc}", file=sys.stderr)
        return 1

    state = str(data.get("state") or "unknown")
    print(f"auto-post workflow state={state}")
    if not state.startswith("disabled"):
        return 0
    if not ENABLE_DISABLED_WORKFLOW:
        print("workflow is disabled and auto-enable is disabled", file=sys.stderr)
        return 1

    enable_code, enable_out = run(["gh", "api", "-X", "PUT", workflow_api_path("/enable")], timeout=60)
    if enable_code != 0:
        print(f"workflow enable failed: {sanitize(enable_out)}", file=sys.stderr)
        return enable_code
    print(f"workflow was {state}; enabled")
    return 0


def dispatch(reason: str) -> int:
    print(f"dispatching {WORKFLOW}: {reason}")
    code, out = run(["gh", "workflow", "run", WORKFLOW, "--repo", REPO], timeout=60)
    print(out.strip())
    return code


def main() -> int:
    enable_code = ensure_workflow_enabled()
    if enable_code != 0:
        return enable_code

    code, out = run(
        [
            "gh",
            "run",
            "list",
            "--repo",
            REPO,
            "--workflow",
            WORKFLOW,
            "--limit",
            "6",
            "--json",
            "databaseId,event,status,conclusion,createdAt,headSha",
        ],
        timeout=60,
    )
    if code != 0:
        print(f"gh run list failed: {out[:500]}", file=sys.stderr)
        return code

    try:
        runs = json.loads(out)
    except json.JSONDecodeError as exc:
        print(f"invalid gh JSON: {exc}", file=sys.stderr)
        return 1

    if not runs:
        return dispatch("no previous auto-post runs found")

    latest = runs[0]
    latest_id = latest.get("databaseId", "?")
    latest_event = latest.get("event", "?")
    latest_status = latest.get("status", "?")
    latest_conclusion = latest.get("conclusion") or "none"
    latest_age = age_minutes(latest.get("createdAt"))
    age_text = "unknown" if latest_age is None else f"{latest_age:.1f}m"
    print(
        "latest auto-post run: "
        f"id={latest_id} event={latest_event} status={latest_status} "
        f"conclusion={latest_conclusion} age={age_text}"
    )

    if latest_status in {"queued", "in_progress", "waiting", "requested", "pending"}:
        print("auto-post workflow is already active; skip dispatch")
        return 0

    if latest_age is None:
        return dispatch("latest run timestamp was not parseable")

    failed = latest_status == "completed" and latest_conclusion in {
        "failure",
        "cancelled",
        "timed_out",
        "action_required",
        "startup_failure",
    }
    if failed:
        if latest_age >= FAILURE_RETRY_MINUTES:
            return dispatch(f"latest run failed and is {latest_age:.1f}m old")
        print("latest run failed recently; wait before retrying to avoid retry storms")
        return 0

    if latest_age >= STALE_MINUTES:
        return dispatch(f"latest run is stale: {latest_age:.1f}m >= {STALE_MINUTES}m")

    print("auto-post workflow is fresh; skip dispatch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
