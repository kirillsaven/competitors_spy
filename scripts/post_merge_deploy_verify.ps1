[CmdletBinding()]
param(
    [string]$Server = "deploy@YOUR_SERVER_IP",
    [string]$RemoteDir = "YOUR_APP_DIR",
    [string]$Ref = "origin/main",
    [Parameter(Mandatory = $true)]
    [int]$SmokeUserId
)

$ErrorActionPreference = "Stop"

function Invoke-RemoteBash {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Script,
        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    # PowerShell on Windows can push CRLF through stdin pipelines into ssh, which breaks
    # remote bash scripts (`cd /path\r`). Encode the script and decode it remotely instead.
    # Also capture ssh stdout/stderr via temp files so remote stderr progress lines do not
    # become terminating PowerShell errors under ErrorActionPreference=Stop.
    $normalizedScript = ($Script -replace "`r`n", "`n" -replace "`r", "`n").Trim()
    $encodedScript = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($normalizedScript))
    $remoteCommand = "printf '%s' '$encodedScript' | base64 -d | bash"

    $stdoutPath = [System.IO.Path]::GetTempFileName()
    $stderrPath = [System.IO.Path]::GetTempFileName()

    try {
        $process = Start-Process `
            -FilePath "ssh" `
            -ArgumentList @($Server, $remoteCommand) `
            -NoNewWindow `
            -Wait `
            -PassThru `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath

        $stdout = if (Test-Path $stdoutPath) { [System.IO.File]::ReadAllText($stdoutPath) } else { "" }
        $stderr = if (Test-Path $stderrPath) { [System.IO.File]::ReadAllText($stderrPath) } else { "" }
        $output = (@($stdout.TrimEnd(), $stderr.TrimEnd()) | Where-Object { $_ }) -join "`n"

        if ($process.ExitCode -ne 0) {
            throw "Remote command failed [$Label]`n$output"
        }
    }
    finally {
        Remove-Item -Force -ErrorAction SilentlyContinue $stdoutPath, $stderrPath
    }

    return ([string]$output).Trim()
}

Write-Host "== Post-merge deploy verify =="
Write-Host "Server: $Server"
Write-Host "RemoteDir: $RemoteDir"
Write-Host "Ref: $Ref"

& git fetch origin --prune
if ($LASTEXITCODE -ne 0) {
    throw "Command failed [git fetch origin --prune]"
}

$localMainSha = (& git rev-parse $Ref 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Command failed [git rev-parse $Ref]`n$localMainSha"
}

$remoteShaBefore = Invoke-RemoteBash -Label "remote sha before" -Script @"
cd $RemoteDir
git rev-parse HEAD
"@

Write-Host "Local main SHA: $localMainSha"
Write-Host "Remote deployed SHA before: $remoteShaBefore"
Write-Host "Production behind local main: $([bool]($remoteShaBefore -ne $localMainSha))"

Write-Host "`n== Deploy =="
Invoke-RemoteBash -Label "prod update" -Script @"
cd $RemoteDir
bash scripts/prod-update.sh $Ref
"@ | Write-Host

Write-Host "`n== Services =="
$servicesStatus = Invoke-RemoteBash -Label "compose ps" -Script @"
cd $RemoteDir
docker compose -f docker-compose.prod.yml ps
"@
Write-Host $servicesStatus

Write-Host "`n== Health =="
Invoke-RemoteBash -Label "prod health" -Script @"
cd $RemoteDir
bash scripts/prod-health.sh
"@ | Write-Host

$remoteShaAfter = Invoke-RemoteBash -Label "remote sha after" -Script @"
cd $RemoteDir
git rev-parse HEAD
"@

Write-Host "`n== Runtime import verify =="
$runtimeVerify = Invoke-RemoteBash -Label "runtime import verify" -Script @"
cd $RemoteDir
docker compose -f docker-compose.prod.yml exec -T web python manage.py shell <<'PY'
import inspect
import tracking.services.reporting as reporting

print(f"module_file={reporting.__file__}")
print(f"title_limit={getattr(reporting, 'REGULAR_REPORT_TITLE_LIMIT', None)}")
source = inspect.getsource(reporting)
print(f"has_truthful_no_active={'Нет активных конкурентов для этой платформы.' in source}")
print(f"has_truthful_no_short_form={'У конкурентов не нашлось недавних коротких роликов с метриками.' in source}")
print(f"has_truthful_stopwords={'Все подходящие ролики скрыты вашими стоп-словами.' in source}")
PY
"@
Write-Host $runtimeVerify

Write-Host "`n== Smoke check =="
$smokeOutput = Invoke-RemoteBash -Label "manual smoke report" -Script @"
cd $RemoteDir
docker compose -f docker-compose.prod.yml exec -T web python manage.py shell <<'PY'
from tracking.models import Report
from tracking.services.reporting import render_report_text
from tracking.tasks import run_user_report_now

user_id = $SmokeUserId
run_user_report_now.run(user_id, trigger="manual")
report = Report.objects.filter(user_id=user_id, status="sent").order_by("-id").first()
sections = {section["platform"]: section for section in report.payload["sections"]}
text = render_report_text(payload=report.payload, timezone_str="UTC")

print(f"smoke_report_id={report.id}")
print(f"smoke_sent_at={report.sent_at.isoformat() if report.sent_at else None}")
print(f"youtube_items={len(sections['youtube']['items'])}")
print(f"tiktok_items={len(sections['tiktok']['items'])}")
print(f"instagram_items={len(sections['instagram']['items'])}")
print(f"youtube_has_diagnostics={'diagnostics' in sections['youtube']}")
print(f"instagram_has_diagnostics={'diagnostics' in sections['instagram']}")
print("smoke_preview_start")
for line in text.splitlines()[:20]:
    print(line)
print("smoke_preview_end")
PY
"@
Write-Host $smokeOutput

Write-Host "`n== Truth report =="
Write-Host "live deployed SHA before: $remoteShaBefore"
Write-Host "live deployed SHA after: $remoteShaAfter"
Write-Host "local main SHA: $localMainSha"
Write-Host "sha match after deploy: $([bool]($remoteShaAfter -eq $localMainSha))"
Write-Host "services restarted:"
Write-Host $servicesStatus
Write-Host "runtime import verify:"
Write-Host $runtimeVerify
Write-Host "smoke check:"
Write-Host $smokeOutput
