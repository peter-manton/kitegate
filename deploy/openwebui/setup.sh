#!/usr/bin/env sh
# One-step setup: writes .env (generating secrets), then starts everything.
#   ./setup.sh                 # asks a few questions
#   ./setup.sh --local         # use Ollama on this machine, no questions about providers
set -eu
cd "$(dirname "$0")"

command -v docker >/dev/null 2>&1 || { echo "Docker is needed: https://docs.docker.com/get-docker/"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 is needed."; exit 1; }

secret() { python3 -c 'import secrets; print(secrets.token_urlsafe(32))' 2>/dev/null \
           || openssl rand -base64 32 | tr -d '\n=+/'; }

if [ ! -f .env ]; then
  cp .env.example .env
  set_var() {  # escape the characters sed treats specially in a replacement
    esc=$(printf '%s' "$2" | sed 's/[&|\\]/\\&/g')
    sed -i.bak "s|^$1=.*|$1=$esc|" .env && rm -f .env.bak
  }
  set_var KITEGATE_API_KEY "$(secret)"
  set_var WEBUI_SECRET_KEY "$(secret)"

  mode=provider
  key=
  provider_url=https://api.openai.com/v1
  [ "${1:-}" = "--local" ] && mode=local
  if [ "$mode" = provider ]; then
    printf "Model provider API key (blank to run a local model with Ollama instead): "
    read -r key
    [ -z "$key" ] && mode=local
  fi
  if [ "$mode" = local ]; then
    provider_url=http://ollama:11434/v1
    set_var KITEGATE_UPSTREAM_URL "$provider_url"
    set_var KITEGATE_UPSTREAM_MODEL "llama3.2:3b"
    echo "COMPOSE_PROFILES=local" >> .env
  else
    set_var KITEGATE_UPSTREAM_API_KEY "$key"
    printf "Provider URL [https://api.openai.com/v1]: "; read -r url
    [ -n "$url" ] && { provider_url="$url"; set_var KITEGATE_UPSTREAM_URL "$url"; }
    printf "Model [gpt-4.1-mini]: "; read -r model
    [ -n "$model" ] && set_var KITEGATE_UPSTREAM_MODEL "$model"
  fi

  # Pictures are screened by OpenAI's /moderations endpoint, so the provider
  # key is a usable scan key when, and only when, the provider IS OpenAI.
  # Reusing it against any other provider would hand that provider's
  # credential to a third party, so the rest are told how to set their own.
  case "$provider_url" in
    https://api.openai.com/*|https://*.api.openai.com/*)
      set_var KITEGATE_IMAGE_SCAN_API_KEY "$key"
      pictures="checked with your OpenAI key before a child sees them" ;;
    *)
      pictures="off until KITEGATE_IMAGE_SCAN_API_KEY in .env holds an OpenAI key" ;;
  esac
  printf "Values pack (secular-neutral, uk-school, christian-family) [secular-neutral]: "; read -r pack
  [ -n "$pack" ] && set_var KITEGATE_PACK "$pack"
  printf "Children's account emails, comma-separated (can be added later in .env): "; read -r kids
  [ -n "$kids" ] && set_var KITEGATE_CHILD_USERS "$kids"
  chmod 600 .env
  echo "Wrote .env"
  # Said plainly, because "no pictures" and "unchecked pictures" look the same
  # from the outside until a bad one arrives.
  echo "Pictures: $pictures. Children only ever see one that came from a"
  echo "curated source (Wikipedia, NASA)."
fi

grep -q '^COMPOSE_PROFILES=local' .env && echo "The first start downloads the local model (several GB). This can take a while."

docker compose up -d --build
port=$(sed -n 's/^WEBUI_PORT=//p' .env); port=${port:-3000}
echo
echo "Open http://127.0.0.1:${port} and create the first account: it becomes the admin."
echo "Then add each child in Admin Panel > Users with an email listed in KITEGATE_CHILD_USERS."
