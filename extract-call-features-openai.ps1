param(
    [string]$TranscriptManifestPath = ".\export\transcripts\manifest.json",

    [string]$CallMetadataPath = ".\export\call-records-scan\recording-candidates.json",

    [string]$ActivityMetadataPath = ".\export\call-records-scan\activities.source.json",

    [string]$OutputDir = ".\export\call-features",

    [string]$ApiKey,

    [string]$Model = "gpt-4o-mini",

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
        $json = $Data | ConvertTo-Json -Depth 40
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

function Get-CallDirection {
    param(
        [object]$Metadata,
        [object]$ActivityMetadata
    )

    if ($null -eq $Metadata -and $null -eq $ActivityMetadata) {
        return "unknown"
    }

    $direction = ""

    if ($ActivityMetadata) {
        $activityDirection = $ActivityMetadata.PSObject.Properties["DIRECTION"]
        if ($activityDirection) {
            $direction = [string]$activityDirection.Value
        }
    }

    if ($direction -eq "1") {
        return "inbound"
    }

    if ($direction -eq "2") {
        return "outbound"
    }

    $subject = ""
    if ($Metadata) {
        $subject = [string]$Metadata.SUBJECT
    }
    elseif ($ActivityMetadata) {
        $subject = [string]$ActivityMetadata.SUBJECT
    }

    if (-not [string]::IsNullOrWhiteSpace($subject)) {
        if ($subject.StartsWith("Входящий", [System.StringComparison]::OrdinalIgnoreCase) -or
            $subject.Contains("Входящий")) {
            return "inbound"
        }

        if ($subject.StartsWith("Исходящий", [System.StringComparison]::OrdinalIgnoreCase) -or
            $subject.Contains("Исходящий")) {
            return "outbound"
        }
    }

    if ($subject -match "Inbound") {
        return "inbound"
    }

    if ($subject -match "Outbound") {
        return "outbound"
    }

    return "unknown"
}

function Get-FeatureSchema {
    return @{
        type = "object"
        additionalProperties = $false
        required = @(
            "summary",
            "primary_topic",
            "client_request",
            "relevance_to_company",
            "qualification",
            "sales_process",
            "objections",
            "outcome",
            "sentiment",
            "quality_flags",
            "tags"
        )
        properties = @{
            summary = @{
                type = "string"
                description = "Short factual summary in Russian."
            }
            primary_topic = @{
                type = "string"
                description = "Main conversation topic in Russian."
            }
            client_request = @{
                type = "string"
                description = "What the client actually wanted or asked for, in Russian."
            }
            relevance_to_company = @{
                type = "string"
                enum = @("target_client", "not_target_client", "unclear")
            }
            qualification = @{
                type = "object"
                additionalProperties = $false
                required = @(
                    "need_identified",
                    "budget_discussed",
                    "timeline_discussed",
                    "decision_maker_identified"
                )
                properties = @{
                    need_identified = @{
                        type = "string"
                        enum = @("yes", "no", "unknown")
                    }
                    budget_discussed = @{
                        type = "string"
                        enum = @("yes", "no", "unknown")
                    }
                    timeline_discussed = @{
                        type = "string"
                        enum = @("yes", "no", "unknown")
                    }
                    decision_maker_identified = @{
                        type = "string"
                        enum = @("yes", "no", "unknown")
                    }
                }
            }
            sales_process = @{
                type = "object"
                additionalProperties = $false
                required = @(
                    "manager_introduced_self",
                    "manager_asked_questions",
                    "manager_presented_service",
                    "manager_rushed_to_pitch",
                    "manager_agreed_next_step"
                )
                properties = @{
                    manager_introduced_self = @{
                        type = "string"
                        enum = @("yes", "no", "unknown")
                    }
                    manager_asked_questions = @{
                        type = "string"
                        enum = @("yes", "no", "unknown")
                    }
                    manager_presented_service = @{
                        type = "string"
                        enum = @("yes", "no", "unknown")
                    }
                    manager_rushed_to_pitch = @{
                        type = "string"
                        enum = @("yes", "no", "unknown")
                    }
                    manager_agreed_next_step = @{
                        type = "string"
                        enum = @("yes", "no", "unknown")
                    }
                }
            }
            objections = @{
                type = "object"
                additionalProperties = $false
                required = @("has_objections", "items")
                properties = @{
                    has_objections = @{
                        type = "string"
                        enum = @("yes", "no", "unknown")
                    }
                    items = @{
                        type = "array"
                        items = @{
                            type = "string"
                        }
                    }
                }
            }
            outcome = @{
                type = "object"
                additionalProperties = $false
                required = @("status", "next_step", "next_step_confirmed")
                properties = @{
                    status = @{
                        type = "string"
                        enum = @(
                            "qualified_interest",
                            "callback_requested",
                            "follow_up",
                            "not_target_client",
                            "not_interested",
                            "technical_or_short",
                            "other",
                            "unknown"
                        )
                    }
                    next_step = @{
                        type = "string"
                    }
                    next_step_confirmed = @{
                        type = "string"
                        enum = @("yes", "no", "unknown")
                    }
                }
            }
            sentiment = @{
                type = "object"
                additionalProperties = $false
                required = @("client_interest_level", "client_emotion")
                properties = @{
                    client_interest_level = @{
                        type = "string"
                        enum = @("low", "medium", "high", "unknown")
                    }
                    client_emotion = @{
                        type = "string"
                        enum = @("negative", "neutral", "positive", "unknown")
                    }
                }
            }
            quality_flags = @{
                type = "object"
                additionalProperties = $false
                required = @("short_or_low_content", "audio_or_transcript_unclear", "non_sales_call")
                properties = @{
                    short_or_low_content = @{
                        type = "boolean"
                    }
                    audio_or_transcript_unclear = @{
                        type = "boolean"
                    }
                    non_sales_call = @{
                        type = "boolean"
                    }
                }
            }
            tags = @{
                type = "array"
                items = @{
                    type = "string"
                }
            }
        }
    }
}

function Invoke-OpenAiFeatureExtraction {
    param(
        [string]$ApiKeyValue,
        [string]$ModelName,
        [string]$TranscriptText,
        [object]$Context
    )

    $systemPrompt = @"
You analyze sales call transcripts for an MVP analytics pipeline.

Return JSON only and follow the schema exactly.
Use only evidence from the transcript and provided metadata.
If something is not clear from the transcript, use "unknown".
Do not invent prices, budgets, timings, or client intent.
Free-text fields must be in Russian.
Tags must be short Russian labels.

Interpretation rules:
- target_client: the call is relevant to the company's target service offer.
- not_target_client: the caller/request is outside the company's target service or is a partner/counterparty instead of a client.
- technical_or_short: the call is too short or too poor in content for meaningful sales analysis.
- callback_requested: a callback was explicitly requested.
- follow_up: there is a clear next step but not enough evidence for strong qualified interest.
- qualified_interest: the client shows meaningful service interest and the call has real sales value.
- non_sales_call=true when the call is administrative, partner-related, technical, or otherwise not a real sales conversation.
"@

    $userPrompt = @"
Metadata:
- CRM activity ID: $($Context.crm_activity_id)
- Call direction: $($Context.call_direction)
- Manager ID: $($Context.responsible_id)
- Phone number: $($Context.phone_number)
- Subject: $($Context.subject)
- Started at: $($Context.started_at)

Transcript:
$TranscriptText
"@

    $body = @{
        model = $ModelName
        store = $false
        input = @(
            @{
                role = "developer"
                content = @(
                    @{
                        type = "input_text"
                        text = $systemPrompt
                    }
                )
            },
            @{
                role = "user"
                content = @(
                    @{
                        type = "input_text"
                        text = $userPrompt
                    }
                )
            }
        )
        text = @{
            format = @{
                type = "json_schema"
                name = "sales_call_features"
                strict = $true
                schema = Get-FeatureSchema
            }
        }
    }

    $requestFile = [System.IO.Path]::GetTempFileName()
    $responseFile = [System.IO.Path]::GetTempFileName()

    try {
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($requestFile, ($body | ConvertTo-Json -Depth 50), $utf8NoBom)

        $curlArgs = @(
            "-sS",
            "-o", $responseFile,
            "-w", "%{http_code}",
            "-X", "POST",
            "https://api.openai.com/v1/responses",
            "-H", "Authorization: Bearer $ApiKeyValue",
            "-H", "Content-Type: application/json",
            "--data-binary", "@$requestFile"
        )

        $httpCode = & curl.exe @curlArgs
        $curlExitCode = $LASTEXITCODE
        $bodyText = [System.IO.File]::ReadAllText($responseFile, [System.Text.Encoding]::UTF8)

        if ($curlExitCode -ne 0) {
            throw "curl exit code $curlExitCode. Response: $bodyText"
        }

        if ($httpCode -notmatch "^\d{3}$") {
            throw "Unexpected HTTP code output from curl: '$httpCode'. Response: $bodyText"
        }

        if (-not $httpCode.StartsWith("2")) {
            throw "HTTP ${httpCode}: $bodyText"
        }

        $response = $bodyText | ConvertFrom-Json
        $jsonText = $null

        if ($response.PSObject.Properties["output_text"] -and -not [string]::IsNullOrWhiteSpace([string]$response.output_text)) {
            $jsonText = [string]$response.output_text
        }
        else {
            foreach ($item in @($response.output)) {
                if ($item.type -ne "message") {
                    continue
                }

                foreach ($content in @($item.content)) {
                    if ($content.type -eq "output_text" -and -not [string]::IsNullOrWhiteSpace([string]$content.text)) {
                        $jsonText = [string]$content.text
                        break
                    }
                }

                if ($jsonText) {
                    break
                }
            }
        }

        if (-not $jsonText) {
            throw "Structured output text not found in response: $bodyText"
        }

        return [pscustomobject]@{
            ResponseObject = $response
            ParsedFeatures = ($jsonText | ConvertFrom-Json)
        }
    }
    finally {
        foreach ($temp in @($requestFile, $responseFile)) {
            if (Test-Path -LiteralPath $temp) {
                Remove-Item -LiteralPath $temp -Force
            }
        }
    }
}

$projectRoot = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$apiKeyValue = Get-ApiKeyValue -ExplicitKey $ApiKey
$resolvedTranscriptManifestPath = Resolve-ProjectPath -BaseDir $projectRoot -Path $TranscriptManifestPath
$resolvedOutputDir = Resolve-ProjectPath -BaseDir $projectRoot -Path $OutputDir
$featuresDir = Join-Path $resolvedOutputDir "features"
$rawDir = Join-Path $resolvedOutputDir "raw"

if (-not (Test-Path -LiteralPath $resolvedTranscriptManifestPath)) {
    throw "Transcript manifest not found: $resolvedTranscriptManifestPath"
}

foreach ($path in @($resolvedOutputDir, $featuresDir, $rawDir)) {
    if (-not (Test-Path -LiteralPath $path)) {
        New-Item -ItemType Directory -Path $path | Out-Null
    }
}

$transcriptManifest = Get-ListFromJson -Path $resolvedTranscriptManifestPath
$transcriptEntries = @(
    $transcriptManifest | Where-Object {
        $_.STATUS -in @("transcribed", "skipped_existing") -and
        -not [string]::IsNullOrWhiteSpace([string]$_.TEXT_FILE_PATH)
    }
)

if ($Limit -gt 0) {
    $transcriptEntries = @($transcriptEntries | Select-Object -First $Limit)
}

if ($transcriptEntries.Count -eq 0) {
    throw "No transcripts available for feature extraction."
}

$metadataIndex = @{}
$activityIndex = @{}
$resolvedCallMetadataPath = $null
if (-not [string]::IsNullOrWhiteSpace($CallMetadataPath)) {
    $resolvedCallMetadataPath = Resolve-ProjectPath -BaseDir $projectRoot -Path $CallMetadataPath
    if (Test-Path -LiteralPath $resolvedCallMetadataPath) {
        foreach ($item in Get-ListFromJson -Path $resolvedCallMetadataPath) {
            $metadataIndex[[string]$item.CRM_ACTIVITY_ID] = $item
        }
    }
}

$resolvedActivityMetadataPath = $null
if (-not [string]::IsNullOrWhiteSpace($ActivityMetadataPath)) {
    $resolvedActivityMetadataPath = Resolve-ProjectPath -BaseDir $projectRoot -Path $ActivityMetadataPath
    if (Test-Path -LiteralPath $resolvedActivityMetadataPath) {
        foreach ($item in Get-ListFromJson -Path $resolvedActivityMetadataPath) {
            $activityIndex[[string]$item.ID] = $item
        }
    }
}

$featureReport = @()
$errors = @()
$usageRows = @()

foreach ($entry in $transcriptEntries) {
    $crmActivityId = [string]$entry.CRM_ACTIVITY_ID
    $recordFileId = [string]$entry.RECORD_FILE_ID
    $transcriptPath = Resolve-ProjectPath -BaseDir $projectRoot -Path ([string]$entry.TEXT_FILE_PATH)

    if (-not (Test-Path -LiteralPath $transcriptPath)) {
        $errors += [pscustomobject]@{
            CRM_ACTIVITY_ID = $crmActivityId
            RECORD_FILE_ID  = $recordFileId
            Error           = "Transcript file not found."
        }

        Write-Host "Missing transcript for CRM_ACTIVITY_ID=$crmActivityId"
        continue
    }

    $fileStem = "activity_$(Get-SafeFileNamePart -Value $crmActivityId)"
    if (-not [string]::IsNullOrWhiteSpace($recordFileId)) {
        $fileStem += "__record_$(Get-SafeFileNamePart -Value $recordFileId)"
    }

    $featurePath = Join-Path $featuresDir "$fileStem.json"
    $rawPath = Join-Path $rawDir "$fileStem.json"

    if ($SkipExisting -and (Test-Path -LiteralPath $featurePath) -and (Test-Path -LiteralPath $rawPath)) {
        $existing = [System.IO.File]::ReadAllText($featurePath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
        $featureReport += [pscustomobject]@{
            CRM_ACTIVITY_ID      = $crmActivityId
            RECORD_FILE_ID       = $recordFileId
            FEATURE_FILE_PATH    = $featurePath
            RAW_FILE_PATH        = $rawPath
            STATUS               = "skipped_existing"
            PRIMARY_TOPIC        = [string]$existing.primary_topic
            RELEVANCE            = [string]$existing.relevance_to_company
            OUTCOME_STATUS       = [string]$existing.outcome.status
            CLIENT_INTEREST      = [string]$existing.sentiment.client_interest_level
            SHORT_OR_LOW_CONTENT = [bool]$existing.quality_flags.short_or_low_content
        }

        Write-Host "Skipped existing features for CRM_ACTIVITY_ID=$crmActivityId"
        continue
    }

    $transcriptText = [System.IO.File]::ReadAllText($transcriptPath, [System.Text.Encoding]::UTF8).Trim()
    $metadata = $null
    if ($metadataIndex.ContainsKey($crmActivityId)) {
        $metadata = $metadataIndex[$crmActivityId]
    }

    $activityMetadata = $null
    if ($activityIndex.ContainsKey($crmActivityId)) {
        $activityMetadata = $activityIndex[$crmActivityId]
    }

    $context = [pscustomobject]@{
        crm_activity_id = $crmActivityId
        call_id         = if ($metadata) { [string]$metadata.CALL_ID } else { "" }
        record_file_id  = $recordFileId
        responsible_id  = if ($metadata) { [string]$metadata.RESPONSIBLE_ID } else { "" }
        phone_number    = if ($metadata) { [string]$metadata.PHONE_NUMBER } else { "" }
        started_at      = if ($metadata) { [string]$metadata.START_TIME } else { "" }
        subject         = if ($metadata) { [string]$metadata.SUBJECT } else { "" }
        call_direction  = Get-CallDirection -Metadata $metadata -ActivityMetadata $activityMetadata
    }

    try {
        $apiResult = Invoke-OpenAiFeatureExtraction `
            -ApiKeyValue $apiKeyValue `
            -ModelName $Model `
            -TranscriptText $transcriptText `
            -Context $context

        $usageRecord = Get-OpenAiUsageRecord `
            -ResponseObject $apiResult.ResponseObject `
            -Stage "call_feature_extraction" `
            -EntityType "crm_activity" `
            -EntityId $crmActivityId `
            -SourceFilePath $transcriptPath `
            -ModelOverride $Model `
            -ExtraFields @{
                record_file_id = $recordFileId
            }

        if ($usageRecord) {
            $usageRows += $usageRecord
        }

        $parsed = $apiResult.ParsedFeatures
        $finalFeature = [ordered]@{
            schema_version = "mvp_call_features_v1"
            source = [ordered]@{
                crm_activity_id     = $crmActivityId
                call_id             = [string]$context.call_id
                record_file_id      = $recordFileId
                responsible_id      = [string]$context.responsible_id
                phone_number        = [string]$context.phone_number
                started_at          = [string]$context.started_at
                call_direction      = [string]$context.call_direction
                subject             = [string]$context.subject
                transcript_file_path = $transcriptPath
            }
            summary = [string]$parsed.summary
            primary_topic = [string]$parsed.primary_topic
            client_request = [string]$parsed.client_request
            relevance_to_company = [string]$parsed.relevance_to_company
            qualification = $parsed.qualification
            sales_process = $parsed.sales_process
            objections = $parsed.objections
            outcome = $parsed.outcome
            sentiment = $parsed.sentiment
            quality_flags = $parsed.quality_flags
            tags = @($parsed.tags)
        }

        Write-JsonFile -Path $featurePath -Data ([pscustomobject]$finalFeature)
        Write-JsonFile -Path $rawPath -Data $apiResult.ResponseObject

        $featureReport += [pscustomobject]@{
            CRM_ACTIVITY_ID      = $crmActivityId
            RECORD_FILE_ID       = $recordFileId
            FEATURE_FILE_PATH    = $featurePath
            RAW_FILE_PATH        = $rawPath
            STATUS               = "extracted"
            PRIMARY_TOPIC        = [string]$parsed.primary_topic
            RELEVANCE            = [string]$parsed.relevance_to_company
            OUTCOME_STATUS       = [string]$parsed.outcome.status
            CLIENT_INTEREST      = [string]$parsed.sentiment.client_interest_level
            SHORT_OR_LOW_CONTENT = [bool]$parsed.quality_flags.short_or_low_content
            INPUT_TOKENS         = if ($usageRecord) { $usageRecord.input_tokens } else { 0 }
            OUTPUT_TOKENS        = if ($usageRecord) { $usageRecord.output_tokens } else { 0 }
            TOTAL_TOKENS         = if ($usageRecord) { $usageRecord.total_tokens } else { 0 }
            CACHED_TOKENS        = if ($usageRecord) { $usageRecord.cached_tokens } else { 0 }
            REASONING_TOKENS     = if ($usageRecord) { $usageRecord.reasoning_tokens } else { 0 }
        }

        Write-Host "Extracted features for CRM_ACTIVITY_ID=$crmActivityId"
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
            Error           = $errorMessage
        }

        Write-Host "Failed feature extraction for CRM_ACTIVITY_ID=$crmActivityId"
    }
}

Write-JsonFile -Path (Join-Path $resolvedOutputDir "feature-report.json") -Data @($featureReport)
Write-JsonFile -Path (Join-Path $resolvedOutputDir "errors.json") -Data @($errors)
Write-JsonFile -Path (Join-Path $resolvedOutputDir "usage-events.json") -Data @($usageRows)
Write-JsonFile -Path (Join-Path $resolvedOutputDir "usage-summary.json") -Data (Get-OpenAiUsageSummary -Rows @($usageRows))

Write-Host "Feature extraction completed."
Write-Host "Extracted or skipped: $($featureReport.Count)"
Write-Host "Errors: $($errors.Count)"
Write-Host "Usage requests logged: $($usageRows.Count)"
Write-Host "Files saved to $resolvedOutputDir"
