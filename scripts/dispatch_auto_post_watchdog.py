#!/usr/bin/env python3
"""Dispatch the Instagram auto-post workflow if it has gone stale."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cloud_auto_post as cap  # noqa: E402
from ig_auto_post_contract import collect_queue_snapshot  # noqa: E402


REPO = os.environ.get("AUTO_POST_REPO") or os.environ.get("GITHUB_REPOSITORY") or "kueritikx-oss/instagram-insights-sync"
WORKFLOW = os.environ.get("AUTO_POST_WORKFLOW", "auto-post-instagram.yml")
STALE_MINUTES = int(os.environ.get("AUTO_POST_STALE_MINUTES", "28"))
FAILURE_RETRY_MINUTES = int(os.environ.get("AUTO_POST_FAILURE_RETRY_MINUTES", str(STALE_MINUTES)))
QUEUE_HORIZON_HOURS = int(os.environ.get("AUTO_POST_QUEUE_HORIZON_HOURS", "72"))
INFLIGHT_MINUTES = int(os.environ.get("AUTO_POST_INFLIGHT_MINUTES", "45"))
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


def append_step_summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path or not lines:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip() + "\n")


def workflow_api_path(suffix: str = "") -> str:
    return f"repos/{REPO}/actions/workflows/{WORKFLOW}{suffix}"


def ensure_workflow_enabled() -> tuple[bool, str]:
    code, out = run(["gh", "api", workflow_api_path()], timeout=60)
    if code != 0:
        return False, f"workflow state check failed: {sanitize(out)}"
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        return False, f"invalid workflow state JSON: {exc}"

    state = str(data.get("state") or "unknown")
    print(f"auto-post workflow state={state}")
    if not state.startswith("disabled"):
        return True, f"workflow state={state}"
    if not ENABLE_DISABLED_WORKFLOW:
        return False, "workflow is disabled and auto-enable is disabled"

    enable_code, enable_out = run(["gh", "api", "-X", "PUT", workflow_api_path("/enable")], timeout=60)
    if enable_code != 0:
        return False, f"workflow enable failed: {sanitize(enable_out)}"
    print(f"workflow was {state}; enabled")
    return True, f"workflow was {state}; enabled"


def latest_run_state() -> tuple[str, str, float | None]:
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
        return "error", f"gh run list failed: {sanitize(out)}", None

    try:
        runs = json.loads(out)
    except json.JSONDecodeError as exc:
        return "error", f"invalid gh JSON: {exc}", None

    if not runs:
        return "dispatch", "no previous auto-post runs found", None

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
        return "skip", "auto-post workflow is already active; skip dispatch", latest_age

    failed = latest_status == "completed" and latest_conclusion in {
        "failure",
        "cancelled",
        "timed_out",
        "action_required",
        "startup_failure",
    }
    if failed:
        if latest_age is None or latest_age >= FAILURE_RETRY_MINUTES:
            return "dispatch", f"latest run failed and is {age_text} old", latest_age
        return "skip", "latest run failed recently; wait before retrying to avoid retry storms", latest_age

    if latest_age is None:
        return "dispatch", "latest run timestamp was not parseable", None
    if latest_age >= STALE_MINUTES:
        return "dispatch", f"latest run is stale: {latest_age:.1f}m >= {STALE_MINUTES}m", latest_age
    return "skip", "auto-post workflow is fresh; skip dispatch", latest_age


def queue_state() -> tuple[object | None, str | None]:
    try:
        snapshot = collect_queue_snapshot(
            cap,
            horizon_hours=QUEUE_HORIZON_HOURS,
            inflight_minutes=INFLIGHT_MINUTES,
        )
        return snapshot, None
    except Exception as exc:
        return None, f"queue snapshot failed: {type(exc).__name__}: {str(exc)[:180]}"


def dispatch(reason: str) -> int:
    print(f"dispatching {WORKFLOW}: {reason}")
    code, out = run(["gh", "workflow", "run", WORKFLOW, "--repo", REPO, "--ref", "main"], timeout=60)
    print(out.strip())
    return code


def main() -> int:
    ok, workflow_reason = ensure_workflow_enabled()
    if not ok:
        append_step_summary([
            "## IG dispatch watchdog",
            "- result: error",
            f"- reason: {workflow_reason}",
        ])
        print(workflow_reason, file=sys.stderr)
        return 1

    action, reason, latest_age = latest_run_state()
    if action == "error":
        append_step_summary([
            "## IG dispatch watchdog",
            "- result: error",
            f"- reason: {reason}",
        ])
        print(reason, file=sys.stderr)
        return 1
    if action == "skip":
        append_step_summary([
            "## IG dispatch watchdog",
            "- result: skip",
            f"- reason: {reason}",
            f"- actions_age_min: {latest_age:.1f}" if latest_age is not None else "- actions_age_min: unknown",
        ])
        print(reason)
        return 0

    snapshot, queue_error = queue_state()
    if queue_error:
        append_step_summary([
            "## IG dispatch watchdog",
            "- result: error",
            f"- reason: {queue_error}",
        ])
        print(queue_error, file=sys.stderr)
        return 1

    queue_lines = [
        f"- queue_ready_total: {snapshot.ready_total}",
        f"- queue_ready_within_horizon: {snapshot.ready_within_horizon}",
        f"- queue_overdue_ready: {snapshot.overdue_ready}",
        f"- queue_in_flight_fresh: {snapshot.in_flight_fresh}",
        f"- queue_in_flight_stale: {snapshot.in_flight_stale}",
    ]
    if snapshot.in_flight_fresh > 0:
        append_step_summary([
            "## IG dispatch watchdog",
            "- result: skip",
            "- reason: fresh in-flight row exists",
            *queue_lines,
        ])
        print("fresh in-flight row exists; skip dispatch")
        return 0
    if snapshot.ready_within_horizon <= 0 and snapshot.overdue_ready <= 0:
        append_step_summary([
            "## IG dispatch watchdog",
            "- result: skip",
            f"- reason: no ready queue within {QUEUE_HORIZON_HOURS}h",
            *queue_lines,
        ])
        print(f"no ready queue within {QUEUE_HORIZON_HOURS}h; skip dispatch")
        return 0

    code = dispatch(reason)
    append_step_summary([
        "## IG dispatch watchdog",
        f"- result: {'dispatched' if code == 0 else 'error'}",
        f"- reason: {reason}",
        *queue_lines,
        f"- workflow_state: {workflow_reason}",
    ])
    return code


if __name__ == "__main__":
    raise SystemExit(main())
