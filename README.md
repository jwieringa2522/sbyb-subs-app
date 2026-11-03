# SBYB Subs - standalone subscriber dashboard

Justin's private, read-only dashboard of SpyBeforeYouBuy subscribers
(website/Stripe subscribers plus the separate X subscriber plan),
built as its own small FastAPI app. It is a port of the dashboard
that originally lived inside the GEX heatmap app, restructured to be
fully standalone:

- **Separate everything.** Own codebase, own SQLite database, own
  origin when deployed, own web-app manifest and service worker. It
  shares no code, manifest or service worker with the GEX app.
- **Push-fed, no Stripe.** The app holds no Stripe key and never
  calls Stripe (or Telegram, or any other API). A sync process on
  the VM POSTs full snapshots; every page renders from the local
  snapshot copy. Card brand/last4 only ever shows what a snapshot
  supplies in a row's `card_display` field - otherwise a dash.

## Routes

| Route | Key | Notes |
| --- | --- | --- |
| `GET /dashboard?key=` | `SUBS_READ_KEY` | The dashboard. 404 on a bad/missing key; `noindex` + `no-store` headers. |
| `GET /dashboard/manifest.webmanifest?key=` | `SUBS_READ_KEY` | "SBYB Subs" installable-app manifest (`application/manifest+json`). Icon srcs are plain (never versioned) and there is deliberately no `handle_links` field. |
| `GET /sw.js` | - | Service worker `sbyb-subs-icons-v1`; caches only the four app icons, everything else is network. |
| `GET /icons/{name}` | - | Allowlist only: `sbyb-icon-192.png`, `sbyb-icon-512.png`, `sbyb-icon-maskable-512.png`, `sbyb-apple-touch-icon.png` from `static/icons/`. |
| `POST /push/snapshot?key=` | `SUBS_PUSH_KEY` | Full-snapshot ingest (below). 404 on a bad key. Returns `{"ok": true, "subs": N}`. |
| `GET /healthz` | - | Plain `ok`. |

## Snapshot format

`POST /push/snapshot` takes one JSON body, mirroring the shapes the
VM roster sync already builds:

```json
{
  "subs":      [ { "...": "rows in the GET /stripe/subs shape" } ],
  "joins":     [ {"email": "...", "status": "pending|active|removed",
                  "tg_username": "...", "joined_at": "..."} ],
  "x_subs":    [ {"handle": "...", "status": "...",
                  "tg_username": "...", "created_at": "...",
                  "joined_at": "..."} ],
  "grants":    [ {"tv_username": "...", "indicators": ["LDR", "..."],
                  "granted": "2026-11-01", "note": "..."} ],
  "tv_claims": [ {"email": "...", "tv_username": "...",
                  "matched": 1, "ts": 0} ]
}
```

`subs` rows also carry `trial_used_ts` / `refund_used_ts` (folded
into the identity-flags view at ingest) and may optionally carry
`canceled_ts` and `card_display`. `tv_claims` is optional and
defaults to empty. Ingest is **snapshot-replace**: inside one
transaction the local copies of subscribers, identity flags,
Telegram joins, X subscribers, TV grants and claims are replaced
wholesale, and the push time is recorded for the dashboard's
"Data as of: last sync ..." line. Before the first snapshot lands,
the dashboard honestly says "no sync received yet".

## Configuration

Environment only - there are no file fallbacks:

| Variable | Used for |
| --- | --- |
| `SUBS_READ_KEY` | Dashboard + manifest access key. |
| `SUBS_PUSH_KEY` | Snapshot push key. |

State lives in `subs.db` (SQLite) next to `app.py`.

## Run / deploy

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port $PORT --workers 1
```

(with `SUBS_READ_KEY` and `SUBS_PUSH_KEY` set in the environment).
