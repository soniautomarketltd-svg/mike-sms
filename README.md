# Mike SMS — Vercel deploy

Cloud-hosted SMS webhook for Mike (Vapi voice assistant → Twilio). Runs on Vercel's free tier, no laptop needed.

## Files

- `api/sms.py` — the serverless function (all three endpoints live here)
- `requirements.txt` — Python dependencies (just Twilio)
- `vercel.json` — rewrite rules so URLs look clean

## Endpoints after deploy

Your public base URL will be `https://<project-name>.vercel.app`. The endpoints:

- `GET  /api/health` — sanity check (no auth)
- `POST /api/sms-send` — Mike's mid-call sendText tool (requires `X-Vapi-Secret` header)
- `POST /api/end-of-call` — auto thank-you after any call ends (same header)

## Required environment variables (set in Vercel dashboard)

- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_FROM_NUMBER` (default `+16397391131`)
- `VAPI_WEBHOOK_SECRET` — a random string, same value used in Vapi's tool config
