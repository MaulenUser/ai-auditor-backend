param(
    [string]$ManifestPath = ".\export\recordings\manifest.json",

    [string]$OutputDir = ".\export\transcripts",

    [string]$ApiKey,

    [string]$Model = "gpt-4o-transcribe",

    [string]$Language,

    [string]$Prompt,

    [int]$Limit = 0,

    [switch]$SkipExisting
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptBase = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
. (Join-Path $scriptBase "openai-usage-utils.ps1")

function Get-ListFromJson {
    param([string]$Path)

    $parsed = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8) | ConvertFrom-Json

    if ($null -eq $parsed) {
        return @()
    }

    if ($parsed -is [System.Array]) {
        if ($parsed.Count -eq 1 -and $parsed[0] -is [System.Array]) {
            return @($parsed[0])
        }

        return @($parsed)
    }

    return @($parsed)
}

function Write-JsonFile {
    param(
        [string]$Path,
        [object]$Data
    )

    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir | Out-Null
    }

    $json = $null

    if ($Data -is [System.Array] -and $Data.Count -eq 0) {
        $json = "[]"
    }
    else {
        $json = $Data | ConvertTo-Json -Depth 30
    }

    Set-Content -LiteralPath $Path -Value $json -Encoding UTF8
}

function Get-SafeFileNamePart {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return "unknown"
    }

    $safe = $Value
    foreach ($char in [System.IO.Path]::GetInvalidFileNameChars()) {
        $safe = $safe.Replace($char, "_")
    }

    $safe = $safe -replace "\s+", "_"
    $safe = $safe -replace "[^A-Za-z0-9_\-]", "_"
    $safe = $safe.Trim("_")

    if ([string]::IsNullOrWhiteSpace($safe)) {
        return "unknown"
    }

    return $safe
}

function Resolve-ProjectPath {
    param(
        [string]$BaseDir,
        [string]$Path
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "Path is empty."
    }

    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }

    return [System.IO.Path]::GetFullPath((Join-Path $BaseDir $Path))
}

function Get-ApiKeyValue {
    param([string]$ExplicitKey)

    if (-not [string]::IsNullOrWhiteSpace($ExplicitKey)) {
        return $ExplicitKey
    }

    if (-not [string]::IsNullOrWhiteSpace($env:OPENAI_API_KEY)) {
        return $env:OPENAI_API_KEY
    }

    throw "OpenAI API key not provided. Use -ApiKey or set OPENAI_API_KEY in the current shell session."
}

function Invoke-OpenAiTranscription {
    param(
        [string]$ApiKeyValue,
        [string]$FilePath,
        [string]$ModelName,
        [string]$LanguageCode,
        [string]$PromptText
    )

    $tempOutputPath = [System.IO.Path]::GetTempFileName()

    try {
        $curlArgs = @(
            "-sS",
            "-o", $tempOutputPath,
            "-w", "%{http_code}",
            "-X", "POST",
            "https://api.openai.com/v1/audio/transcriptions",
            "-H", "Authorization: Bearer $ApiKeyValue",
            "-F", "file=@$FilePath",
            "-F", "model=$ModelName",
            "-F", "response_format=json"
        )

        if (-not [string]::IsNullOrWhiteSpace($LanguageCode)) {
            $curlArgs += @("-F", "language=$LanguageCode")
        }

        if (-not [string]::IsNullOrWhiteSpace($PromptText)) {
            $curlArgs += @("-F", "prompt=$PromptText")
        }

        $httpCode = & curl.exe @curlArgs
        $curlExitCode = $LASTEXITCODE
        $body = [System.IO.File]::ReadAllText($tempOutputPath, [System.Text.Encoding]::UTF8)

        if ($curlExitCode -ne 0) {
            throw "curl exit code $curlExitCode. Response: $body"
        }

        if ($httpCode -notmatch "^\d{3}$") {
            throw "Unexpected HTTP code output from curl: '$httpCode'. Response: $body"
        }

        if (-not $httpCode.StartsWith("2")) {
            throw "HTTP ${httpCode}: $body"
        }

        return $body | ConvertFrom-Json
    }
    finally {
        if (Test-Path -LiteralPath $tempOutputPath) {
            Remove-Item -LiteralPath $tempOutputPath -Force
        }
    }
}

$projectRoot = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$apiKeyValue = Get-ApiKeyValue -ExplicitKey $ApiKey
$resolvedManifestPath = Resolve-ProjectPath -BaseDir $projectRoot -Path $ManifestPath
$resolvedOutputDir = Resolve-ProjectPath -BaseDir $projectRoot -Path $OutputDir
$textDir = Join-Path $resolvedOutputDir "text"
$rawDir = Join-Path $resolvedOutputDir "raw"

if (-not (Test-Path -LiteralPath $resolvedManifestPath)) {
    throw "Manifest file not found: $resolvedManifestPath"
}

if (-not (Test-Path -LiteralPath $resolvedOutputDir)) {
    New-Item -ItemType Directory -Path $resolvedOutputDir | Out-Null
}

if (-not (Test-Path -LiteralPath $textDir)) {
    New-Item -ItemType Directory -Path $textDir | Out-Null
}

if (-not (Test-Path -LiteralPath $rawDir)) {
    New-Item -ItemType Directory -Path $rawDir | Out-Null
}

$manifestEntries = Get-ListFromJson -Path $resolvedManifestPath
$entries = @(
    $manifestEntries | Where-Object {
        $_.STATUS -in @("downloaded", "skipped_existing") -and
        -not [string]::IsNullOrWhiteSpace([string]$_.FILE_PATH)
    }
)

if ($Limit -gt 0) {
    $entries = @($entries | Select-Object -First $Limit)
}

if ($entries.Count -eq 0) {
    throw "No audio files found in manifest for transcription."
}

$resultManifest = @()
$errors = @()
$usageRows = @()

foreach ($entry in $entries) {
    $crmActivityId = [string]$entry.CRM_ACTIVITY_ID
    $recordFileId = [string]$entry.RECORD_FILE_ID
    $sourceFilePath = Resolve-ProjectPath -BaseDir $projectRoot -Path ([string]$entry.FILE_PATH)

    if (-not (Test-Path -LiteralPath $sourceFilePath)) {
        $errors += [pscustomobject]@{
            CRM_ACTIVITY_ID = $crmActivityId
            RECORD_FILE_ID  = $recordFileId
            SOURCE_FILE     = $sourceFilePath
            Error           = "Audio file not found."
        }

        Write-Host "Missing audio file for CRM_ACTIVITY_ID=$crmActivityId"
        continue
    }

    $fileStem = "activity_$(Get-SafeFileNamePart -Value $crmActivityId)"
    if (-not [string]::IsNullOrWhiteSpace($recordFileId)) {
        $fileStem += "__record_$(Get-SafeFileNamePart -Value $recordFileId)"
    }

    $textPath = Join-Path $textDir "$fileStem.txt"
    $rawPath = Join-Path $rawDir "$fileStem.json"

    if ($SkipExisting -and (Test-Path -LiteralPath $textPath) -and (Test-Path -LiteralPath $rawPath)) {
        $resultManifest += [pscustomobject]@{
            CRM_ACTIVITY_ID = $crmActivityId
            RECORD_FILE_ID  = $recordFileId
            AUDIO_FILE_PATH = $sourceFilePath
            TEXT_FILE_PATH  = $textPath
            RAW_FILE_PATH   = $rawPath
            STATUS          = "skipped_existing"
            MODEL           = $Model
            LANGUAGE        = $Language
        }

        Write-Host "Skipped existing transcript for CRM_ACTIVITY_ID=$crmActivityId"
        continue
    }

    try {
        $response = Invoke-OpenAiTranscription `
            -ApiKeyValue $apiKeyValue `
            -FilePath $sourceFilePath `
            -ModelName $Model `
            -LanguageCode $Language `
            -PromptText $Prompt

        $usageRecord = Get-OpenAiUsageRecord `
            -ResponseObject $response `
            -Stage "transcription" `
            -EntityType "crm_activity" `
            -EntityId $crmActivityId `
            -SourceFilePath $sourceFilePath `
            -ModelOverride $Model `
            -ExtraFields @{
                record_file_id = $recordFileId
            }

        if ($usageRecord) {
            $usageRows += $usageRecord
        }

        $text = ""
        if ($response.PSObject.Properties["text"]) {
            $text = [string]$response.text
        }

        Set-Content -LiteralPath $textPath -Value $text -Encoding UTF8
        Write-JsonFile -Path $rawPath -Data $response

        $resultManifest += [pscustomobject]@{
            CRM_ACTIVITY_ID = $crmActivityId
            RECORD_FILE_ID  = $recordFileId
            AUDIO_FILE_PATH = $sourceFilePath
            TEXT_FILE_PATH  = $textPath
            RAW_FILE_PATH   = $rawPath
            STATUS          = "transcribed"
            MODEL           = $Model
            LANGUAGE        = $Language
            TEXT_LENGTH     = $text.Length
            INPUT_TOKENS    = if ($usageRecord) { $usageRecord.input_tokens } else { 0 }
            OUTPUT_TOKENS   = if ($usageRecord) { $usageRecord.output_tokens } else { 0 }
            TOTAL_TOKENS    = if ($usageRecord) { $usageRecord.total_tokens } else { 0 }
            AUDIO_TOKENS    = if ($usageRecord) { $usageRecord.audio_input_tokens } else { 0 }
            TEXT_TOKENS     = if ($usageRecord) { $usageRecord.text_input_tokens } else { 0 }
        }

        Write-Host "Transcribed CRM_ACTIVITY_ID=$crmActivityId"
    }
    catch {
        $errorMessage = $_.Exception.Message
        $inner = $_.Exception.InnerException
        while ($inner) {
            $errorMessage += " | Inner: $($inner.Message)"
            $inner = $inner.InnerException
        }

        $errors += [pscustomobject]@{
            CRM_ACTIVITY_ID = $crmActivityId
            RECORD_FILE_ID  = $recordFileId
            AUDIO_FILE_PATH = $sourceFilePath
            Error           = $errorMessage
        }

        Write-Host "Failed transcription for CRM_ACTIVITY_ID=$crmActivityId"
    }
}

Write-JsonFile -Path (Join-Path $resolvedOutputDir "manifest.json") -Data @($resultManifest)
Write-JsonFile -Path (Join-Path $resolvedOutputDir "errors.json") -Data @($errors)
Write-JsonFile -Path (Join-Path $resolvedOutputDir "usage-events.json") -Data @($usageRows)
Write-JsonFile -Path (Join-Path $resolvedOutputDir "usage-summary.json") -Data (Get-OpenAiUsageSummary -Rows @($usageRows))

Write-Host "Transcription completed."
Write-Host "Transcribed or skipped: $($resultManifest.Count)"
Write-Host "Errors: $($errors.Count)"
Write-Host "Usage requests logged: $($usageRows.Count)"
Write-Host "Files saved to $resolvedOutputDir"
