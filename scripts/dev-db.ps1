<#
.SYNOPSIS
    Manage NEXA's project-local PostgreSQL 18 instance (development only).

.DESCRIPTION
    Runs a separate PostgreSQL cluster on port 5433, stored in %LOCALAPPDATA%\nexa\postgres-18,
    so local development never needs the password of the system-wide PostgreSQL service.
    The local superuser is postgres/postgres; the NEXA roles come from scripts/db/init.sql.

.EXAMPLE
    ./scripts/dev-db.ps1 start     # start the server (needed after every reboot)
    ./scripts/dev-db.ps1 stop
    ./scripts/dev-db.ps1 status
    ./scripts/dev-db.ps1 init      # create the nexa database and roles (safe to re-run)
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "status", "init")]
    [string]$Command = "status"
)

$ErrorActionPreference = "Stop"
$pgBin = "C:\Program Files\PostgreSQL\18\bin"
$root = Join-Path $env:LOCALAPPDATA "nexa\postgres-18"
$data = Join-Path $root "data"
$log = Join-Path $root "postgres.log"
$port = 5433

if (-not (Test-Path $data)) {
    Write-Error "No data folder at $data. Create it with: initdb -D `"$data`" -U postgres --auth=scram-sha-256 -E UTF8 --no-locale, then set port = $port in postgresql.conf."
}

switch ($Command) {
    "start" { & "$pgBin\pg_ctl.exe" -D $data -l $log -w start }
    "stop" { & "$pgBin\pg_ctl.exe" -D $data -w stop }
    "status" { & "$pgBin\pg_ctl.exe" -D $data status }
    "init" {
        $env:PGPASSWORD = "postgres"
        $sql = Join-Path $PSScriptRoot "db\init.sql"
        & "$pgBin\psql.exe" -h localhost -p $port -U postgres -v ON_ERROR_STOP=1 -f $sql
    }
}
