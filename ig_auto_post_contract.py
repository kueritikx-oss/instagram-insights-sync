#!/usr/bin/env python3
"""Shared contract helpers for IG auto-post dispatch/rescue paths."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


READY_STATUSES = {"ready", "retry"}


@dataclass
class QueueSnapshot:
    ready_total: int
    ready_within_horizon: int
    overdue_ready: int
    in_flight_fresh: int
    in_flight_stale: int
    ready_samples: list[str]
    overdue_samples: list[str]
    in_flight_samples: list[str]


def parse_last_attempt(value: str, jst) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    for parser in (
        lambda: datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(jst),
        lambda: datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=jst),
    ):
        try:
            return parser()
        except Exception:
            continue
    return None


def row_contract_state(row: list[str], cap, now: datetime, inflight_minutes: int) -> dict | None:
    post_num = row[cap.COL_POST_NUM].strip() if len(row) > cap.COL_POST_NUM else ""
    if not post_num:
        return None

    status = (row[cap.COL_STATUS].strip() if len(row) > cap.COL_STATUS else "").lower()
    media_id = row[cap.COL_MEDIA_ID].strip() if len(row) > cap.COL_MEDIA_ID else ""
    error = row[cap.COL_ERROR].strip() if len(row) > cap.COL_ERROR else ""
    caption = row[cap.COL_CAPTION].strip() if len(row) > cap.COL_CAPTION else ""
    image_urls_str = row[cap.COL_IMAGE_URLS].strip() if len(row) > cap.COL_IMAGE_URLS else ""
    date_val = row[cap.COL_DATE] if len(row) > cap.COL_DATE else ""
    time_val = row[cap.COL_TIME] if len(row) > cap.COL_TIME else ""
    last_attempt = row[cap.COL_LAST_ATTEMPT].strip() if len(row) > cap.COL_LAST_ATTEMPT else ""
    scheduled = cap.parse_schedule_time(date_val, time_val)

    if status == "posted" or media_id:
        row_state = "posted"
    elif status == "failed":
        row_state = "failed"
    elif status == "posting" or error.startswith("claim:"):
        attempt_dt = parse_last_attempt(last_attempt, now.tzinfo)
        cutoff = timedelta(minutes=max(1, inflight_minutes))
        if attempt_dt is None or now - attempt_dt <= cutoff:
            row_state = "in_flight_fresh"
        else:
            row_state = "in_flight_stale"
    elif status in READY_STATUSES and image_urls_str and caption and scheduled:
        row_state = "ready"
    else:
        row_state = "other"

    return {
        "post_num": post_num,
        "row_state": row_state,
        "scheduled": scheduled,
        "status": status,
    }


def collect_queue_snapshot(cap, horizon_hours: int = 72, inflight_minutes: int = 45) -> QueueSnapshot:
    service = cap.get_sheets_service()
    cap.refresh_columns_from_header(service)
    rows = cap.read_all_rows(service)
    now = datetime.now(cap.JST)

    ready_total = 0
    ready_within_horizon = 0
    overdue_ready = 0
    in_flight_fresh = 0
    in_flight_stale = 0
    ready_samples: list[str] = []
    overdue_samples: list[str] = []
    in_flight_samples: list[str] = []

    horizon = timedelta(hours=max(1, horizon_hours))

    for row in rows:
        item = row_contract_state(row, cap, now, inflight_minutes)
        if not item:
            continue
        row_state = item["row_state"]
        post_num = item["post_num"]
        scheduled = item["scheduled"]

        if row_state == "ready":
            ready_total += 1
            if scheduled and scheduled <= now + horizon:
                ready_within_horizon += 1
                if len(ready_samples) < 5:
                    ready_samples.append(f"#{post_num} {scheduled.strftime('%m/%d %H:%M')}")
            if scheduled and scheduled <= now:
                overdue_ready += 1
                if len(overdue_samples) < 5:
                    overdue_samples.append(f"#{post_num} {scheduled.strftime('%m/%d %H:%M')}")
        elif row_state == "in_flight_fresh":
            in_flight_fresh += 1
            if len(in_flight_samples) < 5:
                label = scheduled.strftime('%m/%d %H:%M') if scheduled else "no-schedule"
                in_flight_samples.append(f"#{post_num} {label}")
        elif row_state == "in_flight_stale":
            in_flight_stale += 1
            if len(in_flight_samples) < 5:
                label = scheduled.strftime('%m/%d %H:%M') if scheduled else "no-schedule"
                in_flight_samples.append(f"#{post_num} stale {label}")

    return QueueSnapshot(
        ready_total=ready_total,
        ready_within_horizon=ready_within_horizon,
        overdue_ready=overdue_ready,
        in_flight_fresh=in_flight_fresh,
        in_flight_stale=in_flight_stale,
        ready_samples=ready_samples,
        overdue_samples=overdue_samples,
        in_flight_samples=in_flight_samples,
    )
