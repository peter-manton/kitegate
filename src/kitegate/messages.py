"""Reading OpenAI-style chat payloads, shared by every adapter.

Framework-free like the engine: plain dicts in, plain strings out. Each
adapter still decides WHICH parts to screen (the Open WebUI filter and the
gateway differ on history and tool results); this module only knows how to
read them, so the adapters cannot drift apart on that.

The Open WebUI build inlines this file, so it must not use relative imports.
"""
from typing import FrozenSet, Iterable, List, Mapping, Optional, Union

WRONG_MODEL_REPLY = "This helper isn't available here. Please choose your own helper."
ERROR_REPLY = "Something went wrong, so this was stopped."
# Sending a picture is refused rather than screened: a photograph a child
# uploads may be of the child, and that must not be handed to a moderation
# service. Worded so it reads as "I can't do that" and not "you did something
# wrong", because sending a picture is a reasonable thing to try.
NO_UPLOADS_REPLY = ("I can't look at pictures, sorry. "
                    "Tell me in words what you would like to know and I'll do my best.")

# Headers a front end can use to say who is asking. The neutral one is for
# any front end (LibreChat, your own); the others are what Open WebUI sends
# with ENABLE_FORWARD_USER_INFO_HEADERS=True.
USER_EMAIL_HEADERS = ("x-kitegate-user-email", "x-openwebui-user-email", "x-open-webui-user-email")

_TEXT_PART_TYPES = ("text", "input_text", "output_text")

# Picture parts of a multimodal message. Kept SEPARATE from the text types
# above rather than folded into content_text: text screening must not change
# shape because images became visible to the policy.
_IMAGE_PART_TYPES = ("image_url", "input_image", "image")


def split_list(value: Union[str, Iterable[str], None]) -> FrozenSet[str]:
    """A comma-separated setting (or an iterable) as a lower-cased set."""
    items = (value or "").split(",") if isinstance(value, str) or value is None else value
    return frozenset(str(v).strip().lower() for v in items if str(v).strip())


def content_text(content) -> str:
    """Text of a message's content, whether a string or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(p.get("text") or "") for p in content
            if isinstance(p, dict) and p.get("type") in _TEXT_PART_TYPES
        )
    return ""


def image_parts(content) -> List[str]:
    """The image references in a message's content, as URLs or data: URIs.

    The sibling of content_text. A content list can carry pictures as well as
    words, and before this existed they were dropped silently: a message with
    an image and no text read as the empty string, so it was treated as having
    nothing to screen.

    Both spellings are accepted because the two OpenAI APIs differ:
    chat completions nests the value ({"image_url": {"url": ...}}), responses
    puts it flat ({"image_url": "..."}).
    """
    if not isinstance(content, list):
        return []
    out: List[str] = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") not in _IMAGE_PART_TYPES:
            continue
        value = part.get("image_url") or part.get("image") or part.get("url")
        if isinstance(value, dict):
            value = value.get("url")
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
    return out


def _messages(messages) -> List[dict]:
    return [m for m in (messages or []) if isinstance(m, dict)]


def user_turns(messages) -> List[str]:
    """The child's own words: role == "user" text, oldest first, empties dropped.

    Never the system prompt: it names what it forbids, so screening it would
    block every request.
    """
    texts = (content_text(m.get("content")) for m in _messages(messages) if m.get("role") == "user")
    return [t for t in texts if t]


def tool_call_arguments(tool_calls) -> List[str]:
    """The argument strings of a list of OpenAI tool calls."""
    out = []
    for call in tool_calls or []:
        args = ((call or {}).get("function") or {}).get("arguments") if isinstance(call, dict) else None
        if isinstance(args, str) and args:
            out.append(args)
    return out


def tool_texts(messages) -> List[str]:
    """Tool results and tool-call arguments in a request's history.

    These are model-facing text the child did not write (a web snippet, a
    caption), so callers screen them as model output.
    """
    out = []
    for m in _messages(messages):
        if m.get("role") == "tool":
            text = content_text(m.get("content"))
            if text:
                out.append(text)
        out.extend(tool_call_arguments(m.get("tool_calls")))
    return out


def email_from_headers(headers: Optional[Mapping]) -> Optional[str]:
    """The asking user's email from forwarded identity headers, if any.

    Empty values count as absent: LibreChat sends "" when a placeholder has
    no value.
    """
    lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
    for name in USER_EMAIL_HEADERS:
        value = str(lowered.get(name) or "").strip().lower()
        if value:
            return value
    return None


class Scope:
    """Which models are children's, and which accounts are children.

    Deny-by-default: a known child account on any other model is refused, so
    a misconfigured front end fails loudly instead of quietly handing a child
    an unguarded model.
    """

    def __init__(self, child_models: Union[str, Iterable[str], None],
                 child_users: Union[str, Iterable[str], None] = None) -> None:
        self.child_models = split_list(child_models)
        self.child_users = split_list(child_users)

    def is_child_model(self, model: Optional[str]) -> bool:
        # No model id at all: screen anyway. Over-screening an adult chat is
        # the cheaper mistake.
        return model is None or str(model).strip().lower() in self.child_models

    def is_child_user(self, email: Optional[str]) -> bool:
        return bool(email) and str(email).strip().lower() in self.child_users

    def wrong_model(self, email: Optional[str], model: Optional[str]) -> bool:
        return self.is_child_user(email) and not self.is_child_model(model)
