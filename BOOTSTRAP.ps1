# Gold Genious - First-time VPS bootstrap (run this ONE command from any folder)
# PowerShell (Admin):
#   Set-ExecutionPolicy Bypass -Scope Process -Force; irm "https://raw.githubusercontent.com/iAMsaifAdeeb/forexautonbot/main/BOOTSTRAP.ps1" | iex

$ErrorActionPreference = 'Stop'
$dir = 'C:\forexautobot'
$zip = Join-Path $env:TEMP 'forexautonbot-main.zip'

Write-Host ""
Write-Host "  GOLD GENIOUS - Bootstrap" -ForegroundColor Yellow
Write-Host "  ========================" -ForegroundColor Yellow
Write-Host ""

New-Item -ItemType Directory -Force -Path $dir | Out-Null
Set-Location $dir

Write-Host "[1/3] Downloading from GitHub..."
Invoke-WebRequest `
    -Uri 'https://github.com/iAMsaifAdeeb/forexautonbot/archive/refs/heads/main.zip' `
    -OutFile $zip -UseBasicParsing

Write-Host "[2/3] Extracting to $dir ..."
Expand-Archive -Path $zip -DestinationPath $dir -Force
Copy-Item "$dir\forexautonbot-main\*" $dir -Recurse -Force
Remove-Item "$dir\forexautonbot-main" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $zip -Force -ErrorAction SilentlyContinue

Write-Host "[3/3] Running SETUP.bat..."
Set-Location $dir
cmd /c "`"$dir\SETUP.bat`""
