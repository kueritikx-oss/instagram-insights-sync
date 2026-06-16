#!/usr/bin/env python3
"""Dispatch a GitHub Actions workflow when its latest run is stale or failed."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone


REPO = os.environ.get("WORKFLOW_WATCH_REPO") or os.environ.get("GITHUB_REPOSITORY") or "kueritikx-oss/instagram-insights-sync"
WORKFLOW = os.environ["WORKFLOW_WATCH_FILE"]
REF = os.environ.get("WORKFLOW_WATCH_REF", "main")
STALE_MINUTES = int(os.environ.get("WORKFLOW_WATCH_STALE_MINUTES", "45"))
FAILURE_RETRY_MINUTES = int(os.environ.get("WORKFLOW_WATCH_FAILURE_RETRY_MINUTES", str(STALE_MINUTES)))
DRY_RUN = os.environ.get("WORKFLOW_WATCH_DRY_RUN", "").lower() in {"1", "true", "yes"}
ENABLE_DISABLED_WORKFLOW = os.environ.get("WORKFLOW_WATCH_ENABLE_DISABLED", "true").lower() not in {
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


def sanitize(text: str) -> str:
    return text.replace("\n", " / ").strip()[:500]


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


def summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip() + "\n")


def workflow_api_path(suffix: str = "") -> str:
    return f"repos/{REPO}/actions/workflows/{WORKFLOW}{suffix}"


def ensure_workflow_enabled() -> tuple[bool, str]:
    code, out = run(["gh", "api", workflow_api_path()])
    if code != 0:
        return False, f"workflow state check failed: {sanitize(out)}"
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        return False, f"invalid workflow state JSON: {exc}"

    state = str(data.get("state") or "unknown")
    print(f"{WORKFLOW} state={state}")
    if not state.startswith("disabled"):
        return True, f"workflow state={state}"
    if not ENABLE_DISABLED_WORKFLOW:
        return False, "workflow is disabled and auto-enable is disabled"

    enable_code, enable_out = run(["gh", "api", "-X", "PUT", workflow_api_path("/enable")])
    if enable_code != 0:
        return False, f"workflow enable failed: {sanitize(enable_out)}"
    return True, f"workflow was {state}; enabled"


def latest_run_action() -> tuple[str, str, float | None]:
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
        ]
    )
    if code != 0:
        return "error", f"gh run list failed: {sanitize(out)}", None
    try:
        runs = json.loads(out)
    except json.JSONDecodeError as exc:
        return "error", f"invalid gh JSON: {exc}", None
    if not runs:
        return "dispatch", "no previous runs found", None

    latest = runs[0]
    latest_age = age_minutes(latest.get("createdAt"))
    latest_id = latest.get("databaseId", "?")
    latest_status = latest.get("status", "?")
    latest_conclusion = latest.get("conclusion") or "none"
    age_text = "unknown" if latest_age is None else f"{latest_age:.1f}m"
    print(f"latest {WORKFLOW}: id={latest_id} status={latest_status} conclusion={latest_conclusion} age={age_text}")

    if latest_status in {"queued", "in_progress", "waiting", "requested", "pending"}:
        return "skip", "workflow is already active", latest_age

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
        return "skip", "latest run failed recently; wait before retrying", latest_age

    if latest_age is None:
        return "dispatch", "latest run timestamp was not parseable", None
    if latest_age >= STALE_MINUTES:
        return "dispatch", f"latest run is stale: {latest_age:.1f}m >= {STALE_MINUTES}m", latest_age
    return "skip", "workflow is fresh", latest_age


def dispatch(reason: str) -> int:
    if DRY_RUN:
        print(f"dry-run: would dispatch {WORKFLOW}: {reason}")
        return 0
    print(f"dispatching {WORKFLOW}: {reason}")
    code, out = run(["gh", "workflow", "run", WORKFLOW, "--repo", REPO, "--ref", REF])
    print(out.strip())
    return code


def main() -> int:
    ok, workflow_reason = ensure_workflow_enabled()
    if not ok:
        summary(["## workflow stale dispatch", f"- workflow: {WORKFLOW}", "- result: error", f"- reason: {workflow_reason}"])
        print(workflow_reason, file=sys.stderr)
        return 1

    action, reason, latest_age = latest_run_action()
    if action == "error":
        summary(["## workflow stale dispatch", f"- workflow: {WORKFLOW}", "- result: error", f"- reason: {reason}"])
        print(reason, file=sys.stderr)
        return 1
    if action == "skip":
        summary(
            [
                "## workflow stale dispatch",
                f"- workflow: {WORKFLOW}",
                "- result: skip",
                f"- reason: {reason}",
                f"- age_min: {latest_age:.1f}" if latest_age is not None else "- age_min: unknown",
                f"- workflow_state: {workflow_reason}",
            ]
        )
        print(reason)
        return 0

    code = dispatch(reason)
    summary(
        [
            "## workflow stale dispatch",
            f"- workflow: {WORKFLOW}",
            f"- result: {'dry-run-dispatch' if DRY_RUN else ('dispatched' if code == 0 else 'error')}",
            f"- reason: {reason}",
            f"- age_min: {latest_age:.1f}" if latest_age is not None else "- age_min: unknown",
            f"- workflow_state: {workflow_reason}",
        ]
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
