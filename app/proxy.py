"""OpenAI-compatible proxy endpoints.

Flow per chat request:
  auth -> validate body/size -> policy admission -> atomic reserve + durable
  'pending' request row (one tx) -> forward to 9router with the MASTER key
  only -> settle from authoritative usage, or mark uncertain.

Never forward the child's key. Never put per-request state in globals:
everything rides on locals + the DB row keyed by request UUID.
"""

import json
import logging
import time
import uuid

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import billing, policies
from .auth import AuthedChild, authenticate
from .config import get_settings
from .database import session_scope
from .models import Child, RequestRecord

log = logging.getLogger("kidsproxy.proxy")
router = APIRouter(prefix="/v1")

FORWARD_PARAMS = {
    "messages", "model", "temperature", "top_p", "max_tokens",
    "max_completion_tokens", "stop", "n", "presence_penalty",
    "frequency_penalty", "response_format", "seed", "user",
    "tools", "tool_choice", "stream", "reasoning_effort",
}


def api_error(status: int, message: str, err_type: str, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type, "code": code}},
    )


async def get_child(request: Request) -> AuthedChild | None:
    async with session_scope() as s:
        return await authenticate(s, request.headers.get("authorization"))


@router.get("/models")
async def list_models(request: Request):
    child = await get_child(request)
    if child is None:
        return api_error(401, "Invalid API key", "authentication_error", "invalid_api_key")
    client: httpx.AsyncClient = request.app.state.http
    try:
        r = await client.get("/v1/models")
        ok = r.status_code == 200
        data = r.json() if ok else None
    except httpx.HTTPError:
        ok, data = False, None
    if not ok:
        return api_error(502, "Upstream model list failed", "upstream_error", "upstream_unavailable")
    async with session_scope() as sess:
        child_row = await sess.get(Child, child.child_id)
        smap = await policies.load_settings_map(sess)
    allowed = policies.effective_policy(smap, child_row).allowed_models
    if allowed:
        data["data"] = [m for m in data.get("data", []) if m.get("id") in allowed]
    return JSONResponse(data)


@router.post("/chat/completions")
async def chat_completions(request: Request):
    req_id = str(uuid.uuid4())
    child = await get_child(request)
    if child is None:
        return api_error(401, "Invalid API key", "authentication_error", "invalid_api_key")

    s = get_settings()
    body_bytes = await request.body()
    if len(body_bytes) > s.max_body_bytes:
        return api_error(413, "Request body too large", "invalid_request_error", "request_too_large")
    try:
        body = json.loads(body_bytes)
        assert isinstance(body, dict)
    except Exception:
        return api_error(400, "Malformed JSON body", "invalid_request_error", "invalid_json")
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return api_error(400, "'messages' must be a non-empty array", "invalid_request_error", "invalid_request")

    stream = bool(body.get("stream"))
    async with session_scope() as sess:
        child_row = await sess.get(Child, child.child_id)
        if child_row is None or not child_row.active:
            return api_error(401, "Account inactive", "authentication_error", "account_inactive")
        rates = await billing.get_rates(sess)
        verdict = await policies.check_admission(sess, child_row, body, rates)
        if isinstance(verdict, str):
            code, _, msg = verdict.partition("|")
            status = 402 if code == "insufficient_balance" else 403 if code != "invalid_request" else 400
            msg += " (ask a parent to adjust credits or policy)" if code in ("insufficient_balance", "daily_limit_reached", "monthly_limit_reached") else ""
            return api_error(status, msg, "insufficient_quota" if code == "insufficient_balance" else "invalid_request_error", code)
        policy, reserve_amt = verdict
        sess.add(
            RequestRecord(
                id=req_id, child_id=child.child_id, key_id=child.key_id,
                model=body["model"], streamed=int(stream), status="pending",
                created_at=billing.now_iso(), reserved_micro=reserve_amt,
            )
        )
        try:
            await billing.reserve(sess, child.child_id, req_id, reserve_amt)
        except billing.NotFunded:
            await sess.rollback()
            return api_error(402, "Insufficient balance to reserve this request. Ask a parent to add credits.",
                             "insufficient_quota", "insufficient_balance")
    # tx committed: request row + reservation durable before upstream contact.

    fwd_body = {k: v for k, v in body.items() if k in FORWARD_PARAMS}
    try:
        fwd_body["max_tokens"] = min(int(body.get("max_tokens") or policy.max_output_tokens),
                                     policy.max_output_tokens)
    except (TypeError, ValueError):
        fwd_body["max_tokens"] = policy.max_output_tokens
    if stream:
        # Ask for a final usage chunk; harmless if ignored upstream.
        fwd_body["stream_options"] = {"include_usage": True}
    else:
        fwd_body["stream"] = False  # 9router defaults to SSE when omitted
    headers = {"content-type": "application/json", "x-request-id": req_id}
    if s.upstream_api_key:  # master key, server-side only; child keys never forwarded
        headers["authorization"] = f"Bearer {s.upstream_api_key}"
    client: httpx.AsyncClient = request.app.state.http
    t0 = time.monotonic()

    if not stream:
        return await _nonstream(request, client, headers, fwd_body, req_id, child, t0)
    return await _stream(request, client, headers, fwd_body, req_id, child, t0)


async def _finish_error(req_id: str, status: int, message: str, code: str, release: bool) -> JSONResponse:
    async with session_scope() as sess:
        if release:
            await billing.release_reservation(sess, req_id, reason=f"pre-upstream error {code}")
            await sess.execute(
                RequestRecord.__table__.update()
                .where(RequestRecord.id == req_id)
                .values(status="upstream_error", error=message[:2000], settled_at=billing.now_iso())
            )
        else:
            await billing.mark_uncertain(sess, req_id, message)
    return api_error(status, message, "upstream_error", code)


async def _nonstream(request, client, headers, fwd_body, req_id, child, t0) -> JSONResponse:
    try:
        r = await client.post("/v1/chat/completions", json=fwd_body, headers=headers)
    except httpx.TimeoutException:
        # Upstream may or may not have processed it: uncertain, don't assume zero.
        return await _finish_error(req_id, 504, "Upstream timeout", "upstream_timeout", release=False)
    except httpx.HTTPError as e:
        return await _finish_error(req_id, 502, f"Upstream unreachable: {type(e).__name__}", "upstream_unavailable", release=True)

    if r.status_code != 200:
        # Upstream rejected before generation -> no cost -> release.
        log.warning("req=%s child=%s upstream_status=%d", req_id, child.child_id, r.status_code)
        return await _finish_error(req_id, r.status_code, f"Upstream returned {r.status_code}", "upstream_error", release=True)

    try:
        data = r.json()
    except Exception:
        return await _finish_error(req_id, 502, "Upstream returned invalid JSON", "upstream_bad_response", release=False)

    usage = billing.extract_usage(data)
    if usage is None:
        # Delivered but unbilled-able: persist uncertain; never silently zero.
        await _finish_error(req_id, 502, "Upstream omitted usage metadata; request marked uncertain", "usage_unavailable", release=False)
        return JSONResponse(
            data,
            headers={"x-request-id": req_id, "x-kidsproxy-billing": "uncertain"},
        )

    async with session_scope() as sess:
        rates = await billing.get_rates(sess)
        try:
            req = await billing.settle(sess, req_id, child.child_id, usage, rates)
        except (RuntimeError, billing.UnknownChild) as e:
            log.error("req=%s settlement failed: %s", req_id, e)
            return JSONResponse(
                data,
                headers={"x-request-id": req_id, "x-kidsproxy-billing": "settlement-failed"},
            )
        charge = req.charge_micro
    log.info("req=%s child=%s model=%s stream=0 dur=%.2f in=%d out=%d cached=%d charge_micro=%d",
             req_id, child.child_id, fwd_body["model"], time.monotonic() - t0,
             usage["prompt_tokens"], usage["completion_tokens"], usage["cached_tokens"], charge)
    return JSONResponse(data, headers={"x-request-id": req_id})


async def _stream(request, client, headers, fwd_body, req_id, child, t0):
    req = client.build_request("POST", "/v1/chat/completions", json=fwd_body, headers=headers)
    try:
        upstream = await client.send(req, stream=True)
    except httpx.HTTPError as e:
        return await _finish_error(req_id, 502, f"Upstream unreachable: {type(e).__name__}", "upstream_unavailable", release=True)

    if upstream.status_code != 200:
        detail = (await upstream.aread())[:500]
        await upstream.aclose()
        return await _finish_error(req_id, upstream.status_code, f"Upstream error: {detail.decode(errors='replace')}", "upstream_error", release=True)

    async def relay():
        usage: dict | None = None
        line_buf = b""
        done_seen = False
        outcome = "ok"
        try:
            async for chunk in upstream.aiter_raw():  # passthrough; no full buffering
                line_buf += chunk
                *lines, line_buf = line_buf.split(b"\n")
                for line in lines:
                    if not line.startswith(b"data: "):
                        continue
                    payload = line[6:].strip()
                    if payload == b"[DONE]":
                        done_seen = True
                    else:
                        try:
                            obj = json.loads(payload)
                        except Exception:
                            continue
                        u = billing.extract_usage(obj)
                        if u is not None:
                            usage = u
                yield chunk
        except httpx.TimeoutException:
            outcome = "timeout"
        except (httpx.HTTPError, Exception) as e:
            outcome = f"stream_error:{type(e).__name__}"
        finally:
            await upstream.aclose()

        if usage is None and outcome == "ok" and not done_seen:
            outcome = "truncated"  # ended without [DONE] or usage

        if usage is not None:
            try:
                async with session_scope() as sess:
                    rates = await billing.get_rates(sess)
                    await billing.settle(sess, req_id, child.child_id, usage, rates)
                log.info("req=%s child=%s model=%s stream=1 dur=%.2f usage=%s",
                         req_id, child.child_id, fwd_body["model"], time.monotonic() - t0, usage)
            except Exception as e:  # delivery succeeded; billing receipt failed
                log.error("req=%s delivered but settlement failed: %s", req_id, e)
        else:
            try:
                async with session_scope() as sess:
                    await billing.mark_uncertain(sess, req_id, f"stream ended without usage ({outcome})")
            except Exception as e:
                log.error("req=%s failed to persist uncertain state: %s", req_id, e)
            log.warning("req=%s child=%s stream uncertain outcome=%s", req_id, child.child_id, outcome)

    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        headers={"x-request-id": req_id, "cache-control": "no-cache", "x-accel-buffering": "no"},
    )
