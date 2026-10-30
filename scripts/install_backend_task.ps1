<#
  install_backend_task.ps1 - run the API (uvicorn) as a startup task so it
  survives logoff/reboot, bound to 127.0.0.1:<BackendPort> (private; IIS fronts it).

  Run ON THE SERVER in an ELEVATED PowerShell.

  NOTE: Ollama must also be reachable headless. The Ollama Windows app usually
  runs in the logged-in user's session - if the server has no one logged in,
  either keep an RDP/console session (or enable auto-logon), OR run 'ollama serve'
  as its own startup task. uvicorn reaching Ollama on 127.0.0.1:11434 is all that
  matters.
#>
[CmdletBinding()]
param(
    [string]$AppDir = "C:\CAG Chatbot",
    [int]$BackendPort = 8471,
    [string]$RootPath = "/cagchat",   # must match the IIS app path; "" if root
    [string]$TaskName = "CAGChatbotAPI"
)
$ErrorActionPreference = "Stop"

# keep the model warm for every process (system-wide)
setx OLLAMA_KEEP_ALIVE "-1" /M | Out-Null

# small launcher so the task has the env + working dir right
$launcher = Join-Path $AppDir "start_api.ps1"
$rootArg = if ($RootPath) { " --root-path $RootPath" } else { "" }
@"
`$env:OLLAMA_KEEP_ALIVE = '-1'
Set-Location "$AppDir"
& "$AppDir\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port $BackendPort$rootArg
"@ | Set-Content -Path $launcher -Encoding ASCII

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$launcher`""
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 0)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Registered startup task '$TaskName' -> uvicorn on 127.0.0.1:$BackendPort" -ForegroundColor Green
Write-Host "Start it now without rebooting:  Start-ScheduledTask -TaskName $TaskName" -ForegroundColor Green
Write-Host "Check:  curl.exe http://127.0.0.1:$BackendPort/api/v1/health" -ForegroundColor Green
