"""SBYB Subs - standalone owner subscriber dashboard.

A push-fed, read-only port of the subscriber dashboard that used to
live inside the GEX heatmap app. This app is fully independent of it:
separate codebase, separate origin, separate manifest and service
worker. It holds no Stripe key and never calls Stripe, Telegram or
any other API - a VM sync process POSTs full snapshots to
/push/snapshot and every page renders from the local SQLite copy.

Routes:
  GET  /dashboard?key=<SUBS_READ_KEY>            the dashboard (404
                                                 without the key)
  GET  /dashboard/manifest.webmanifest?key=...   installable-app
                                                 manifest (keyed)
  GET  /sw.js                                    service worker
                                                 (icons only)
  GET  /icons/{name}                             the four SBYB icons
  POST /push/snapshot?key=<SUBS_PUSH_KEY>        snapshot ingest
  GET  /healthz                                  plain "ok"

Config comes from the environment only (no file fallbacks):
  SUBS_READ_KEY   key for the dashboard + manifest
  SUBS_PUSH_KEY   key for snapshot pushes
"""
import hmac
import json
import os
import sqlite3
import time

from fastapi import FastAPI
from fastapi.responses import (FileResponse, HTMLResponse,
                               JSONResponse, PlainTextResponse)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "subs.db")

SUBS_READ_KEY = os.environ.get("SUBS_READ_KEY", "")
SUBS_PUSH_KEY = os.environ.get("SUBS_PUSH_KEY", "")

IO_PLAN = "Indicators Only"  # TradingView access only - never Telegram

app = FastAPI()


def _db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    return con


def _subs_db():
    con = _db()
    con.execute("""CREATE TABLE IF NOT EXISTS site_subs(
        email TEXT PRIMARY KEY,
        plan TEXT, status TEXT,
        stripe_customer_id TEXT, stripe_subscription_id TEXT,
        checkout_session_id TEXT, invite_link TEXT,
        current_period_end REAL, created_ts REAL, updated_ts REAL)""")
    cols = {r[1] for r in con.execute("PRAGMA table_info(site_subs)")}
    if "tv_username" not in cols:
        con.execute("ALTER TABLE site_subs ADD COLUMN tv_username TEXT")
    if "welcome_sent_ts" not in cols:
        con.execute(
            "ALTER TABLE site_subs ADD COLUMN welcome_sent_ts REAL")
    if "trial_repeat" not in cols:
        con.execute(
            "ALTER TABLE site_subs ADD COLUMN trial_repeat INTEGER")
    if "canceled_ts" not in cols:
        con.execute("ALTER TABLE site_subs ADD COLUMN canceled_ts REAL")
    if "card_display" not in cols:
        # Snapshot-provided card label (e.g. "Mastercard •••• 3020").
        # This app never talks to Stripe, so without a value in the
        # snapshot the dashboard honestly shows a dash.
        con.execute("ALTER TABLE site_subs ADD COLUMN card_display TEXT")
    # One-trial / one-refund identity registry, derived at push time
    # from the trial_used_ts / refund_used_ts fields the snapshot
    # carries on each subscriber row (the shape GET /stripe/subs on
    # the GEX app returns). The dashboard reads kind='email' rows.
    con.execute("""CREATE TABLE IF NOT EXISTS identity_flags(
        kind TEXT, key TEXT, trial_used_ts REAL, refund_used_ts REAL,
        source TEXT, updated_ts REAL, PRIMARY KEY(kind, key))""")
    con.execute("""CREATE TABLE IF NOT EXISTS tv_claims(
        email TEXT, tv_username TEXT, matched INTEGER, ts REAL)""")
    # Telegram join state per website subscriber, pushed as part of
    # each snapshot (pending = invite sent, active = joined, removed).
    con.execute("""CREATE TABLE IF NOT EXISTS telegram_joins(
        email TEXT PRIMARY KEY, status TEXT, tg_username TEXT,
        joined_at TEXT, updated_ts REAL)""")
    # X subscribers are a SEPARATE plan from website subs: no Stripe,
    # no site_subs row - they live in the VM roster and each snapshot
    # carries the full current list.
    con.execute("""CREATE TABLE IF NOT EXISTS x_subs(
        handle TEXT PRIMARY KEY, status TEXT, tg_username TEXT,
        created_at TEXT, joined_at TEXT, updated_ts REAL)""")
    # TradingView grants are executed by hand (no TV API); the record
    # of who holds what arrives with each snapshot. Without a record
    # for a subscriber's username the dashboard honestly shows the
    # grant as pending.
    con.execute("""CREATE TABLE IF NOT EXISTS tv_grants(
        tv TEXT PRIMARY KEY, indicators TEXT, granted TEXT,
        note TEXT, updated_ts REAL)""")
    # Snapshot bookkeeping: when the last full snapshot landed.
    con.execute("""CREATE TABLE IF NOT EXISTS meta(
        key TEXT PRIMARY KEY, value TEXT)""")
    return con


def _key_ok(key: str) -> bool:
    return bool(SUBS_READ_KEY) and hmac.compare_digest(
        key, SUBS_READ_KEY)


def _push_key_ok(key: str) -> bool:
    return bool(SUBS_PUSH_KEY) and hmac.compare_digest(
        key, SUBS_PUSH_KEY)


def _norm_tv(tv):
    return (tv or "").strip().lstrip("@").lower() or None


def _num(v):
    if isinstance(v, bool):
        return None
    return v if isinstance(v, (int, float)) else None


# ---------- owner dashboard (read-only, keyed) ----------
# Justin's private view of the subscriber registry: who is
# subscribed, what plan, what state, how long is left. Read-only by
# design - cancellations and refunds stay in Stripe / through the
# hub. Same key guard throughout: without the key the page is a
# plain 404 and no subscriber data ever renders.
_PLAN_PRICES = {
    "Monthly": "$20/mo",
    "Quarterly": "$45/qtr",
    "Yearly": "$150/yr",
    "Lifetime": "$500 once",
    IO_PLAN: "$10/mo",
}
_PLAN_AMOUNTS = {
    "Monthly": 20,
    "Quarterly": 45,
    "Yearly": 150,
    "Lifetime": 500,
    IO_PLAN: 10,
}
_PLAN_MONTHLY_EQ = {
    "Monthly": 20,
    "Quarterly": 15,
    "Yearly": 12.5,
    "Lifetime": 0,
    IO_PLAN: 10,
}
_STATUS_LABELS = {
    "trialing": "Trialing",
    "active": "Active",
    "past_due": "Past due",
    "canceled": "Canceled",
    "cancelled": "Canceled",
    "refunded": "Refunded",
    "unpaid": "Unpaid",
    "lifetime": "Lifetime",
}

DASHBOARD_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="theme-color" content="#04090a">
<link rel="manifest" href="/dashboard/manifest.webmanifest?key=__KEY__">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<meta name="apple-mobile-web-app-title" content="SBYB Subs">
<link rel="apple-touch-icon" href="/icons/sbyb-apple-touch-icon.png">
<link rel="icon" type="image/png" sizes="192x192"
href="/icons/sbyb-icon-192.png">
<title>Subscribers - SpyBeforeYouBuy</title>
<style>
*{box-sizing:border-box}
body{background:#04090a;color:#e8ecef;margin:0;
font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;
padding:20px 14px 48px}
.wrap{max-width:880px;margin:0 auto}
.brand{color:#7fb3c8;font-weight:800;letter-spacing:2px;font-size:12px}
h1{font-size:24px;margin:4px 0 2px}
.sub{color:#8fa3ad;font-size:13px;margin-bottom:18px;line-height:1.6}
.strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(104px,1fr));
gap:10px;margin-bottom:14px}
.tile{background:#0a151a;border:1px solid #175263;border-radius:12px;
padding:12px 12px 10px}
.tile .n{font-size:26px;font-weight:800;line-height:1}
.tile .k{color:#8fa3ad;font-size:11px;letter-spacing:1px;margin-top:5px;
text-transform:uppercase}
.tile.warn{border-color:#8a6d1f}
.tile.warn .n{color:#f2c94c}
.card{background:#0a151a;border:1px solid #1d323c;border-radius:12px;
padding:14px 16px;margin-bottom:12px}
.card .top{display:flex;flex-wrap:wrap;gap:8px;align-items:center;
justify-content:space-between}
.email{font-weight:700;font-size:16px;word-break:break-all}
.badge{display:inline-block;padding:3px 10px;border-radius:999px;
font-size:12px;font-weight:700;letter-spacing:.4px;white-space:nowrap}
.b-trialing{background:#175263;color:#dff3fa}
.b-active,.b-lifetime{background:#14532d;color:#bbf7d0}
.b-past_due,.b-unpaid{background:#7c2d12;color:#fed7aa}
.b-canceled,.b-refunded{background:#3f1d1d;color:#fca5a5}
.b-other{background:#263238;color:#cfd8dc}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
gap:8px 18px;margin-top:12px}
.f .k{color:#8fa3ad;font-size:11px;letter-spacing:1px;text-transform:uppercase}
.f .v{font-size:14px;margin-top:2px;word-break:break-word}
.f .v .s{display:block;color:#8fa3ad;font-size:12px;margin-top:1px}
.chip{display:inline-block;background:#175263;color:#dff3fa;border-radius:6px;
padding:1px 7px;font-size:12px;margin:1px 2px 1px 0}
.chip.red{background:#7c2d12;color:#fed7aa}
.mut{color:#8fa3ad}
h2{font-size:19px;margin:28px 0 2px}
.secnote{color:#8fa3ad;font-size:13px;margin-bottom:14px}
.line{font-size:14px;color:#c9d4da;margin:3px 0;line-height:1.55}
.attn{border:1px solid #8a6d1f;background:#171106;border-radius:12px;
padding:6px 16px 10px;margin-bottom:6px}
.aitem{padding:8px 0;border-top:1px solid #2a2110;font-size:14px;line-height:1.5}
.aitem:first-child{border-top:none}
.aitem .why{color:#f2c94c;font-weight:700}
.controls{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 14px}
.controls input,.controls select{background:#0a151a;color:#e8ecef;
border:1px solid #1d323c;border-radius:8px;padding:9px 10px;font-size:14px}
.controls input{flex:1;min-width:170px}
details{margin-top:10px}
summary{cursor:pointer;color:#7fb3c8;font-size:13px}
details .line{color:#8fa3ad;font-size:13px;word-break:break-all}
.foot{color:#637a84;font-size:12px;margin-top:20px;line-height:1.6}
</style></head><body><div class="wrap">
<div class="brand">SPY BEFORE YOU BUY</div>
<h1>Subscribers</h1>
<div class="sub">Private dashboard - read only.<br>__ASOF__</div>
__ATTENTION__
<h2>Website subscribers</h2>
<div class="secnote">From the Stripe-backed registry - trials, refunds
and period timing live here.</div>
<div class="controls"><input id="q" type="search"
placeholder="Search email, username, handle">
<select id="fs"><option value="">All statuses</option>__STATUSOPTS__</select>
<select id="fp"><option value="">All plans</option>__PLANOPTS__</select></div>
__STRIP__
__SUMMARY2__
__CARDS__
__GRANTSYNC__
<h2>X subscribers</h2>
<div class="secnote">Separate plan, billed on X - tracked in the
Telegram roster, confirmed in and out by Justin. Nothing below is a
website subscription.</div>
__XSECTION__
__STATS__
__INSIGHTS__
<div class="foot">Actions (cancel, refund) stay in Stripe for website
subscribers. Telegram shows Joined when the roster sync has seen the
join, otherwise Invite sent from the invite link on file. X, join and
grant data are snapshots the roster sync pushes every 15 minutes.
Next-charge amounts are the plan list prices.
<button id="installbtn" style="display:none;background:#175263;
color:#dff3fa;border:none;border-radius:8px;padding:10px 18px;
font-size:14px;font-weight:700;cursor:pointer;margin-top:12px">
Install the SBYB app</button>
<div id="installtip" style="margin-top:10px">Tip: put this on your home
screen like the GEX app - open your browser menu and tap
'Install app' (or 'Add to Home screen').</div></div>
</div>
<script>
var q=document.getElementById('q'),fs=document.getElementById('fs'),
fp=document.getElementById('fp');
function filt(){
 var s=q.value.toLowerCase(),st=fs.value,pl=fp.value;
 document.querySelectorAll('.card[data-search]').forEach(function(c){
  var okS=!s||c.dataset.search.indexOf(s)>=0;
  var isX=c.dataset.scope==='x';
  var okSt=isX||!st||c.dataset.status===st;
  var okPl=isX||!pl||c.dataset.plan===pl;
  c.style.display=(okS&&okSt&&okPl)?'':'none';});
}
[q,fs,fp].forEach(function(e){e.addEventListener('input',filt);});
</script>
<script>
if('serviceWorker' in navigator){window.addEventListener('load',
function(){navigator.serviceWorker.register('/sw.js')
.catch(function(){})})}
var _dip=null;
window.addEventListener('beforeinstallprompt',function(e){
 e.preventDefault();_dip=e;
 document.getElementById('installbtn').style.display='inline-block';
 document.getElementById('installtip').style.display='none';});
document.getElementById('installbtn').addEventListener('click',
function(){if(!_dip)return;_dip.prompt();
 _dip.userChoice.then(function(){_dip=null;
 document.getElementById('installbtn').style.display='none';});});
if(window.matchMedia('(display-mode: standalone)').matches||
 window.navigator.standalone){
 document.getElementById('installtip').style.display='none';}
</script>
</body></html>"""



# ---------- installable app (PWA): manifest, service worker, icons --
# This app's own manifest and service worker, on its own origin -
# nothing is shared with any other app. HARD RULES learned from the
# GEX app (Nov 2 2026): icon srcs stay PLAIN (a "?v=" version suffix
# once forced a Chrome WebAPK re-mint that produced a package which
# would not open at all), and the manifest carries NO handle_links
# field. The service worker caches ONLY the icons - pages are never
# cached, so subscriber data is always live from the server.
@app.get("/dashboard/manifest.webmanifest")
def dashboard_manifest(key: str = ""):
    """Installable-app manifest for the subscriber dashboard (the
    'SBYB Subs' home-screen app). Keyed exactly like the page: the
    start_url carries the read key, so the manifest is never served
    without it - 404 otherwise."""
    if not _key_ok(key):
        return JSONResponse({"detail": "not found"}, status_code=404)
    man = {
        "name": "SPY BEFORE YOU BUY - Subscribers",
        "short_name": "SBYB Subs",
        "description": ("Owner dashboard - SpyBeforeYouBuy website "
                        "and X subscribers, trials, renewals and "
                        "indicator grants at a glance."),
        "start_url": "/dashboard?key=" + key,
        "scope": "/dashboard",
        "display": "standalone",
        "background_color": "#04090a",
        "theme_color": "#04090a",
        "icons": [
            {"src": "/icons/sbyb-icon-192.png", "sizes": "192x192",
             "type": "image/png", "purpose": "any"},
            {"src": "/icons/sbyb-icon-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "any"},
            {"src": "/icons/sbyb-icon-maskable-512.png",
             "sizes": "512x512", "type": "image/png",
             "purpose": "maskable"},
        ],
    }
    return JSONResponse(man, media_type="application/manifest+json")


SW_JS = """// SBYB Subs service worker. Network for everything except the
// icons, which are cache-first. Pages are never cached, so the
// dashboard always renders from fresh server data.
var CACHE = 'sbyb-subs-icons-v1';
var ICONS = ['/icons/sbyb-icon-192.png', '/icons/sbyb-icon-512.png',
             '/icons/sbyb-icon-maskable-512.png',
             '/icons/sbyb-apple-touch-icon.png'];
self.addEventListener('install', function(e) {
  e.waitUntil(caches.open(CACHE).then(function(c) {
    return c.addAll(ICONS);
  }).then(function() { return self.skipWaiting(); }));
});
self.addEventListener('activate', function(e) {
  e.waitUntil(self.clients.claim());
});
self.addEventListener('fetch', function(e) {
  var url = new URL(e.request.url);
  if (url.pathname.indexOf('/icons/') === 0) {
    e.respondWith(caches.match(e.request).then(function(r) {
      return r || fetch(e.request);
    }));
  }
});
"""


@app.get("/sw.js")
def service_worker():
    return HTMLResponse(SW_JS, media_type="application/javascript")


@app.get("/icons/{name}")
def icon(name: str):
    safe = {"sbyb-icon-192.png", "sbyb-icon-512.png",
            "sbyb-icon-maskable-512.png", "sbyb-apple-touch-icon.png"}
    if name not in safe:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(os.path.join(APP_DIR, "static", "icons", name),
                        media_type="image/png")


@app.get("/healthz")
def healthz():
    return PlainTextResponse("ok")


# ---------- snapshot ingest ----------
@app.post("/push/snapshot")
def push_snapshot(payload: dict, key: str = ""):
    """Keyed full-snapshot push from the VM sync process. The body
    mirrors the shapes the sync already builds for the GEX app:
      subs:      rows in the GET /stripe/subs shape (email, plan,
                 status, ids, invite_link, period timestamps,
                 tv_username, welcome_sent_ts, trial_repeat, plus
                 trial_used_ts / refund_used_ts and optionally
                 canceled_ts and card_display)
      joins:     [{email, status, tg_username, joined_at}]
      x_subs:    [{handle, status, tg_username, created_at,
                 joined_at}]
      grants:    [{tv_username, indicators, granted, note}]
      tv_claims: optional [{email, tv_username, matched, ts}]
    Snapshot-replace semantics: inside one transaction the local
    copies are replaced wholesale, so anything absent from the
    snapshot disappears here too. 404 on key mismatch."""
    if not _push_key_ok(key):
        return JSONResponse({"detail": "not found"}, status_code=404)
    subs = payload.get("subs") or []
    joins = payload.get("joins") or []
    x_subs = payload.get("x_subs") or []
    grants = payload.get("grants") or []
    tv_claims = payload.get("tv_claims") or []
    for section in (subs, joins, x_subs, grants, tv_claims):
        if not isinstance(section, list):
            return JSONResponse({"detail": "bad payload"},
                                status_code=400)
    pushed_at = time.time()
    con = _subs_db()
    try:
        con.execute("DELETE FROM site_subs")
        con.execute("DELETE FROM identity_flags")
        n = 0
        for s in subs:
            if not isinstance(s, dict):
                continue
            email = (s.get("email") or "").strip().lower()[:254]
            if not email or "@" not in email:
                continue
            con.execute(
                """INSERT INTO site_subs(
                       email, plan, status, stripe_customer_id,
                       stripe_subscription_id, checkout_session_id,
                       invite_link, current_period_end, created_ts,
                       updated_ts, tv_username, welcome_sent_ts,
                       trial_repeat, canceled_ts, card_display)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (email,
                 (str(s["plan"])[:64] if s.get("plan") else None),
                 (str(s["status"])[:32] if s.get("status") else None),
                 s.get("stripe_customer_id") or None,
                 s.get("stripe_subscription_id") or None,
                 s.get("checkout_session_id") or None,
                 s.get("invite_link") or None,
                 _num(s.get("current_period_end")),
                 _num(s.get("created_ts")),
                 _num(s.get("updated_ts")),
                 (str(s["tv_username"])[:64]
                  if s.get("tv_username") else None),
                 _num(s.get("welcome_sent_ts")),
                 1 if s.get("trial_repeat") else 0,
                 _num(s.get("canceled_ts")),
                 (str(s["card_display"])[:64]
                  if s.get("card_display") else None)))
            trial_ts = _num(s.get("trial_used_ts"))
            refund_ts = _num(s.get("refund_used_ts"))
            if trial_ts is not None or refund_ts is not None:
                con.execute(
                    """INSERT INTO identity_flags(
                           kind, key, trial_used_ts, refund_used_ts,
                           source, updated_ts)
                       VALUES('email',?,?,?,?,?)""",
                    (email, trial_ts, refund_ts, "snapshot",
                     pushed_at))
            n += 1
        con.execute("DELETE FROM telegram_joins")
        for j in joins:
            if not isinstance(j, dict):
                continue
            email = (j.get("email") or "").strip().lower()[:254]
            if not email or "@" not in email:
                continue
            con.execute(
                """INSERT INTO telegram_joins(email, status,
                       tg_username, joined_at, updated_ts)
                   VALUES(?,?,?,?,?)""",
                (email, (j.get("status") or "")[:32],
                 (j.get("tg_username") or "")[:64] or None,
                 (j.get("joined_at") or "")[:32] or None, pushed_at))
        con.execute("DELETE FROM x_subs")
        for x in x_subs:
            if not isinstance(x, dict):
                continue
            handle = (x.get("handle") or "").strip()[:128]
            if not handle:
                continue
            con.execute(
                """INSERT INTO x_subs(handle, status, tg_username,
                       created_at, joined_at, updated_ts)
                   VALUES(?,?,?,?,?,?)""",
                (handle, (x.get("status") or "")[:32],
                 (x.get("tg_username") or "")[:64] or None,
                 (x.get("created_at") or "")[:32] or None,
                 (x.get("joined_at") or "")[:32] or None, pushed_at))
        con.execute("DELETE FROM tv_grants")
        for g in grants:
            if not isinstance(g, dict):
                continue
            tvn = _norm_tv(g.get("tv_username") or g.get("tv"))
            if not tvn:
                continue
            inds = g.get("indicators") or []
            if not isinstance(inds, list):
                inds = [str(inds)]
            con.execute(
                """INSERT INTO tv_grants(tv, indicators, granted,
                       note, updated_ts) VALUES(?,?,?,?,?)""",
                (tvn, json.dumps([str(i)[:24] for i in inds][:12]),
                 str(g.get("granted") or "")[:32] or None,
                 str(g.get("note") or "")[:120] or None, pushed_at))
        con.execute("DELETE FROM tv_claims")
        for c in tv_claims:
            if not isinstance(c, dict):
                continue
            email = (c.get("email") or "").strip().lower()[:254]
            if not email:
                continue
            con.execute(
                """INSERT INTO tv_claims(email, tv_username, matched,
                       ts) VALUES(?,?,?,?)""",
                (email,
                 (str(c["tv_username"])[:64]
                  if c.get("tv_username") else None),
                 1 if c.get("matched") else 0,
                 _num(c.get("ts"))))
        con.execute(
            """INSERT INTO meta(key, value) VALUES('pushed_at', ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (repr(pushed_at),))
        # Lifecycle statistics ride along with the snapshot when the
        # sync built them; a push without stats leaves the stored
        # copy untouched (stats are cumulative state, not a mirror).
        stats = payload.get("stats")
        if isinstance(stats, dict):
            con.execute(
                """INSERT INTO meta(key, value) VALUES('stats', ?)
                   ON CONFLICT(key) DO UPDATE SET
                   value=excluded.value""",
                (json.dumps(stats),))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "subs": n}



def _render_stats(stats, dstr, esc):
    """Statistics + "What I see" sections, rendered from the stats
    payload the VM sync builds (stats_builder) and pushes with each
    snapshot. Honest placeholders until the first stats payload
    lands; every rate shows its counts, nothing is estimated."""
    secnote = ('<div class="secnote">Lifecycle tracking since '
               "Nov 3, 2026 - trials, conversions, cancellations "
               "and refunds, counted from the registry and the "
               "roster. Rates always show their counts.</div>")
    isec = ('<h2>What I see</h2><div class="secnote">Read of the '
            "numbers above - suggestions only, nothing here changes "
            "anything by itself.</div>")
    if not isinstance(stats, dict):
        return (("<h2>Statistics</h2>" + secnote
                 + '<div class="line mut">Statistics start '
                   "compiling with the next sync.</div>"),
                (isec + '<div class="line mut">Nothing to flag '
                        "yet.</div>"))
    totals = stats.get("totals") or {}
    trials = stats.get("trials") or {}
    paid_c = stats.get("paid_cancels") or {}
    refunds = stats.get("refunds") or 0
    if isinstance(refunds, dict):
        refunds = refunds.get("count", 0)
    tiles = [
        (totals.get("signups_tracked", 0), "Signups tracked"),
        (trials.get("started", 0), "Trials started"),
        (trials.get("converted", 0), "Trials converted"),
        (trials.get("canceled_in_trial", 0), "Trial cancels"),
        (paid_c.get("total", 0), "Paid cancels"),
        (refunds, "Refunds"),
    ]
    out = ["<h2>Statistics</h2>", secnote, '<div class="strip">']
    for n, k in tiles:
        out.append(f'<div class="tile"><div class="n">{esc(str(n))}'
                   f'</div><div class="k">{esc(k)}</div></div>')
    out.append("</div>")
    conv = trials.get("converted", 0)
    canc = trials.get("canceled_in_trial", 0)
    done = trials.get("completed")
    if done is None:
        done = conv + canc
    if done > 0:
        pct = round(100.0 * conv / done)
        out.append(f'<div class="line">Trial outcome: {conv} of '
                   f"{done} converted ({pct}%), {canc} "
                   "canceled.</div>")
        if canc > 0:
            buckets = trials.get("cancel_buckets") or {}
            out.append('<div class="line">Trial cancels by timing: '
                       f"{buckets.get('le2d', 0)} within 2 days, "
                       f"{buckets.get('d3_7', 0)} on days 3-7.</div>")
    else:
        out.append('<div class="line mut">No completed trials '
                   "yet.</div>")
    if (paid_c.get("total") or 0) > 0:
        pb = paid_c.get("buckets") or {}
        out.append('<div class="line">Paid cancels by tenure: '
                   f"{pb.get('le7d', 0)} in the first week, "
                   f"{pb.get('d8_30', 0)} in days 8-30, "
                   f"{pb.get('gt30', 0)} after 30 days.</div>")
    plan_mix = stats.get("plan_mix") or {}
    if plan_mix:
        items = ", ".join(
            f"{esc(str(p))} {c}" for p, c in
            sorted(plan_mix.items(), key=lambda kv: (-kv[1], kv[0])))
        out.append(f'<div class="line">Plan mix now: {items}</div>')
    sources = stats.get("sources") or {}
    x = sources.get("x") or {}
    if sources.get("x_available") is False:
        out.append('<div class="line">Sources: '
                   f"{sources.get('website_signups', 0)} signups "
                   "via the website; X roster unavailable right "
                   "now.</div>")
    else:
        out.append('<div class="line">Sources: '
                   f"{sources.get('website_signups', 0)} signups "
                   f"via the website; X roster: {x.get('in', 0)} "
                   f"in, {x.get('invite_sent', 0)} invite sent, "
                   f"{x.get('out', 0)} out "
                   f"({x.get('added_30d', 0)} added in the last "
                   "30 days).</div>")
    events = stats.get("events") or []
    if events:
        out.append('<div class="line" style="margin-top:10px">'
                   "Recent activity</div>")
        for e in events:
            if not isinstance(e, dict):
                continue
            when = dstr(e.get("ts")) or "-"
            out.append(f'<div class="line">{esc(when)} - '
                       f"{esc(e.get('email') or '')} - "
                       f"{esc(e.get('text') or '')}</div>")
    else:
        out.append('<div class="line mut">No activity tracked '
                   "yet.</div>")
    insights = stats.get("insights") or []
    if insights:
        cards = []
        for ins in insights:
            if not isinstance(ins, dict):
                continue
            level = ins.get("level") or "info"
            style = ""
            if level == "watch":
                style = ' style="border-left:4px solid #8a6d1f"'
            elif level == "good":
                style = ' style="border-left:4px solid #175263"'
            cards.append(
                f'<div class="card"{style}><div class="line" '
                f'style="margin:0">{esc(ins.get("text") or "")}'
                "</div></div>")
        insights_html = isec + "".join(cards)
    else:
        insights_html = isec + ('<div class="line mut">Nothing to '
                                "flag yet.</div>")
    return "".join(out), insights_html


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(key: str = ""):
    """Justin's private subscriber dashboard. 404 unless the read
    key configured in the environment matches - reveals nothing when
    unconfigured, and the page itself is noindex."""
    if not _key_ok(key):
        return HTMLResponse("Not found", status_code=404)
    from datetime import datetime, timezone
    from html import escape as _esc
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/Winnipeg")
    now = time.time()
    today = datetime.fromtimestamp(now, tz).date()
    con = _subs_db()
    try:
        rows = con.execute(
            """SELECT email, plan, status, invite_link,
               current_period_end, created_ts, tv_username,
               welcome_sent_ts, trial_repeat, stripe_customer_id,
               stripe_subscription_id, canceled_ts, card_display
               FROM site_subs""").fetchall()
        flags = {r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT key, trial_used_ts, refund_used_ts "
            "FROM identity_flags WHERE kind='email'")}
        joins = {r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT email, status, tg_username FROM telegram_joins")}
        xrows = con.execute(
            """SELECT handle, status, tg_username, created_at, joined_at,
               updated_ts FROM x_subs ORDER BY created_at""").fetchall()
        grants = {}
        grants_synced = None
        for tvn, inds, granted, note, gu in con.execute(
                "SELECT tv, indicators, granted, note, updated_ts "
                "FROM tv_grants"):
            try:
                ilist = json.loads(inds or "[]")
            except ValueError:
                ilist = []
            grants[tvn] = (ilist, granted, note)
            grants_synced = max(grants_synced or 0, gu or 0) or None
        claims = {}
        for em, cts in con.execute(
                "SELECT email, ts FROM tv_claims WHERE matched=1"):
            claims[em] = max(claims.get(em, 0), cts or 0)
        mrow = con.execute(
            "SELECT value FROM meta WHERE key='pushed_at'").fetchone()
        try:
            pushed_at = float(mrow[0]) if mrow else None
        except (TypeError, ValueError):
            pushed_at = None
        srow = con.execute(
            "SELECT value FROM meta WHERE key='stats'").fetchone()
        stats = None
        if srow:
            try:
                stats = json.loads(srow[0])
            except (TypeError, ValueError):
                stats = None
    finally:
        con.close()

    def dstr(ts, full=False):
        if not ts:
            return None
        d = datetime.fromtimestamp(ts, tz)
        s = f"{d.strftime('%b')} {d.day}"
        return s + f", {d.year}" if full else s

    def days_until(ts):
        if not ts:
            return None
        return (datetime.fromtimestamp(ts, tz).date() - today).days

    def money(x):
        return f"${x:,.0f}" if float(x) == int(x) else f"${x:,.2f}"

    def left_text(days):
        if days is None:
            return ""
        if days < 0:
            return " - ended"
        if days == 0:
            return " - today"
        if days == 1:
            return " - 1 day left"
        return f" - {days} days left"

    GOOD = ("active", "trialing", "past_due", "lifetime")
    counts = {}
    plan_counts = {}
    trials3 = trials7 = 0
    renewals = []
    conversions = []
    mrr = 0.0
    recent_cancels = []
    attention = []  # (priority, email, why-html)

    def sort_key(r):
        return (r[4] is None, r[4] or 0, (r[0] or ""))

    cards = []
    for r in sorted(rows, key=sort_key):
        (email, plan, status, invite, period_end, created_ts, tv,
         welcomed, trial_repeat, cust_id, sub_id, canceled_ts,
         card_display) = r
        st = (status or "unknown").lower()
        counts[st] = counts.get(st, 0) + 1
        plan_counts[plan or "-"] = plan_counts.get(plan or "-", 0) + 1
        label = _STATUS_LABELS.get(st, (status or "Unknown").title())
        price = _PLAN_PRICES.get(plan or "")
        plan_text = f"{plan or '-'} - {price}" if price else (plan or "-")
        amt = _PLAN_AMOUNTS.get(plan or "")
        trial_ts, refund_ts = flags.get(email, (None, None))
        du = days_until(period_end)

        # --- billing / ends line ---
        if plan == "Lifetime" or st == "lifetime":
            paid = f"Paid {money(amt)}" if amt else "Paid in full"
            if created_ts:
                paid += f" on {dstr(created_ts, full=True)}"
            billing = paid + " - no renewals"
        elif st == "trialing":
            billing = (f"Trial ends {dstr(period_end, full=True)}"
                       f"{left_text(du)}")
            if amt:
                billing += f" - then {money(amt)}"
            if du is not None and 0 <= du <= 7:
                conversions.append((email, period_end, amt))
            if du is not None and 0 <= du <= 3:
                trials3 += 1
            if du is not None and 0 <= du <= 7:
                trials7 += 1
        elif st in ("active", "past_due"):
            billing = f"Next charge {dstr(period_end, full=True)}" \
                      f"{left_text(du)}"
            if amt:
                billing += f" - {money(amt)}"
            if du is not None and 0 <= du <= 7:
                renewals.append((email, period_end, amt))
            if st == "active" or st == "past_due":
                mrr += _PLAN_MONTHLY_EQ.get(plan or "", 0)
        elif st == "canceled":
            if canceled_ts:
                billing = f"Canceled {dstr(canceled_ts, full=True)}"
            elif du is not None and du >= 0:
                billing = f"Access until {dstr(period_end, full=True)}"
            else:
                billing = "Canceled"
            if canceled_ts and now - canceled_ts <= 60 * 86400:
                recent_cancels.append((email, "Canceled", canceled_ts))
        elif st == "refunded":
            billing = "Refunded"
            ref_ts = refund_ts or canceled_ts
            if ref_ts and now - ref_ts <= 60 * 86400:
                recent_cancels.append((email, "Refunded", ref_ts))
        else:
            billing = (f"Period ends {dstr(period_end, full=True)}"
                       if period_end else "-")

        # --- signed up / membership length ---
        if created_ts:
            mdays = (today - datetime.fromtimestamp(
                created_ts, tz).date()).days
            if mdays <= 0:
                mlen = "joined today"
            elif mdays == 1:
                mlen = "1 day in"
            else:
                mlen = f"{mdays} days in"
            signup = f"{dstr(created_ts, full=True)} - {mlen}"
        else:
            signup = "-"

        # --- telegram ---
        if plan == IO_PLAN:
            tg_text = '<span class="mut">No Telegram (indicators only)</span>'
            tg_search = ""
        elif email in joins:
            jstatus, juser = joins[email]
            if jstatus == "active":
                tg_text = "Joined" + (f" - @{_esc(juser)}" if juser else "")
            elif jstatus == "removed":
                tg_text = "Removed"
            else:
                tg_text = "Invite sent"
            tg_search = juser or ""
        elif invite:
            tg_text = "Invite sent"
            tg_search = ""
        else:
            tg_text = '<span class="mut">No invite yet</span>'
            tg_search = ""

        # --- tradingview + grant state ---
        grant_rec = grants.get((tv or "").strip().lower()) if tv else None
        if not tv:
            tv_html = '<span class="mut">No username claimed</span>'
        elif grant_rec:
            inds = ", ".join(_esc(i) for i in grant_rec[0]) or "all"
            tv_html = (f"{_esc(tv)}"
                       f'<span class="s">Granted: {inds}</span>')
        else:
            tv_html = f"{_esc(tv)}" \
                      '<span class="s">Grant pending</span>'

        # --- welcome ---
        if welcomed:
            welcome_text = f"Sent - {dstr(welcomed, full=True)}"
        else:
            welcome_text = '<span class="mut">Not sent</span>'

        # --- card on file (from the snapshot only - this app has no
        # Stripe access, so a missing value honestly shows a dash) ---
        card_text = (card_display or "").strip() or "-"

        # --- flags ---
        chips = []
        if trial_ts:
            chips.append('<span class="chip">Trial used</span>')
        if refund_ts:
            chips.append('<span class="chip red">Refund used</span>')
        if trial_repeat:
            chips.append('<span class="chip red">Repeat trial</span>')
        flags_text = "".join(chips) if chips else '<span class="mut">-</span>'

        # --- needs-attention derivation ---
        if st in ("past_due", "unpaid"):
            attention.append((0, email,
                                "Payment failed - status is "
                                f"{label.lower()}"))
        if st in GOOD:
            if plan != IO_PLAN and joins:
                js = joins.get(email, ("", None))[0]
                if js != "active":
                    attention.append(
                        (2, email, "Has not joined Telegram yet"
                         + (" (invite sent)" if invite or js == "pending"
                            else "")))
            if not tv:
                attention.append(
                    (3, email, "No TradingView username claimed yet"))
            elif not grant_rec:
                basis = claims.get(email) or created_ts
                since = dstr(basis, full=True) if basis else "unknown"
                over = basis and (now - basis) > 86400
                attention.append(
                    (1 if over else 2, email,
                     f"Indicator grant pending - waiting since {since}"
                     + (" - over 24 hours" if over else "")))
            if not welcomed and created_ts and now - created_ts > 3600:
                attention.append(
                    (3, email, "Welcome email not sent yet"))

        det = [f"Signup: {dstr(created_ts, full=True) or '-'}"]
        if period_end:
            det.append(f"Period end: {dstr(period_end, full=True)}")
        if canceled_ts:
            det.append(f"Canceled: {dstr(canceled_ts, full=True)}")
        if grant_rec and grant_rec[2]:
            det.append(f"Grant note: {_esc(grant_rec[2])}")
        if grant_rec and grant_rec[1]:
            det.append(f"Granted on: {_esc(str(grant_rec[1]))}")
        if cust_id:
            det.append(f"Stripe customer: {_esc(cust_id)}")
        if sub_id:
            det.append(f"Stripe subscription: {_esc(sub_id)}")
        details = ("<details><summary>Details</summary>"
                   + "".join(f'<div class="line">{d}</div>' for d in det)
                   + "</details>")

        search_text = _esc(
            f"{email} {tv or ''} {tg_search}".lower())
        cards.append(
            f'<div class="card" data-scope="web" data-status="{_esc(st)}" '
            f'data-plan="{_esc(plan or "")}" data-search="{search_text}">'
            '<div class="top">'
            f'<div class="email">{_esc(email or "")}</div>'
            f'<span class="badge b-{_esc(st)}">{_esc(label)}</span></div>'
            '<div class="grid">'
            f'<div class="f"><div class="k">Plan</div>'
            f'<div class="v">{_esc(plan_text)}</div></div>'
            f'<div class="f"><div class="k">Billing</div>'
            f'<div class="v">{_esc(billing)}</div></div>'
            f'<div class="f"><div class="k">Signed up</div>'
            f'<div class="v">{_esc(signup)}</div></div>'
            f'<div class="f"><div class="k">TradingView</div>'
            f'<div class="v">{tv_html}</div></div>'
            f'<div class="f"><div class="k">Telegram</div>'
            f'<div class="v">{tg_text}</div></div>'
            f'<div class="f"><div class="k">Welcome email</div>'
            f'<div class="v">{welcome_text}</div></div>'
            f'<div class="f"><div class="k">Card</div>'
            f'<div class="v">{_esc(card_text)}</div></div>'
            f'<div class="f"><div class="k">Flags</div>'
            f'<div class="v">{flags_text}</div></div>'
            "</div>"
            f"{details}</div>")

    paid_active = counts.get("active", 0) + counts.get("lifetime", 0)
    tiles = [
        (str(len(rows)), "Subscribers"),
        (str(paid_active), "Active paid"),
        (str(counts.get("trialing", 0)), "Trialing"),
        (str(trials3), "Trials ending in 3 days"),
        (str(counts.get("past_due", 0) + counts.get("unpaid", 0)),
         "Past due"),
        (str(counts.get("canceled", 0) + counts.get("refunded", 0)),
         "Canceled / refunded"),
    ]
    strip = '<div class="strip">' + "".join(
        f'<div class="tile{" warn" if k.startswith("Trials ending") and n != "0" else ""}">'
        f'<div class="n">{_esc(n)}</div><div class="k">{_esc(k)}</div></div>'
        for n, k in tiles) + "</div>"

    # --- summary level 2: by plan, 7-day money, MRR, cancellations ---
    s2 = []
    if plan_counts:
        by_plan = " · ".join(
            f"{_esc(p)} {c}" for p, c in sorted(plan_counts.items()))
        s2.append(f'<div class="line">By plan: {by_plan}</div>')
    tiles2 = [
        (str(trials7), "Trials ending in 7 days"),
        (str(len(renewals)), "Renewals due in 7 days"),
        (str(len(conversions)), "Trial conversions in 7 days"),
        (money(mrr), "Est. monthly recurring"),
    ]
    s2.append('<div class="strip">' + "".join(
        f'<div class="tile"><div class="n">{_esc(n)}</div>'
        f'<div class="k">{_esc(k)}</div></div>'
        for n, k in tiles2) + "</div>")
    s2.append('<div class="line mut" style="margin-top:-6px">Monthly '
              "recurring is an estimate from plan list prices of "
              "active plans (Lifetime excluded).</div>")
    if renewals:
        tot = sum(a or 0 for _, _, a in renewals)
        items = " · ".join(
            f"{_esc(e)} - {dstr(pe)}"
            + (f" - {money(a)}" if a else "")
            for e, pe, a in sorted(renewals, key=lambda x: x[1]))
        s2.append(f'<div class="line">Renewals due: {items} '
                  f"(total {money(tot)})</div>")
    if conversions:
        tot = sum(a or 0 for _, _, a in conversions)
        items = " · ".join(
            f"{_esc(e)} - {dstr(pe)}"
            + (f" - {money(a)}" if a else "")
            for e, pe, a in sorted(conversions, key=lambda x: x[1]))
        s2.append(f'<div class="line">Trials converting: {items} '
                  f"(total {money(tot)})</div>")
    if recent_cancels:
        items = " · ".join(
            f"{_esc(e)} - {w.lower()} {dstr(ts, full=True)}"
            for e, w, ts in sorted(recent_cancels, key=lambda x: -x[2]))
        s2.append(f'<div class="line">Recent cancellations '
                  f"(60 days): {items}</div>")
    else:
        s2.append('<div class="line mut">Recent cancellations: none '
                  "in the last 60 days.</div>")
    summary2 = "".join(s2)

    if not cards:
        cards.append('<div class="card">No subscribers yet.</div>')

    # --- needs-attention strip ---
    if attention:
        items_html = "".join(
            f'<div class="aitem"><span class="why">{_esc(why)}</span>'
            f' - {_esc(em)}</div>'
            for _p, em, why in sorted(attention,
                                      key=lambda x: (x[0], x[1])))
        attention_html = ('<h2>Needs attention</h2>'
                          f'<div class="attn">{items_html}</div>')
    else:
        attention_html = ('<h2>Needs attention</h2>'
                          '<div class="attn"><div class="aitem">Nothing '
                          "needs attention.</div></div>")

    if grants_synced:
        gs = datetime.fromtimestamp(grants_synced, tz)
        grantsync = ('<div class="foot" style="margin-top:0">TradingView '
                     f"grant list synced {gs.strftime('%b')} {gs.day}, "
                     f"{gs.strftime('%I:%M %p')} Winnipeg.</div>")
    else:
        grantsync = ""

    status_opts = "".join(
        f'<option value="{_esc(s)}">{_esc(_STATUS_LABELS.get(s, s.title()))}</option>'
        for s in sorted(counts))
    plan_opts = "".join(
        f'<option value="{_esc(p)}">{_esc(p)}</option>'
        for p in sorted(plan_counts))

    # --- X subscribers section (separate plan, roster-fed) ---
    def fmt_iso(s):
        if not s:
            return None
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        d = d.astimezone(tz)
        return f"{d.strftime('%b')} {d.day}, {d.year}"

    if not xrows:
        xsection = ('<div class="card"><span class="mut">No X subscriber '
                    "data synced yet - the roster sync pushes it every "
                    "15 minutes.</span></div>")
    else:
        x_counts = {}
        for xr in xrows:
            x_counts[xr[1] or "unknown"] = \
                x_counts.get(xr[1] or "unknown", 0) + 1
        x_tiles = [
            (str(x_counts.get("active", 0)), "In"),
            (str(x_counts.get("pending", 0)), "Invite sent"),
            (str(x_counts.get("removed", 0)), "Out"),
        ]
        xsection = '<div class="strip">' + "".join(
            f'<div class="tile"><div class="n">{_esc(n)}</div>'
            f'<div class="k">{_esc(k)}</div></div>'
            for n, k in x_tiles) + "</div>"
        x_badges = {"active": ("In", "b-active"),
                    "pending": ("Invite sent", "b-trialing"),
                    "removed": ("Out", "b-canceled")}
        for handle, xstatus, xtg, xcreated, xjoined, _xu in xrows:
            blabel, bclass = x_badges.get(
                xstatus or "", ((xstatus or "Unknown").title(), "b-other"))
            added = fmt_iso(xcreated)
            joined = fmt_iso(xjoined)
            if xstatus == "active":
                since = f"In since {joined or added or '-'}"
            elif xstatus == "removed":
                since = f"Added {added or '-'}"
                if joined:
                    since += f" - joined {joined}"
            else:
                since = f"Added {added or '-'}"
            tg_line = f" - Telegram @{_esc(xtg)}" if xtg else ""
            xsection += (
                f'<div class="card" data-scope="x" '
                f'data-search="{_esc(((handle or "") + " " + (xtg or "")).lower())}">'
                '<div class="top">'
                f'<div class="email">{_esc(handle or "")}</div>'
                f'<span class="badge {bclass}">{_esc(blabel)}</span></div>'
                f'<div class="sub" style="margin:8px 0 0">'
                f"{_esc(since)}{tg_line}</div></div>")
        maxu = max((xr[5] or 0) for xr in xrows)
        if maxu:
            mu = datetime.fromtimestamp(maxu, tz)
            xsection += (
                '<div class="foot">Roster snapshot pushed '
                f"{mu.strftime('%b')} {mu.day}, "
                f"{mu.strftime('%I:%M %p')} Winnipeg.</div>")

    stats_html, insights_html = _render_stats(stats, dstr, _esc)

    upd = datetime.fromtimestamp(now, tz)
    updated = f"{upd.strftime('%b')} {upd.day}, {upd.strftime('%I:%M %p')}"
    if pushed_at:
        sd = datetime.fromtimestamp(pushed_at, tz)
        sync_str = (f"{sd.strftime('%b')} {sd.day}, "
                    f"{sd.strftime('%I:%M %p')} Winnipeg")
        asof = (f"Data as of: last sync {sync_str} "
                f"({len(rows)} subscribers) "
                f"- page generated {updated} Winnipeg")
    else:
        asof = ("Data as of: no sync received yet "
                f"- page generated {updated} Winnipeg")
    page = (DASHBOARD_PAGE.replace("__KEY__", key)
            .replace("__ASOF__", _esc(asof))
            .replace("__ATTENTION__", attention_html)
            .replace("__STATUSOPTS__", status_opts)
            .replace("__PLANOPTS__", plan_opts)
            .replace("__STRIP__", strip)
            .replace("__SUMMARY2__", summary2)
            .replace("__CARDS__", "".join(cards))
            .replace("__GRANTSYNC__", grantsync)
            .replace("__XSECTION__", xsection)
            .replace("__STATS__", stats_html)
            .replace("__INSIGHTS__", insights_html))
    return HTMLResponse(page, headers={
        "X-Robots-Tag": "noindex, nofollow",
        "Cache-Control": "no-store"})
