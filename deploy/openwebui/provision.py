"""Install the Kitegate photo tool into Open WebUI, without anyone pasting code.

Open WebUI keeps Workspace tools in its own database and has no way to load
one from a file or an environment variable — confirmed against this image's
own routers, and the project's open discussions say the same. So the only
honest automation is to do what the admin panel does: sign in and POST the
tool to the API.

Runs as a one-shot Compose service after Open WebUI reports healthy, and is
idempotent: it creates the tool, or updates it when the built file has
changed, and does nothing when it is already current. So `docker compose up -d`
after a rebuild ships the new tool with no extra step.

Deliberately STDLIB ONLY. It runs inside the open-webui image, which is
already present and is a Python app, so nothing extra is downloaded and
nothing is installed. Importing `requests` would tie this to that image's
dependency set for no gain.

    WEBUI_URL        default http://open-webui:8080
    ADMIN_NAME       display name for the first account
    ADMIN_EMAIL      the admin account. Blank disables provisioning entirely.
    ADMIN_PASSWORD   its password. Blank disables provisioning entirely.
    TOOL_FILE        the built single-file tool
    TOOL_ID          Python identifier, e.g. kitegate_photo
    TOOL_NAME        display name
    ATTACH_TO_MODELS comma-separated model ids to enable the tool on

WHY THIS CREATES THE ADMIN ACCOUNT
Open WebUI's first signup becomes an admin and is deliberately not gated by
ENABLE_SIGNUP (its own comment says so), so the only way to hold an API token
at setup time is to make that account here. The alternative — wait for a human
to sign up in a browser, then run a second command — is the manual step this
file exists to remove.

If ADMIN_EMAIL or ADMIN_PASSWORD is blank this exits quietly without doing
anything, so an existing deployment that created its admin in the browser is
unaffected and keeps working exactly as before.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

URL = os.getenv("WEBUI_URL", "http://open-webui:8080").rstrip("/")
# urlopen honours whatever scheme it is handed, so a stray WEBUI_URL of
# file:///... would have `call` read a local path and report it as an API
# reply. Refuse anything but HTTP(S) here, once, rather than at each call.
if urllib.parse.urlsplit(URL).scheme not in ("http", "https"):
    raise SystemExit(f"provision: WEBUI_URL must be http:// or https://, got {URL!r}")
ADMIN_NAME = os.getenv("ADMIN_NAME", "Admin").strip()
ADMIN_EMAIL = (os.getenv("ADMIN_EMAIL") or "").strip().lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD") or ""
TOOL_FILE = os.getenv("TOOL_FILE", "/tools/kitegate_openwebui_photo.py")
TOOL_ID = os.getenv("TOOL_ID", "kitegate_photo").strip().lower()
TOOL_NAME = os.getenv("TOOL_NAME", "Kitegate Photo").strip()
ATTACH = [m.strip() for m in (os.getenv("ATTACH_TO_MODELS") or "").split(",") if m.strip()]

WAIT_SECONDS = int(os.getenv("WAIT_SECONDS", "180"))


def log(message):
    print(f"provision: {message}", flush=True)


def call(path, payload=None, token=None, method=None):
    """(status, body). Never raises for an HTTP error status."""
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        URL + path, data=data, method=method or ("POST" if data else "GET"))
    request.add_header("content-type", "application/json")
    if token:
        request.add_header("authorization", "Bearer " + token)
    try:
        # nosec B310 - the scheme is checked once where URL is set above;
        # bandit flags the call site because it cannot see that.
        with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310
            raw = response.read().decode("utf-8", "replace")
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"detail": raw[:200]}
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        return 0, {"detail": str(exc)}


def detail(body):
    if isinstance(body, dict):
        return str(body.get("detail") or body)[:200]
    return str(body)[:200]


def wait_for_webui():
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        status, _ = call("/health")
        if status == 200:
            return True
        time.sleep(2)
    return False


def token_for_admin():
    """Sign in, or create the first account. Returns a token or None."""
    status, body = call("/api/v1/auths/signin",
                        {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    if status == 200 and isinstance(body, dict) and body.get("token"):
        log(f"signed in as {ADMIN_EMAIL}")
        return body["token"]

    status, body = call("/api/v1/auths/signup",
                        {"name": ADMIN_NAME, "email": ADMIN_EMAIL,
                         "password": ADMIN_PASSWORD})
    if status == 200 and isinstance(body, dict) and body.get("token"):
        log(f"created the admin account {ADMIN_EMAIL}")
        return body["token"]

    # Most likely cause, worth naming: the instance already has a different
    # admin (created in a browser), so signup is refused and these credentials
    # do not match anyone.
    log(f"ERROR could not sign in or sign up as {ADMIN_EMAIL}: "
        f"HTTP {status} {detail(body)}")
    log("      If this instance already has an admin, set ADMIN_EMAIL and "
        "ADMIN_PASSWORD in .env to those credentials.")
    return None


def frontmatter_description(source):
    """The `description:` line from the tool's frontmatter docstring."""
    match = re.search(r"^description:\s*(.+)$", source[:4000], re.M)
    return match.group(1).strip() if match else "Kitegate photo tool."


def install_tool(token, source):
    form = {"id": TOOL_ID, "name": TOOL_NAME, "content": source,
            "meta": {"description": frontmatter_description(source)}}

    status, body = call(f"/api/v1/tools/id/{TOOL_ID}", token=token)
    if status == 200:
        existing = (body or {}).get("content")
        if existing == source:
            log(f"tool '{TOOL_ID}' is already up to date")
            return True
        status, body = call(f"/api/v1/tools/id/{TOOL_ID}/update", form, token=token)
        if status == 200:
            log(f"updated tool '{TOOL_ID}' from {TOOL_FILE}")
            return True
        log(f"ERROR updating tool: HTTP {status} {detail(body)}")
        return False

    status, body = call("/api/v1/tools/create", form, token=token)
    if status == 200:
        log(f"created tool '{TOOL_ID}' from {TOOL_FILE}")
        return True
    log(f"ERROR creating tool: HTTP {status} {detail(body)}")
    return False


def attach_to_model(token, model_id):
    """Enable the tool on a model, by id.

    The front end reads `model.meta.toolIds`, so that is what has to be set.
    Existing meta and params are carried over rather than replaced: this runs
    on every start, and a provisioner that quietly reset a model's settings
    would be worse than a manual step.
    """
    status, body = call(f"/api/v1/models/model?id={urllib.parse.quote(model_id)}", token=token)
    existing = body if status == 200 and isinstance(body, dict) else None

    meta = dict((existing or {}).get("meta") or {})
    tool_ids = [t for t in (meta.get("toolIds") or []) if isinstance(t, str)]
    if TOOL_ID in tool_ids:
        log(f"tool already enabled on model '{model_id}'")
        return True
    meta["toolIds"] = tool_ids + [TOOL_ID]

    form = {"id": model_id,
            "name": (existing or {}).get("name") or model_id,
            "base_model_id": (existing or {}).get("base_model_id"),
            "meta": meta,
            "params": (existing or {}).get("params") or {},
            "is_active": True if existing is None else existing.get("is_active", True)}

    path = "/api/v1/models/model/update" if existing else "/api/v1/models/create"
    status, body = call(path, form, token=token)
    if status == 200:
        log(f"enabled tool on model '{model_id}'")
        return True
    log(f"ERROR enabling tool on '{model_id}': HTTP {status} {detail(body)}")
    return False


def main():
    if not ADMIN_EMAIL or not ADMIN_PASSWORD:
        log("ADMIN_EMAIL/ADMIN_PASSWORD not set, so nothing to do. The photo "
            "tool can still be installed by hand (quickstart section 9).")
        return 0
    if not os.path.isfile(TOOL_FILE):
        log(f"ERROR {TOOL_FILE} is missing. Run tools/build_owui_photo.py.")
        return 1
    source = open(TOOL_FILE, encoding="utf-8").read()

    if not wait_for_webui():
        log(f"ERROR Open WebUI did not become ready within {WAIT_SECONDS}s")
        return 1
    token = token_for_admin()
    if not token:
        return 1
    if not install_tool(token, source):
        return 1
    ok = all(attach_to_model(token, model_id) for model_id in ATTACH)
    log("done" if ok else "finished with errors")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
