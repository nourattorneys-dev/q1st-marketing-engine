# routers/mkt_clients.py - EXT-3 (2026-10-20): Marketing clients (outside sites on the ext_marketing service). Admin only.
# The pages hold no data of their own: every read and write goes to the service's /admin/v1 API with the admin key,
# naming the signed-in staff user (X-Acting-User) so the service's audit log shows who did it.
import os

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..auth import can, get_current_user
from ..flash import set_flash
from ..shell import STR, resolve_lang
from ..templating import templates

router = APIRouter()
API = os.environ.get("EXT_MKT_URL", "http://127.0.0.1:8140").rstrip("/")
BASE = "/marketing-clients"


def _gate(request):
    user = get_current_user(request)
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    if not can(user):
        return None, HTMLResponse("Access denied: admin only", status_code=403)
    return user, None


async def _api(user, method, path, json=None):
    """(data, error) -- error is the service's {code, message, fields} or a 'down' error."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.request(method, API + path, json=json, headers={
                "X-Admin-Key": os.environ.get("EXT_MKT_ADMIN_KEY", ""), "X-Acting-User": str(user.get("username") or user.get("id"))})
        d = r.json()
    except Exception:                                                   # noqa: BLE001
        return None, {"code": "down", "message": ""}
    return (_dates(d.get("data")), None) if d.get("ok") else (None, d.get("error") or {"code": "error", "message": "HTTP %s" % r.status_code})


_DATES = {"at", "start", "end"}                                         # plus every key ending in "At" (lastDeliveryAt, sentAt, ...)


def _dates(o):
    """ISO strings from the service -> datetime, so the portal's when() shows them like every other page."""
    from datetime import datetime
    if isinstance(o, list):
        return [_dates(x) for x in o]
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            if (k in _DATES or k.endswith("At")) and isinstance(v, str):
                try:
                    v = datetime.fromisoformat(v)
                except ValueError:
                    pass
            out[k] = _dates(v)
        return out
    return o


def _T(request, user):
    return STR[resolve_lang(request, user)]


def _msg(T, err):
    if err.get("code") == "down":
        return T["mc_err_down"]
    f = err.get("fields") or {}
    return (err.get("message") or "") + ((" — " + "; ".join(f"{k}: {v}" for k, v in f.items())) if f else "")


def _back(to, T, err=None, ok_key="mc_saved", data=None):
    """data: the service's answer; its dashboardError (the client dashboard didn't follow a change) shows as a warning."""
    r = RedirectResponse(to, status_code=303)
    follow = (data or {}).get("dashboardError") if isinstance(data, dict) else None
    if err:
        set_flash(r, _msg(T, err), "error")
    elif follow:
        set_flash(r, T["mc_dash_follow_bad"].replace("{reason}", follow), "error")
    else:
        set_flash(r, T[ok_key], "success")
    return r


def _num(v, kind=float):
    v = (v or "").strip()
    if v == "":
        return None
    try:
        return kind(v)
    except ValueError:
        return v                                                        # the service refuses it with a field message


async def _form(request):
    return await request.form()


# ------------------------------------------------------------------------------ clients
@router.get("/", response_class=HTMLResponse)
async def clients(request: Request):
    user, deny = _gate(request)
    if deny:
        return deny
    rows, err = await _api(user, "GET", "/admin/v1/accounts")
    plans, _ = await _api(user, "GET", "/admin/v1/plans")
    return templates.TemplateResponse(request, "mktc_list.html", {"user": user, "rows": rows or [], "plans": plans or [],
                                                                  "down": bool(err)})


@router.post("/new")
async def new_client(request: Request):
    user, deny = _gate(request)
    if deny:
        return deny
    f = await _form(request); T = _T(request, user)
    d, err = await _api(user, "POST", "/admin/v1/accounts", {"name": f.get("name"), "planId": f.get("plan_id") or None,
                                                            "priceOverride": _num(f.get("price_override")), "notes": f.get("notes") or None})
    return _back(f"{BASE}/{d['id']}" if d else BASE + "/", T, err, "mc_created_ok")


# ------------------------------------------------------------------------------ plans
async def _metrics(user, request=None):
    """[{key, label, unit}] from the admin API, labelled in the page's language (falls back to English)."""
    m, _ = await _api(user, "GET", "/admin/v1/metrics")
    ar = request is not None and resolve_lang(request, user) == "ar"
    return [{**x, "label": (x.get("labelAr") if ar else None) or x["label"]} for x in m or []]


async def _categories(request, user):
    """{value: name} for site categories in the page's language (falls back to the value)."""
    c, _ = await _api(user, "GET", "/admin/v1/categories")
    lang = resolve_lang(request, user)
    return {x["value"]: (x.get("names") or {}).get(lang) or x["value"] for x in c or []}


def _quotas_from_form(f, metrics):
    out = []
    for m in metrics:
        k = m["key"]
        if f.get(f"q_{k}_on"):
            out.append({"metric": k, "limit": _num(f.get(f"q_{k}_limit"), int), "mode": f.get(f"q_{k}_mode") or "hard",
                        "warnAt": _num(f.get(f"q_{k}_warn"))})
    return out


@router.get("/plans", response_class=HTMLResponse)
async def plans(request: Request):
    user, deny = _gate(request)
    if deny:
        return deny
    rows, err = await _api(user, "GET", "/admin/v1/plans")
    return templates.TemplateResponse(request, "mktc_plans.html", {"user": user, "rows": rows or [], "metrics": await _metrics(user, request),
                                                                   "down": bool(err)})


@router.post("/plans/new")
async def new_plan(request: Request):
    user, deny = _gate(request)
    if deny:
        return deny
    f = await _form(request); T = _T(request, user)
    _, err = await _api(user, "POST", "/admin/v1/plans", {
        "name": f.get("name"), "price": _num(f.get("price")), "currency": f.get("currency") or None,
        "queuePriority": _num(f.get("queue_priority"), int) or 0, "quotas": _quotas_from_form(f, await _metrics(user))})
    return _back(BASE + "/plans", T, err, "mc_plan_created")


@router.post("/plans/{pid}/update")
async def update_plan(pid: str, request: Request):
    user, deny = _gate(request)
    if deny:
        return deny
    f = await _form(request); T = _T(request, user)
    _, err = await _api(user, "PUT", f"/admin/v1/plans/{pid}", {
        "version": _num(f.get("version"), int), "name": f.get("name"), "price": _num(f.get("price")), "currency": f.get("currency") or None,
        "queuePriority": _num(f.get("queue_priority"), int), "active": bool(f.get("active")),
        "quotas": _quotas_from_form(f, await _metrics(user))})
    return _back(BASE + "/plans", T, err)


# ------------------------------------------------------------------------------ usage & revenue
@router.get("/revenue", response_class=HTMLResponse)
async def revenue(request: Request, month: str = ""):
    user, deny = _gate(request)
    if deny:
        return deny
    import re as _re
    q = f"?month={month}" if _re.fullmatch(r"\d{4}-\d{2}", month or "") else ""
    d, err = await _api(user, "GET", "/admin/v1/revenue" + q)
    ups, _ = await _api(user, "GET", "/admin/v1/upgrade-requests?status=open")
    labels = {m["key"]: m["label"] for m in await _metrics(user, request)}
    return templates.TemplateResponse(request, "mktc_revenue.html", {"user": user, "d": d or {}, "ups": ups or [], "labels": labels,
                                                                     "down": bool(err) and (err or {}).get("code") == "down", "err": err})


# ------------------------------------------------------------------------------ settings
@router.get("/settings", response_class=HTMLResponse)
async def settings(request: Request):
    user, deny = _gate(request)
    if deny:
        return deny
    rows, err = await _api(user, "GET", "/admin/v1/settings")
    import json as _j
    for r in rows or []:
        r["text"] = "" if r["value"] is None else _j.dumps(r["value"], ensure_ascii=False)
    return templates.TemplateResponse(request, "mktc_settings.html", {"user": user, "rows": rows or [], "down": bool(err)})


@router.post("/settings")
async def save_settings(request: Request):
    user, deny = _gate(request)
    if deny:
        return deny
    f = await _form(request); T = _T(request, user)
    body = {k[2:]: (f[k].strip() or "null") for k in f.keys() if k.startswith("s_")}
    d, err = await _api(user, "PUT", "/admin/v1/settings", body)
    return _back(BASE + "/settings", T, err, "mc_saved" if (err or (d or {}).get("changed")) else "mc_nothing_changed")


# ------------------------------------------------------------------------------ one client
async def _detail(request, user, aid, new_key=None):
    d, err = await _api(user, "GET", f"/admin/v1/accounts/{aid}")
    if err and err.get("code") == "not_found":
        return HTMLResponse("Not found", status_code=404)
    plans, _ = await _api(user, "GET", "/admin/v1/plans")
    meta, _ = await _api(user, "GET", "/api/v1/meta")
    return templates.TemplateResponse(request, "mktc_detail.html", {
        "user": user, "d": d or {}, "plans": plans or [], "meta": meta or {}, "metrics": await _metrics(user, request), "cats": await _categories(request, user), "new_key": new_key,
        "down": bool(err)})


@router.get("/{aid}", response_class=HTMLResponse)
async def client(aid: str, request: Request, reveal: str = ""):
    """?reveal=<key id> shows a just-issued key ONCE (the service deletes its copy as it hands it over); a refresh shows nothing."""
    user, deny = _gate(request)
    if deny:
        return deny
    new_key = None
    import re as _re
    if reveal and _re.fullmatch(r"key_[0-9a-f]{16}", reveal):
        new_key, _ = await _api(user, "POST", f"/admin/v1/accounts/{aid}/api-keys/{reveal}/reveal")
    resp = await _detail(request, user, aid, new_key=new_key)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/{aid}/update")
async def update_client(aid: str, request: Request):
    user, deny = _gate(request)
    if deny:
        return deny
    f = await _form(request); T = _T(request, user)
    d, err = await _api(user, "PUT", f"/admin/v1/accounts/{aid}", {
        "version": _num(f.get("version"), int), "name": f.get("name"), "planId": f.get("plan_id") or None,
        "status": f.get("status"), "priceOverride": _num(f.get("price_override")), "notes": f.get("notes"),
        "brandColor": f.get("brand_color"), "brandColorDark": f.get("brand_color_dark"), "brandLogoUrl": f.get("brand_logo"),
        "dashboardName": f.get("dashboard_name")})
    return _back(f"{BASE}/{aid}", T, err, data=d)


@router.post("/{aid}/delete")
async def delete_client(aid: str, request: Request):
    """P1c: only a closed client can be deleted; the service refuses any other with its own message."""
    user, deny = _gate(request)
    if deny:
        return deny
    T = _T(request, user)
    _, err = await _api(user, "POST", f"/admin/v1/accounts/{aid}/delete")
    return _back(f"{BASE}/{aid}" if err else BASE + "/", T, err, "mc_client_deleted")


@router.post("/{aid}/sites/new")
async def new_site(aid: str, request: Request):
    user, deny = _gate(request)
    if deny:
        return deny
    f = await _form(request); T = _T(request, user)
    _, err = await _api(user, "POST", f"/admin/v1/accounts/{aid}/sites", {
        "name": f.get("name"), "url": f.get("url"), "category": f.get("category"), "timezone": f.get("timezone"),
        "languages": f.getlist("languages"), "defaultLanguage": f.get("default_language")})
    return _back(f"{BASE}/{aid}", T, err, "mc_site_added")


@router.post("/{aid}/sites/{sid}/delete")
async def delete_site(aid: str, sid: str, request: Request):
    user, deny = _gate(request)
    if deny:
        return deny
    T = _T(request, user)
    d, err = await _api(user, "DELETE", f"/admin/v1/accounts/{aid}/sites/{sid}")
    return _back(f"{BASE}/{aid}", T, err, "mc_site_deleted", data=d)


@router.post("/{aid}/sites/{sid}/rename")
async def rename_site(aid: str, sid: str, request: Request):
    """A site's name, as its people see it on the client dashboard too."""
    user, deny = _gate(request)
    if deny:
        return deny
    f = await _form(request); T = _T(request, user)
    d, err = await _api(user, "PUT", f"/admin/v1/accounts/{aid}/sites/{sid}", {"name": (f.get("name") or "").strip()})
    return _back(f"{BASE}/{aid}", T, err, "mc_saved", data=d)


@router.post("/{aid}/sites/{sid}/dashboard")
async def site_dashboard(aid: str, sid: str, request: Request):
    """P1c: puts the site on the client dashboard (new key, kept by the dashboard only) and invites the client's first Admin."""
    user, deny = _gate(request)
    if deny:
        return deny
    f = await _form(request); T = _T(request, user)
    d, err = await _api(user, "POST", f"/admin/v1/accounts/{aid}/sites/{sid}/dashboard", {"adminEmail": (f.get("admin_email") or "").strip() or None})
    adm = (d or {}).get("admin")
    return _back(f"{BASE}/{aid}", T, err, {"invited": "mc_dash_ok_invited", "reinvited": "mc_dash_ok_reinvited"}.get(adm, "mc_dash_ok"))


@router.post("/{aid}/sites/{sid}/webhook-test")
async def webhook_test(aid: str, sid: str, request: Request):
    """Sends one signed test event to the site's webhook; the message says whether it arrived."""
    user, deny = _gate(request)
    if deny:
        return deny
    T = _T(request, user)
    d, err = await _api(user, "POST", f"/admin/v1/sites/{sid}/webhook/test")
    last = ((d or {}).get("recent") or [{}])[0]
    r = RedirectResponse(f"{BASE}/{aid}#webhooks", status_code=303)
    if err:
        set_flash(r, _msg(T, err), "error")
    elif last.get("status") == "sent":
        set_flash(r, T["mc_wh_test_ok"], "success")
    else:
        set_flash(r, T["mc_wh_test_bad"].replace("{reason}", last.get("error") or T["mc_wh_waiting_h"]), "error")
    return r


@router.post("/{aid}/keys/new")
async def new_key(aid: str, request: Request):
    """Issue, then REDIRECT (POST -> 303 -> GET): a refresh can never issue a second key."""
    user, deny = _gate(request)
    if deny:
        return deny
    f = await _form(request); T = _T(request, user)
    d, err = await _api(user, "POST", f"/admin/v1/accounts/{aid}/api-keys", {"siteId": f.get("site_id"), "label": f.get("label") or None})
    if err:
        return _back(f"{BASE}/{aid}", T, err)
    return RedirectResponse(f"{BASE}/{aid}?reveal={d['id']}", status_code=303)


@router.post("/{aid}/keys/{kid}/revoke")
async def revoke_key(aid: str, kid: str, request: Request):
    user, deny = _gate(request)
    if deny:
        return deny
    T = _T(request, user)
    _, err = await _api(user, "DELETE", f"/admin/v1/accounts/{aid}/api-keys/{kid}")
    return _back(f"{BASE}/{aid}", T, err, "mc_key_revoked_ok")


@router.post("/{aid}/keys/{kid}/delete")
async def delete_key(aid: str, kid: str, request: Request):
    user, deny = _gate(request)
    if deny:
        return deny
    T = _T(request, user)
    _, err = await _api(user, "POST", f"/admin/v1/accounts/{aid}/api-keys/{kid}/delete")
    return _back(f"{BASE}/{aid}", T, err, "mc_key_deleted_ok")


@router.post("/{aid}/overrides")
async def set_override(aid: str, request: Request):
    user, deny = _gate(request)
    if deny:
        return deny
    f = await _form(request); T = _T(request, user)
    _, err = await _api(user, "PUT", f"/admin/v1/accounts/{aid}/quota-overrides", {
        "metric": f.get("metric"), "siteId": f.get("site_id") or None, "limit": _num(f.get("limit"), int),
        "mode": f.get("mode") or "hard", "warnAt": _num(f.get("warn_at")), "reason": f.get("reason") or None})
    return _back(f"{BASE}/{aid}", T, err, "mc_override_ok")


@router.post("/{aid}/overrides/{oid}/delete")
async def remove_override(aid: str, oid: int, request: Request):
    user, deny = _gate(request)
    if deny:
        return deny
    T = _T(request, user)
    _, err = await _api(user, "DELETE", f"/admin/v1/accounts/{aid}/quota-overrides/{oid}")
    return _back(f"{BASE}/{aid}", T, err, "mc_override_removed")


@router.post("/{aid}/requests/{uid}")
async def handle_request(aid: str, uid: str, request: Request):
    """An upgrade request from the client's app: mark it done (after changing the plan or a limit) or declined."""
    user, deny = _gate(request)
    if deny:
        return deny
    f = await _form(request); T = _T(request, user)
    _, err = await _api(user, "POST", f"/admin/v1/upgrade-requests/{uid}", {"status": f.get("status")})
    nxt = f.get("next") or ""
    return _back(nxt if nxt.startswith(BASE + "/") and "//" not in nxt else f"{BASE}/{aid}", T, err, "mc_request_saved")   # only back into this section
