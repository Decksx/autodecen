param(
    [switch]$Gpu
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ThirdPartyRoot = Join-Path $RepoRoot '.third_party'
$AletheiaRoot = Join-Path $ThirdPartyRoot 'Aletheia-Lens'
$PinnedCommit = 'd70e68f4342b01c79d22af58030f8ca27a5324f4'
$WeightsUrl = 'https://github.com/Cec1c/Aletheia-Lens/releases/download/models-v1/weights.onnx'
$WeightsSha256 = '94E49F37B2454C1964385673E80C2F6664AD5D5153F4D2C9DB85661AC5EFC87B'

New-Item -ItemType Directory -Force -Path $ThirdPartyRoot | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $AletheiaRoot '.git'))) {
    git clone --filter=blob:none https://github.com/Cec1c/Aletheia-Lens.git $AletheiaRoot
}

if ((git -C $AletheiaRoot status --porcelain --untracked-files=no) -and (git -C $AletheiaRoot rev-parse HEAD) -ne $PinnedCommit) {
    throw "Existing Aletheia-Lens checkout has changes; refusing to switch revisions: $AletheiaRoot"
}
git -C $AletheiaRoot fetch origin $PinnedCommit --depth 1
if ((git -C $AletheiaRoot rev-parse HEAD) -ne $PinnedCommit) {
    git -C $AletheiaRoot checkout --detach $PinnedCommit
}

$VenvPython = Join-Path $AletheiaRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $VenvPython)) {
    py -3.10 -m venv (Join-Path $AletheiaRoot '.venv')
}

$Requirements = if ($Gpu) { 'requirements-gpu.txt' } else { 'requirements.txt' }
& $VenvPython -m pip install --disable-pip-version-check --no-cache-dir -r (Join-Path $AletheiaRoot $Requirements)

$WeightsPath = Join-Path $AletheiaRoot 'models\mrcnn\weights.onnx'
if (-not (Test-Path -LiteralPath $WeightsPath) -or (Get-FileHash -Algorithm SHA256 -LiteralPath $WeightsPath).Hash -ne $WeightsSha256) {
    New-Item -ItemType Directory -Force -Path (Split-Path $WeightsPath) | Out-Null
    Invoke-WebRequest -UseBasicParsing -Uri $WeightsUrl -OutFile $WeightsPath
}

$ActualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $WeightsPath).Hash
if ($ActualHash -ne $WeightsSha256) {
    throw "Aletheia-Lens detector checksum mismatch: $ActualHash"
}

Write-Host "Aletheia-Lens mosaic backend installed at $AletheiaRoot"
Write-Host "Interpreter: $VenvPython"
