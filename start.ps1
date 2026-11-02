$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

if (-not (Test-Path -LiteralPath '.env')) {
    $template = Get-Content -LiteralPath '.env.example' -Raw
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $bytes = New-Object byte[] 32
        $rng.GetBytes($bytes)
        $dbPassword = ([BitConverter]::ToString($bytes)).Replace('-', '').ToLower()
        $rng.GetBytes($bytes)
        $secretKey = ([BitConverter]::ToString($bytes)).Replace('-', '').ToLower()
    } finally { $rng.Dispose() }
    $template = $template.Replace('DB_PASSWORD=generated-by-start-script', "DB_PASSWORD=$dbPassword")
    $template = $template.Replace('SECRET_KEY=generated-by-start-script', "SECRET_KEY=$secretKey")
    [IO.File]::WriteAllText((Join-Path $PSScriptRoot '.env'), $template, [Text.UTF8Encoding]::new($false))
}

docker compose up -d --build --wait --wait-timeout 180
if ($LASTEXITCODE -ne 0) { throw 'Docker Compose failed. Check Docker Desktop and run docker compose logs.' }
docker compose ps
$port = ((Get-Content -LiteralPath '.env' | Where-Object { $_ -match '^HTTP_PORT=' }) -replace '^HTTP_PORT=', '').Trim()
Write-Host "Open http://localhost:$port"
$grafanaPort = (([string](Get-Content -LiteralPath '.env' | Where-Object { $_ -match '^GRAFANA_PORT=' })) -replace '^GRAFANA_PORT=', '').Trim()
if (-not $grafanaPort) { $grafanaPort = '3000' }
Write-Host "Grafana http://localhost:$grafanaPort/d/legal-dms/containers"
