"""gcal_api.py — a thin Google Calendar API v3 layer for clife.

Why this exists: `gcalcli` owns every calendar op in `week.py` / `pool.py`, but
gcalcli *cannot edit an event by id* — `gcalcli edit` is a search + interactive
prompt flow. Surface's month grid knows each event's id and needs to write to it
directly. So `cl events` goes to the API.

No new auth: this unpickles the credentials gcalcli already wrote to
`paths.GCAL_OAUTH`. That pickle is a `google.oauth2.credentials.Credentials`
carrying its own refresh_token + client_id/secret, so it is self-sufficient —
`gcalclirc` is never parsed. Its scope is `.../auth/calendar` (full read/write).

Imports only `paths` (which imports nothing from clife), so this is cycle-free.
Surface must NOT import this — thin-skin rule; it shells out to `cl events --json`.

  credentials()                 unpickle → refresh if stale → Credentials
  service()                     memoized discovery client
  calendar_map()                name → {id, timezone, access_role, primary}
  resolve_calendar(name)        name → (id, tz), with a writability guard
  find_event(id, calendar=None) → (cal_name, cal_id, resource)
  event_dict(res, cal_name)     → the same shape `cl agenda --month` emits
"""

import json
import os
import pickle
import time
from pathlib import Path

import paths

CACHE_TTL = 24 * 3600          # calendar list is ~static; keep name resolution offline
_CACHE = paths.DATA_DIR / "gcal-calendars.json"

_service = None
_cal_map = None


class GcalError(Exception):
    """Carries a machine-readable `code` alongside the human message, so
    `cl events --json` can emit {"ok": false, "error": …, "code": …}."""

    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


# ── credentials ──────────────────────────────────────────────────────────────

def credentials():
    """Unpickle gcalcli's token, refreshing it in place if expired."""
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
    except ImportError:
        raise GcalError("no-deps", "googleapiclient/google-auth missing from this venv")

    p = paths.GCAL_OAUTH
    if not p.exists():
        raise GcalError("no-creds",
                        f"no gcalcli credentials at {p} — run `gcalcli list` once to authorize")
    try:
        with open(p, "rb") as fh:
            creds = pickle.load(fh)
    except Exception:
        raise GcalError("bad-creds", f"{p} is not a gcalcli credentials pickle")
    if not hasattr(creds, "valid") or not hasattr(creds, "refresh_token"):
        raise GcalError("bad-creds", f"{p} is not a gcalcli credentials pickle")

    if not creds.valid:
        if not creds.refresh_token:
            raise GcalError("refresh-failed",
                            "credentials have no refresh token — re-run `gcalcli list`")
        try:
            creds.refresh(Request())
        except RefreshError:
            raise GcalError("refresh-failed",
                            "credentials rejected (revoked or expired) — re-run `gcalcli list`")
        _write_back(creds)
    return creds


def _write_back(creds):
    """Re-pickle the refreshed token so gcalcli sees it too and we don't pay a
    ~200ms refresh on every call. Same class round-tripped → gcalcli-compatible.
    Atomic (tmp + os.replace) so a concurrent gcalcli read can't see a torn file.
    Best-effort: a failure here must never break the command."""
    try:
        p = paths.GCAL_OAUTH
        tmp = p.with_suffix(p.suffix + f".clife-{os.getpid()}.tmp")
        with open(tmp, "wb") as fh:
            pickle.dump(creds, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def service():
    """Memoized Calendar v3 client. Discovery build costs ~0.3s — one-shot CRUD
    only; never call this from a hot list path."""
    global _service
    if _service is None:
        try:
            from googleapiclient.discovery import build
        except ImportError:
            raise GcalError("no-deps", "googleapiclient missing from this venv")
        _service = build("calendar", "v3", credentials=credentials(), cache_discovery=False)
    return _service


# ── error mapping ────────────────────────────────────────────────────────────

def _wrap(fn, *, missing="not-found"):
    """Run an API call, mapping HttpError → GcalError."""
    try:
        from googleapiclient.errors import HttpError
    except ImportError:
        raise GcalError("no-deps", "googleapiclient missing from this venv")
    try:
        return fn()
    except HttpError as e:
        status = getattr(getattr(e, "resp", None), "status", None)
        detail = _http_reason(e)
        if status == 404:
            raise GcalError(missing, detail or "not found")
        if status == 403:
            raise GcalError("forbidden", detail or "forbidden — no write access")
        if status == 401:
            raise GcalError("auth", detail or "authentication rejected — re-run `gcalcli list`")
        raise GcalError("api-error", detail or f"calendar API error (HTTP {status})")


def _http_reason(e):
    try:
        body = json.loads(e.content.decode())
        return body["error"]["message"]
    except Exception:
        return None


# ── calendars ────────────────────────────────────────────────────────────────

def _read_cache():
    try:
        blob = json.loads(_CACHE.read_text())
        if time.time() - blob.get("fetched_at", 0) < CACHE_TTL:
            return blob["calendars"]
    except Exception:
        pass
    return None


def _write_cache(cals):
    try:
        paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE.write_text(json.dumps({"fetched_at": time.time(), "calendars": cals}))
    except Exception:
        pass


def calendar_map(force=False):
    """name → {id, timezone, access_role, primary}. Summaries are unique on this
    account (verified), so the name→id join is safe. Disk-cached 24h."""
    global _cal_map
    if _cal_map is not None and not force:
        return _cal_map
    if not force:
        cached = _read_cache()
        if cached is not None:
            _cal_map = cached
            return _cal_map
    items = _wrap(lambda: service().calendarList().list().execute()).get("items", [])
    _cal_map = {
        it["summary"]: {
            "id": it["id"],
            "timezone": it.get("timeZone", "UTC"),
            "access_role": it.get("accessRole", "reader"),
            "primary": bool(it.get("primary")),
        }
        for it in items
    }
    _write_cache(_cal_map)
    return _cal_map


WRITABLE = ("owner", "writer")


def resolve_calendar(name, *, writable=False):
    """name → (calendar_id, timezone). Miss → refresh the cache once (a newly
    created calendar shouldn't need a manual cache bust) before giving up."""
    cals = calendar_map()
    if name not in cals:
        cals = calendar_map(force=True)
    if name not in cals:
        known = ", ".join(sorted(cals)) or "(none)"
        raise GcalError("unknown-calendar", f"no calendar named '{name}' — known: {known}")
    c = cals[name]
    if writable and c["access_role"] not in WRITABLE:
        raise GcalError("read-only",
                        f"calendar '{name}' is read-only (access: {c['access_role']})")
    return c["id"], c["timezone"]


def writable_calendars():
    """Calendar names you can write to (owner/writer). Deliberately NOT filtered
    by week.EXCLUDE_CALENDARS — that set drops {Sydney, sydneyslavitt@} from
    *block placement*, but 'Sydney' is owner-writable and a legitimate edit
    target for a month-grid click."""
    return [n for n, c in calendar_map().items() if c["access_role"] in WRITABLE]


# ── events ───────────────────────────────────────────────────────────────────

def get_event(cal_name, eid):
    cal_id, _ = resolve_calendar(cal_name)
    return _wrap(lambda: service().events().get(calendarId=cal_id, eventId=eid).execute())


def find_event(eid, calendar=None):
    """→ (cal_name, cal_id, resource).

    `calendar` given → one events.get (the happy path; Surface always has
    seg.calendar). Omitted → scan writable calendars until a hit. A wrong
    calendarId returns a clean 404 (verified), so the scan is unambiguous — but
    it costs up to one call per calendar, so it's a fallback, not the norm."""
    if calendar:
        cal_id, _ = resolve_calendar(calendar)
        res = _wrap(lambda: service().events().get(calendarId=cal_id, eventId=eid).execute(),
                    missing="not-found")
        return calendar, cal_id, res
    for name in writable_calendars():
        cal_id = calendar_map()[name]["id"]
        try:
            res = _wrap(lambda: service().events().get(calendarId=cal_id, eventId=eid).execute())
            return name, cal_id, res
        except GcalError as e:
            if e.code == "not-found":
                continue
            raise
    raise GcalError("not-found", f"no event {eid} on any writable calendar")


def exclusive_to_inclusive(end_date, start_date):
    """gcal all-day end dates are exclusive; every user-facing surface in clife
    shows them inclusive. Mirrors agenda.py's --month branch."""
    from datetime import date as _date, timedelta
    try:
        e = _date.fromisoformat(end_date) - timedelta(days=1)
    except (ValueError, TypeError):
        return start_date
    s = _date.fromisoformat(start_date)
    return (s if e < s else e).isoformat()


def event_dict(res, cal_name):
    """Normalize an API event resource to the shape `cl agenda --month` emits,
    plus recurrence + status. all-day end_date is exclusive→INCLUSIVE here."""
    start, end = res.get("start", {}), res.get("end", {})
    all_day = "date" in start
    if all_day:
        start_date = start.get("date")
        end_date = exclusive_to_inclusive(end.get("date"), start_date)
        start_time = end_time = None
    else:
        sdt, edt = start.get("dateTime", ""), end.get("dateTime", "")
        start_date, start_time = _split_dt(sdt)
        end_date, end_time = _split_dt(edt)
    return {
        "id": res.get("id"),
        "calendar": cal_name,
        "title": res.get("summary", ""),
        "start_date": start_date,
        "end_date": end_date,
        "start_time": start_time,
        "end_time": end_time,
        "all_day": all_day,
        "recurring": bool(res.get("recurringEventId")),
        "recurring_event_id": res.get("recurringEventId"),
        "status": res.get("status"),
        "html_link": res.get("htmlLink"),
    }


def _split_dt(s):
    """'2026-07-15T13:00:00-04:00' → ('2026-07-15', '13:00'). Local wall time as
    the API returns it — no tz math, matching gcalcli --tsv's columns."""
    if not s or "T" not in s:
        return s or None, None
    d, t = s.split("T", 1)
    return d, t[:5]
