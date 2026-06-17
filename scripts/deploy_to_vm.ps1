# scripts/deploy_to_vm.ps1
# ---------------------------------------------------------------------
# BC series deploy automation
#   1. Cleanup junk files (PowerShell escaping artifacts)
#   2. git add (grouped) + commit + push
#   3. gh workflow run (deploy_target=all by default)
#   4. gh run watch
# ---------------------------------------------------------------------
# Usage (PowerShell):
#   .\scripts\deploy_to_vm.ps1 -CommitMessage "BC2 v3: Lead Convert"
#   .\scripts\deploy_to_vm.ps1 -CommitMessage "..." -Target dashboard-only
#   .\scripts\deploy_to_vm.ps1 -CommitMessage "..." -SkipCommit
#   .\scripts\deploy_to_vm.ps1 -DryRun
# ---------------------------------------------------------------------

[CmdletBinding()]
param(
    [string]$CommitMessage = "",
    [ValidateSet('all', 'docker-only', 'dashboard-only')]
    [string]$Target = 'all',
    [string]$Branch = 'main',
    [switch]$SkipCleanup,
    [switch]$SkipCommit,
    [switch]$SkipDeploy,
    [switch]$DryRun
)

# Settings
$REPO     = "ai_mcp_multi_agent_oosdk/ai_mcp_multi_agent_oosdk"
$WORKFLOW = "Deploy Multi-Agent OOSDK to GCP VM"
$VM_IP    = "REDACTED_VM_IP"
$DASH_KR  = "http://" + $VM_IP + ":9601"
$DASH_EN  = "http://" + $VM_IP + ":9602"

# Helpers
function Step([string]$msg) {
    Write-Host ""
    Write-Host "===================================================================" -ForegroundColor Cyan
    Write-Host ("  " + $msg) -ForegroundColor Cyan
    Write-Host "===================================================================" -ForegroundColor Cyan
}
function Run([string]$cmd) {
    Write-Host ("-> " + $cmd) -ForegroundColor Yellow
    if ($DryRun) { return }
    Invoke-Expression $cmd
    if ($LASTEXITCODE -ne 0) {
        Write-Host ("  FAILED (exit " + $LASTEXITCODE + ")") -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

# Step 0: Pre-flight
Step "0. Pre-flight check"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot
Write-Host ("  cwd: " + $projectRoot)

$ghAuth = gh auth status 2>&1 | Out-String
if ($ghAuth -notmatch "Logged in") {
    Write-Host "ERROR: gh CLI not authenticated. Run 'gh auth login' first." -ForegroundColor Red
    exit 1
}

# Step 1: Cleanup junk files
if (-not $SkipCleanup) {
    Step "1. Cleanup junk files"

    $junkPatterns = @('hboard*', 'audit-first*')
    foreach ($pattern in $junkPatterns) {
        $found = Get-ChildItem -Force -Filter $pattern -ErrorAction SilentlyContinue | Where-Object { -not $_.PSIsContainer }
        foreach ($f in $found) {
            Write-Host ("  rm: " + $f.Name) -ForegroundColor DarkGray
            if (-not $DryRun) { Remove-Item -Force -LiteralPath $f.FullName }
        }
    }
    # Junk files containing literal double-quote in name (PowerShell escape leftovers)
    $quoteJunk = Get-ChildItem -Force -ErrorAction SilentlyContinue | Where-Object {
        -not $_.PSIsContainer -and $_.Name.Contains([char]34)
    }
    foreach ($f in $quoteJunk) {
        Write-Host ("  rm: " + $f.Name) -ForegroundColor DarkGray
        if (-not $DryRun) { Remove-Item -Force -LiteralPath $f.FullName }
    }

    # Cache busting test copies
    $junkTests = @('tests\test_bc2_sales_v2.py', 'tests\test_bc2_sales_v3.py')
    foreach ($f in $junkTests) {
        if (Test-Path $f) {
            Write-Host ("  rm: " + $f) -ForegroundColor DarkGray
            if (-not $DryRun) { Remove-Item -Force $f }
        }
    }
}

# Step 2: Git staging + commit + push
if (-not $SkipCommit) {
    if ([string]::IsNullOrWhiteSpace($CommitMessage)) {
        Write-Host "ERROR: -CommitMessage required (or use -SkipCommit)" -ForegroundColor Red
        exit 1
    }

    Step "2. Git staging + commit + push"

    Run "git add -u"

    # NOTE: git add -u 는 이미 tracked 된 파일의 수정만 stage. 새 파일은 여기 명시 필요.
    # 누락 시 "deploy 는 성공하는데 VM 의 streamlit 이 ModuleNotFoundError" 같은
    # silent failure 가 난다 (BC3 SO 재고 탭 첫 deploy 때 발생) — 새 파일을 추가하면
    # 반드시 이 목록에 등록할 것.
    $newFiles = @(
        'mcp_server/agents/analytics_agent.py',
        'mcp_server/agents/erp_agent.py',
        'tests/test_bc2_sales_ontology.py',
        'BC2_WEEK1_SPEC_v3_leadconvert_pricing.md',
        'scripts/deploy_to_vm.ps1',
        'scripts/bc2_auto_create_opps.py',
        'scripts/bc2_test_odoo_connection.py',
        'scripts/bc2_to_odoo_handoff.py',
        # ─── BC3 SO 재고 탭 (2026-05-26) ───
        'dashboard_modules/so_inventory_view.py'
    ) | Where-Object { Test-Path $_ }

    if ($newFiles.Count -gt 0) {
        Run ("git add " + ($newFiles -join ' '))
    }

    $diff = git diff --cached --stat
    if ([string]::IsNullOrWhiteSpace($diff)) {
        Write-Host "  (no staged changes - skip commit)" -ForegroundColor DarkGray
    } else {
        $msgEscaped = $CommitMessage -replace '"', '\"'
        Run ('git commit -m "' + $msgEscaped + '"')
    }

    Run ("git push origin " + $Branch)
}

# Step 3: GitHub Actions dispatch + watch
if (-not $SkipDeploy) {
    Step ("3. GitHub Actions dispatch (target=" + $Target + ")")

    Run ('gh workflow run "' + $WORKFLOW + '" --repo ' + $REPO + ' --ref ' + $Branch + ' -f deploy_target=' + $Target)

    Write-Host ""
    Write-Host "  waiting 3s for workflow_dispatch to register..." -ForegroundColor DarkGray
    if (-not $DryRun) { Start-Sleep -Seconds 3 }

    $runId = gh run list --repo $REPO --workflow "$WORKFLOW" --limit 1 --json databaseId --jq '.[0].databaseId'
    Write-Host ("  run id: " + $runId) -ForegroundColor Green

    Step "4. Watching run (~7 min expected)"
    Run ("gh run watch " + $runId + " --repo " + $REPO)
}

# Step 5: Post info
Step "DONE - verify dashboards"
Write-Host ("  KR: " + $DASH_KR)
Write-Host ("  EN: " + $DASH_EN)
Write-Host ""
Write-Host "  Next:"
Write-Host "    1) Dashboard 'Agent Status' tab shows ERP/Analytics agents"
Write-Host "    2) Call process_sales_opportunity for 4 scenarios from Claude Desktop"
Write-Host "    3) 'Recent Decisions' table shows 4 sales decisions"
Write-Host ""
