param(
    [switch]$UseRunningServer,
    [switch]$StartVllm,
    [switch]$AllowDownload,
    [switch]$SkipInstall,
    [string]$BaseUrl = "http://127.0.0.1:8001/v1",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Script = Join-Path $Root "scripts/bootstrap_vllm_opencode_test.py"
$ArgsList = @("--base-url", $BaseUrl)

if ($UseRunningServer) {
    $ArgsList += "--use-running-server"
}
if ($StartVllm) {
    $ArgsList += "--start-vllm"
}
if ($AllowDownload) {
    $ArgsList += "--allow-download"
}
if ($SkipInstall) {
    $ArgsList += "--skip-install"
}

& $Python $Script @ArgsList
exit $LASTEXITCODE
