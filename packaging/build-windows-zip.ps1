<#
.SYNOPSIS
  Build the Windows portable zip winget installs.

.DESCRIPTION
  Compiles packaging/git-roost-launcher.cs into git-roost.exe and bundles it
  with git_roost.py into dist/git-roost-<version>-windows.zip. Mirrors
  build-deb.sh's shape and role, but is PowerShell rather than sh for one
  hard reason: producing a real PE executable needs a C# compiler, and the
  only one guaranteed present without installing anything is the one
  Windows PowerShell's Add-Type reaches into .NET Framework for.

  MUST run under Windows PowerShell 5.1 (powershell.exe), not PowerShell 7
  (pwsh) -- `Add-Type -OutputAssembly ... -OutputType ConsoleApplication`
  throws "assembly types 'ConsoleApplication' ... are not currently
  supported" under pwsh's Core-based Add-Type. This is exactly the kind of
  failure that looks like it should work and doesn't; verified by hand
  against both hosts before this script existed in CI.

.PARAMETER Version
  Defaults to __version__ in git_roost.py, matching build-deb.sh's own
  default-from-source-of-truth behaviour.
#>
param(
    [string]$Version
)

$ErrorActionPreference = "Stop"

if ($PSVersionTable.PSEdition -eq "Core") {
    throw "build-windows-zip.ps1 must run under Windows PowerShell (powershell.exe), not PowerShell 7/Core (pwsh) -- Add-Type -OutputType ConsoleApplication is unsupported there. See the script's header comment."
}

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")

if (-not $Version) {
    $match = Select-String -Path (Join-Path $Root "git_roost.py") -Pattern '^__version__ = "(.*)"' | Select-Object -First 1
    if (-not $match) { throw "could not determine version" }
    $Version = $match.Matches[0].Groups[1].Value
}

$Build = New-Item -ItemType Directory -Path (Join-Path ([System.IO.Path]::GetTempPath()) ([System.Guid]::NewGuid()))
try {
    $exePath = Join-Path $Build.FullName "git-roost.exe"
    $src = Get-Content (Join-Path $Root "packaging/git-roost-launcher.cs") -Raw
    Add-Type -TypeDefinition $src -OutputAssembly $exePath -OutputType ConsoleApplication

    Copy-Item (Join-Path $Root "git_roost.py") $Build.FullName

    $distDir = Join-Path $Root "dist"
    New-Item -ItemType Directory -Force -Path $distDir | Out-Null
    $zipPath = Join-Path $distDir "git-roost-$Version-windows.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath }

    # Compress-Archive rather than an external zip tool: it is a built-in
    # PowerShell cmdlet, so this script's only dependency beyond Windows
    # PowerShell itself is .NET Framework, which Add-Type above already
    # requires.
    Compress-Archive -Path $exePath, (Join-Path $Build.FullName "git_roost.py") -DestinationPath $zipPath

    Write-Output "dist/git-roost-$Version-windows.zip"
} finally {
    Remove-Item -Recurse -Force $Build.FullName
}
