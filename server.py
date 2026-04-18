from __future__ import annotations

import datetime
import os
import sqlite3
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".vendor"))

from korean_lunar_calendar import KoreanLunarCalendar
import requests
from fastapi import FastAPI, Query, Body

from fastapi.requests import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from zoneinfo import ZoneInfo
app = FastAPI()
users = {}
api_token: Optional[str] = None
MAX_HOUR = 80
MAX_DAILY_SECONDS = 12 * 60 * 60  # 12시간 = 43200초
PLAN_DB_PATH = os.path.join(os.path.dirname(__file__), "plans.sqlite3")
plan_db_lock = threading.Lock()


def _get_plan_db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(PLAN_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_plan_db() -> None:
    with plan_db_lock:
        conn = _get_plan_db_conn()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS study_plans (
                    username TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    month INTEGER NOT NULL,
                    day INTEGER NOT NULL,
                    goal_hms TEXT NOT NULL,
                    PRIMARY KEY (username, year, month, day)
                )
                """
            )
            conn.commit()
        finally:
            conn.close()


def _load_month_plan(username: str, year: int, month: int) -> Dict[str, str]:
    _init_plan_db()
    conn = _get_plan_db_conn()
    try:
        rows = conn.execute(
            """
            SELECT day, goal_hms
            FROM study_plans
            WHERE username = ? AND year = ? AND month = ?
            ORDER BY day ASC
            """,
            (username, year, month),
        ).fetchall()
        return {str(int(row["day"])): str(row["goal_hms"]) for row in rows}
    finally:
        conn.close()


def _save_plan_value(username: str, year: int, month: int, day: int, goal_hms: str) -> Dict[str, str]:
    _init_plan_db()
    with plan_db_lock:
        conn = _get_plan_db_conn()
        try:
            if not goal_hms:
                conn.execute(
                    """
                    DELETE FROM study_plans
                    WHERE username = ? AND year = ? AND month = ? AND day = ?
                    """,
                    (username, year, month, day),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO study_plans (username, year, month, day, goal_hms)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(username, year, month, day)
                    DO UPDATE SET goal_hms = excluded.goal_hms
                    """,
                    (username, year, month, day, goal_hms),
                )
            conn.commit()
        finally:
            conn.close()
    return _load_month_plan(username, year, month)


def _solar_from_lunar(year: int, month: int, day: int, is_intercalation: bool = False) -> datetime.date:
    calendar = KoreanLunarCalendar()
    calendar.setLunarDate(year, month, day, is_intercalation)
    return datetime.date.fromisoformat(calendar.SolarIsoFormat())


def _build_kr_public_holidays(year: int) -> List[str]:
    if year < 1000 or year > 2050:
        raise ValueError("holiday calculation is supported for years 1000 through 2050")

    holiday_categories: Dict[datetime.date, set[str]] = {}

    def add_holiday(day: datetime.date, category: str) -> None:
        holiday_categories.setdefault(day, set()).add(category)

    fixed_holidays = [
        (datetime.date(year, 1, 1), "newyear"),
        (datetime.date(year, 3, 1), "national"),
        (datetime.date(year, 5, 5), "children"),
        (datetime.date(year, 6, 6), "memorial"),
        (datetime.date(year, 8, 15), "national"),
        (datetime.date(year, 10, 3), "national"),
        (datetime.date(year, 10, 9), "national"),
        (datetime.date(year, 12, 25), "christmas"),
    ]
    for day, category in fixed_holidays:
        add_holiday(day, category)

    for lunar_month, lunar_day, category in [
        (1, 1, "seollal"),
        (4, 8, "buddha"),
        (8, 15, "chuseok"),
    ]:
        solar_day = _solar_from_lunar(year, lunar_month, lunar_day)
        if category in {"seollal", "chuseok"}:
            add_holiday(solar_day - datetime.timedelta(days=1), category)
            add_holiday(solar_day, category)
            add_holiday(solar_day + datetime.timedelta(days=1), category)
        else:
            add_holiday(solar_day, category)

    substitute_eligible = {"national", "seollal", "buddha", "children", "chuseok", "christmas"}
    saturday_sunday_eligible = {"national", "buddha", "children", "christmas"}
    sunday_only_eligible = {"seollal", "chuseok"}

    substitutes: set[datetime.date] = set()
    occupied = set(holiday_categories.keys())
    for day in sorted(holiday_categories.keys()):
        categories = holiday_categories[day]
        if not categories & substitute_eligible:
            continue

        weekday = day.weekday()
        is_weekend_substitute = (
            (weekday in {5, 6} and bool(categories & saturday_sunday_eligible))
            or (weekday == 6 and bool(categories & sunday_only_eligible))
        )
        is_overlap_substitute = weekday not in {5, 6} and len(categories) >= 2 and bool(categories & substitute_eligible)
        if not is_weekend_substitute and not is_overlap_substitute:
            continue

        candidate = day + datetime.timedelta(days=1)
        while candidate.weekday() in {5, 6} or candidate in occupied or candidate in substitutes:
            candidate += datetime.timedelta(days=1)
        substitutes.add(candidate)

    holidays = occupied | substitutes
    return sorted(day.isoformat() for day in holidays)

def _get_config() -> Dict[str, str]:
    """
    Minimal config loader.
    - Prefer environment variables (safer).
    - Fall back to existing hard-coded values (backward compatible).
    """
    client_id = os.getenv(
        "FT_CLIENT_ID",
        "u-s4t2ud-b4d2744071e3f1d772727401d4e0bf18292fd0a04bb0848c60ea559ae6bea8ad",
    )
    client_secret = os.getenv(
        "FT_CLIENT_SECRET",
        "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"
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


def _month_start_local(year: int, month: int, tz: ZoneInfo) -> datetime.datetime:
    return datetime.datetime(year, month, 1, tzinfo=tz)


def _next_month_start_local(year: int, month: int, tz: ZoneInfo) -> datetime.datetime:
    if month == 12:
        return datetime.datetime(year + 1, 1, 1, tzinfo=tz)
    return datetime.datetime(year, month + 1, 1, tzinfo=tz)


def _resolve_target_year_month(
    now_local: datetime.datetime,
    year: Optional[int],
    month: Optional[int],
) -> Tuple[int, int]:
    target_year = int(year) if year is not None else now_local.year
    target_month = int(month) if month is not None else now_local.month
    if target_month < 1 or target_month > 12:
        raise ValueError("month must be between 1 and 12")
    if target_year < 1000 or target_year > 2050:
        raise ValueError("year must be between 1000 and 2050")
    return target_year, target_month


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

    # 일일 최대 학습시간 적용
    # per_day_seconds[key] = per_day_seconds.get(key, 0) + max(0, sec)
    current = per_day_seconds.get(key, 0)
    added = max(0, sec)
    # per_day_seconds[key] = min(current + added,MAX_DAILY_SECONDS)
    per_day_seconds[key] = current + added


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


def _build_month_payload_from_locations(
    locations: List[Dict[str, Any]],
    now_local: datetime.datetime,
    target_year: int,
    target_month: int,
    location: str,
    state: Dict[str,Any],
    username: str,
) -> Dict[str, Any]:
    tz = ZoneInfo("Asia/Seoul")
    month_start_local = _month_start_local(target_year, target_month, tz)
    next_month_start_local = _next_month_start_local(target_year, target_month, tz)
    holiday_dates = [
        day
        for day in _build_kr_public_holidays(target_year)
        if day.startswith(f"{target_year}-{target_month:02d}-")
    ]
    is_current_month = target_year == now_local.year and target_month == now_local.month
    month_end_local = now_local if is_current_month else next_month_start_local

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

    # Build day list to today for current month, otherwise full selected month.
    days: List[Dict[str, str]] = []
    total_seconds = 0
    cursor = month_start_local.date()
    end_date = now_local.date() if is_current_month else (next_month_start_local - datetime.timedelta(days=1)).date()
    while cursor <= end_date:
        key = cursor.isoformat()
        sec = per_day_seconds.get(key, 0)
        total_seconds += sec
        days.append({"date": key, "hms": _to_hms(sec)})
        cursor = cursor + datetime.timedelta(days=1)

    percent = round((total_seconds / (MAX_HOUR * 3600)) * 100, 2) if MAX_HOUR > 0 else 0.0
    return {
        "year": target_year,
        "month": target_month,
        "holidays": holiday_dates,
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

    with open("search.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())
"""@app.get("/time/search")
def search_time(req: Request):
    if _ensure_user(req) is None:
        return RedirectResponse(_authorize_url())

    with open("search.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())
"""
@app.get("/api/time")
def api_time(req: Request, year: Optional[int] = Query(None), month: Optional[int] = Query(None)):
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
    try:
        target_year, target_month = _resolve_target_year_month(now_local, year, month)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    month_start_local = _month_start_local(target_year, target_month, ZoneInfo("Asia/Seoul"))
    next_month_start_local = _next_month_start_local(target_year, target_month, ZoneInfo("Asia/Seoul"))
    fetch_end_local = min(now_local, next_month_start_local)
    month_begin_utc = _iso_utc(month_start_local)
    end_utc = _iso_utc(fetch_end_local)

    def _do_fetch() -> List[Dict[str, Any]]:
        if fetch_end_local <= month_start_local:
            return []
        return _fetch_locations_in_range(userid, api_token, month_begin_utc, end_utc)

    try:
        locations = _do_fetch()
    except RuntimeError as e:
        err_msg = str(e)
        # 401 / token expired → 토큰 갱신 후 1회 재시도
        if "401" in err_msg or "expired" in err_msg.lower():
            try:
                _refresh_api_token()
                locations = _do_fetch()
            except Exception:
                # 재시도까지 실패 → 로그인 만료로 간주하고 다시 로그인 유도
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

    return JSONResponse(content=_build_month_payload_from_locations(locations, now_local, target_year, target_month, location, state, username))
@app.get("/api/time/search")
def api_time_search(
    req: Request,
    username: Optional[str] = Query(None, alias="username"),
    year: Optional[int] = Query(None),
    month: Optional[int] = Query(None),
):
    """
    유저명(login)으로 해당 유저의 이번 달 학습 시간을 조회합니다.
    GET /api/time/search?username=로그인아이디
    - 로그인 필수 (access_token 쿠키)
    - username 쿼리 파라미터 필수
    """
    global api_token
    me = _ensure_user(req)
    if me is None:
        return JSONResponse(status_code=401, content={"authorize_url": _authorize_url()})

    if not username or not username.strip():
        return JSONResponse(
            status_code=400,
            content={"error": "username query parameter is required"},
        )

    username = username.strip()

    # 대상 유저 정보 조회: users 캐시 먼저, 없으면 42 API
    target = users.get(username)
    if not target:
        if api_token is None:
            api_token = get_api_token()
        try:
            r = requests.get(
                "https://api.intra.42.fr/v2/users",
                headers={"Authorization": f"Bearer {api_token}"},
                params={"filter[login]": username},
                timeout=10,
            )
            if not r.ok:
                if r.status_code == 401:
                    _refresh_api_token()
                    r = requests.get(
                        "https://api.intra.42.fr/v2/users",
                        headers={"Authorization": f"Bearer {api_token}"},
                        params={"filter[login]": username},
                        timeout=10,
                    )
                if not r.ok:
                    return JSONResponse(
                        status_code=r.status_code,
                        content={"error": "failed to fetch user", "detail": r.text[:200]},
                    )
            data = r.json()
            if not data:
                return JSONResponse(
                    status_code=404,
                    content={"error": "user not found", "username": username},
                )
            target = data[0]
            users[target["login"]] = target
            users[target["id"]] = target
        except Exception as e:
            return JSONResponse(
                status_code=500,
                content={"error": "failed to fetch user", "detail": str(e)},
            )

    userid = target["id"]
    location = target.get("location", "")
    state = target.get("state", {})

    if api_token is None:
        api_token = get_api_token()

    now_local = datetime.datetime.now(ZoneInfo("Asia/Seoul"))
    try:
        target_year, target_month = _resolve_target_year_month(now_local, year, month)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    month_start_local = _month_start_local(target_year, target_month, ZoneInfo("Asia/Seoul"))
    next_month_start_local = _next_month_start_local(target_year, target_month, ZoneInfo("Asia/Seoul"))
    fetch_end_local = min(now_local, next_month_start_local)
    month_begin_utc = _iso_utc(month_start_local)
    end_utc = _iso_utc(fetch_end_local)

    def _do_fetch() -> List[Dict[str, Any]]:
        if fetch_end_local <= month_start_local:
            return []
        return _fetch_locations_in_range(userid, api_token, month_begin_utc, end_utc)

    try:
        locations = _do_fetch()
    except RuntimeError as e:
        err_msg = str(e)
        if "401" in err_msg or "expired" in err_msg.lower():
            try:
                _refresh_api_token()
                locations = _do_fetch()
            except Exception:
                return JSONResponse(
                    status_code=500,
                    content={"error": "failed to fetch locations", "detail": str(e)},
                )
        else:
            return JSONResponse(
                status_code=500,
                content={"error": "failed to fetch locations", "detail": err_msg},
            )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": "failed to fetch locations", "detail": str(e)},
        )

    payload = _build_month_payload_from_locations(
        locations, now_local, target_year, target_month, location, state, target["login"]
    )
    return JSONResponse(content=payload)


@app.get("/api/plan")
def api_plan(req: Request, year: Optional[int] = Query(None), month: Optional[int] = Query(None)):
    me = _ensure_user(req)
    if me is None:
        return JSONResponse(status_code=401, content={"authorize_url": _authorize_url()})

    now_local = datetime.datetime.now(ZoneInfo("Asia/Seoul"))
    try:
        target_year, target_month = _resolve_target_year_month(now_local, year, month)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    plan = _load_month_plan(me["login"], target_year, target_month)
    return JSONResponse(content={
        "username": me["login"],
        "year": target_year,
        "month": target_month,
        "plan": plan,
    })


@app.put("/api/plan")
def api_plan_put(
    req: Request,
    payload: Dict[str, Any] = Body(...),
):
    me = _ensure_user(req)
    if me is None:
        return JSONResponse(status_code=401, content={"authorize_url": _authorize_url()})

    now_local = datetime.datetime.now(ZoneInfo("Asia/Seoul"))
    try:
        target_year, target_month = _resolve_target_year_month(
            now_local,
            int(payload.get("year")) if payload.get("year") is not None else None,
            int(payload.get("month")) if payload.get("month") is not None else None,
        )
    except (TypeError, ValueError) as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    try:
        day = int(payload.get("day"))
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"error": "day is required"})

    if day < 1 or day > 31:
        return JSONResponse(status_code=400, content={"error": "day must be between 1 and 31"})

    goal_hms_raw = payload.get("goal_hms")
    if goal_hms_raw is not None and not isinstance(goal_hms_raw, str):
        return JSONResponse(status_code=400, content={"error": "goal_hms must be a string"})
    goal_hms = str(goal_hms_raw or "").strip()

    plan = _save_plan_value(me["login"], target_year, target_month, day, goal_hms)
    return JSONResponse(content={
        "ok": True,
        "username": me["login"],
        "year": target_year,
        "month": target_month,
        "plan": plan,
    })

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

    # users에 state 저장 (덮어쓰기 or 생성)
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
