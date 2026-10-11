"""The Kitegate gateway: an OpenAI-compatible endpoint that enforces the policy.

Every front end (Open WebUI, LibreChat, the hosted service, anything that
speaks the OpenAI chat API) points here. For a children's model it:

  1. refuses a known child on any other model (deny-by-default by identity)
  2. screens the child's newest message, then tool results and earlier
     model turns in the history
  3. asks upstream (a model, or NeMo Guardrails) for the WHOLE reply, offering
     the gateway's own find_photo tool and running it when the model asks
     (phototool.py), so a front end needs no tool installed
  4. translates a NeMo rail block into the pack's wording
  5. screens the reply and its tool-call arguments
  6. returns it, replaying it as a stream if the client asked for one

Blocks come back as a normal assistant message in the pack's words, with a
machine-readable "kitegate" verdict alongside, so a front end shows a reply
rather than an error and a service can record what happened.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .. import PolicyEngine, load_pack
from ..blocklog import BlockLog
from ..engine import PASS, Verdict
from ..messages import (ERROR_REPLY, NO_UPLOADS_REPLY, WRONG_MODEL_REPLY, Scope, content_text,
                        email_from_headers, tool_call_arguments, tool_texts)
from ..photos import image_hosts_for
from . import payloads
from .config import GatewaySettings
from .imagescan import scanner_for
from .phototool import MAX_ROUNDS, TOOL_NAME, PhotoTool

# What the model is told when a photo caption fails the text screen. The
# question is not blocked for it: the child asked about something fine, and
# the picture's own text is what fell short.
UNSUITABLE_PHOTO_REPLY = "No suitable photograph was found. Answer in words instead."

logger = logging.getLogger("kitegate.gateway")

PACK_HEADER = "x-kitegate-pack"


def _error(status: int, message: str, code: str) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": "kitegate_error", "code": code}},
                        status_code=status)


def create_app(settings: GatewaySettings, transport: Optional[httpx.AsyncBaseTransport] = None) -> FastAPI:
    scope = Scope(settings.child_models.keys(), settings.child_users)
    block_log = BlockLog(settings.block_log)
    engines: Dict[str, PolicyEngine] = {}
    for name in settings.packs:  # load every pack now: a bad pack fails at start, not mid-chat
        engines[name.lower()] = PolicyEngine(load_pack(name))  # requests look packs up lower-cased
    client = httpx.AsyncClient(transport=transport, timeout=settings.timeout, follow_redirects=False)
    scanner = scanner_for(settings, client)
    pending: set = set()

    # Say out loud what the picture policy is. A safety setting an adult cannot
    # observe is one they cannot trust, and the two permissive values here are
    # exactly the ones someone would want to find in a log before handing the
    # service to a child.
    logger.info("kitegate: pictures policy=%s hosts=%s uploads=%s",
                settings.image_policy, ",".join(sorted(settings.image_hosts)) or "any",
                "allowed" if settings.allow_uploads else "refused")
    if settings.image_policy == "allow":
        logger.warning("kitegate: KITEGATE_IMAGE_POLICY=allow, so pictures reach children UNCHECKED")
    if settings.image_policy == "scan" and not settings.image_hosts:
        logger.warning("kitegate: KITEGATE_IMAGE_HOSTS=* so pictures from any host are scanned; "
                       "a passing picture from an uncurated source is still shown")
    if settings.image_policy == "block" and not settings.image_scan_api_key:
        # Not a warning: removing pictures is the safe outcome. But it is the
        # one that looks like a bug to an adult expecting photographs, so the
        # reason and the fix go in the log.
        logger.info("kitegate: no KITEGATE_IMAGE_SCAN_API_KEY, so pictures are removed from "
                    "replies. Set one to show pictures that pass a moderation check")
    if settings.allow_uploads:
        logger.warning("kitegate: KITEGATE_IMAGE_UPLOADS is on. Children's own pictures are forwarded "
                       "to the model and are NOT screened")

    # The photo tool. Run here rather than in a front end, so no front end
    # needs anything installed and every one gets the same, screened tool.
    photo_tool = PhotoTool(settings.photo_providers, client) if settings.photos_enabled else None
    # Upstream models that refused a request carrying the tool (no tool
    # calling). They are asked without it from then on, so a model that
    # cannot use tools still answers every question in words.
    no_tools: set = set()
    if photo_tool is not None:
        logger.info("kitegate: photo tool on for children's models, providers=%s",
                    ",".join(settings.photo_providers))
        if settings.image_policy == "scan" and settings.image_hosts:
            missing = [h for h in image_hosts_for(settings.photo_providers)
                       if h not in settings.image_hosts]
            if missing:
                logger.warning("kitegate: KITEGATE_IMAGE_HOSTS leaves out %s, so photographs from "
                               "the photo tool there are always removed", ",".join(missing))
    elif settings.photo_providers:
        logger.info("kitegate: photo tool off, because pictures are removed from replies")

    app = FastAPI(title="kitegate", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.photo_tool = photo_tool
    app.router.add_event_handler("shutdown", client.aclose)

    def authorised(request: Request) -> bool:
        header = request.headers.get("authorization") or ""
        token = header[7:].strip() if header[:7].lower() == "bearer " else ""
        return bool(token) and hmac.compare_digest(token.encode(), settings.api_key.encode())

    def upstream_headers(key: str) -> Dict[str, str]:
        # Only these: the child's identity and the client's key never go upstream.
        headers = {"content-type": "application/json"}
        if key:
            headers["authorization"] = f"Bearer {key}"
        return headers

    async def alert(verdict: Verdict, email: Optional[str], asked: str) -> None:
        if not settings.alert_webhook:
            return
        event = {"event": "distress", "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "user": email or "unknown", "rule": verdict.rule, "asked": asked}
        try:
            r = await client.post(settings.alert_webhook, json=event, timeout=10)
            if r.status_code >= 400:
                logger.error("kitegate: alert webhook returned HTTP %s", r.status_code)
        except Exception:  # an alert failure must never change what the child sees
            logger.exception("kitegate: alert webhook failed")

    def blocked(verdict: Verdict, stage: str, alias: str, stream: bool, include_usage: bool,
                email: Optional[str], asked: str) -> Response:
        logger.warning("kitegate blocked stage=%s tier=%s rule=%s", stage, verdict.tier, verdict.rule)
        block_log.record(email, stage, verdict, asked)
        if verdict.needs_adult_alert:
            task = asyncio.ensure_future(alert(verdict, email, asked))
            pending.add(task)  # keep a reference until it finishes
            task.add_done_callback(pending.discard)
        body = payloads.reply_completion(verdict.reply or ERROR_REPLY, alias,
                                         payloads.verdict_info(verdict, stage))
        return respond(body, stream, include_usage)

    def respond(body: dict, stream: bool, include_usage: bool) -> Response:
        if stream:
            return StreamingResponse(payloads.sse_replay(body, include_usage),
                                     media_type="text/event-stream")
        return JSONResponse(body)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/v1/models")
    async def models(request: Request) -> Response:
        if not authorised(request):
            return _error(401, "Invalid or missing API key.", "unauthorised")
        data = [{"id": alias, "object": "model", "owned_by": "kitegate"} for alias in settings.child_models]
        if settings.passthrough:
            try:
                r = await client.get(f"{settings.passthrough_url}/models",
                                     headers=upstream_headers(settings.passthrough_api_key))
                if r.status_code == 200:
                    data += [m for m in (r.json().get("data") or [])
                             if isinstance(m, dict) and not scope.is_child_model(m.get("id"))]
            except Exception:
                logger.warning("kitegate: could not list passthrough models", exc_info=True)
        return JSONResponse({"object": "list", "data": data})

    async def passthrough(raw: bytes, stream: bool) -> Response:
        url = f"{settings.passthrough_url}/chat/completions"
        req = client.build_request("POST", url, content=raw,
                                   headers=upstream_headers(settings.passthrough_api_key))
        try:
            r = await client.send(req, stream=stream)
        except httpx.HTTPError:
            logger.exception("kitegate: passthrough upstream failed")
            return _error(502, "The model is not reachable right now.", "upstream_unavailable")
        media = r.headers.get("content-type", "application/json")
        if stream:
            return StreamingResponse(r.aiter_raw(), status_code=r.status_code, media_type=media,
                                     background=_Close(r))
        return Response(r.content, status_code=r.status_code, media_type=media)

    async def post_upstream(outbound: dict) -> Optional[httpx.Response]:
        """One children's-model call. None if the upstream could not be reached."""
        try:
            return await client.post(f"{settings.upstream_url}/chat/completions", json=outbound,
                                     headers=upstream_headers(settings.upstream_api_key))
        except httpx.HTTPError:
            logger.exception("kitegate: upstream failed")
            return None

    def upstream_failure(r: Optional[httpx.Response]) -> Optional[Response]:
        if r is None:
            return _error(502, "The model is not reachable right now.", "upstream_unavailable")
        if r.status_code != 200:
            # The upstream body is not passed on: it may quote the request.
            logger.error("kitegate: upstream returned HTTP %s", r.status_code)
            return _error(502, "The model could not answer right now.", "upstream_error")
        return None

    async def photo_rounds(outbound: dict, data: dict, engine: PolicyEngine, client_tools: set
                           ) -> Tuple[Optional[dict], Optional[Verdict], Optional[Response]]:
        """Run the model's find_photo calls until it answers in words.

        Returns (reply, None, None), or a block verdict for the caller to
        answer with, or an upstream error response. Nothing from an
        intermediate turn reaches the child: only the final reply does, and
        it is screened like any other.
        """
        usage = payloads.add_usage(None, data)
        for round_no in range(MAX_ROUNDS + 1):
            stray = payloads.stray_tool_call(data)
            if stray is not None:
                name, args = stray
                if name == TOOL_NAME and round_no < MAX_ROUNDS:
                    # The right call, written in the wrong place. Run it.
                    data = payloads.as_tool_call(data, name, args)
                else:
                    # A made-up tool, or one call too many, written as the
                    # answer: the tool being on offer is what confused the
                    # model, so ask once more with no tools at all.
                    logger.info("kitegate: model answered with a bare tool call (%s); "
                                "asking again without tools", name)
                    r = await post_upstream(payloads.in_words(outbound))
                    failure = upstream_failure(r)
                    if failure is not None:
                        return None, None, failure
                    data = r.json()
                    usage = payloads.add_usage(usage, data)
                    if payloads.stray_tool_call(data) is not None:
                        return None, Verdict(False, "error", "photo.unfinished", ERROR_REPLY), None
                    break
            calls = payloads.own_tool_calls(data, TOOL_NAME)
            if not calls:
                break
            # The arguments are the model's output, so they get the response
            # screen before any of them leaves the gateway, exactly as they
            # would on the way to a client that ran the tool itself.
            verdict = engine.screen_response(tool_call_arguments(calls))
            if not verdict.allowed:
                return None, verdict, None
            if any((c.get("function") or {}).get("name") in client_tools for c in calls):
                # The model also called one of the client's tools, which only
                # the client can run. Hand the turn back with the gateway's own
                # calls taken out; the tool is offered again on the next turn.
                data = payloads.drop_tool_calls(data, TOOL_NAME)
                break
            if round_no == MAX_ROUNDS:
                # Told to answer in words (tool_choice none) and asked anyway.
                data = payloads.drop_tool_calls(data, TOOL_NAME)
                if not payloads.response_texts(data):
                    return None, Verdict(False, "error", "photo.unfinished", ERROR_REPLY), None
                break
            results = await photo_tool.answer(calls)
            for result in results:
                # A caption is text the model may repeat to the child. One
                # that fails becomes "nothing suitable" rather than a block.
                if not engine.screen_response([result["content"]]).allowed:
                    result["content"] = UNSUITABLE_PHOTO_REPLY
            outbound = payloads.after_tool_round(outbound, data, results)
            if round_no + 1 == MAX_ROUNDS:
                outbound["tool_choice"] = "none"
            r = await post_upstream(outbound)
            failure = upstream_failure(r)
            if failure is not None:
                return None, None, failure
            data = r.json()
            usage = payloads.add_usage(usage, data)
        if usage is not None:
            data = {**data, "usage": usage}
        return data, None, None

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> Response:
        if not authorised(request):
            return _error(401, "Invalid or missing API key.", "unauthorised")
        raw = await request.body()
        try:
            body = json.loads(raw)
        except ValueError:
            return _error(400, "Request body must be JSON.", "invalid_json")
        if not isinstance(body, dict) or not body.get("model"):
            return _error(400, "A model is required.", "model_required")

        model = str(body["model"])
        stream = bool(body.get("stream"))
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        email = email_from_headers(request.headers)
        if settings.passthrough and not email:
            # With adult traffic flowing through, a missing identity is a
            # misconfigured front end, and a child could slip through it.
            return _error(400, "This endpoint needs the user's identity header (x-kitegate-user-email).",
                          "identity_required")

        if scope.wrong_model(email, model):
            verdict = Verdict(False, "policy", "policy.wrong_model", WRONG_MODEL_REPLY)
            return blocked(verdict, "policy", model, stream, include_usage, email, "")
        if not scope.is_child_model(model):
            if not settings.passthrough:
                return _error(404, f"The model '{model}' is not available here.", "model_not_found")
            return await passthrough(raw, stream)

        alias = model
        pack = (request.headers.get(PACK_HEADER) or settings.pack).strip().lower()
        if pack not in engines:
            return _error(400, f"Pack '{pack}' is not enabled on this gateway.", "pack_not_allowed")
        engine = engines[pack]
        messages = [m for m in (body.get("messages") or []) if isinstance(m, dict)]
        asked = payloads.newest_user_text(messages)
        uploaded = payloads.newest_user_images(messages)

        try:
            # Pictures the CHILD sent. Refused by default, and refused before
            # the text tiers so the reason given is the real one.
            #
            # This also closes a hole rather than only adding a feature: a turn
            # carrying a picture and no words produced asked == "" and took the
            # `else PASS` branch below, so it was forwarded upstream with
            # nothing screened at all. A question photographed or screenshotted
            # into an image therefore bypassed every text tier, which is the
            # ordinary way a filter like this gets walked around.
            if uploaded and not settings.allow_uploads:
                verdict = Verdict(False, "image", "image.upload_refused", NO_UPLOADS_REPLY)
                return blocked(verdict, "request", alias, stream, include_usage, email, asked)
            verdict = engine.screen_request([asked]) if asked else PASS
            if not verdict.allowed:
                return blocked(verdict, "request", alias, stream, include_usage, email, asked)

            def history_rule(m: dict) -> Optional[str]:
                # The same tests the turn faced when it was the newest, so a
                # turn refused then is not answered now.
                if payloads.newest_user_images([m]) and not settings.allow_uploads:
                    return "image.upload_refused"
                text = content_text(m.get("content"))
                v = engine.screen_request([text]) if text else PASS
                return None if v.allowed else (v.rule or v.tier)

            messages, redacted = payloads.redact_history(messages, history_rule)
            if redacted:
                logger.info("kitegate: removed %d earlier turn(s) from the history: %s",
                            len(redacted), ",".join(redacted))
            verdict = engine.screen_response(tool_texts(messages) + payloads.assistant_history(messages))
            if not verdict.allowed:
                return blocked(verdict, "history", alias, stream, include_usage, email, asked)
            outbound = payloads.upstream_body(
                {**body, "messages": messages},
                upstream_model=settings.child_models[alias.lower()], engine=engine,
                inject_framing=settings.inject_framing, kind=settings.upstream_kind,
                nemo_config_id=settings.nemo_config_id, client_system=settings.client_system)
        except Exception:  # fail closed
            logger.exception("kitegate screening failed; blocking")
            return blocked(Verdict(False, "error", "error", ERROR_REPLY), "error", alias, stream,
                           include_usage, email, asked)

        upstream_model = outbound["model"]
        use_tool = (photo_tool is not None and payloads.offers_own_tool(body, TOOL_NAME)
                    and upstream_model not in no_tools)
        client_tools = set(payloads.tool_names(body))
        if use_tool:
            outbound = payloads.with_tool(outbound, photo_tool.spec)
        r = await post_upstream(outbound)
        if use_tool and r is not None and r.status_code in (400, 422):
            # Most likely a model without tool calling. Answer in words
            # rather than failing every question, and stop offering it.
            logger.warning("kitegate: upstream model %s refused the photo tool (HTTP %s), so it "
                           "answers without pictures from now on. Use a model with tool calling, "
                           "or set KITEGATE_PHOTOS=off", upstream_model, r.status_code)
            no_tools.add(upstream_model)
            use_tool = False
            outbound = payloads.without_own_tool(outbound, TOOL_NAME)
            r = await post_upstream(outbound)
        failure = upstream_failure(r)
        if failure is not None:
            return failure

        try:
            data = r.json()
            if use_tool:
                data, verdict, failure = await photo_rounds(outbound, data, engine, client_tools)
                if failure is not None:
                    return failure
                if verdict is not None:
                    stage = "error" if verdict.tier == "error" else "response"
                    return blocked(verdict, stage, alias, stream, include_usage, email,
                                   "(model reply)")
            if settings.upstream_kind == "nemo":
                verdict = payloads.nemo_verdict(data, engine)
                if verdict is not None:
                    return blocked(verdict, "classifier", alias, stream, include_usage, email, asked)
            verdict = engine.screen_response(payloads.response_texts(data))
            if not verdict.allowed:
                return blocked(verdict, "response", alias, stream, include_usage, email, "(model reply)")
            info = payloads.verdict_info(PASS, "pass")
            clean = payloads.clean_response(data, alias, info)

            # Pictures the service would SHOW the child. Runs after the text
            # screen so a reply that was going to be blocked anyway costs no
            # scan, and only when the reply actually contains a picture, so
            # ordinary answers are untouched and make no extra call.
            #
            # A failing picture is REMOVED and the words are kept, which is the
            # one place this project redacts rather than blocks. Refusing a
            # question about Rome because its lead image scored above the
            # violence threshold would read to a child as "you asked something
            # wrong", when the honest outcome is that there is no good picture.
            #
            # Deliberately NOT an adult alert, whatever the category: this is a
            # picture the service chose to fetch, not something the child
            # disclosed. Distress stays about the child's own words.
            if scanner is not None:
                pictures = payloads.response_images(clean)
                if pictures:
                    removed = await scanner.check(pictures)
                    if removed:
                        scanner.log_removals(removed)
                        clean = payloads.strip_response_images(clean, removed)
                        block_log.record(email, "image",
                                         Verdict(False, "image", sorted(removed.values())[0]), asked)
                    info["images"] = payloads.image_info(len(pictures) - len(removed), removed)
        except Exception:  # fail closed
            logger.exception("kitegate response screening failed; blocking")
            return blocked(Verdict(False, "error", "error", ERROR_REPLY), "error", alias, stream,
                           include_usage, email, asked)
        return respond(clean, stream, include_usage)

    return app


class _Close:
    """Starlette background task that closes a streamed upstream response."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    async def __call__(self) -> None:
        await self.response.aclose()
