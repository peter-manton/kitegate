"""
title: Kitegate Filter
author: Kitegate project
version: 0.1.0
license: Apache-2.0
description: A safety core for children's models (distress routing, hard blocks, personal-details screening) plus a chosen values pack. Holds streamed replies until they are screened.
"""
# This is the SOURCE of the Open WebUI filter. Do not paste this file into
# Open WebUI: it imports kitegate. Paste dist/kitegate_openwebui_filter.py,
# which tools/build_owui_filter.py builds from this file with the package inlined.
#
# Install it as a GLOBAL filter (Admin Panel > Functions, enable, set Global),
# so the deny-by-default check sees every request. It acts only on the models
# listed in child_model_ids, apart from that check.
#
# Hooks and why each is used:
#   inlet    once per turn: the child's newest message, and deny-by-default
#   request  before EVERY model call, including each hop of a tool loop: tool
#            results and tool-call arguments, so a bad web snippet or caption
#            cannot reach the model's answer unscreened
#   stream   holds each streamed reply and releases it only once screened;
#            outlet runs after the child has already seen streamed text, so it
#            cannot do this job
#   outlet   the non-streamed path
#
# Limits, stated plainly:
#   - A block shows as an error in the chat, not a normal reply.
#   - Distress is answered (trusted adult + Childline) and logged, but this
#     filter cannot notify anyone. For alerts, connect Open WebUI through
#     the gateway instead (adapters/openwebui/gateway-connection.md).
#   - The bundled packs are signature-checked when this file is built. A
#     pack file of your own is checked when loaded, which needs the
#     'cryptography' package in Open WebUI's Python environment; without it,
#     a signed pack file is refused rather than loaded unchecked.
#   - If a stream ends without a finish_reason, the held text is dropped
#     rather than released unscreened.
#   - Chat titles, tags and follow-up suggestions are model-written but skip
#     function filters, so they cannot be screened here. The filter refuses
#     children's models until an admin switches them off (valve
#     require_unscreened_tasks_off).
from typing import Optional
import logging

from pydantic import BaseModel, Field

from kitegate import PolicyEngine, load_pack  # kitegate:inline
from kitegate.core import CORE_FRAMING  # kitegate:inline
from kitegate.messages import WRONG_MODEL_REPLY, Scope, content_text, tool_texts  # kitegate:inline
from kitegate.signing import PackTrust  # kitegate:inline

log = logging.getLogger("kitegate_filter")

_MAX_HELD = 256  # streams in flight; oldest dropped beyond this


class Filter:
    class Valves(BaseModel):
        priority: int = Field(default=-100, description="Run before other filters.")
        pack: str = Field(
            default="secular-neutral",
            description="Values pack: uk-school, secular-neutral, christian-family, "
                        "or the path to your own pack file (signature in <pack>.yaml.sig).",
        )
        pack_public_keys: str = Field(
            default="",
            description="Comma-separated public keys trusted to sign your own pack files.",
        )
        require_signed_packs: bool = Field(
            default=False,
            description="Refuse a pack file without a trusted signature. Bundled packs are "
                        "always signature-checked.",
        )
        child_model_ids: str = Field(
            default="kids-encyclopedia",
            description="Comma-separated model ids children use. Only these are screened.",
        )
        child_user_emails: str = Field(
            default="",
            description="Comma-separated child account emails. They are refused on any other model.",
        )
        require_unscreened_tasks_off: bool = Field(
            default=True,
            description="Refuse children's models while title, tag or follow-up generation is on. "
                        "Those are model-written and this filter cannot screen them.",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()
        self._engine: Optional[PolicyEngine] = None
        self._engine_pack: Optional[tuple] = None
        self._held: dict = {}

    # -- helpers ---------------------------------------------------------
    def _eng(self) -> PolicyEngine:
        wanted = (self.valves.pack, self.valves.pack_public_keys, self.valves.require_signed_packs)
        if self._engine is None or self._engine_pack != wanted:
            trust = PackTrust.from_keys(self.valves.pack_public_keys.split(","),
                                        self.valves.require_signed_packs)
            self._engine = PolicyEngine(load_pack(self.valves.pack, trust=trust))
            self._engine_pack = wanted
        return self._engine

    def _scope(self) -> Scope:
        # Built per call: valves can change at any time from the admin panel.
        return Scope(self.valves.child_model_ids, self.valves.child_user_emails)

    def _is_child_model(self, model_id: Optional[str]) -> bool:
        return self._scope().is_child_model(model_id)

    def _wrong_model(self, user: Optional[dict], model_id: Optional[str]) -> bool:
        return self._scope().wrong_model((user or {}).get("email"), model_id)

    @staticmethod
    def _model_id(body: dict, model: Optional[dict]) -> Optional[str]:
        return (model or {}).get("id") or body.get("model")

    def _block(self, verdict, where: str) -> None:
        log.warning("kitegate blocked %s tier=%s rule=%s", where, verdict.tier, verdict.rule)
        raise Exception(verdict.reply)

    # -- hooks -----------------------------------------------------------
    async def _unscreened_tasks_enabled(self, request) -> list:
        """Which model-written extras are on that this filter cannot screen.

        Chat titles, tags and follow-up suggestions are generated by separate
        model calls that bypass function filters, so their text reaches the
        child unscreened: a blocked reply can still get a title that repeats
        what was blocked.
        """
        on = []
        try:
            from open_webui.models.config import Config  # Open WebUI >= 0.11
            for key in ("task.title.enable", "task.tags.enable", "task.follow_up.enable"):
                if await Config.get(key):
                    on.append(key.split(".")[1])
            return on
        except Exception:  # nosec B110 - older Open WebUI has no Config: fall through
            pass
        cfg = getattr(getattr(getattr(request, "app", None), "state", None), "config", None)
        for attr, label in (("ENABLE_TITLE_GENERATION", "title"), ("ENABLE_TAGS_GENERATION", "tags"),
                            ("ENABLE_FOLLOW_UP_GENERATION", "follow_up")):
            if cfg is not None and getattr(cfg, attr, False):
                on.append(label)
        return on

    async def inlet(self, body: dict, __user__: Optional[dict] = None,
                    __model__: Optional[dict] = None,
                    __metadata__: Optional[dict] = None,
                    __request__=None) -> dict:
        model_id = self._model_id(body, __model__)
        if self._wrong_model(__user__, model_id):
            raise Exception(WRONG_MODEL_REPLY)
        if not self._is_child_model(model_id):
            return body

        # Fail closed: refuse to serve a children's model while unscreenable
        # generated text is switched on. A grown-up sees exactly what to fix.
        if self.valves.require_unscreened_tasks_off:
            on = await self._unscreened_tasks_enabled(__request__)
            if on:
                log.warning("kitegate: refusing, unscreened tasks enabled: %s", on)
                raise Exception(
                    "This helper is switched off until a grown-up changes a setting. "
                    "(Admin: turn off title, tag and follow-up generation in "
                    "Admin Panel > Settings > Interface. Enabled now: " + ", ".join(on) + ".)")


        # Newest child turn only. A blocked message still sits in the chat
        # history in Open WebUI, so re-screening history would block every
        # later message in that chat.
        turns = [m for m in body.get("messages") or [] if isinstance(m, dict) and m.get("role") == "user"]
        if turns:
            verdict = self._eng().screen_request([content_text(turns[-1].get("content"))])
            if not verdict.allowed:
                self._block(verdict, "request")

        # The pack's framing is the admin's job here: in Open WebUI the system
        # prompt belongs to the model entry, which this filter does not own.
        # The core framing is not a choice, so it is added anyway, ahead of
        # whatever Open WebUI supplies. Tier 4 still screens the reply, so a
        # model that ignores this is caught on the way out either way.
        msgs = [m for m in body.get("messages") or [] if isinstance(m, dict)]
        if not any(m.get("content") == CORE_FRAMING for m in msgs):
            body["messages"] = [{"role": "system", "content": CORE_FRAMING}] + msgs
        return body

    async def request(self, body: dict, __user__: Optional[dict] = None,
                      __model__: Optional[dict] = None,
                      __metadata__: Optional[dict] = None,
                      __task__: Optional[str] = None) -> dict:
        model_id = self._model_id(body, __model__)
        if self._wrong_model(__user__, model_id):
            raise Exception(WRONG_MODEL_REPLY)
        if not self._is_child_model(model_id):
            return body

        # Background tasks (chat title, tags, follow-up suggestions, query
        # generation) are separate model calls whose output never passes
        # stream() or outlet(), so it would reach the child unscreened.
        # Refuse them for children's models; the chat keeps its default
        # title and shows no suggestions. This is a second line only: Open
        # WebUI (0.11.x) runs these calls without function filters at all,
        # which is why inlet() also refuses to run until they are off.
        task = __task__ or (__metadata__ or {}).get("task")
        if task:
            raise Exception("Background tasks are disabled for children's models.")

        texts = tool_texts(body.get("messages"))
        if texts:
            verdict = self._eng().screen_response(texts)
            if not verdict.allowed:
                self._block(verdict, "tool content")
        return body

    def stream(self, event: dict, __model__: Optional[dict] = None) -> dict:
        if __model__ is not None and not self._is_child_model(__model__.get("id")):
            return event
        choices = event.get("choices") if isinstance(event, dict) else None
        if not choices:
            return event
        for choice in choices:
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            key = (event.get("id"), choice.get("index", 0))
            piece = delta.get("content")
            if piece:
                if key not in self._held and len(self._held) >= _MAX_HELD:
                    self._held.pop(next(iter(self._held)))
                self._held[key] = self._held.get(key, "") + piece
                delta["content"] = ""
            if choice.get("finish_reason"):
                text = self._held.pop(key, "")
                if text:
                    verdict = self._eng().screen_response([text])
                    delta["content"] = text if verdict.allowed else verdict.reply
                    if not verdict.allowed:
                        log.warning("kitegate blocked stream tier=%s rule=%s", verdict.tier, verdict.rule)
        return event

    async def outlet(self, body: dict, __model__: Optional[dict] = None) -> dict:
        if not self._is_child_model(self._model_id(body, __model__)):
            return body
        for m in reversed(body.get("messages") or []):
            if isinstance(m, dict) and m.get("role") == "assistant":
                text = content_text(m.get("content"))
                verdict = self._eng().screen_response([text]) if text else None
                if verdict is not None and not verdict.allowed:
                    log.warning("kitegate blocked outlet tier=%s rule=%s", verdict.tier, verdict.rule)
                    m["content"] = verdict.reply
                break
        return body
