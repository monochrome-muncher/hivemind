# Hivemind Cline hook: PostToolUse (Windows) — the end-of-task write-and-rate
# nudge.
#
# Cline fires PostToolUse after every tool call, with the payload JSON on
# stdin (taskId at the top level, postToolUse.toolName the tool; the older
# tool_name spelling is still read). This counts the task's tool calls in a
# temp file (reset by the TaskStart hook; the sixth field is the
# UserPromptSubmit hook's "full line already sent" flag, kept as is) and, once a task has made
# HIVEMIND_STOP_MIN_TOOLS (default 20) calls without calling hive_feedback
# and either without hive_write or after reading entries (hive_search,
# hive_get), steers the agent once, via contextModification, to write what
# another agent could reuse and rate what it tested; if nothing is new it
# just finishes.
#
# Subagent tasks are never counted and never reminded (a non-empty string in
# parent_agent_id, which the CLI hook runner sends at the top level, or in
# parentAgentId, which the other runner sends in sessionContext, marks one;
# a JSON null is not a string and so does not). The nudge stays silent in
# incognito sessions and while HIVEMIND_API_KEY is unset. Static and
# offline; it never prints the key. Extensionless PostToolUse is the Unix
# twin (Cline ignores .ps1 on Unix).

$payload = [Console]::In.ReadToEnd()

function Field {
    param([string]$Name)
    $m = [regex]::Match($payload, '"' + $Name + '":"([^"]*)"')
    if ($m.Success) { $m.Groups[1].Value } else { "" }
}

$taskId = Field "taskId"
$parent = Field "parent_agent_id"
if ($parent -eq "") { $parent = Field "parentAgentId" }
$toolName = Field "toolName"
if ($toolName -eq "") { $toolName = Field "tool_name" }

# A subagent reports to its parent; leave it alone, as in Claude Code.
if ($parent -ne "") {
    Write-Output '{"cancel":false}'
    return
}

$safe = if ($taskId -ne "") { $taskId -replace '[^a-zA-Z0-9]', '_' } else { "none" }
$stateDir = Join-Path $env:TEMP "hivemind-cline"
New-Item -ItemType Directory -Force -Path $stateDir -ErrorAction SilentlyContinue | Out-Null
$state = Join-Path $stateDir $safe

$calls = 0
$contributed = 0
$reminded = 0
$rated = 0
$seen = 0
$prompted = 0
if (Test-Path $state) {
    $parts = (Get-Content $state -Raw -ErrorAction SilentlyContinue) -split '\s+'
    if ($parts.Count -ge 3) {
        $calls = [int]$parts[0]
        $contributed = [int]$parts[1]
        $reminded = [int]$parts[2]
    }
    if ($parts.Count -ge 5) {
        $rated = [int]$parts[3]
        $seen = [int]$parts[4]
    }
    if ($parts.Count -ge 6) {
        $prompted = [int]$parts[5]
    }
}

if ($toolName -like "*hive_write") {
    $contributed = 1
} elseif ($toolName -like "*hive_feedback") {
    $contributed = 1
    $rated = 1
} elseif ($toolName -like "*hive_search" -or $toolName -like "*hive_get") {
    $seen = 1
}
$calls++

$threshold = 20
if ($env:HIVEMIND_STOP_MIN_TOOLS) {
    $parsed = 0
    if ([int]::TryParse($env:HIVEMIND_STOP_MIN_TOOLS, [ref]$parsed)) { $threshold = $parsed }
}

$incognito = if ($env:HIVEMIND_INCOGNITO) { $env:HIVEMIND_INCOGNITO.ToLower() } else { "" }
if (-not @("1", "true", "yes", "on").Contains($incognito)) { $incognito = "" }

$emitNudge = $false
if ($calls -ge $threshold -and $reminded -eq 0 -and $rated -eq 0 -and ($contributed -eq 0 -or $seen -eq 1)) {
    if ($incognito -ne "" -or [string]::IsNullOrWhiteSpace($env:HIVEMIND_API_KEY)) {
        # Still record the count so a later key change does not fire it.
        Set-Content -Path $state -Value "$calls $contributed $reminded $rated $seen $prompted" -NoNewline -ErrorAction SilentlyContinue
    } else {
        $reminded = 1
        $emitNudge = $true
        Set-Content -Path $state -Value "$calls $contributed $reminded $rated $seen $prompted" -NoNewline -ErrorAction SilentlyContinue
    }
} else {
    Set-Content -Path $state -Value "$calls $contributed $reminded $rated $seen $prompted" -NoNewline -ErrorAction SilentlyContinue
}

if ($emitNudge) {
    Write-Output '{"cancel":false,"contextModification":"HIVEMIND: before finishing, write to Hivemind (hive_write) what another agent could reuse from this task, and rate the entries your work tested (hive_feedback). If nothing is new, write nothing and just finish.","errorMessage":""}'
} else {
    Write-Output '{"cancel":false}'
}
