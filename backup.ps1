# Creates a consistent backup of PostgreSQL and the document volume.
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Run-Docker([string[]]$Arguments) {
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Docker command failed: $($Arguments -join ' ')" }
}

$backupDir = Join-Path $PSScriptRoot ('backups\' + (Get-Date -Format 'yyyy-MM-dd_HH-mm-ss'))
New-Item -ItemType Directory -Path $backupDir | Out-Null
$dumpName = '/tmp/backup-' + [Guid]::NewGuid().ToString('N') + '.dump'
$stopped = $false
try {
    # Stop writers while both parts of the backup are being created.
    Run-Docker @('compose', 'stop', 'nginx', 'worker', 'app')
    $stopped = $true
    Run-Docker @('compose', 'exec', '-T', 'db', 'pg_dump', '-U', 'legal_app', '-d', 'legal_archive', '-Fc', '-f', $dumpName)
    Run-Docker @('compose', 'cp', "db:$dumpName", (Join-Path $backupDir 'database.dump'))
    Run-Docker @('compose', 'run', '--rm', '--no-deps', '-T', '--volume', "${backupDir}:/backup", '--entrypoint', 'tar', 'app', '-czf', '/backup/files.tar.gz', '-C', '/data/files', '.')
    Copy-Item -LiteralPath '.env' -Destination (Join-Path $backupDir '.env')
    Set-Content -LiteralPath (Join-Path $backupDir 'COMPLETE') -Value 'PostgreSQL and files backup completed.' -Encoding ascii
    Write-Host "Backup saved: $backupDir"
} finally {
    & docker compose exec -T db rm -f $dumpName
    if ($stopped) {
        & docker compose up -d --wait --wait-timeout 120
        if ($LASTEXITCODE -ne 0) { Write-Warning 'Run docker compose up -d to restart the application.' }
    }
}
