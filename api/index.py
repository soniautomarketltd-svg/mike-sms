"""
Mike SMS — Vercel serverless (WSGI).

Same logic as before, restructured as a WSGI `app` callable so it works with
Vercel's current Python runtime. Three endpoints under /api/:

  POST /api/sms-send     — Mike's mid-call sendText tool
  POST /api/end-of-call  — Auto thank-you after any call ends
  GET  /api/health       — Sanity check

Env vars (Vercel dashboard):
  TWILIO_ACCOUNT_SID
  TWILIO_AUTH_TOKEN
  TWILIO_FROM_NUMBER
  VAPI_WEBHOOK_SECRET
"""

import os
import re
import json
from datetime import datetime

from twilio.rest import Client

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "+16397391131")
VAPI_WEBHOOK_SECRET = os.environ.get("VAPI_WEBHOOK_SECRET", "")

MAX_MESSAGE_LENGTH = 800

AFTER_CALL_MESSAGE = (
    "Thanks for calling Soni Auto Market! Browse our inventory: "
    "https://soniautomarket.com/cars. Text or call back anytime - Mike."
)

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
    p = (path or "").split("?")[0].rstrip("/").lower()
    if p.startswith("/api"):
        p = p[4:]
    return p or "/"


def _check_auth(headers):
    provided = headers.get("x-vapi-secret") or ""
    if not provided:
        bearer = headers.get("authorization") or ""
        if bearer.lower().startswith("bearer "):
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
            results.append({"toolCallId": call_id,
                            "result": f"error: invalid phone '{args.get('to')}'"})
            continue
        if not message:
            results.append({"toolCallId": call_id, "result": "error: empty body"})
            continue
        if len(message) > MAX_MESSAGE_LENGTH:
            message = message[:MAX_MESSAGE_LENGTH - 3] + "..."

        try:
            msg = twilio_client.messages.create(
                to=to, from_=TWILIO_FROM_NUMBER, body=message)
            results.append({"toolCallId": call_id,
                            "result": f"Text sent to {to}. SID: {msg.sid}"})
        except Exception as e:
            results.append({"toolCallId": call_id,
                            "result": f"error: Twilio rejected ({e})"})
    return {"results": results}


def _handle_end_of_call(body):
    if not twilio_client:
        return 200, {"status": "skipped", "reason": "twilio not configured"}

    msg = body.get("message") or {}
    msg_type = msg.get("type", "")

    if msg_type not in ("end-of-call-report", "status-update"):
        return 200, {"status": "ignored", "type": msg_type}
    if msg_type == "status-update" and msg.get("status") != "ended":
        return 200, {"status": "ignored", "type": msg_type}

    call = msg.get("call") or {}
    customer = msg.get("customer") or call.get("customer") or {}
    to_number = normalize_phone(customer.get("number"))

    if not to_number:
        return 200, {"status": "skipped", "reason": "no phone number"}
    if to_number in AFTER_CALL_SKIP_NUMBERS:
        return 200, {"status": "skipped", "reason": "skip list"}

    try:
        sms = twilio_client.messages.create(
            to=to_number, from_=TWILIO_FROM_NUMBER, body=AFTER_CALL_MESSAGE)
        return 200, {"status": "sent", "to": to_number, "sid": sms.sid}
    except Exception as e:
        return 200, {"status": "error", "error": str(e)}


STATUS_TEXT = {
    200: "200 OK",
    401: "401 Unauthorized",
    404: "404 Not Found",
    500: "500 Internal Server Error",
}


def _respond(start_response, status, body):
    payload = json.dumps(body).encode("utf-8")
    start_response(STATUS_TEXT.get(status, f"{status} OK"),
                   [("Content-Type", "application/json"),
                    ("Content-Length", str(len(payload)))])
    return [payload]


def app(environ, start_response):
    """WSGI entrypoint Vercel auto-detects."""
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "") or "/"
    route = _route(path)

    # Build lowercase headers dict
    headers = {}
    for k, v in environ.items():
        if k.startswith("HTTP_"):
            headers[k[5:].replace("_", "-").lower()] = v
    if "CONTENT_TYPE" in environ:
        headers["content-type"] = environ["CONTENT_TYPE"]

    # Read body
    body = {}
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except Exception:
        length = 0
    if length > 0:
        try:
            raw = environ["wsgi.input"].read(length)
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            body = {}

    if method == "GET":
        if route in ("/health", "/"):
            return _respond(start_response, 200, {
                "ok": True,
                "twilio_configured": twilio_client is not None,
                "from": TWILIO_FROM_NUMBER,
                "time": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            })
        return _respond(start_response, 404, {"error": "not found"})

    if method == "POST":
        if not _check_auth(headers):
            return _respond(start_response, 401, {"error": "unauthorized"})

        if route == "/sms-send":
            if not twilio_client:
                return _respond(start_response, 500, {"error": "Twilio not configured"})
            return _respond(start_response, 200, _send_tool_response(body))

        if route == "/end-of-call":
            status, result = _handle_end_of_call(body)
            return _respond(start_response, status, result)

        return _respond(start_response, 404, {"error": f"unknown route {route}"})

    return _respond(start_response, 404, {"error": "method not allowed"})
