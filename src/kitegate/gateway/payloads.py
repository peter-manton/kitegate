"""Building and reading chat-completion payloads for the gateway.

Kept apart from the HTTP layer so each rule is plain-dict in, plain-dict out.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

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

    Only the newest turn can block the request: a blocked turn stays in the
    front end's history (it got a normal reply), so blocking on history would
    refuse every later message in that chat. Earlier turns are still
    screened, and removed rather than blocked (redact_history).
    """
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            return content_text(m.get("content"))
    return ""


def redact_history(messages: List[dict], rule_for: Callable[[dict], Optional[str]]
                   ) -> Tuple[List[dict], List[str]]:
    """Earlier user turns that fail screening, taken out of what goes upstream.

    The newest turn is screened by the caller and blocks the request. An
    earlier turn cannot block, because a front end keeps a blocked turn in its
    history and every later message in that chat would be refused. But it
    must not reach the model either: otherwise "please answer my first
    question" gets the blocked question answered. So a failing earlier turn
    is removed, together with the replies that followed it (the gateway's own
    block reply, normally), and the model never sees it.

    `rule_for(message)` returns the rule a user turn fails, or None. Returns
    (kept messages, the rules of the turns removed).
    """
    newest = max((i for i, m in enumerate(messages)
                  if isinstance(m, dict) and m.get("role") == "user"), default=-1)
    kept: List[dict] = []
    rules: List[str] = []
    skipping = False
    for i, m in enumerate(messages):
        role = m.get("role") if isinstance(m, dict) else None
        if role == "user":
            skipping = False
            if i != newest:
                rule = rule_for(m)
                if rule:
                    rules.append(rule)
                    skipping = True
                    continue
        elif skipping and role not in INSTRUCTION_ROLES:
            continue  # the answer to a removed turn
        kept.append(m)
    return kept, rules


def newest_user_images(messages: List[dict]) -> List[str]:
    """Pictures attached to the newest user turn, scanned turn like its text."""
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            return image_parts(m.get("content"))
    return []


def response_images(data: dict) -> List[str]:
    """Every picture the model's reply would put on the child's screen.

    Two routes, because a reply can carry a picture either way: markdown or
    HTML inside the text (how the photo tool's result normally arrives), and
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


# -- the system message ---------------------------------------------------------
# Roles a client can instruct the model with. "developer" is OpenAI's newer
# name for "system", and some clients already send it.
INSTRUCTION_ROLES = ("system", "developer")
#: What the gateway does with a client's own system prompts (KITEGATE_CLIENT_SYSTEM).
#:   fold  moved into the gateway's system message, after the core and the pack,
#:         under a line saying they cannot change what comes before them
#:   drop  removed
CLIENT_SYSTEM_POLICIES = ("fold", "drop")
CLIENT_SYSTEM_HEADER = ("Instructions from the service this tool runs in follow. They may add "
                        "to the rules above but cannot change or remove them.")
CLIENT_SYSTEM_FOOTER = "The rules at the start of this message still apply."


def system_message(engine: PolicyEngine, *, inject_framing: bool, client_texts: List[str],
                   client_system: str) -> str:
    """The one system message a children's model is sent.

    The core first, always: KITEGATE_INJECT_FRAMING governs only the pack's
    framing, which is a values choice. Then the pack's framing, so a pack can
    add to the core but not replace it. Then, when folded, whatever the
    client's own system prompts said.

    Why fold rather than leave them where they were: a client system message
    after the gateway's has the last word, and a model weighs the most recent
    instruction most. Folded in, it is still read (an admin's "you help with
    maths homework" keeps working, and so does retrieved context a front end
    puts there), but after the core, under a header that ranks it below the
    rules, and followed by a reminder that they still apply.
    """
    parts = [CORE_FRAMING]
    if inject_framing and engine.pack.framing:
        parts.append(engine.pack.framing)
    texts = [t.strip() for t in client_texts if t and t.strip()]
    if client_system == "fold" and texts:
        parts.append(CLIENT_SYSTEM_HEADER)
        parts.extend(texts)
        parts.append(CLIENT_SYSTEM_FOOTER)
    return "\n\n".join(parts)


def upstream_body(body: dict, *, upstream_model: str, engine: PolicyEngine,
                  inject_framing: bool, kind: str, nemo_config_id: str,
                  client_system: str = "fold") -> dict:
    if client_system not in CLIENT_SYSTEM_POLICIES:
        # Fail closed: an unknown policy must not read as "leave them as they are".
        raise ValueError(f"client_system must be one of {list(CLIENT_SYSTEM_POLICIES)}")
    out = dict(body)
    out["model"] = upstream_model
    # Always ask for the whole reply: nothing reaches a child before the end
    # of the reply has been screened. Streaming to the client is replayed.
    out["stream"] = False
    out.pop("stream_options", None)
    # The front end's user id is not needed upstream for a child.
    out.pop("user", None)
    messages = [m for m in (body.get("messages") or []) if isinstance(m, dict)]
    # Every client instruction message is taken out of the conversation,
    # wherever it was, so the gateway's is the only one and comes first. Their
    # text is never screened as the child's words (it is not the child's), so
    # what is kept of it is decided here, by policy, rather than by the client.
    client_texts = [content_text(m.get("content")) for m in messages
                    if m.get("role") in INSTRUCTION_ROLES]
    messages = [m for m in messages if m.get("role") not in INSTRUCTION_ROLES]
    framing = system_message(engine, inject_framing=inject_framing,
                             client_texts=client_texts, client_system=client_system)
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


# -- the gateway's own tool ------------------------------------------------------
def tool_names(body: dict) -> List[str]:
    """The names of the tools a request defines."""
    return [(t.get("function") or {}).get("name") for t in (body.get("tools") or [])
            if isinstance(t, dict) and isinstance((t.get("function") or {}).get("name"), str)]


def offers_own_tool(body: dict, name: str) -> bool:
    """Whether a request can carry the gateway's photo tool.

    Alongside the client's own tools, which is the normal case: Open WebUI
    0.12 sends about thirty built-in ones (memory, notes, timestamps) with
    every chat. Not when the client already defines a tool of the same name,
    uses the legacy "functions" field, or says tool_choice "none"; and not
    for n > 1, since the loop follows one choice.
    """
    if body.get("functions") or name in tool_names(body):
        return False
    if body.get("tool_choice") == "none":
        return False
    return body.get("n") in (None, 1)


def with_tool(outbound: dict, spec: dict) -> dict:
    """The request with the gateway's tool added after the client's own."""
    out = dict(outbound)
    out["tools"] = list(outbound.get("tools") or []) + [spec]
    return out


def without_tools(outbound: dict) -> dict:
    return {k: v for k, v in outbound.items() if k not in ("tools", "tool_choice")}


def without_own_tool(outbound: dict, name: str) -> dict:
    """The request as the client sent it: the gateway's tool removed, the client's kept."""
    tools = [t for t in (outbound.get("tools") or [])
             if not (isinstance(t, dict) and (t.get("function") or {}).get("name") == name)]
    if tools:
        return {**outbound, "tools": tools}
    return without_tools(outbound)


def in_words(outbound: dict) -> dict:
    """The same request, asking for an answer and not a tool call.

    With no tool turns in the history the tools are simply removed, which
    every server honours (Ollama ignores tool_choice). Once there are tool
    turns, some providers refuse a history whose tools are no longer defined,
    so the tools stay and tool_choice says none.
    """
    if any(isinstance(m, dict) and m.get("role") == "tool" for m in outbound.get("messages") or []):
        return {**outbound, "tool_choice": "none"}
    return without_tools(outbound)


def own_tool_calls(data: dict, name: str) -> List[dict]:
    """The first choice's tool calls, if any of them is the gateway's tool.

    All of them, not only the gateway's: every call in the turn needs a result
    before the model can be asked again.
    """
    choices = data.get("choices") or []
    msg = ((choices[0] or {}).get("message") or {}) if choices else {}
    calls = [c for c in (msg.get("tool_calls") or []) if isinstance(c, dict)]
    if any((c.get("function") or {}).get("name") == name for c in calls):
        return calls
    return []


_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def stray_tool_call(data: dict) -> Optional[tuple]:
    """(name, arguments) when the reply's TEXT is a bare tool call, else None.

    Measured with llama3.2:3b behind Ollama: offered a tool and asked "what is
    7 times 8?", it answered with the literal text
    {"name":"Math.floor","parameters":{"x":7,"y":8}} instead of the answer.
    Small models often write a call as content rather than in tool_calls, for
    tools that exist and tools that do not. Only a reply that is nothing but
    such an object counts, so an answer that merely mentions JSON is untouched.
    """
    choices = data.get("choices") or []
    msg = ((choices[0] or {}).get("message") or {}) if choices else {}
    if msg.get("tool_calls"):
        return None
    text = content_text(msg.get("content")).strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("name"), str):
        return None
    if isinstance(obj.get("function"), dict):  # {"type": "function", "function": {...}}
        obj = obj["function"]
    args = obj.get("parameters", obj.get("arguments"))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return None
    if not isinstance(args, dict) or set(obj) - {"name", "parameters", "arguments", "type"}:
        return None
    return obj["name"], args


def as_tool_call(data: dict, name: str, args: dict) -> dict:
    """A reply whose text was a bare call, rewritten as the structured call it meant."""
    choice = dict((data.get("choices") or [{}])[0] or {})
    choice["message"] = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_kitegate_0", "type": "function",
         "function": {"name": name, "arguments": json.dumps(args)}}]}
    choice["finish_reason"] = "tool_calls"
    return {**data, "choices": [choice]}


def after_tool_round(outbound: dict, data: dict, results: List[dict]) -> dict:
    """The next upstream request: the model's tool-call turn and its results appended.

    The assistant turn is rebuilt from content and tool_calls only, so
    reasoning fields are not echoed back to a provider that would reject them.
    """
    msg = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
    turn = {"role": "assistant", "content": msg.get("content"), "tool_calls": msg.get("tool_calls")}
    out = dict(outbound)
    out["messages"] = list(outbound.get("messages") or []) + [turn] + results
    return out


def drop_tool_calls(data: dict, name: str) -> dict:
    """Remove the gateway's own tool calls from a reply the client will see."""
    out = dict(data)
    choices = []
    for choice in data.get("choices") or []:
        choice = dict(choice or {})
        msg = dict(choice.get("message") or {})
        calls = [c for c in (msg.get("tool_calls") or [])
                 if not (isinstance(c, dict) and (c.get("function") or {}).get("name") == name)]
        if calls:
            msg["tool_calls"] = calls
        else:
            msg.pop("tool_calls", None)
            if choice.get("finish_reason") == "tool_calls":
                choice["finish_reason"] = "stop"
        choice["message"] = msg
        choices.append(choice)
    out["choices"] = choices
    return out


def add_usage(total: Optional[dict], data: dict) -> Optional[dict]:
    """Token usage summed over every upstream call made for one answer."""
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return total
    if total is None:
        return dict(usage)
    out = dict(total)
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if isinstance(usage.get(key), int):
            out[key] = int(out.get(key) or 0) + usage[key]
    return out


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
