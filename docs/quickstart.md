# Quick start

Pick the row that matches what you have. Every route enforces the same safety
core and values packs; they differ in where the check runs.

| You have | Use | Section |
|---|---|---|
| Nothing yet, and want a chat site for children | the Open WebUI bundle | [1](#1-out-of-the-box-open-webui-with-kitegate) |
| Open WebUI already | the gateway, or the single-file filter | [2](#2-an-existing-open-webui) |
| LibreChat | the gateway | [3](#3-librechat) |
| Your own app | the gateway with any OpenAI SDK | [4](#4-your-own-app) |
| LiteLLM already | the gateway in front of LiteLLM | [5](#5-an-existing-litellm) |
| A need for a second, model-based check | NeMo Guardrails behind the gateway | [6](#6-add-nemo-guardrails) |
| Python code only, no server | the library | [7](#7-the-python-library-only) |
| Values of your own | a custom signed pack | [8](#8-your-own-values-pack) |
| Pictures in children's answers | the image policy | [9](#9-showing-pictures) |

Read `docs/regulation.md` before children use any of these.

## 1. Out of the box: Open WebUI with Kitegate

One command starts Open WebUI and the Kitegate gateway, and optionally a local
model. You need [Docker](https://docs.docker.com/get-docker/) (Docker Desktop
on Windows and macOS) and either a model provider API key or a machine that
can run a small model.

Clone or download this repository, then:

```bash
cd kitegate/deploy/openwebui
./setup.sh               # Linux, macOS
```

On Windows, double-click `deploy\openwebui\start.cmd`, or run `.\setup.ps1`.

Setup asks for:

- a provider API key, URL and model (any OpenAI-compatible API; the defaults
  are OpenAI and `gpt-4.1-mini`). Leave the key blank to run `llama3.2:3b`
  locally with Ollama instead; `./setup.sh --local` or `.\setup.ps1 -Local`
  skips the question. The local model is a download of several GB on the
  first start.
- the values pack: `secular-neutral`, `uk-school` or `christian-family`
- the children's account emails, comma-separated
- your own admin name, email and password

It generates the secrets, writes `.env` (never commit it), starts everything,
creates your admin account and installs the photo tool. Then:

1. Open <http://127.0.0.1:3000> and sign in with the admin email and password
   you gave setup.
2. In Admin Panel > Users, add an account for each child, using an email you
   listed. Anyone else who signs up waits for your approval.
3. Sign in as a child. The model is **kids-encyclopedia**: every question and
   every reply is screened. Other models are listed too, but refuse a child
   with a short reply.

What the bundle sets up:

```
browser ─► Open WebUI ─► kitegate ─► your provider
                                           └► or Ollama on this machine
```

- Open WebUI's only connection is the gateway. Users cannot add their own
  connections. Adults' models pass through the gateway unscreened.
- Web search, code execution, image generation, sharing and arena models are
  off. Titles and follow-ups are generated through the gateway, so they are
  screened for children's chats.
- The photo tool is installed and enabled on `kids-encyclopedia` by the
  `provision` service, which runs on every `up` and does nothing if the tool
  is already current. Pictures from it are still subject to
  `KITEGATE_IMAGE_POLICY` (section 9).
- Pictures in a reply are screened rather than forbidden: one must come from a
  curated source and pass a moderation check. Setup reuses your provider key
  for that when the provider is OpenAI, and says so; otherwise pictures stay
  off until `KITEGATE_IMAGE_SCAN_API_KEY` in `.env` holds an OpenAI key.
  Either way nothing appears until you install the photo tool by hand
  (section 9), and `KITEGATE_IMAGE_POLICY=block` removes pictures outright.
- Only Open WebUI is published, on 127.0.0.1. The gateway and Ollama are
  reachable only on the internal Docker network.
- Every model is visible to every signed-in user
  (`BYPASS_MODEL_ACCESS_CONTROL=True`): the gateway, not the model list, keeps
  children on kids-encyclopedia. To hide models per group instead, set it to
  `"False"` in `docker-compose.yml` and make kids-encyclopedia visible to the
  children in Admin Panel > Settings > Models.
- The settings in `docker-compose.yml` apply on every start
  (`ENABLE_PERSISTENT_CONFIG=False`). Change them there, not in the admin
  panel. Accounts and chats are kept.

Day to day, from `deploy/openwebui`:

| To | Run |
|---|---|
| add or remove a child | edit `KITEGATE_CHILD_USERS` in `.env`, then `docker compose up -d` |
| change the pack or model | edit `.env`, then `docker compose up -d` |
| read the block log | `docker compose exec kitegate cat /var/lib/kitegate/blocks.jsonl` |
| update | `git pull`, then `docker compose up -d --build` |
| stop | `docker compose down` (keeps accounts, chats and the block log) |
| delete everything | `docker compose down -v` |

The block log holds what children typed when they were blocked: treat it as
sensitive. Set `KITEGATE_ALERT_WEBHOOK` in `.env` to an https URL to be sent
distress events as JSON.

To use it from other devices, keep the port on 127.0.0.1 and put a TLS
reverse proxy (Caddy, for example) in front of Open WebUI. Do not publish
port 3000 on a network interface without TLS.

## 2. An existing Open WebUI

**Gateway (recommended).** Children's chats, titles and follow-ups are all
screened, blocks read as normal replies, and distress can alert an adult.

1. Run the gateway (Docker):

   ```bash
   cp adapters/gateway/gateway.env.example adapters/gateway/gateway.env
   # fill in: KITEGATE_API_KEY, KITEGATE_UPSTREAM_URL/_API_KEY,
   # KITEGATE_CHILD_MODELS, KITEGATE_CHILD_USERS, KITEGATE_PASSTHROUGH=true
   docker compose -f adapters/gateway/docker-compose.yml up -d --build
   ```

2. Put Open WebUI on the `kitegate` Docker network and point its only
   connection at `http://kitegate:8080/v1`, following
   `adapters/openwebui/gateway-connection.md`.

**Single-file filter**, when you cannot run another container: paste
`dist/kitegate_openwebui_filter.py` into Admin Panel > Functions, enable it,
make it Global, and set the valves `pack`, `child_model_ids` and
`child_user_emails`. Needs Open WebUI 0.11.2 or later. It cannot screen titles
or follow-ups (it refuses children's models while those are on) and cannot
alert an adult.

## 3. LibreChat

1. Run the gateway as in section 2, with `KITEGATE_PASSTHROUGH=true`.
2. Merge `adapters/librechat/librechat.yaml` into your `librechat.yaml` and
   copy `adapters/librechat/docker-compose.override.yml` next to LibreChat's
   `docker-compose.yml`.
3. In LibreChat's `.env`: `ENDPOINTS=custom` and
   `KITEGATE_API_KEY=<the gateway's key>`.
4. `docker compose up -d` in your LibreChat checkout.

`ENDPOINTS=custom` removes every route to a model except the gateway. Keep
agents, MCP servers, web search, file search and code execution off for
child accounts; check those switches against your LibreChat version.

## 4. Your own app

Run the gateway, on the same Docker network as your app (section 2) or
directly:

```bash
pip install ".[gateway]"
export KITEGATE_API_KEY=...            # long and random: python -c "import secrets; print(secrets.token_urlsafe(32))"
export KITEGATE_UPSTREAM_URL=https://api.openai.com/v1
export KITEGATE_UPSTREAM_API_KEY=...
export KITEGATE_CHILD_MODELS=kids-encyclopedia=gpt-4.1-mini
kitegate                                        # the gateway, on http://127.0.0.1:8080/v1
```

Then use any OpenAI SDK, and say who is asking:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8080/v1",
    api_key=GATEWAY_KEY,
    default_headers={"x-kitegate-user-email": child_email},
)
reply = client.chat.completions.create(
    model="kids-encyclopedia",
    messages=[{"role": "user", "content": "Why do volcanoes erupt?"}],
    extra_headers={"x-kitegate-pack": "uk-school"},   # optional; must be in KITEGATE_ALLOWED_PACKS
)
print(reply.choices[0].message.content)    # the answer, or the pack's block reply
print(reply.model_extra["kitegate"])      # {"allowed": ..., "stage": ..., "tier": ...}
```

A block is a normal reply, not an error, so a child always sees words from the
pack. Record the `kitegate` object if you keep an audit trail. Streaming
works too: the gateway screens the whole reply first, then streams it.

The gateway serves plain HTTP on 127.0.0.1 by default. If your app is on
another host, set `KITEGATE_TLS_CERT` and `KITEGATE_TLS_KEY`
or put a TLS reverse proxy in front.

## 5. An existing LiteLLM

The gateway's upstream can be any OpenAI-compatible endpoint, including
LiteLLM or another router. LiteLLM keeps doing what it is good at (many
providers, budgets, fallbacks, keys per team) and the gateway does the
screening:

```
front end ─► kitegate ─► LiteLLM ─► providers
```

In `gateway.env`:

```
KITEGATE_UPSTREAM_URL=http://litellm:4000/v1     # LiteLLM on the same Docker network
KITEGATE_UPSTREAM_API_KEY=<a LiteLLM virtual key>
KITEGATE_CHILD_MODELS=kids-encyclopedia=<a LiteLLM model name>
KITEGATE_PASSTHROUGH=true                        # adults' models go to LiteLLM unscreened
```

Front ends then connect to the gateway, not to LiteLLM. Remove any direct
route from a front end to LiteLLM, or a child could go around the gateway.

## 6. Add NeMo Guardrails

The core is pattern matching. NeMo's NemoGuard classifier catches meaning the
patterns miss: misspellings, other languages, indirect requests.

1. Self-host `nvidia/llama-3.1-nemoguard-8b-content-safety` (NIM or vLLM) and
   set its `base_url` in `adapters/nemo/config/kitegate/config.yml`, along with
   the main model's provider. Do not send children's messages to NVIDIA's
   hosted evaluation endpoints.
2. In `gateway.env`: `KITEGATE_UPSTREAM_URL=http://nemo:8000/v1` and
   `KITEGATE_UPSTREAM_KIND=nemo`.
3. `MAIN_MODEL_API_KEY=... docker compose -f adapters/gateway/docker-compose.yml --profile nemo up -d --build`

The gateway still runs the core before and after NeMo, and answers NeMo's
blocks in the pack's words. A self-harm flag on a child's message gets the
distress reply and an alert. NeMo's server has no authentication: never
publish its port.

## 7. The Python library only

```bash
pip install .
```

```python
from kitegate import PolicyEngine, load_pack

engine = PolicyEngine(load_pack("secular-neutral"))

def answer_child(question: str) -> str:
    verdict = engine.screen_request([question])        # the child's own words only
    if not verdict.allowed:
        return verdict.reply
    answer = call_your_model(question)                 # your model call
    verdict = engine.screen_response([answer])
    return answer if verdict.allowed else verdict.reply
```

Screen the child's message before the model call and the model's answer before
showing it. `verdict.tier` says why something was blocked: `distress`,
`hard_block`, `redirect` (a topic the pack sends to a trusted adult), `pii`, or
`persona` (the model presenting itself as a person, a friend or a romantic
interest; output side only). On `distress`, tell an adult as well: that is your
app's job here.

If you call the engine directly like this, send `kitegate.core.CORE_FRAMING` to
the model as a system message. It is the one instruction a pack cannot change,
and it is what stops most `persona` blocks being needed in the first place.

## 8. Your own values pack

Copy the bundled pack closest to what you want from
`src/kitegate/packs/`, edit it, and sign it:

```bash
kitegate-pack keygen --out ~/.kitegate --name family-packs   # once; keep the .pem private and offline
kitegate-pack sign my-pack.yaml --key ~/.kitegate/family-packs.pem
kitegate-pack verify my-pack.yaml --key ~/.kitegate/family-packs.pub
```

Wherever the pack is loaded, trust your public key and point at the file:

```
KITEGATE_PACK=/packs/my-pack.yaml
KITEGATE_PACK_KEYS=<contents of family-packs.pub>
KITEGATE_REQUIRE_SIGNED_PACKS=true
```

In Docker, mount the pack and its `.sig` into the gateway container (for
example at `/packs`). The Open WebUI filter has the same settings as the
valves `pack_public_keys` and `require_signed_packs`. A pack sets who a child
is sent to, which topics go to that person, the reply wording and the framing
given to the model; it cannot switch off any part of the safety core. The
pack's framing is added after the core framing, never instead of it, so a pack
can set tone and worldview but cannot let the model pose as a child's friend.

## 9. Showing pictures

Pictures are two separate jobs: getting one, and deciding whether to show it.
Both are on by default, and a child only ever sees a picture that passes both:
it came from a curated source, and a moderation model did not object to it.

The second job needs a key. Supply `KITEGATE_IMAGE_SCAN_API_KEY` and pictures
are scanned and shown; leave it empty and they are removed, because there is
no safe way to show an unscanned one. `setup.sh` fills it in for you when your
provider is OpenAI, since the same key works for both.

The first job is a manual step: Open WebUI cannot install a tool from the
environment, so until you add the photo tool (9a) there are no pictures to
screen and children get text-only answers. The model will say it cannot show
pictures, which is accurate rather than a fault.

### 9a. Getting a picture: the photo tool

`dist/kitegate_openwebui_photo.py` is a single-file Open WebUI **tool** that
finds one real photograph for a subject.

**In the bundle (section 1) this is automatic.** Open WebUI keeps Workspace
tools in its own database and cannot load one from a file or an environment
variable, so the `provision` service does what the admin panel does: it signs
in with `ADMIN_EMAIL` / `ADMIN_PASSWORD` from `.env`, POSTs the built tool to
Open WebUI's API, and enables it on `kids-encyclopedia`. It runs on every
`docker compose up -d`, updates the tool when the built file has changed, and
does nothing when it is already current. Leave `ADMIN_EMAIL` blank to opt out
and install by hand instead.

Rebuild after changing a provider, then restart to ship it:

```bash
python tools/build_owui_photo.py
cd deploy/openwebui && docker compose up -d
```

**Elsewhere**, install it in Admin Panel > Functions > Tools and enable it on
the children's model, or run the same provisioner against your own instance:

```bash
ADMIN_EMAIL=you@example.org ADMIN_PASSWORD=... \
WEBUI_URL=http://127.0.0.1:3000 \
TOOL_FILE=dist/kitegate_openwebui_photo.py \
ATTACH_TO_MODELS=kids-encyclopedia \
python3 deploy/openwebui/provision.py
```

It is not an image search. A provider is a narrow, curated source:

| Provider | What it returns | Notes |
|---|---|---|
| `wikimedia` (default) | the Wikipedia article lead image | an editor's choice for the subject; Simple English first |
| `nasa` | NASA's public image library | public domain, no key, **space only** |

Set the `providers` valve to a comma-separated list, tried in order. Keep
`nasa` after `wikimedia`: it answers confidently about anything, so "tiger"
returns Enceladus's tiger stripes.

Open image search is deliberately not offered. It is the largest
unsafe-content surface in a feature like this, and a media-repository keyword
search is no better: Wikimedia Commons holds ~100M files and a keyword query
across it surfaces clinical and graphic material for innocent subjects.
Article lead images are a far narrower, better-governed set.

**Adding your own provider.** Subclass `PhotoProvider` in
`src/kitegate/photos.py`, implement `lookup` / `read_lookup` (and `search` /
`read_search` if it needs a second step), register it in `PROVIDERS`, and
rebuild with `python tools/build_owui_photo.py`. Providers are pure request-
building and response-parsing with no HTTP, so a test covers one with a
recorded payload and no network.

Declare `image_hosts` honestly: it is what goes in `KITEGATE_IMAGE_HOSTS`, and
a missing host means working pictures get dropped with no error anywhere.
`image_hosts_for("wikimedia,nasa")` prints the list. When judging a candidate
provider, ask what its worst result for an innocent subject looks like, not
its best: an art museum API is a poor children's provider however reputable
the museum, because art includes nudity and war.

### 9b. Deciding whether to show it: the image policy

The text tiers are patterns, so they can say nothing about a photograph. Two
settings decide what a child sees, and a picture must get past both.

**Where it may come from.** `KITEGATE_IMAGE_HOSTS` is a host allowlist, and
left unset it is the hosts of the providers in `src/kitegate/photos.py`:
`thumb.wikimedia.org`, `upload.wikimedia.org` and `images-assets.nasa.gov`.
Anything else is removed without an API call, so a picture the model invented
or a tool fetched from somewhere unexpected costs nothing to drop. Note both
Wikimedia hosts: measured over ten subjects, scaled thumbnails come from
`thumb.wikimedia.org` and unscaled originals from `upload.wikimedia.org`, so
listing only the obvious one drops the rest without an error. Add the hosts of
any provider you add. `*` turns the allowlist off and scans whatever arrives.

**Whether it is shown.** `KITEGATE_IMAGE_POLICY`:

| Policy | What a child sees | Cost |
|---|---|---|
| `scan` | the answer, with allowed pictures that pass a moderation model | one API call per new picture |
| `block` | the answer, with every picture removed | nothing |
| `allow` | the answer with every picture, unchecked, allowlist ignored | nothing |

Unset, it is `scan` when `KITEGATE_IMAGE_SCAN_API_KEY` is set and `block` when
it is not: pictures are on by default, but an unscanned one is never shown.
A text-only service can say `block` outright and never think about keys. Asking
for `scan` with no key is a startup error rather than a silent downgrade, so a
deployment that meant to scan cannot quietly end up showing nothing.

```
# the default, spelled out
KITEGATE_IMAGE_POLICY=scan
KITEGATE_IMAGE_SCAN_API_KEY=<an OpenAI key>

# or no pictures at all, whatever else is configured
KITEGATE_IMAGE_POLICY=block
```

A picture that fails is removed and **the words are kept**. This is the one
place Kitegate redacts instead of blocking: refusing a question about Rome
because its lead image scored high on violence would read to a child as "you
asked something wrong", when the honest outcome is that there is no good
picture.

Three things keep this cheap. The moderation endpoint fetches the image, so
the gateway never downloads or decodes one. Verdicts are cached by URL, and a
public lead image has the same answer for every child forever, so a service
answering the same few hundred subjects settles into almost all cache hits.
And the host allowlist is checked first, so a picture from an unexpected host
is dropped without an API call.

The API's own `flagged` boolean is **not** used as the decision. It is tuned
for general-purpose platforms, and a classical nude or a battle painting can
sit below it. `KITEGATE_IMAGE_THRESHOLDS` compares category scores against
much lower lines instead; lower is stricter, and the defaults are in
`src/kitegate/images.py`.

### What this does not cover

- **It is not a CSAM control.** The `sexual/minors` category is text-only
  upstream, so a hosted scan will not tell you that a picture sexualises a
  child, and OpenAI's guidance is not to send suspected material to the
  endpoint at all. Treat this as screening public pictures before display,
  and read `docs/regulation.md`.
- **Children cannot send pictures** unless you set
  `KITEGATE_IMAGE_UPLOADS=allow`. It is off because a photograph a child
  uploads may be of the child, and handing that to a moderation service is
  not a reasonable default. When it is on, uploads are forwarded to the model
  and are **not** scanned. A refused upload gets a short reply rather than an
  error.
- **The single-file Open WebUI filter has no image policy.** It cannot make
  an outbound call, so it screens text only. Use the gateway where pictures
  matter.
- **The photo tool runs in Open WebUI's backend, not through the gateway**, so
  it screens the subject itself before the request leaves. The picture it
  returns is checked by the image policy on the way back to the child.
- Removals are recorded in the block log and in the `kitegate.images` object
  on the response, so an adult can see that a picture was dropped and why.
  The child is not told, because a missing photo needs no explanation.
