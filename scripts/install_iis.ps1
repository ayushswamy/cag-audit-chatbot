<#
  install_iis.ps1 - expose the CAG Chatbot API as an IIS reverse proxy at
  http://<HostHeader>/<AppName>  (e.g. http://itstaging.ntpc.co.in/cagchat).

  Run ON THE SERVER in an ELEVATED PowerShell, from the transferred iis\ folder
  (contains urlrewrite_amd64_en-US.msi and requestRouter_amd64.msi).

  It:
    1. ensures the IIS role is installed
    2. installs URL Rewrite + ARR from the MSIs
    3. enables the ARR proxy + streaming/timeout (for the SSE /ask endpoint)
    4. creates a "<AppName>" IIS APPLICATION under <ParentSite> that proxies
       everything to the backend (uvicorn on 127.0.0.1:<BackendPort>). The app
       being mounted at /<AppName> auto-strips the prefix on the way to uvicorn.
    5. (optional) adds a host binding <HostHeader>:<Port> to <ParentSite>

  The backend (uvicorn) MUST run with:
      --host 127.0.0.1 --port <BackendPort> --root-path /<AppName>
  (install_backend_task.ps1 sets that up.)

  Example:
    powershell -ExecutionPolicy Bypass -File .\install_iis.ps1 `
        -ParentSite "Default Web Site" -HostHeader "itstaging.ntpc.co.in" `
        -AppName "cagchat" -BackendPort 8471
#>
[CmdletBinding()]
param(
    [string]$MsiDir = "",
    [string]$ParentSite = "Default Web Site",
    [string]$HostHeader = "itstaging.ntpc.co.in",
    [int]$Port = 80,
    [string]$AppName = "cagchat",
    [string]$AppFolder = "C:\CAG Chatbot\iis-cagchat",
    [int]$BackendPort = 8471,
    [switch]$AddHostBinding
)
$ErrorActionPreference = "Stop"
if (-not $MsiDir) { $MsiDir = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path } }
function Step($m){ Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Ok($m){ Write-Host "    OK  $m" -ForegroundColor Green }
function Warn($m){ Write-Host "    !!  $m" -ForegroundColor Yellow }

Step "1. Ensure IIS role + management tools"
foreach($f in @("Web-Server","Web-Mgmt-Console","Web-Scripting-Tools")){
    $s = Get-WindowsFeature -Name $f -ErrorAction SilentlyContinue
    if($s -and -not $s.Installed){ Install-WindowsFeature -Name $f | Out-Null; Ok "installed $f" } else { Ok "$f present" }
}
Import-Module WebAdministration -ErrorAction Stop

Step "2. Install URL Rewrite + ARR (rewrite first)"
$rw  = Join-Path $MsiDir "urlrewrite_amd64_en-US.msi"
$arr = Join-Path $MsiDir "requestRouter_amd64.msi"
if(-not (Test-Path $rw))  { throw "missing $rw" }
if(-not (Test-Path $arr)) { throw "missing $arr" }
Start-Process msiexec.exe -Wait -ArgumentList "/i","`"$rw`"","/qn","/norestart"; Ok "URL Rewrite installed"
Start-Process msiexec.exe -Wait -ArgumentList "/i","`"$arr`"","/qn","/norestart"; Ok "ARR installed"

Step "3. Enable ARR proxy + streaming + timeout (for SSE /ask)"
$appcmd = "$env:windir\system32\inetsrv\appcmd.exe"
& $appcmd set config -section:system.webServer/proxy /enabled:"true" /commit:apphost | Out-Null
& $appcmd set config -section:system.webServer/proxy /timeout:"00:20:00" /commit:apphost | Out-Null
& $appcmd set config -section:system.webServer/proxy /responseBufferLimit:"0" /commit:apphost | Out-Null
Ok "proxy enabled, timeout 20m, response buffering off"

Step "4. Ensure parent site exists (+ optional host binding)"
if(-not (Test-Path "IIS:\Sites\$ParentSite")){ throw "Parent site '$ParentSite' not found. Pass -ParentSite <name of the site that serves $HostHeader>." }
Ok "parent site: $ParentSite"
if($AddHostBinding){
    $b = Get-WebBinding -Name $ParentSite -Protocol http -HostHeader $HostHeader -ErrorAction SilentlyContinue
    if(-not $b){ New-WebBinding -Name $ParentSite -Protocol http -Port $Port -HostHeader $HostHeader | Out-Null; Ok "added binding $HostHeader`:$Port to $ParentSite" }
    else { Ok "host binding already present" }
} else { Warn "not touching bindings (assuming $ParentSite already serves $HostHeader). Use -AddHostBinding to add one." }

Step "5. Create the /$AppName proxy application"
New-Item -ItemType Directory -Force $AppFolder | Out-Null
$webconfig = @"
<?xml version="1.0" encoding="UTF-8"?>
<configuration>
  <system.webServer>
    <rewrite>
      <rules>
        <clear />
        <rule name="ProxyCAGChatbot" stopProcessing="true">
          <match url="(.*)" />
          <action type="Rewrite" url="http://127.0.0.1:$BackendPort/{R:1}" />
        </rule>
      </rules>
    </rewrite>
  </system.webServer>
</configuration>
"@
Set-Content -Path (Join-Path $AppFolder "web.config") -Value $webconfig -Encoding UTF8
Ok "web.config -> proxies to http://127.0.0.1:$BackendPort/ (prefix /$AppName stripped by the app mount)"

$pool = "CAGChatbotPool"
if(-not (Test-Path "IIS:\AppPools\$pool")){
    New-WebAppPool -Name $pool | Out-Null
    Set-ItemProperty "IIS:\AppPools\$pool" -Name managedRuntimeVersion -Value ""   # No Managed Code
    Ok "app pool $pool created"
} else { Ok "app pool $pool exists" }

if(Test-Path "IIS:\Sites\$ParentSite\$AppName"){ Warn "application /$AppName already exists - leaving as is" }
else {
    New-WebApplication -Site $ParentSite -Name $AppName -PhysicalPath $AppFolder -ApplicationPool $pool | Out-Null
    Ok "application /$AppName created under $ParentSite"
}

Write-Host "`nDONE." -ForegroundColor Green
Write-Host "  Backend must run:  uvicorn ... --host 127.0.0.1 --port $BackendPort --root-path /$AppName" -ForegroundColor Green
Write-Host "  Public URL:        http://$HostHeader/$AppName/api/v1/health" -ForegroundColor Green
Write-Host "  Swagger:           http://$HostHeader/$AppName/docs" -ForegroundColor Green
