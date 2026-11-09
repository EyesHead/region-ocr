param(
    [string]$OutputPath = (Join-Path $PSScriptRoot '..\..\DocumentConverter.exe')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) {
    $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
}
if (-not (Test-Path -LiteralPath $compiler)) {
    throw 'The system .NET Framework C# compiler was not found.'
}

$source = Join-Path $PSScriptRoot 'DocumentConverterLauncher.cs'
$target = [IO.Path]::GetFullPath($OutputPath)
$converterRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
if ([IO.Path]::GetDirectoryName($target) -ne $converterRoot) {
    throw "EXE must be placed directly in the converter folder: $converterRoot"
}

$icon = Join-Path $PSScriptRoot '..\assets\app-icon.ico'
& $compiler /nologo /target:winexe /platform:anycpu /optimize+ /utf8output "/win32icon:$icon" "/out:$target" /reference:System.Windows.Forms.dll $source
if ($LASTEXITCODE -ne 0) { throw "Compilation failed with exit code $LASTEXITCODE" }
Get-Item -LiteralPath $target | Select-Object FullName,Length
$sha = [System.Security.Cryptography.SHA256]::Create()
$stream = [IO.File]::OpenRead($target)
try { [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '') }
finally { $stream.Dispose(); $sha.Dispose() }
