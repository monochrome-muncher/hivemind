# Hivemind Cline hook: TaskStart (Windows).
#
# Resets the stop-nudge counter the PostToolUse hook keeps, so each Cline
# task counts its own tool calls. Cline runs this .ps1 from its global hooks
# directory on Windows (~/Documents/Cline/Hooks/) when a task begins; stdin
# is the payload JSON, which carries the task's id at the top level.
# Extensionless TaskStart is the Unix twin (Cline ignores .ps1 on Unix).

$payload = [Console]::In.ReadToEnd()
$taskMatch = [regex]::Match($payload, '"taskId":"([^"]*)"')
$taskId = if ($taskMatch.Success) { $taskMatch.Groups[1].Value } else { "" }
$safe = if ($taskId -ne "") { $taskId -replace '[^a-zA-Z0-9]', '_' } else { "none" }
$state = Join-Path $env:TEMP "hivemind-cline-$safe"

Set-Content -Path $state -Value "0 0 0" -NoNewline -ErrorAction SilentlyContinue
Write-Output '{"cancel":false}'
