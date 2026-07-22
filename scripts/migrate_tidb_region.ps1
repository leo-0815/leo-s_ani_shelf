param(
    [string]$SourceHost = "gateway01.ap-northeast-1.prod.aws.tidbcloud.com",

    [string]$SourceUser = "33VTXrQyXYai4sB.root",

    [Parameter(Mandatory = $true)]
    [string]$TargetHost,

    [Parameter(Mandatory = $true)]
    [string]$TargetUser,

    [string]$SourceDatabase = "anishelf",
    [string]$TargetDatabase = "anishelf",
    [int]$SourcePort = 4000,
    [int]$TargetPort = 4000,
    [switch]$Execute,
    [switch]$Resume,
    [switch]$ResetTarget,
    [string]$ConfirmTarget,
    [switch]$SkipDigest
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ScriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepositoryRoot = (Resolve-Path (Join-Path $ScriptDirectory "..")).Path
$OriginalPythonPath = [Environment]::GetEnvironmentVariable("PYTHONPATH", "Process")
$VendorCandidates = @(
    (Join-Path $RepositoryRoot ".vendor"),
    (Join-Path (Split-Path -Parent $RepositoryRoot) ".vendor")
)
$VendorPath = $VendorCandidates |
    Where-Object { Test-Path -LiteralPath (Join-Path $_ "pymysql") } |
    Select-Object -First 1
if ($null -ne $VendorPath) {
    $env:PYTHONPATH = if ([string]::IsNullOrWhiteSpace($OriginalPythonPath)) {
        $VendorPath
    }
    else {
        "$VendorPath$([IO.Path]::PathSeparator)$OriginalPythonPath"
    }
}

if (($Resume -or $ResetTarget) -and -not $Execute) {
    throw "-Resume and -ResetTarget require -Execute."
}
if ($ResetTarget -and $ConfirmTarget -cne $TargetHost) {
    throw "-ConfirmTarget must exactly match -TargetHost when -ResetTarget is used."
}

function ConvertTo-PlainText {
    param([Parameter(Mandatory = $true)][Security.SecureString]$SecureValue)

    $Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureValue)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
    }
}

$SourcePassword = ConvertTo-PlainText (Read-Host "Tokyo TiDB password" -AsSecureString)
$TargetPassword = ConvertTo-PlainText (Read-Host "Singapore TiDB password" -AsSecureString)

try {
    $env:ANISHELF_MIGRATION_SOURCE_PASSWORD = $SourcePassword
    $env:ANISHELF_MIGRATION_TARGET_PASSWORD = $TargetPassword

    $Arguments = @(
        "-m", "app.tidb_migration",
        "--source-host", $SourceHost,
        "--source-port", "$SourcePort",
        "--source-user", $SourceUser,
        "--source-database", $SourceDatabase,
        "--target-host", $TargetHost,
        "--target-port", "$TargetPort",
        "--target-user", $TargetUser,
        "--target-database", $TargetDatabase
    )
    if ($Execute) { $Arguments += "--execute" }
    if ($Resume) { $Arguments += "--resume" }
    if ($ResetTarget) {
        $Arguments += "--reset-target"
        $Arguments += @("--confirm-target", $ConfirmTarget)
    }
    if ($SkipDigest) { $Arguments += "--skip-digest" }

    Push-Location $RepositoryRoot
    try {
        & python @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "TiDB migration command failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }
}
finally {
    Remove-Item Env:ANISHELF_MIGRATION_SOURCE_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:ANISHELF_MIGRATION_TARGET_PASSWORD -ErrorAction SilentlyContinue
    $SourcePassword = $null
    $TargetPassword = $null
    [Environment]::SetEnvironmentVariable("PYTHONPATH", $OriginalPythonPath, "Process")
}
