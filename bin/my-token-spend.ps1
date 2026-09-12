$ErrorActionPreference = 'Stop'
$cli = Join-Path (Split-Path -Parent $PSScriptRoot) 'src/cli.py'

if ($env:MY_TOKEN_SPEND_PYTHON) {
  & $env:MY_TOKEN_SPEND_PYTHON $cli @args
  exit $LASTEXITCODE
}

foreach ($name in @('python3', 'python', 'py')) {
  $found = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue |
    Where-Object { $_.Source -notmatch 'WindowsApps' } |
    Select-Object -First 1
  if (-not $found) { continue }
  if ($name -eq 'py') { & $found.Source -3 $cli @args } else { & $found.Source $cli @args }
  exit $LASTEXITCODE
}

Write-Error 'my-token-spend: no usable python interpreter found on PATH (tried python3, python, py). Install Python 3.11+ or set MY_TOKEN_SPEND_PYTHON to its absolute path.'
exit 1
