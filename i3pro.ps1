# Run i3pro straight from the source tree (no install, no network needed).
#   .\i3pro.ps1 info "i2pro_data\20260908-cjh 高避5圈.ld"
$env:PYTHONPATH = "$PSScriptRoot\src;$env:PYTHONPATH"
python -m i3pro @args
