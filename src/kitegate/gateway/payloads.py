"""Building and reading chat-completion payloads for the gateway.

Kept apart from the HTTP layer so each rule is plain-dict in, plain-dict out.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Dict, Iterable, Iterator, List, Optional

from ..core import CORE_FRAMING, DISTRESS_REPLY
from ..engine import PolicyEngine, Verdict
from ..images import image_urls, strip_images
from ..messages import content_text, image_parts, tool_call_arguments

# Fields some models return alongside the answer. Output rails do not check
# reasoning (NeMo says so explicitly), and children do not need it, so it is
# removed rather than screened.
_REASONING_FIELDS = ("reasoning_content", "reasoning", "reasoning_details", "thinking")

# NemoGuard content-safety categories that mean "this child may be at risk",
# not "this child asked for something bad". S6 is suicide and self harm; S7
# is sexual content involving a minor, which from a child's own message is a
# possible disclosure. Those get the distress reply and an adult alert.
_NEMO_DISTRESS = re.compile(r"\bS6\b|\bS7\b|self[- ]?harm|suicid|sexual \(minor\)", re.IGNORECASE)


def verdict_info(verdict: Verdict, stage: str) -> Dict[str, Any]:
    """The machine-readable verdict returned alongside every child response."""
    return {"allowed": verdict.allowed, "stage": stage, "tier": verdict.tier,
            "rule": verdict.rule, "needs_adult_alert": verdict.needs_adult_alert}


def image_info(shown: int, removed: Dict[str, str]) -> Dict[str, Any]:
    """What happened to the pictures in a reply, for the "kitegate" block.

    Added only when a reply actually contained pictures, so the verdict object
    keeps its existing shape for the ordinary text-only case. An operator
    keeping an audit trail can see that a picture was dropped and why, which
    is otherwise invisible: the child just gets an answer with no photo.
    """
    return {"shown": shown, "removed": [{"url": u, "rule": r} for u, r in removed.items()]}


def newest_user_text(messages: List[dict]) -> str:
    """The newest user message, even if it has no text.

    Only the newest turn is screened as the child's words: a blocked turn
    stays in the front end's history (it got a normal reply), so screening
    history again would block every later message in that chat.
    """
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            return content_text(m.get("content"))
    return ""


def newest_user_images(messages: List[dict]) -> List[str]:
    """Pictures attached to the newest user turn, scanned turn like its text."""
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            return image_parts(m.get("content"))
    return []


def response_images(data: dict) -> List[str]:
    """Every picture the model's reply would put on the child's screen.

    Two routes, because a reply can carry a picture either way: markdown or
    HTML inside the text (how a photo tool's result normally arrives), and
    structured image parts in the content list.
    """
    out: List[str] = []
    for choice in data.get("choices") or []:
        msg = (choice or {}).get("message") or {}
        content = msg.get("content")
        out.extend(image_urls(content_text(content)))
        out.extend(image_parts(content))
    seen = set()
    unique = []
    for url in out:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def strip_response_images(data: dict, urls: Iterable[str]) -> dict:
    """Remove the named pictures from a reply, keeping the words.

    Returns a new dict: the gateway screens before it responds, so mutating
    the upstream payload in place would make the "what was blocked" record
    disagree with what was actually sent.
    """
    targets = {u for u in urls if u}
    if not targets:
        return data
    out = dict(data)
    choices = []
    for choice in data.get("choices") or []:
        choice = dict(choice or {})
        msg = dict(choice.get("message") or {})
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = strip_images(content, targets)
        elif isinstance(content, list):
            parts = []
            for part in content:
                if not isinstance(part, dict):
                    parts.append(part)
                    continue
                if image_parts([part]) and set(image_parts([part])) <= targets:
                    continue  # drop the picture part entirely
                part = dict(part)
                if isinstance(part.get("text"), str):
                    part["text"] = strip_images(part["text"], targets)
                parts.append(part)
            msg["content"] = parts
        choice["message"] = msg
        choices.append(choice)
    out["choices"] = choices
    return out


def assistant_history(messages: List[dict]) -> List[str]:
    """Earlier model turns. Screened again because a front end may let users edit them."""
    texts = (content_text(m.get("content")) for m in messages
             if isinstance(m, dict) and m.get("role") == "assistant")
    return [t for t in texts if t]


def upstream_body(body: dict, *, upstream_model: str, engine: PolicyEngine,
                  inject_framing: bool, kind: str, nemo_config_id: str) -> dict:
    out = dict(body)
    out["model"] = upstream_model
    # Always ask for the whole reply: nothing reaches a child before the end
    # of the reply has been screened. Streaming to the client is replayed.
    out["stream"] = False
    out.pop("stream_options", None)
    # The front end's user id is not needed upstream for a child.
    out.pop("user", None)
    messages = [m for m in (body.get("messages") or []) if isinstance(m, dict)]
    # The core framing goes in whatever the deployment has configured:
    # KITEGATE_INJECT_FRAMING governs the pack's framing, which is a values
    # choice, not the core's, which says the tool is not a person. A pack's
    # framing follows it, so a pack can add to it but cannot replace it.
    framing = CORE_FRAMING
    if inject_framing and engine.pack.framing:
        framing = f"{CORE_FRAMING}\n\n{engine.pack.framing}"
    messages = [{"role": "system", "content": framing}] + messages
    out["messages"] = messages
    # Never let a client choose or switch off NeMo rails.
    out.pop("guardrails", None)
    if kind == "nemo":
        guardrails: Dict[str, Any] = {"options": {"log": {"activated_rails": True}}}
        if nemo_config_id:
            guardrails["config_id"] = nemo_config_id
        out["guardrails"] = guardrails
    return out


def nemo_verdict(data: dict, engine: PolicyEngine) -> Optional[Verdict]:
    """A block by a NeMo rail, translated into the pack's wording.

    NeMo marks the rail that stopped generation with stop=true in
    guardrails.log.activated_rails. Without that log a NeMo refusal looks
    like an ordinary answer and is passed on as one.
    """
    log = (data.get("guardrails") or {}).get("log") or {}
    for rail in log.get("activated_rails") or []:
        if not isinstance(rail, dict) or not rail.get("stop"):
            continue
        name = str(rail.get("name") or "rail")
        rule = "nemo." + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        # Only the actions' results, e.g. {"decision": "block", "metadata":
        # {"policy_violations": ["S6"]}} (NeMo 0.24). Not llm_calls: those
        # hold the classifier prompt, which lists every category name.
        evidence = json.dumps([a.get("return_value") for a in rail.get("executed_actions") or []
                               if isinstance(a, dict)], default=str)
        if rail.get("type") == "input" and _NEMO_DISTRESS.search(evidence):
            return Verdict(False, "distress", rule,
                           DISTRESS_REPLY.format(trusted_adult=engine.pack.trusted_adult))
        return Verdict(False, "classifier", rule, engine.pack.replies.hard_block)
    return None


def response_texts(data: dict) -> List[str]:
    """Everything the model produced that could reach the child."""
    texts: List[str] = []
    for choice in data.get("choices") or []:
        msg = (choice or {}).get("message") or {}
        for key in ("content", "refusal"):
            text = content_text(msg.get(key))
            if text:
                texts.append(text)
        texts.extend(tool_call_arguments(msg.get("tool_calls")))
    return texts


def clean_response(data: dict, alias: str, info: Dict[str, Any]) -> dict:
    out = {k: v for k, v in data.items() if k != "guardrails"}
    out["model"] = alias
    choices = []
    for choice in data.get("choices") or []:
        choice = dict(choice or {})
        msg = {k: v for k, v in (choice.get("message") or {}).items() if k not in _REASONING_FIELDS}
        choice["message"] = msg
        choices.append(choice)
    out["choices"] = choices
    out["kitegate"] = info
    return out


def reply_completion(reply: str, alias: str, info: Dict[str, Any]) -> dict:
    """A normal assistant message carrying a block reply, so the child sees
    the pack's words in the chat rather than an error."""
    return {
        "id": f"chatcmpl-kitegate-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": alias,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": reply},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "kitegate": info,
    }


def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def sse_replay(completion: dict, include_usage: bool) -> Iterator[str]:
    """Replay an already-screened completion as an OpenAI stream."""
    base = {"id": completion.get("id") or f"chatcmpl-kitegate-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion.chunk",
            "created": completion.get("created") or int(time.time()),
            "model": completion.get("model")}
    choices = completion.get("choices") or []
    for n, choice in enumerate(choices):
        msg = choice.get("message") or {}
        index = choice.get("index", n)
        delta: Dict[str, Any] = {"role": "assistant"}
        if msg.get("content"):
            delta["content"] = msg["content"]
        if msg.get("refusal"):
            delta["refusal"] = msg["refusal"]
        if msg.get("tool_calls"):
            delta["tool_calls"] = [dict(call, index=i) for i, call in enumerate(msg["tool_calls"])]
        yield _sse({**base, "choices": [{"index": index, "delta": delta, "finish_reason": None}]})
        final = {**base, "choices": [{"index": index, "delta": {},
                                      "finish_reason": choice.get("finish_reason") or "stop"}]}
        if n == len(choices) - 1 and "kitegate" in completion:
            final["kitegate"] = completion["kitegate"]
        yield _sse(final)
    if include_usage and completion.get("usage"):
        yield _sse({**base, "choices": [], "usage": completion["usage"]})
    yield "data: [DONE]\n\n"
