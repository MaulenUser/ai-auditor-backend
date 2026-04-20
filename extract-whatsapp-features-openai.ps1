param(
    [string]$ConversationReportPath = ".\export\whatsapp-timeline\report.json",

    [string]$ConversationDir = ".\export\whatsapp-timeline\conversations",

    [string]$OutputDir = ".\export\whatsapp-features",

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
        $json = $Data | ConvertTo-Json -Depth 50
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

function Get-ConversationEntries {
    param(
        [string]$ProjectRoot,
        [string]$ReportPath,
        [string]$DirectoryPath
    )

    $entries = @()
    $resolvedReportPath = Resolve-ProjectPath -BaseDir $ProjectRoot -Path $ReportPath

    if (Test-Path -LiteralPath $resolvedReportPath) {
        $reportObject = [System.IO.File]::ReadAllText($resolvedReportPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json

        if ($reportObject.PSObject.Properties["rows"]) {
            foreach ($row in @($reportObject.rows)) {
                $conversationPath = Resolve-ProjectPath -BaseDir $ProjectRoot -Path ([string]$row.output_file)
                $entries += [pscustomobject]@{
                    DEAL_ID          = [string]$row.deal_id
                    CONTACT_ID       = [string]$row.contact_id
                    TOTAL_MESSAGES   = [int]$row.total_messages
                    CONVERSATION_PATH = $conversationPath
                }
            }
        }
    }

    if ($entries.Count -gt 0) {
        return $entries
    }

    $resolvedConversationDir = Resolve-ProjectPath -BaseDir $ProjectRoot -Path $DirectoryPath
    if (-not (Test-Path -LiteralPath $resolvedConversationDir)) {
        throw "Conversation directory not found: $resolvedConversationDir"
    }

    foreach ($file in Get-ChildItem -LiteralPath $resolvedConversationDir -Filter "*.json" | Sort-Object Name) {
        $conversation = [System.IO.File]::ReadAllText($file.FullName, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
        $entries += [pscustomobject]@{
            DEAL_ID           = [string]$conversation.deal_id
            CONTACT_ID        = [string]$conversation.contact_id
            TOTAL_MESSAGES    = [int]$conversation.stats.total_messages
            CONVERSATION_PATH = $file.FullName
        }
    }

    return $entries
}

function Convert-MessageToPromptLine {
    param([object]$Message)

    $role = [string]$Message.sender_role
    if ([string]::IsNullOrWhiteSpace($role)) {
        $role = "unknown"
    }

    $createdAt = [string]$Message.created_at
    $text = [string]$Message.text

    $lines = @()
    $lines += "[${createdAt}] ${role}: ${text}"

    foreach ($attachment in @($Message.attachments)) {
        if ($null -eq $attachment) {
            continue
        }

        $attachmentType = [string]$attachment.type
        $attachmentLabel = [string]$attachment.label
        $attachmentUrl = [string]$attachment.url
        $lines += "attachment: type=$attachmentType; label=$attachmentLabel; url=$attachmentUrl"
    }

    return ($lines -join "`n")
}

function Convert-ConversationToPromptText {
    param([object]$Conversation)

    $parts = @()

    foreach ($message in @($Conversation.messages)) {
        $parts += Convert-MessageToPromptLine -Message $message
    }

    return ($parts -join "`n`n").Trim()
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
                description = "Main chat topic in Russian."
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
                            "awaiting_response",
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
                required = @("short_or_low_content", "fragmented_chat", "non_sales_chat")
                properties = @{
                    short_or_low_content = @{
                        type = "boolean"
                    }
                    fragmented_chat = @{
                        type = "boolean"
                    }
                    non_sales_chat = @{
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
        [string]$ConversationText,
        [object]$Context
    )

    $systemPrompt = @"
You analyze WhatsApp sales chats for an MVP analytics pipeline.

Return JSON only and follow the schema exactly.
Use only evidence from the chat and provided metadata.
If something is not clear from the chat, use "unknown".
Do not invent prices, budgets, timelines, intentions, or next steps.
Free-text fields must be in Russian.
Tags must be short Russian labels.

Interpretation rules:
- target_client: the chat is relevant to the company's target services.
- not_target_client: the client request is outside the company's target service, or the conversation is not a real lead conversation.
- technical_or_short: the chat is too short, too fragmented, or too poor in content for meaningful sales analysis.
- callback_requested: the client explicitly asks to continue via phone call or asks to be called back.
- awaiting_response: the dialogue is unfinished and a side is clearly waiting for the next reply or action.
- follow_up: there is a clear next step but not enough evidence for strong qualified interest.
- qualified_interest: the client shows meaningful service interest and the chat has real sales value.
- non_sales_chat=true when the chat is administrative, technical, wrong-thread, or otherwise not a real sales conversation.
- fragmented_chat=true when the dialogue is incomplete, one-sided, mostly media/system notices, or lacks enough back-and-forth context.
"@

    $userPrompt = @"
Metadata:
- Deal ID: $($Context.deal_id)
- Contact ID: $($Context.contact_id)
- Source ID: $($Context.source_id)
- Stage ID: $($Context.stage_id)
- Assigned manager ID: $($Context.assigned_by_id)
- Created at: $($Context.date_create)
- Updated at: $($Context.date_modify)
- Total messages: $($Context.total_messages)
- Manager messages: $($Context.manager_messages)
- Client messages: $($Context.client_messages)
- System messages: $($Context.system_messages)

Chat:
$ConversationText
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
                name = "whatsapp_chat_features"
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
$resolvedOutputDir = Resolve-ProjectPath -BaseDir $projectRoot -Path $OutputDir
$featuresDir = Join-Path $resolvedOutputDir "features"
$rawDir = Join-Path $resolvedOutputDir "raw"

foreach ($path in @($resolvedOutputDir, $featuresDir, $rawDir)) {
    if (-not (Test-Path -LiteralPath $path)) {
        New-Item -ItemType Directory -Path $path | Out-Null
    }
}

$entries = Get-ConversationEntries `
    -ProjectRoot $projectRoot `
    -ReportPath $ConversationReportPath `
    -DirectoryPath $ConversationDir

$entries = @(
    $entries | Where-Object {
        -not [string]::IsNullOrWhiteSpace([string]$_.CONVERSATION_PATH)
    }
)

if ($Limit -gt 0) {
    $entries = @($entries | Select-Object -First $Limit)
}

if ($entries.Count -eq 0) {
    throw "No WhatsApp conversations available for feature extraction."
}

$featureReport = @()
$errors = @()
$usageRows = @()

foreach ($entry in $entries) {
    $dealId = [string]$entry.DEAL_ID
    $contactId = [string]$entry.CONTACT_ID
    $conversationPath = Resolve-ProjectPath -BaseDir $projectRoot -Path ([string]$entry.CONVERSATION_PATH)

    if (-not (Test-Path -LiteralPath $conversationPath)) {
        $errors += [pscustomobject]@{
            DEAL_ID    = $dealId
            CONTACT_ID = $contactId
            Error      = "Conversation file not found."
        }

        Write-Host "Missing conversation for deal ID=$dealId"
        continue
    }

    $fileStem = "deal_$(Get-SafeFileNamePart -Value $dealId)"
    $featurePath = Join-Path $featuresDir "$fileStem.json"
    $rawPath = Join-Path $rawDir "$fileStem.json"

    if ($SkipExisting -and (Test-Path -LiteralPath $featurePath) -and (Test-Path -LiteralPath $rawPath)) {
        $existing = [System.IO.File]::ReadAllText($featurePath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
        $featureReport += [pscustomobject]@{
            DEAL_ID               = $dealId
            CONTACT_ID            = $contactId
            FEATURE_FILE_PATH     = $featurePath
            RAW_FILE_PATH         = $rawPath
            STATUS                = "skipped_existing"
            PRIMARY_TOPIC         = [string]$existing.primary_topic
            RELEVANCE             = [string]$existing.relevance_to_company
            OUTCOME_STATUS        = [string]$existing.outcome.status
            CLIENT_INTEREST       = [string]$existing.sentiment.client_interest_level
            SHORT_OR_LOW_CONTENT  = [bool]$existing.quality_flags.short_or_low_content
        }

        Write-Host "Skipped existing features for deal ID=$dealId"
        continue
    }

    $conversation = [System.IO.File]::ReadAllText($conversationPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
    $conversationText = Convert-ConversationToPromptText -Conversation $conversation

    if ([string]::IsNullOrWhiteSpace($conversationText)) {
        $errors += [pscustomobject]@{
            DEAL_ID    = $dealId
            CONTACT_ID = $contactId
            Error      = "Conversation text is empty after formatting."
        }

        Write-Host "Empty conversation for deal ID=$dealId"
        continue
    }

    $context = [pscustomobject]@{
        deal_id          = [string]$conversation.deal_id
        contact_id       = [string]$conversation.contact_id
        source_id        = [string]$conversation.source_id
        stage_id         = [string]$conversation.stage_id
        assigned_by_id   = [string]$conversation.assigned_by_id
        date_create      = [string]$conversation.date_create
        date_modify      = [string]$conversation.date_modify
        total_messages   = [int]$conversation.stats.total_messages
        manager_messages = [int]$conversation.stats.manager_messages
        client_messages  = [int]$conversation.stats.client_messages
        system_messages  = [int]$conversation.stats.system_messages
    }

    try {
        $apiResult = Invoke-OpenAiFeatureExtraction `
            -ApiKeyValue $apiKeyValue `
            -ModelName $Model `
            -ConversationText $conversationText `
            -Context $context

        $usageRecord = Get-OpenAiUsageRecord `
            -ResponseObject $apiResult.ResponseObject `
            -Stage "whatsapp_feature_extraction" `
            -EntityType "deal" `
            -EntityId $dealId `
            -SourceFilePath $conversationPath `
            -ModelOverride $Model `
            -ExtraFields @{
                contact_id = $contactId
            }

        if ($usageRecord) {
            $usageRows += $usageRecord
        }

        $parsed = $apiResult.ParsedFeatures
        $finalFeature = [ordered]@{
            schema_version = "mvp_whatsapp_features_v1"
            source = [ordered]@{
                channel               = [string]$conversation.channel
                integration           = [string]$conversation.integration
                deal_id               = [string]$conversation.deal_id
                contact_id            = [string]$conversation.contact_id
                source_id             = [string]$conversation.source_id
                assigned_by_id        = [string]$conversation.assigned_by_id
                stage_id              = [string]$conversation.stage_id
                category_id           = [string]$conversation.category_id
                date_create           = [string]$conversation.date_create
                date_modify           = [string]$conversation.date_modify
                last_communication_time = [string]$conversation.last_communication_time
                total_messages        = [int]$conversation.stats.total_messages
                manager_messages      = [int]$conversation.stats.manager_messages
                client_messages       = [int]$conversation.stats.client_messages
                system_messages       = [int]$conversation.stats.system_messages
                messages_with_files   = [int]$conversation.stats.messages_with_files
                conversation_file_path = $conversationPath
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
            DEAL_ID               = $dealId
            CONTACT_ID            = $contactId
            FEATURE_FILE_PATH     = $featurePath
            RAW_FILE_PATH         = $rawPath
            STATUS                = "extracted"
            PRIMARY_TOPIC         = [string]$parsed.primary_topic
            RELEVANCE             = [string]$parsed.relevance_to_company
            OUTCOME_STATUS        = [string]$parsed.outcome.status
            CLIENT_INTEREST       = [string]$parsed.sentiment.client_interest_level
            SHORT_OR_LOW_CONTENT  = [bool]$parsed.quality_flags.short_or_low_content
            INPUT_TOKENS          = if ($usageRecord) { $usageRecord.input_tokens } else { 0 }
            OUTPUT_TOKENS         = if ($usageRecord) { $usageRecord.output_tokens } else { 0 }
            TOTAL_TOKENS          = if ($usageRecord) { $usageRecord.total_tokens } else { 0 }
            CACHED_TOKENS         = if ($usageRecord) { $usageRecord.cached_tokens } else { 0 }
            REASONING_TOKENS      = if ($usageRecord) { $usageRecord.reasoning_tokens } else { 0 }
        }

        Write-Host "Extracted WhatsApp features for deal ID=$dealId"
    }
    catch {
        $errorMessage = $_.Exception.Message
        $inner = $_.Exception.InnerException
        while ($inner) {
            $errorMessage += " | Inner: $($inner.Message)"
            $inner = $inner.InnerException
        }

        $errors += [pscustomobject]@{
            DEAL_ID    = $dealId
            CONTACT_ID = $contactId
            Error      = $errorMessage
        }

        Write-Host "Failed WhatsApp feature extraction for deal ID=$dealId"
    }
}

Write-JsonFile -Path (Join-Path $resolvedOutputDir "feature-report.json") -Data @($featureReport)
Write-JsonFile -Path (Join-Path $resolvedOutputDir "errors.json") -Data @($errors)
Write-JsonFile -Path (Join-Path $resolvedOutputDir "usage-events.json") -Data @($usageRows)
Write-JsonFile -Path (Join-Path $resolvedOutputDir "usage-summary.json") -Data (Get-OpenAiUsageSummary -Rows @($usageRows))

Write-Host "WhatsApp feature extraction completed."
Write-Host "Extracted or skipped: $($featureReport.Count)"
Write-Host "Errors: $($errors.Count)"
Write-Host "Usage requests logged: $($usageRows.Count)"
Write-Host "Files saved to $resolvedOutputDir"
