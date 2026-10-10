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

    # The admin account is created by the provisioner rather than in the
    # browser, because the photo tool is installed through Open WebUI's API
    # and that needs a token, which needs an account. Leave the email blank to
    # sign up in the browser instead; the tool then has to be installed by hand.
    $adminName = Read-Host 'Your (grown-up) name [Admin]'
    if ($adminName) { Set-Var ADMIN_NAME $adminName }
    $adminEmail = Read-Host 'Your admin email, for signing in (blank to do this in the browser)'
    if ($adminEmail) {
        Set-Var ADMIN_EMAIL $adminEmail
        do {
            $secure = Read-Host 'Admin password (at least 8 characters)' -AsSecureString
            $adminPass = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
                [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
            if ($adminPass.Length -lt 8) { Write-Host 'Too short.' }
        } while ($adminPass.Length -lt 8)
        Set-Var ADMIN_PASSWORD $adminPass
    }
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
# The photo tool is built from the package, so build it before starting: the
# provision service mounts dist/ and installs whatever is there.
$py = Get-Command python, python3 -ErrorAction SilentlyContinue | Select-Object -First 1
if ($py) { & $py.Source (Join-Path $PSScriptRoot '..\..\tools\build_owui_photo.py') *> $null }

docker compose up -d --build
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$port = (Get-Content .env | Where-Object { $_ -like 'WEBUI_PORT=*' }) -replace '^WEBUI_PORT=', ''
if (-not $port) { $port = '3000' }
$admin = (Get-Content .env | Where-Object { $_ -like 'ADMIN_EMAIL=*' }) -replace '^ADMIN_EMAIL=', ''
Write-Host ''
if ($admin) {
    # Wait for the one-shot provisioner so its result is part of setup's
    # output rather than something to go looking for in the logs.
    docker compose wait provision *> $null
    docker compose logs provision --no-log-prefix 2>$null | Select-Object -Last 5
    Write-Host ''
    Write-Host "Open http://127.0.0.1:$port and sign in as $admin."
} else {
    Write-Host "Open http://127.0.0.1:$port and create the first account: it becomes the admin."
    Write-Host 'The photo tool was NOT installed: see docs/quickstart.md section 9.'
}
Write-Host 'Then add each child in Admin Panel > Users with an email listed in KITEGATE_CHILD_USERS.'
