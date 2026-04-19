"""
Mike SMS — Vercel serverless version.

Same logic as the laptop Flask server, restructured for Vercel's serverless
Python runtime. Three endpoints under /api/:

  POST /api/sms-send          — Mike's mid-call sendText tool
  POST /api/end-of-call       — Auto thank-you after any call ends
  GET  /api/health            — Sanity check

Environment variables (set in the Vercel dashboard):
  TWILIO_ACCOUNT_SID
  TWILIO_AUTH_TOKEN
  TWILIO_FROM_NUMBER      (default +16397391131)
  VAPI_WEBHOOK_SECRET     (shared secret — same value in Vapi's tool config)
"""

import os
import re
import json
from datetime import datetime

from http.server import BaseHTTPRequestHandler
from twilio.rest import Client

# --- Config pulled from Vercel env vars ------------------------------------

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "+16397391131")
VAPI_WEBHOOK_SECRET = os.environ.get("VAPI_WEBHOOK_SECRET", "")

MAX_MESSAGE_LENGTH = 800

AFTER_CALL_MESSAGE = (
    "Thanks for calling Soni Auto Market! Browse our inventory: "
    "https://soniautomarket.com/cars. Text or call back anytime — Mike."
)

# Put numbers here that should NOT receive the auto thank-you SMS
# (e.g. your own cell, staff phones).
AFTER_CALL_SKIP_NUMBERS = set()

E164 = re.compile(r"^\+[1-9]\d{7,14}$")

twilio_client = (
    Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN
    else None
)


def normalize_phone(raw):
    if not raw:
        return None
    digits = re.sub(r"[^\d+]", "", str(raw))
    if digits.startswith("+"):
        return digits if E164.match(digits) else None
    if len(digits) == 10:
        digits = "+1" + digits
    elif len(digits) == 11 and digits.startswith("1"):
        digits = "+" + digits
    else:
        return None
    return digits if E164.match(digits) else None


def _route(path):
    # Strip query string + trailing slash
    p = (path or "").split("?")[0].rstrip("/").lower()
    # Vercel rewrites may add /api prefix; normalize
    return p.replace("/api", "", 1) if p.startswith("/api") else p


def _check_auth(headers):
    provided = headers.get("x-vapi-secret") or headers.get("X-Vapi-Secret") or ""
    if not provided:
        bearer = headers.get("authorization") or headers.get("Authorization") or ""
        if bearer.startswith("Bearer "):
            provided = bearer[7:]
    return bool(VAPI_WEBHOOK_SECRET) and provided == VAPI_WEBHOOK_SECRET


def _send_tool_response(body):
    tool_calls = (body.get("message") or {}).get("toolCalls") or []
    if not tool_calls:
        tool_calls = [{"id": "manual-test",
                       "function": {"name": "sendText", "arguments": body}}]

    results = []
    for call in tool_calls:
        call_id = call.get("id") or "unknown"
        args = (call.get("function") or {}).get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}

        to = normalize_phone(args.get("to") or args.get("phone"))
        message = (args.get("body") or args.get("message") or "").strip()

        if not to:
            results.append({"toolCallId": call_id, "result": f"error: invalid phone '{args.get('to')}'"})
            continue
        if not message:
            results.append({"toolCallId": call_id, "result": "error: empty body"})
            continue
        if len(message) > MAX_MESSAGE_LENGTH:
            message = message[:MAX_MESSAGE_LENGTH - 3] + "..."

        try:
            msg = twilio_client.messages.create(to=to, from_=TWILIO_FROM_NUMBER, body=message)
            results.append({"toolCallId": call_id,
                            "result": f"Text sent to {to}. SID: {msg.sid}"})
        except Exception as e:
            results.append({"toolCallId": call_id,
                            "result": f"error: Twilio rejected ({e})"})
    return {"results": results}


def _handle_end_of_call(body):
    if not twilio_client:
        return {"status": "skipped", "reason": "twilio not configured"}, 200

    msg = body.get("message") or {}
    msg_type = msg.get("type", "")

    if msg_type not in ("end-of-call-report", "status-update"):
        return {"status": "ignored", "type": msg_type}, 200
    if msg_type == "status-update" and msg.get("status") != "ended":
        return {"status": "ignored", "type": msg_type}, 200

    call = msg.get("call") or {}
    customer = msg.get("customer") or call.get("customer") or {}
    to_number = normalize_phone(customer.get("number"))

    if not to_number:
        return {"status": "skipped", "reason": "no phone number"}, 200
    if to_number in AFTER_CALL_SKIP_NUMBERS:
        return {"status": "skipped", "reason": "skip list"}, 200

    try:
        sms = twilio_client.messages.create(to=to_number, from_=TWILIO_FROM_NUMBER, body=AFTER_CALL_MESSAGE)
        return {"status": "sent", "to": to_number, "sid": sms.sid}, 200
    except Exception as e:
        # Return 200 so Vapi doesn't retry
        return {"status": "error", "error": str(e)}, 200


class handler(BaseHTTPRequestHandler):
    def _reply(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode("utf-8"))

    def do_GET(self):
        route = _route(self.path)
        if route in ("/health", ""):
            return self._reply(200, {
                "ok": True,
                "twilio_configured": twilio_client is not None,
                "from": TWILIO_FROM_NUMBER,
                "time": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            })
        return self._reply(404, {"error": "not found"})

    def do_POST(self):
        route = _route(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            body = {}

        headers = {k.lower(): v for k, v in self.headers.items()}
        if not _check_auth(headers):
            return self._reply(401, {"error": "unauthorized"})

        if route == "/sms-send":
            if not twilio_client:
                return self._reply(500, {"error": "Twilio not configured"})
            return self._reply(200, _send_tool_response(body))

        if route == "/end-of-call":
            result, status = _handle_end_of_call(body)
            return self._reply(status, result)

        return self._reply(404, {"error": f"unknown route {route}"})
