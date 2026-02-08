from __future__ import annotations

import datetime
import os
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import FastAPI, Query, Body

from fastapi.requests import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from zoneinfo import ZoneInfo
app = FastAPI()
users = {}
api_token: Optional[str] = None
MAX_HOUR = 80


def _get_config() -> Dict[str, str]:
    """
    Minimal config loader.
    - Prefer environment variables (safer).
    - Fall back to existing hard-coded values (backward compatible).
    """
    client_id = os.getenv(
        "FT_CLIENT_ID",
        "42_API_CLIENT_ID_HERE",
    )
    client_secret = os.getenv(
        "FT_CLIENT_SECRET",
        "42_API_CLIENT_SECRET_HERE",
    )

    # Base URL where this FastAPI server is reachable from the browser.
    # Example: http://localhost:8000
    base_url = os.getenv("APP_BASE_URL", "https://42time.lkim.me").rstrip("/")
    redirect_uri = os.getenv("FT_REDIRECT_URI", f"{base_url}/callback")
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "base_url": base_url,
        "redirect_uri": redirect_uri,
    }


def _authorize_url() -> str:
    cfg = _get_config()
    return (
        "https://api.intra.42.fr/oauth/authorize"
        f"?client_id={cfg['client_id']}"
        f"&redirect_uri={requests.utils.quote(cfg['redirect_uri'], safe='')}"
        "&response_type=code"
    )


def get_api_token() -> str:
    cfg = _get_config()
    r = requests.post(
        "https://api.intra.42.fr/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
        },
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _refresh_api_token() -> str:
    """새 client_credentials 토큰을 발급받고 전역 api_token 을 갱신한다."""
    global api_token
    api_token = get_api_token()
    return api_token

def _iso_utc(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _sum_locations(locations: List[Dict[str, Any]]) -> datetime.timedelta:
    total = datetime.timedelta()
    for x in locations:
        if x.get("end_at"):
            end = datetime.datetime.strptime(x["end_at"][:19], "%Y-%m-%dT%H:%M:%S")
        else:
            end = datetime.datetime.utcnow()
        start = datetime.datetime.strptime(x["begin_at"][:19], "%Y-%m-%dT%H:%M:%S")
        total += (end - start)
    return total


def _parse_duration_to_seconds(value: Any) -> int:
    """
    42 API 'locations_stats' values are not consistent:
    - "HH:MM:SS" (string)
    - "HH:MM" (string)
    - "57.885179" (string/number) -> treat as hours (float)
    """
    if value is None:
        return 0

    # numeric -> hours (float)
    if isinstance(value, (int, float)):
        return max(0, int(round(float(value) * 3600)))

    s = str(value).strip()
    if not s:
        return 0

    if ":" in s:
        parts = s.split(":")
        try:
            if len(parts) == 3:
                h, m, sec = parts
                return max(0, int(h) * 3600 + int(m) * 60 + int(sec))
            if len(parts) == 2:
                h, m = parts
                return max(0, int(h) * 3600 + int(m) * 60)
        except ValueError:
            return 0
        return 0

    # fallback: parse as hours float
    try:
        return max(0, int(round(float(s) * 3600)))
    except ValueError:
        return 0


def _to_hms(total_seconds: int) -> str:
    total_seconds = max(0, int(total_seconds))
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _parse_iso_dt(value: str) -> datetime.datetime:
    # Handles "2026-02-05T12:34:56.000Z" and similar.
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    return datetime.datetime.fromisoformat(v)


def _month_range_local(now_local: datetime.datetime) -> Tuple[datetime.datetime, datetime.datetime]:
    tz = now_local.tzinfo
    start = datetime.datetime(now_local.year, now_local.month, 1, tzinfo=tz)
    # end is "now" (not month end), so we don't show future days.
    return start, now_local


def _add_duration_per_day(
    per_day_seconds: Dict[str, int],
    start_utc: datetime.datetime,
    end_utc: datetime.datetime,
    tz: ZoneInfo,
) -> None:
    """
    Split a session that can span midnight into per-day buckets (Asia/Seoul).
    """
    if end_utc <= start_utc:
        return

    start_local = start_utc.astimezone(tz)
    end_local = end_utc.astimezone(tz)

    cursor = start_local
    while cursor.date() < end_local.date():
        next_midnight = datetime.datetime(cursor.year, cursor.month, cursor.day, tzinfo=tz) + datetime.timedelta(days=1)
        sec = int((next_midnight - cursor).total_seconds())
        key = cursor.date().isoformat()
        per_day_seconds[key] = per_day_seconds.get(key, 0) + max(0, sec)
        cursor = next_midnight

    # last partial (same date)
    sec = int((end_local - cursor).total_seconds())
    key = cursor.date().isoformat()
    per_day_seconds[key] = per_day_seconds.get(key, 0) + max(0, sec)


def _fetch_locations_in_range(
    userid: int,
    bearer_token: str,
    begin_utc: str,
    end_utc: str,
) -> List[Dict[str, Any]]:
    """
    Fetch all locations within [begin_utc, end_utc] with pagination.
    """
    all_locations: List[Dict[str, Any]] = []
    page = 1
    while True:
        r = requests.get(
            f"https://api.intra.42.fr/v2/users/{userid}/locations",
            headers={"Authorization": f"Bearer {bearer_token}"},
            params={
                "page[size]": 100,
                "page[number]": page,
                "range[begin_at]": f"{begin_utc},{end_utc}",
            },
            timeout=10,
        )
        if not r.ok:
            raise RuntimeError(f"locations request failed: {r.status_code} {r.text}")
        batch = r.json()
        if not batch:
            break
        all_locations.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return all_locations


def _build_month_payload_from_locations(locations: List[Dict[str, Any]], now_local: datetime.datetime, location: str, state: Dict[str,Any], username: str) -> Dict[str, Any]:
    tz = ZoneInfo("Asia/Seoul")
    month_start_local, month_end_local = _month_range_local(now_local)

    per_day_seconds: Dict[str, int] = {}
    for loc in locations:
        begin_at = loc.get("begin_at")
        if not begin_at:
            continue
        end_at = loc.get("end_at")

        start_utc = _parse_iso_dt(begin_at)
        end_utc = _parse_iso_dt(end_at) if end_at else datetime.datetime.now(datetime.timezone.utc)

        # Clamp to month range in local time (convert clamp bounds to UTC).
        clamp_start_utc = month_start_local.astimezone(datetime.timezone.utc)
        clamp_end_utc = month_end_local.astimezone(datetime.timezone.utc)
        if end_utc <= clamp_start_utc or start_utc >= clamp_end_utc:
            continue
        if start_utc < clamp_start_utc:
            start_utc = clamp_start_utc
        if end_utc > clamp_end_utc:
            end_utc = clamp_end_utc

        _add_duration_per_day(per_day_seconds, start_utc, end_utc, tz)

    # Build day list from month start to today (local).
    days: List[Dict[str, str]] = []
    total_seconds = 0
    cursor = month_start_local.date()
    end_date = now_local.date()
    while cursor <= end_date:
        key = cursor.isoformat()
        sec = per_day_seconds.get(key, 0)
        total_seconds += sec
        days.append({"date": key, "hms": _to_hms(sec)})
        cursor = cursor + datetime.timedelta(days=1)

    percent = round((total_seconds / (MAX_HOUR * 3600)) * 100, 2) if MAX_HOUR > 0 else 0.0
    return {
        "year": now_local.year,
        "month": now_local.month,
        "alltime_hms": _to_hms(total_seconds),
        "percent": percent,
        "max_hour": MAX_HOUR,
        "days": days,
		"location": location,
		"state": state,
        "username": username
    }

def _ensure_user(req: Request) -> Optional[Dict[str, Any]]:
    token = req.cookies.get("access_token")
    if not token:
        return None

    me = users.get(token)
    if me:
        return me

    try:
        r = requests.get(
            "https://api.intra.42.fr/v2/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if not r.ok:
            return None
        me = r.json()
        users[me["login"]] = me
        users[me["id"]] = me
        users[token] = me
        return me
    except Exception:
        return None


@app.get("/")
def root():
    return RedirectResponse("/time")


@app.get("/callback")
def callback(code: str = Query(...)):
    cfg = _get_config()
    token_res = requests.post(
        "https://api.intra.42.fr/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "code": code,
            "redirect_uri": cfg["redirect_uri"],
        },
        timeout=10,
    )
    token_res.raise_for_status()
    access_token = token_res.json()["access_token"]

    me_res = requests.get(
        "https://api.intra.42.fr/v2/me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    me_res.raise_for_status()
    me = me_res.json()
    users[me["login"]] = me
    users[me["id"]] = me
    users[access_token] = me
    respose = RedirectResponse("/time")
    respose.set_cookie("access_token",access_token,max_age=3600)

    return respose


@app.get("/time")
def get_time(req: Request):
    # Serve the React SPA HTML (auth gate here).
    if _ensure_user(req) is None:
        return RedirectResponse(_authorize_url())

    with open("html.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/api/time")
def api_time(req: Request):
    """
    Returns month logtime data as JSON for the React UI.
    - Requires a valid 'access_token' cookie (set by /callback)
    - Uses a client_credentials token to call intra API endpoints
    """
    global api_token
    me = _ensure_user(req)
    if me is None:
        return JSONResponse(status_code=401, content={"authorize_url": _authorize_url()})

    username = me["login"]
    userid = me["id"]
    location = me.get("location","")
    state = me.get("state", {})

    if api_token is None:
        api_token = get_api_token()

    now_local = datetime.datetime.now(ZoneInfo("Asia/Seoul"))
    begin_local = datetime.datetime(
        now_local.year, now_local.month, now_local.day, tzinfo=ZoneInfo("Asia/Seoul")
    )
    begin_utc = _iso_utc(begin_local)
    end_utc = _iso_utc(now_local)
    month_start_local, _ = _month_range_local(now_local)
    month_begin_utc = _iso_utc(month_start_local)

    def _do_fetch() -> List[Dict[str, Any]]:
        return _fetch_locations_in_range(userid, api_token, month_begin_utc, end_utc)

    try:
        locations = _do_fetch()
    except RuntimeError as e:
        err_msg = str(e)
        # 401 / token expired -> 토큰 갱신 후 1회 재시도
        if "401" in err_msg or "expired" in err_msg.lower():
            try:
                _refresh_api_token()
                locations = _do_fetch()
            except Exception:
                # 재시도까지 실패 -> 로그인 만료로 간주하고 다시 로그인 유도
                resp = JSONResponse(
                    status_code=401,
                    content={"authorize_url": _authorize_url()},
                )
                resp.delete_cookie("access_token")
                return resp
        else:
            return JSONResponse(
                status_code=500,
                content={"error": "failed to build month payload", "detail": err_msg},
            )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": "failed to build month payload", "detail": str(e)},
        )

    return JSONResponse(content=_build_month_payload_from_locations(locations, now_local, location, state, username))
@app.post("/api/state")
def set_user_state(
    payload: Dict[str, Any] = Body(...)
):
    """
    payload 예시:
    {
        "username": "USERNAME",
        "monitor_state": "",
        "last_monitor_off_time": 0,
        "last_monitor_on_time": 0,
        "is_locked_screen": "",
        "last_screenlock_time": 0,
        "last_screenunlock_time": 0
    }
    """

    username = payload.get("username")
    if not username:
        return JSONResponse(status_code=400, content={"error": "username required"})

    me = users.get(username)
    if not me:
        return JSONResponse(status_code=404, content={"error": "user not found"})

    # users에 상태 저장 (덮어쓰기 or 생성)
    me["state"] = {
        "monitor_state": payload.get("monitor_state"),
        "last_monitor_off_time": payload.get("last_monitor_off_time"),
        "last_monitor_on_time": payload.get("last_monitor_on_time"),
        "is_locked_screen": payload.get("is_locked_screen"),
        "last_screenlock_time": payload.get("last_screenlock_time"),
        "last_screenunlock_time": payload.get("last_screenunlock_time"),
        "updated_at": datetime.datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
    }

    return JSONResponse(
        status_code=200,
        content={"ok": True},
    )
