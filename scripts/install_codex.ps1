# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

[CmdletBinding()]
param(
    [switch]$Launch = $false,
    [switch]$Open = $false,
    [switch]$Help = $false
)

$ErrorActionPreference = "Stop"

if ($Help) {
    Write-Host "Usage: install_codex.bat [-Launch] [-Open] [-Help]"
    Write-Host "Install ARTEMIS for Codex using project-local dependencies."
    Write-Host "-Launch / -Open also starts the WebUI after installation."
    exit 0
}

$RootDir = Split-Path -Parent $PSScriptRoot
$ToolsDir = Join-Path $RootDir ".artemis-tools"
$UvDir = Join-Path $ToolsDir "uv"
$EnvironmentNames = @(
    "PATH", "UV_PYTHON_INSTALL_DIR", "UV_CACHE_DIR", "UV_PYTHON_BIN_DIR",
    "UV_NO_MODIFY_PATH", "UV_PYTHON_PREFERENCE", "UV_PROJECT_ENVIRONMENT",
    "UV_PYTHON_INSTALL_REGISTRY", "UV_PYTHON_NO_REGISTRY", "UV_PYTHON_INSTALL_BIN",
    "UV_NO_ENV_FILE", "UV_WORKING_DIR", "UV_PROJECT"
)
$OriginalEnvironment = @{}
foreach ($Name in $EnvironmentNames) {
    $OriginalEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name, "Process")
}
$OriginalSecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol
$DownloadDir = $null
$ExitCode = 1

Push-Location $RootDir
try {
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw "The Codex installer requires 64-bit Windows (x64 or ARM64)."
    }
    $Architecture = $env:PROCESSOR_ARCHITEW6432
    if (-not $Architecture) {
        $Architecture = $env:PROCESSOR_ARCHITECTURE
    }
    switch ($Architecture.ToUpperInvariant()) {
        "AMD64" { $Target = "x86_64-pc-windows-msvc" }
        "ARM64" { $Target = "aarch64-pc-windows-msvc" }
        default { throw "Unsupported Windows architecture: $Architecture" }
    }

    $ManagedDirectories = @(
        $ToolsDir,
        $UvDir,
        (Join-Path $ToolsDir "python"),
        (Join-Path $ToolsDir "uv-cache"),
        (Join-Path $ToolsDir "bin"),
        (Join-Path $RootDir ".venv")
    )
    foreach ($Directory in $ManagedDirectories) {
        if ((Test-Path -LiteralPath $Directory) -and
            ((Get-Item -LiteralPath $Directory).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Refusing to install through a linked directory: $Directory"
        }
    }

    $Uv = Join-Path $UvDir "uv.exe"
    if (-not (Test-Path -LiteralPath $Uv -PathType Leaf)) {
        $ExistingUv = Get-Command "uv.exe" -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($ExistingUv) {
            $Uv = $ExistingUv.Source
        } else {
            if (Test-Path -LiteralPath $UvDir) {
                throw "Incomplete local uv directory: $UvDir. Move it aside before retrying."
            }
            New-Item -ItemType Directory -Path $ToolsDir -Force | Out-Null
            $DownloadDir = Join-Path $ToolsDir (".uv-download-" + [Guid]::NewGuid().ToString("N"))
            New-Item -ItemType Directory -Path $DownloadDir | Out-Null
            $Archive = Join-Path $DownloadDir "uv.zip"
            $Extracted = Join-Path $DownloadDir "extracted"
            $Url = "https://github.com/astral-sh/uv/releases/download/0.11.18/uv-$Target.zip"
            Write-Host "Downloading uv into the ARTEMIS project..."
            [System.Net.ServicePointManager]::SecurityProtocol = $OriginalSecurityProtocol -bor [System.Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri $Url -OutFile $Archive -UseBasicParsing -TimeoutSec 180
            Expand-Archive -LiteralPath $Archive -DestinationPath $Extracted
            $DownloadedUv = @(Get-ChildItem -LiteralPath $Extracted -Filter "uv.exe" -File -Recurse)
            if ($DownloadedUv.Count -ne 1) {
                throw "The downloaded uv archive does not contain exactly one uv.exe."
            }
            # Rename only after extraction is complete; never replace an existing install.
            if (Test-Path -LiteralPath $UvDir) {
                throw "The local uv directory appeared during installation. Retry the installer."
            }
            [IO.Directory]::Move($DownloadedUv[0].DirectoryName, $UvDir)
        }
    }

    $env:PATH = (Split-Path -Parent $Uv) + ";" + $env:PATH
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $ToolsDir "python"
    $env:UV_CACHE_DIR = Join-Path $ToolsDir "uv-cache"
    $env:UV_PYTHON_BIN_DIR = Join-Path $ToolsDir "bin"
    $env:UV_NO_MODIFY_PATH = "1"
    $env:UV_PYTHON_PREFERENCE = "managed"
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $RootDir ".venv"
    $env:UV_PYTHON_INSTALL_REGISTRY = "0"
    $env:UV_PYTHON_NO_REGISTRY = "1"
    $env:UV_PYTHON_INSTALL_BIN = "0"
    $env:UV_NO_ENV_FILE = "1"
    $env:UV_WORKING_DIR = $RootDir
    $env:UV_PROJECT = $RootDir

    $InstallerArgs = @(
        "run", "--directory", $RootDir, "--no-project", "--python", "3.12", "python",
        (Join-Path $PSScriptRoot "install_codex.py")
    )
    if ($Launch -or $Open) {
        $InstallerArgs += "--launch"
    }
    & $Uv @InstallerArgs
    $ExitCode = $LASTEXITCODE
    if ($ExitCode -ne 0) {
        throw "Codex installation failed with exit code $ExitCode."
    }
} catch {
    Write-Host "[FAIL] $($_.Exception.Message)" -ForegroundColor Red
    if ($ExitCode -eq 0) {
        $ExitCode = 1
    }
} finally {
    if ($DownloadDir -and (Test-Path -LiteralPath $DownloadDir)) {
        Remove-Item -LiteralPath $DownloadDir -Recurse -Force
    }
    foreach ($Name in $EnvironmentNames) {
        [Environment]::SetEnvironmentVariable($Name, $OriginalEnvironment[$Name], "Process")
    }
    [System.Net.ServicePointManager]::SecurityProtocol = $OriginalSecurityProtocol
    Pop-Location
}
exit $ExitCode
