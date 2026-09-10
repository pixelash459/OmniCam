# Install Inno Setup 6 (used to compile OmniCam-PC-*-Setup.exe).
$ErrorActionPreference = "Stop"
$ver = "6.7.3"
$url = "https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-$ver.exe"
$out = Join-Path $env:TEMP "innosetup-$ver.exe"
Write-Host "Downloading Inno Setup $ver ..."
Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing
Write-Host "Installing (silent, current user if possible) ..."
# /DIR under LocalAppData avoids needing elevation when possible.
$dest = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6"
$p = Start-Process -FilePath $out -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART","/SP-","/DIR=`"$dest`"" -Wait -PassThru
if ($p.ExitCode -ne 0) { throw "Inno Setup installer exited $($p.ExitCode)" }
$iscc = Join-Path $dest "ISCC.exe"
if (-not (Test-Path $iscc)) { throw "ISCC.exe missing at $iscc" }
Write-Host "ISCC: $iscc"
