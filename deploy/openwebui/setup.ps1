# One-step setup for Windows: writes .env (generating secrets), then starts everything.
#   .\setup.ps1            # asks a few questions
#   .\setup.ps1 -Local     # use Ollama on this machine
# Works in Windows PowerShell 5.1 (what start.cmd uses) and PowerShell 7.
param([switch]$Local)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host 'Docker Desktop is needed: https://docs.docker.com/desktop/'; exit 1
}
docker compose version *> $null
if ($LASTEXITCODE -ne 0) { Write-Host 'Docker Compose v2 is needed.'; exit 1 }

function New-Secret {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

if (-not (Test-Path .env)) {
    $script:lines = @(Get-Content .env.example)
    function Set-Var([string]$name, [string]$value) {
        # Plain string work, no regex replacement: keys may contain $ or &.
        $script:lines = @($script:lines | ForEach-Object {
            if ($_.StartsWith("$name=")) { "$name=$value" } else { $_ }
        })
    }
    Set-Var KITEGATE_API_KEY (New-Secret)
    Set-Var WEBUI_SECRET_KEY (New-Secret)

    $mode = if ($Local) { 'local' } else { 'provider' }
    $key = ''
    $providerUrl = 'https://api.openai.com/v1'
    if ($mode -eq 'provider') {
        $key = Read-Host 'Model provider API key (blank to run a local model with Ollama instead)'
        if (-not $key) { $mode = 'local' }
    }
    if ($mode -eq 'local') {
        $providerUrl = 'http://ollama:11434/v1'
        Set-Var KITEGATE_UPSTREAM_URL $providerUrl
        Set-Var KITEGATE_UPSTREAM_MODEL 'llama3.2:3b'
        $script:lines += 'COMPOSE_PROFILES=local'
    } else {
        Set-Var KITEGATE_UPSTREAM_API_KEY $key
        $url = Read-Host 'Provider URL [https://api.openai.com/v1]'
        if ($url) { $providerUrl = $url; Set-Var KITEGATE_UPSTREAM_URL $url }
        $model = Read-Host 'Model [gpt-4.1-mini]'
        if ($model) { Set-Var KITEGATE_UPSTREAM_MODEL $model }
    }

    # Pictures are screened by OpenAI's /moderations endpoint, so the provider
    # key is a usable scan key when, and only when, the provider IS OpenAI.
    # Reusing it against another provider would hand that provider's
    # credential to a third party.
    if ($providerUrl -like 'https://api.openai.com/*') {
        Set-Var KITEGATE_IMAGE_SCAN_API_KEY $key
        $pictures = 'checked with your OpenAI key before a child sees them'
    } else {
        $pictures = 'off until KITEGATE_IMAGE_SCAN_API_KEY in .env holds an OpenAI key'
    }
    $pack = Read-Host 'Values pack (secular-neutral, uk-school, christian-family) [secular-neutral]'
    if ($pack) { Set-Var KITEGATE_PACK $pack }
    $kids = Read-Host "Children's account emails, comma-separated (can be added later in .env)"
    if ($kids) { Set-Var KITEGATE_CHILD_USERS $kids }
    [IO.File]::WriteAllLines((Join-Path $PSScriptRoot '.env'), [string[]]$script:lines)
    Write-Host 'Wrote .env'
    # Said plainly, because "no pictures" and "unchecked pictures" look the
    # same from the outside until a bad one arrives.
    Write-Host "Pictures: $pictures. Children only ever see one that came from"
    Write-Host 'a curated source (Wikipedia, NASA).'
}

if (Select-String -Path .env -Pattern '^COMPOSE_PROFILES=local' -Quiet) {
    Write-Host 'The first start downloads the local model (several GB). This can take a while.'
}

docker compose up -d --build
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$port = (Get-Content .env | Where-Object { $_ -like 'WEBUI_PORT=*' }) -replace '^WEBUI_PORT=', ''
if (-not $port) { $port = '3000' }
Write-Host ''
Write-Host "Open http://127.0.0.1:$port and create the first account: it becomes the admin."
Write-Host 'Then add each child in Admin Panel > Users with an email listed in KITEGATE_CHILD_USERS.'
