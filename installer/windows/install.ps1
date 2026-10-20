param(
    [switch]$AcceptCoquiLicense,
    [switch]$SkipTtsModel,
    [switch]$RepairDistributionOnly
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$ServerDir = Join-Path $RootDir "server"
$MonitorDir = Join-Path $ServerDir "external\tiktok-live-monitoring-server"
$RuntimeDir = Join-Path $ServerDir "runtime"
$PythonRuntime = Join-Path $RuntimeDir "python"
$NodeRuntime = Join-Path $RuntimeDir "node"
$TorRuntime = Join-Path $RuntimeDir "tor"
$DownloadDir = Join-Path ([IO.Path]::GetTempPath()) "SnakeReyersInstaller"

$PythonUrl = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
$PythonSha256 = "67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb"
$NodeUrl = "https://nodejs.org/dist/v22.23.3/node-v22.23.3-win-x64.zip"
$NodeSha256 = "2b0ff57b049cda1bbcea2240eec20467018713c1efe1f7360c2681859b90ed71"
$TorUrl = "https://dist.torproject.org/torbrowser/15.0.25/tor-expert-bundle-windows-x86_64-15.0.25.tar.gz"
$TorSha256 = "c1988c701de2f24f7d098d1d6d7f507b58f27740d097f29c246e0524d639b252"
$DefaultUpdateManifestUrl = "https://raw.githubusercontent.com/golgol2/Snake-Reyers/main/update-manifest.json"

function Get-VerifiedDownload {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$Sha256,
        [long]$ExpectedBytes = 0
    )

    New-Item -ItemType Directory -Force -Path (Split-Path $Destination -Parent) | Out-Null
    if (Test-Path $Destination) {
        $ExistingHash = (Get-FileHash -Algorithm SHA256 -Path $Destination).Hash.ToLowerInvariant()
        if ($ExistingHash -eq $Sha256) {
            return
        }
        Remove-Item -Force $Destination
    }

    $Temporary = "$Destination.download"
    $Curl = Get-Command "curl.exe" -ErrorAction SilentlyContinue
    for ($Attempt = 1; $Attempt -le 4; $Attempt++) {
        if (Test-Path $Temporary) {
            Remove-Item -Force $Temporary
        }

        try {
            if ($ExpectedBytes -gt 0) {
                $DownloadRoot = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($Temporary))
                $DownloadDrive = [IO.DriveInfo]::new($DownloadRoot)
                $RequiredBytes = $ExpectedBytes + 536870912
                if ($DownloadDrive.AvailableFreeSpace -lt $RequiredBytes) {
                    $FreeGb = [Math]::Round($DownloadDrive.AvailableFreeSpace / 1GB, 1)
                    $RequiredGb = [Math]::Round($RequiredBytes / 1GB, 1)
                    throw "Espaco insuficiente em $DownloadRoot. Livre: $FreeGb GB; necessario agora: $RequiredGb GB."
                }
            }

            Write-Host "Baixando $([IO.Path]::GetFileName($Destination)) (tentativa $Attempt de 4)..."
            if ($Curl) {
                & $Curl.Source `
                    --fail `
                    --location `
                    --silent `
                    --show-error `
                    --connect-timeout 30 `
                    --max-time 1800 `
                    --retry 2 `
                    --retry-delay 2 `
                    --output $Temporary `
                    $Url
                if ($LASTEXITCODE -ne 0) {
                    if ($LASTEXITCODE -eq 23) {
                        throw "O Windows nao conseguiu gravar o download. Verifique o espaco livre e se o antivirus bloqueou a pasta temporaria."
                    }
                    throw "curl.exe terminou com o codigo $LASTEXITCODE"
                }
            } else {
                Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Temporary
            }

            if (-not (Test-Path $Temporary) -or (Get-Item $Temporary).Length -le 0) {
                throw "O servidor nao enviou dados."
            }
            $ActualHash = (Get-FileHash -Algorithm SHA256 -Path $Temporary).Hash.ToLowerInvariant()
            if ($ActualHash -ne $Sha256) {
                throw "O SHA-256 recebido nao confere."
            }

            Move-Item -Force -Path $Temporary -Destination $Destination
            return
        } catch {
            if (Test-Path $Temporary) {
                Remove-Item -Force $Temporary
            }
            if (
                $_.Exception.Message -match "Espaco insuficiente|nao conseguiu gravar"
            ) {
                throw $_.Exception.Message
            }
            if ($Attempt -eq 4) {
                throw "Falha ao baixar ou validar $Url depois de 4 tentativas: $($_.Exception.Message)"
            }
            Write-Warning "Download interrompido. Nova tentativa sera feita automaticamente."
            Start-Sleep -Seconds (3 * $Attempt)
        }
    }
}

function Repair-DistributionPayload {
    $RequiredFiles = @(
        (Join-Path $RootDir "game\windows\SnakeReyers.exe"),
        (Join-Path $ServerDir "assets\audio-manifest.json")
    )
    $MissingFiles = @(
        $RequiredFiles | Where-Object { -not (Test-Path $_ -PathType Leaf) }
    )
    if ($MissingFiles.Count -eq 0) {
        return
    }

    Write-Warning "O pacote baixado esta incompleto (normalmente Code > Download ZIP)."
    Write-Host "Baixando o pacote oficial completo do Snake Reyers..."

    $ManifestUrl = if ($env:SNAKE_REYERS_UPDATE_MANIFEST_URL) {
        $env:SNAKE_REYERS_UPDATE_MANIFEST_URL
    } else {
        $DefaultUpdateManifestUrl
    }
    if (-not $ManifestUrl.StartsWith("https://")) {
        throw "O manifesto oficial precisa usar HTTPS: $ManifestUrl"
    }

    $BootstrapDir = Join-Path $DownloadDir "distribution-repair"
    $ManifestPath = Join-Path $BootstrapDir "update-manifest.json"
    $ExtractDir = Join-Path $BootstrapDir "extracted"
    New-Item -ItemType Directory -Force -Path $BootstrapDir | Out-Null
    Invoke-WebRequest -UseBasicParsing -Uri $ManifestUrl -OutFile $ManifestPath
    $Manifest = Get-Content -Raw $ManifestPath | ConvertFrom-Json
    $Package = $Manifest.packages.'windows-x86_64'
    if (-not $Package) {
        throw "O manifesto oficial nao possui um pacote para Windows x64."
    }

    $PackageUrl = [string]$Package.url
    $PackageHash = ([string]$Package.sha256).ToLowerInvariant()
    if (
        -not $PackageUrl.StartsWith("https://") -or
        $PackageHash -notmatch "^[0-9a-f]{64}$"
    ) {
        throw "Os dados do pacote Windows no manifesto oficial sao invalidos."
    }

    $Archive = Join-Path $BootstrapDir "SnakeReyers-Windows-x64.zip"
    Get-VerifiedDownload -Url $PackageUrl -Destination $Archive -Sha256 $PackageHash
    if (Test-Path $ExtractDir) {
        Remove-Item -Recurse -Force $ExtractDir
    }
    Expand-Archive -Force -Path $Archive -DestinationPath $ExtractDir

    $SourceRoot = Join-Path $ExtractDir "SnakeReyers"
    $SourceGame = Join-Path $SourceRoot "game\windows\SnakeReyers.exe"
    $SourceAudioManifest = Join-Path $SourceRoot "server\assets\audio-manifest.json"
    if (
        -not (Test-Path $SourceGame -PathType Leaf) -or
        -not (Test-Path $SourceAudioManifest -PathType Leaf)
    ) {
        throw "O pacote oficial nao contem todos os arquivos obrigatorios."
    }

    Get-ChildItem -Path $SourceRoot -Recurse -File -Force | ForEach-Object {
        $RelativePath = $_.FullName.Substring($SourceRoot.Length).TrimStart([char]'\')
        $Destination = Join-Path $RootDir $RelativePath
        New-Item -ItemType Directory -Force -Path (Split-Path $Destination -Parent) | Out-Null
        Copy-Item -Force -Path $_.FullName -Destination $Destination
    }

    $StillMissing = @(
        $RequiredFiles | Where-Object { -not (Test-Path $_ -PathType Leaf) }
    )
    if ($StillMissing.Count -gt 0) {
        throw "Nao foi possivel reparar o pacote incompleto: $($StillMissing -join ', ')"
    }

    Write-Host "Pacote oficial recuperado e validado com sucesso."
}

function Invoke-PipInstall {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    & $Python -m pip install --disable-pip-version-check @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Falha ao instalar dependencias Python: $($Arguments -join ' ')"
    }
}

function Install-AudioPackages {
    $ManifestPath = Join-Path $ServerDir "assets\audio-manifest.json"
    if (-not (Test-Path $ManifestPath)) {
        throw "Manifesto dos pacotes de audio ausente: $ManifestPath"
    }

    $ManifestHash = (Get-FileHash -Algorithm SHA256 -Path $ManifestPath).Hash.ToLowerInvariant()
    $MarkerPath = Join-Path $ServerDir "assets\.audio-package-version"
    $BackgroundReady = @(
        Get-ChildItem -Path (Join-Path $ServerDir "assets\music\background") -File -ErrorAction SilentlyContinue
    ).Count -gt 0
    $GiftSongReady = @(
        Get-ChildItem -Path (Join-Path $ServerDir "assets\singing\processed\FANCLUB") -Filter "manifest.json" -File -Recurse -ErrorAction SilentlyContinue
    ).Count -gt 0

    if (
        (Test-Path $MarkerPath) -and
        ((Get-Content -Raw $MarkerPath).Trim() -eq $ManifestHash) -and
        $BackgroundReady -and
        $GiftSongReady
    ) {
        Write-Host "Vozes e musicas ja estao instaladas."
        return
    }

    $Manifest = Get-Content -Raw $ManifestPath | ConvertFrom-Json
    foreach ($Pack in $Manifest.packs) {
        $PackName = [string]$Pack.name
        $PackHash = ([string]$Pack.sha256).ToLowerInvariant()
        $PackBytes = [long]($Pack.bytes)
        $Candidates = @(
            (Join-Path (Split-Path $RootDir -Parent) $PackName),
            (Join-Path $RootDir $PackName),
            (Join-Path (Join-Path $RootDir "packages") $PackName)
        )
        $Archive = $Candidates | Where-Object { Test-Path $_ } | Select-Object -First 1

        $DownloadedArchive = $false
        if (-not $Archive) {
            $Archive = Join-Path $DownloadDir $PackName
            $DownloadedArchive = $true
            Get-VerifiedDownload `
                -Url ([string]$Pack.url) `
                -Destination $Archive `
                -Sha256 $PackHash `
                -ExpectedBytes $PackBytes
        } else {
            $ActualHash = (Get-FileHash -Algorithm SHA256 -Path $Archive).Hash.ToLowerInvariant()
            if ($ActualHash -ne $PackHash) {
                throw "Pacote de audio local invalido: $Archive"
            }
        }

        Write-Host "Instalando $PackName..."
        try {
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            $Zip = [IO.Compression.ZipFile]::OpenRead($Archive)
            try {
                $ExpandedBytes = [long](
                    ($Zip.Entries | Measure-Object -Property Length -Sum).Sum
                )
            } finally {
                $Zip.Dispose()
            }

            $InstallRoot = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($RootDir))
            $InstallDrive = [IO.DriveInfo]::new($InstallRoot)
            $RequiredInstallBytes = $ExpandedBytes + 536870912
            if ($InstallDrive.AvailableFreeSpace -lt $RequiredInstallBytes) {
                $FreeGb = [Math]::Round($InstallDrive.AvailableFreeSpace / 1GB, 1)
                $RequiredGb = [Math]::Round($RequiredInstallBytes / 1GB, 1)
                throw "Espaco insuficiente para extrair $PackName em $InstallRoot. Livre: $FreeGb GB; necessario agora: $RequiredGb GB."
            }

            Expand-Archive -Force -Path $Archive -DestinationPath $RootDir
        } finally {
            if ($DownloadedArchive -and (Test-Path $Archive)) {
                Remove-Item -Force $Archive
            }
        }
    }

    Set-Content -Encoding ASCII -NoNewline -Path $MarkerPath -Value $ManifestHash
}

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
New-Item -ItemType Directory -Force -Path $RuntimeDir, $DownloadDir | Out-Null
Repair-DistributionPayload
if ($RepairDistributionOnly) {
    Write-Host "Arquivos obrigatorios da distribuicao estao prontos."
    exit 0
}

$PythonExe = Join-Path $PythonRuntime "python.exe"
if (-not (Test-Path $PythonExe)) {
    $PythonInstaller = Join-Path $DownloadDir "python-3.12.10-amd64.exe"
    Get-VerifiedDownload -Url $PythonUrl -Destination $PythonInstaller -Sha256 $PythonSha256
    New-Item -ItemType Directory -Force -Path $PythonRuntime | Out-Null
    $PythonInstall = Start-Process -Wait -PassThru -FilePath $PythonInstaller -ArgumentList @(
        "/quiet",
        "InstallAllUsers=0",
        "Include_launcher=0",
        "Include_test=0",
        "Include_doc=0",
        "Include_tcltk=0",
        "Include_pip=1",
        "PrependPath=0",
        "TargetDir=`"$PythonRuntime`""
    )
    if ($PythonInstall.ExitCode -ne 0 -or -not (Test-Path $PythonExe)) {
        throw "Nao foi possivel instalar o Python portatil. Codigo: $($PythonInstall.ExitCode)"
    }
}

$NodeExe = Join-Path $NodeRuntime "node.exe"
if (-not (Test-Path $NodeExe)) {
    $NodeArchive = Join-Path $DownloadDir "node-v22.23.3-win-x64.zip"
    $NodeExtract = Join-Path $DownloadDir "node"
    Get-VerifiedDownload -Url $NodeUrl -Destination $NodeArchive -Sha256 $NodeSha256
    if (Test-Path $NodeExtract) {
        Remove-Item -Recurse -Force $NodeExtract
    }
    if (Test-Path $NodeRuntime) {
        Remove-Item -Recurse -Force $NodeRuntime
    }
    Expand-Archive -Force -Path $NodeArchive -DestinationPath $NodeExtract
    $NodeSource = Get-ChildItem -Directory $NodeExtract | Select-Object -First 1
    if (-not $NodeSource) {
        throw "Pacote do Node.js sem diretorio principal."
    }
    New-Item -ItemType Directory -Force -Path $NodeRuntime | Out-Null
    Copy-Item -Recurse -Force -Path (Join-Path $NodeSource.FullName "*") -Destination $NodeRuntime
}

$TorExe = Join-Path $TorRuntime "tor\tor.exe"
if (-not (Test-Path $TorExe)) {
    $TorArchive = Join-Path $DownloadDir "tor-expert-bundle-windows-x86_64-15.0.25.tar.gz"
    Get-VerifiedDownload -Url $TorUrl -Destination $TorArchive -Sha256 $TorSha256
    if (Test-Path $TorRuntime) {
        Remove-Item -Recurse -Force $TorRuntime
    }
    New-Item -ItemType Directory -Force -Path $TorRuntime | Out-Null
    & tar -xzf $TorArchive -C $TorRuntime
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $TorExe)) {
        throw "Nao foi possivel extrair o Tor Expert Bundle."
    }
}

$NodeMajor = [int](& $NodeExe -p "Number(process.versions.node.split('.')[0])")
if ($NodeMajor -lt 22) {
    throw "O Node.js local precisa ser versao 22 ou superior."
}

$VenvDir = Join-Path $ServerDir ".venv"
$Python = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $Python)) {
    & $PythonExe -m venv $VenvDir
}

Invoke-PipInstall -Arguments @("--upgrade", "pip")
Invoke-PipInstall -Arguments @("-r", (Join-Path $ServerDir "requirements.txt"))

Write-Host "Instalando o mecanismo XTTS..."
& $Python -m pip install --disable-pip-version-check `
    --index-url "https://download.pytorch.org/whl/cu126" `
    "torch==2.7.1" "torchaudio==2.7.1"
if ($LASTEXITCODE -ne 0) {
    Write-Warning "CUDA nao ficou disponivel; instalando o modo CPU do TTS."
    Invoke-PipInstall -Arguments @(
        "--index-url", "https://download.pytorch.org/whl/cpu",
        "torch==2.7.1", "torchaudio==2.7.1"
    )
}
Invoke-PipInstall -Arguments @("-r", (Join-Path $ServerDir "requirements-voice.txt"))

$env:TTS_HOME = Join-Path $RuntimeDir "tts"
$TtsModelFile = Join-Path $env:TTS_HOME "tts\tts_models--multilingual--multi-dataset--xtts_v2\model.pth"
if (-not $SkipTtsModel -and -not (Test-Path $TtsModelFile)) {
    if (-not $AcceptCoquiLicense) {
        Write-Host "O XTTS usa a licenca Coqui Public Model License (CPML)."
        $Answer = Read-Host "Voce aceita a CPML para baixar o modelo XTTS agora? [S/N]"
        if ($Answer -notmatch "^[SsYy]") {
            throw "A instalacao completa exige aceitar a licenca do modelo XTTS."
        }
    }
    $env:COQUI_TOS_AGREED = "1"
    & $Python (Join-Path $ServerDir "scripts\prepare_tts_model.py")
    if ($LASTEXITCODE -ne 0) {
        throw "Nao foi possivel preparar o modelo XTTS."
    }
} elseif (Test-Path $TtsModelFile) {
    Write-Host "Modelo XTTS ja esta preparado."
}

Install-AudioPackages

$env:PATH = "$NodeRuntime;$env:PATH"
$Npm = Join-Path $NodeRuntime "npm.cmd"
Push-Location $MonitorDir
try {
    & $Npm ci --omit=dev
    if ($LASTEXITCODE -ne 0) {
        throw "Falha ao instalar as dependencias do monitor TikTok."
    }
} finally {
    Pop-Location
}

New-Item -ItemType Directory -Force -Path `
    (Join-Path $ServerDir "private"), `
    (Join-Path $ServerDir "runs\logs") | Out-Null

if (Test-Path $DownloadDir) {
    Remove-Item -Recurse -Force $DownloadDir
}

$ShortcutInstaller = Join-Path $RootDir "launcher\create_shortcut.ps1"
try {
    & $ShortcutInstaller -InstallRoot $RootDir
} catch {
    Write-Warning "Nao foi possivel criar o atalho: $($_.Exception.Message)"
}

Write-Host "Snake Reyers instalado com TTS, vozes, musicas, Python, Node.js e Tor locais."
Write-Host "Use o atalho Snake Reyers na Area de Trabalho para iniciar."
