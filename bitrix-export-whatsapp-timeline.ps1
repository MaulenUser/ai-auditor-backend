param(
    [Parameter(Mandatory = $true)]
    [string]$WebhookBaseUrl,

    [string]$OutputDir = ".\export\whatsapp-timeline",

    [int]$Limit = 100,

    [string]$ModifiedFrom,

    [string[]]$DealIds,

    [switch]$SkipExisting,

    [int]$PageDelayMilliseconds = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12

function Normalize-WebhookBaseUrl {
    param([string]$Url)

    $trimmed = $Url.Trim()
    if (-not $trimmed.EndsWith("/")) {
        $trimmed += "/"
    }

    return $trimmed
}

function Invoke-BitrixMethod {
    param(
        [string]$BaseUrl,
        [string]$Method,
        [hashtable]$Body = @{},
        [string]$RequestLabel,
        [int]$MaxAttempts = 4
    )

    $uri = "$BaseUrl$Method.json"
    $jsonBody = $Body | ConvertTo-Json -Depth 20
    $label = if ([string]::IsNullOrWhiteSpace($RequestLabel)) { $Method } else { $RequestLabel }

    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        try {
            return Invoke-RestMethod `
                -Method Post `
                -Uri $uri `
                -ContentType "application/json; charset=utf-8" `
                -Body $jsonBody
        }
        catch {
            $statusCode = $null
            $responseBody = $null

            if ($_.Exception.Response) {
                try {
                    $statusCode = [int]$_.Exception.Response.StatusCode
                }
                catch {
                }

                try {
                    $stream = $_.Exception.Response.GetResponseStream()
                    if ($stream) {
                        $reader = New-Object System.IO.StreamReader($stream)
                        $responseBody = $reader.ReadToEnd()
                        $reader.Close()
                    }
                }
                catch {
                }
            }

            $shouldRetry = ($statusCode -in @(429, 500, 502, 503, 504))
            if (-not $shouldRetry -and $attempt -lt $MaxAttempts) {
                $messageText = [string]$_.Exception.Message
                if ($messageText -match "timed out|timeout|temporarily unavailable|The remote server returned an error") {
                    $shouldRetry = $true
                }
            }

            if ($shouldRetry -and $attempt -lt $MaxAttempts) {
                $delaySeconds = [Math]::Pow(2, $attempt - 1)
                Write-Host "Retrying Bitrix request '$label' after transient failure on attempt $attempt/$MaxAttempts..."
                Start-Sleep -Seconds $delaySeconds
                continue
            }

            $message = "Bitrix request '$label' failed."
            if ($null -ne $statusCode) {
                $message += " HTTP $statusCode."
            }
            if ($responseBody) {
                $message += " Response: $responseBody"
            }

            throw $message
        }
    }
}

function Get-BitrixList {
    param(
        [string]$BaseUrl,
        [string]$Method,
        [string[]]$Select,
        [hashtable]$Filter = @{},
        [hashtable]$Order = @{ ID = "ASC" },
        [string]$RequestContext
    )

    $all = New-Object System.Collections.Generic.List[object]
    $start = 0

    while ($true) {
        $pageNumber = [int]($start / 50) + 1
        $body = @{
            select = $Select
            filter = $Filter
            order  = $Order
            start  = $start
        }

        $requestLabel = if ([string]::IsNullOrWhiteSpace($RequestContext)) {
            "$Method page $pageNumber (start=$start)"
        }
        else {
            "$Method $RequestContext page $pageNumber (start=$start)"
        }

        $response = Invoke-BitrixMethod `
            -BaseUrl $BaseUrl `
            -Method $Method `
            -Body $body `
            -RequestLabel $requestLabel
        $resultProperty = $response.PSObject.Properties["result"]
        if ($null -eq $resultProperty -or $null -eq $resultProperty.Value) {
            Write-Host "${requestLabel}: 0 rows, stopping pagination."
            break
        }

        $pageItems = @($resultProperty.Value)
        $pageCount = $pageItems.Count

        foreach ($item in $pageItems) {
            [void]$all.Add($item)
        }

        Write-Host "${requestLabel}: loaded $pageCount rows, total $($all.Count)"

        $nextProperty = $response.PSObject.Properties["next"]
        if ($null -eq $nextProperty -or $null -eq $nextProperty.Value) {
            $summaryLabel = if ([string]::IsNullOrWhiteSpace($RequestContext)) {
                $Method
            }
            else {
                "$Method $RequestContext"
            }
            Write-Host "$summaryLabel pagination completed on page $pageNumber. Total rows: $($all.Count)"
            break
        }

        if ($PageDelayMilliseconds -gt 0) {
            Start-Sleep -Milliseconds $PageDelayMilliseconds
        }

        $start = [int]$nextProperty.Value
    }

    return ,($all.ToArray())
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

    $Data | ConvertTo-Json -Depth 40 | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Normalize-Whitespace {
    param([string]$Text)

    if ($null -eq $Text) {
        return ""
    }

    $value = $Text.Replace([char]0x00A0, " ")
    $value = $value -replace "`r`n", "`n"
    $value = $value -replace "`r", "`n"

    $lines = $value -split "`n"
    $normalizedLines = foreach ($line in $lines) {
        ($line -replace '\s+', ' ').Trim()
    }

    return ($normalizedLines -join "`n").Trim()
}

function Get-UrlAttachments {
    param([string]$CommentText)

    $attachments = New-Object System.Collections.Generic.List[object]
    $pattern = '\[url=(?<url>[^\]]+)\](?<label>.*?)\[/url\]'
    $matches = [System.Text.RegularExpressions.Regex]::Matches(
        $CommentText,
        $pattern,
        [System.Text.RegularExpressions.RegexOptions]::Singleline
    )

    foreach ($match in $matches) {
        $label = Normalize-Whitespace -Text $match.Groups["label"].Value
        $url = $match.Groups["url"].Value.Trim()
        $type = "file"

        [void]$attachments.Add([pscustomobject]@{
            type  = $type
            label = $label
            url   = $url
        })
    }

    return $attachments.ToArray()
}

function Convert-CommentToNormalizedMessage {
    param(
        [object]$TimelineComment,
        [object]$Deal
    )

    $rawComment = ""
    if ($TimelineComment.PSObject.Properties["COMMENT"] -and $null -ne $TimelineComment.COMMENT) {
        $rawComment = [string]$TimelineComment.COMMENT
    }

    $isWhatsapp = $rawComment -match 'wazzup24|whatsapp\.png|SYSTEM WZ'
    if (-not $isWhatsapp) {
        return $null
    }

    $attachments = @(Get-UrlAttachments -CommentText $rawComment)
    $decoded = [System.Net.WebUtility]::HtmlDecode($rawComment)
    $decoded = $decoded -replace '\[img\][^\]]*?\[/img\]', ''
    $decoded = [System.Text.RegularExpressions.Regex]::Replace(
        $decoded,
        '\[url=(?<url>[^\]]+)\](?<label>.*?)\[/url\]',
        {
            param($match)
            return $match.Groups["label"].Value
        },
        [System.Text.RegularExpressions.RegexOptions]::Singleline
    )

    $clean = Normalize-Whitespace -Text $decoded
    $senderLabel = $null
    $messageText = $clean

    $lineBreakIndex = $clean.IndexOf("`n")
    if ($lineBreakIndex -ge 0) {
        $firstLine = $clean.Substring(0, $lineBreakIndex).Trim()
        $remaining = $clean.Substring($lineBreakIndex + 1).Trim()

        if ($firstLine.EndsWith(":")) {
            $senderLabel = $firstLine.TrimEnd(":").Trim()
            $messageText = $remaining
        }
    }

    $senderRole = "unknown"
    if ($messageText -like "=== SYSTEM WZ ===*") {
        $senderRole = "system"
    }
    elseif ($senderLabel) {
        if ($senderLabel -eq [string]$Deal.TITLE) {
            $senderRole = "client"
        }
        else {
            $senderRole = "manager"
        }
    }

    return [pscustomobject]@{
        timeline_comment_id = [string]$TimelineComment.ID
        created_at          = [string]$TimelineComment.CREATED
        author_id           = [string]$TimelineComment.AUTHOR_ID
        sender_role         = $senderRole
        sender_label        = $senderLabel
        text                = $messageText
        attachments         = $attachments
        is_system_message   = ($senderRole -eq "system")
        raw_comment         = $rawComment
        deal_id             = [string]$Deal.ID
    }
}

function Convert-DealTimelineToConversation {
    param(
        [object]$Deal,
        [object[]]$TimelineComments,
        [string]$TimelineSource = "deal",
        [string]$TimelineEntityType = "deal",
        [string]$TimelineEntityId = ""
    )

    $messages = New-Object System.Collections.Generic.List[object]

    foreach ($comment in @($TimelineComments)) {
        $normalized = Convert-CommentToNormalizedMessage -TimelineComment $comment -Deal $Deal
        if ($null -ne $normalized) {
            [void]$messages.Add($normalized)
        }
    }

    $messageArray = $messages.ToArray()
    $managerCount = @($messageArray | Where-Object { $_.sender_role -eq "manager" }).Count
    $clientCount = @($messageArray | Where-Object { $_.sender_role -eq "client" }).Count
    $systemCount = @($messageArray | Where-Object { $_.sender_role -eq "system" }).Count
    $attachmentCount = @($messageArray | Where-Object { @($_.attachments).Count -gt 0 }).Count

    return [pscustomobject]@{
        channel                 = "whatsapp"
        integration             = "wazzup"
        deal_id                 = [string]$Deal.ID
        contact_id              = [string]$Deal.CONTACT_ID
        deal_title              = [string]$Deal.TITLE
        source_id               = [string]$Deal.SOURCE_ID
        assigned_by_id          = [string]$Deal.ASSIGNED_BY_ID
        stage_id                = [string]$Deal.STAGE_ID
        category_id             = [string]$Deal.CATEGORY_ID
        date_create             = [string]$Deal.DATE_CREATE
        date_modify             = [string]$Deal.DATE_MODIFY
        last_communication_time = [string]$Deal.LAST_COMMUNICATION_TIME
        timeline_source         = $TimelineSource
        timeline_entity_type    = $TimelineEntityType
        timeline_entity_id      = $TimelineEntityId
        stats                   = [pscustomobject]@{
            total_messages       = $messageArray.Count
            manager_messages     = $managerCount
            client_messages      = $clientCount
            system_messages      = $systemCount
            messages_with_files  = $attachmentCount
            first_message_at     = if ($messageArray.Count -gt 0) { $messageArray[0].created_at } else { $null }
            last_message_at      = if ($messageArray.Count -gt 0) { $messageArray[$messageArray.Count - 1].created_at } else { $null }
        }
        messages                = $messageArray
    }
}

$baseUrl = Normalize-WebhookBaseUrl -Url $WebhookBaseUrl

$rawDir = Join-Path $OutputDir "raw"
$conversationsDir = Join-Path $OutputDir "conversations"

foreach ($dir in @($OutputDir, $rawDir, $conversationsDir)) {
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir | Out-Null
    }
}

$profile = Invoke-BitrixMethod -BaseUrl $baseUrl -Method "profile"
Write-Host "Connected as user ID $($profile.result.ID): $($profile.result.NAME) $($profile.result.LAST_NAME)"
Write-JsonFile -Path (Join-Path $OutputDir "profile.json") -Data $profile

$dealFilter = @{}
if ($ModifiedFrom) {
    $dealFilter[">=DATE_MODIFY"] = $ModifiedFrom
}

$deals = Get-BitrixList `
    -BaseUrl $baseUrl `
    -Method "crm.deal.list" `
    -Select @(
        "ID",
        "TITLE",
        "CONTACT_ID",
        "SOURCE_ID",
        "ASSIGNED_BY_ID",
        "STAGE_ID",
        "CATEGORY_ID",
        "DATE_CREATE",
        "DATE_MODIFY",
        "LAST_COMMUNICATION_TIME"
    ) `
    -Filter $dealFilter `
    -RequestContext "deals source"

$whatsappDeals = @(
    $deals | Where-Object {
        ($_.SOURCE_ID -like "WZ*") -or ($_.TITLE -like "*WhatsApp*")
    }
)

if ($DealIds -and $DealIds.Count -gt 0) {
    $dealIdLookup = @{}
    foreach ($dealId in $DealIds) {
        $dealIdLookup[[string]$dealId] = $true
    }

    $whatsappDeals = @(
        $whatsappDeals | Where-Object {
            $dealIdLookup.ContainsKey([string]$_.ID)
        }
    )
}

$selectedDeals = @(
    $whatsappDeals | Sort-Object `
        @{ Expression = { if ($_.DATE_MODIFY) { [DateTimeOffset]::Parse($_.DATE_MODIFY) } else { [DateTimeOffset]::MinValue } }; Descending = $true }, `
        @{ Expression = { [int]$_.ID }; Descending = $true }
)

if ($Limit -gt 0) {
    $selectedDeals = @($selectedDeals | Select-Object -First $Limit)
}

Write-JsonFile -Path (Join-Path $OutputDir "deals.source.json") -Data $selectedDeals

$errors = New-Object System.Collections.Generic.List[object]
$reportRows = New-Object System.Collections.Generic.List[object]
$totals = [ordered]@{
    deals_scanned         = $selectedDeals.Count
    chats_exported        = 0
    skipped_existing      = 0
    total_messages        = 0
    manager_messages      = 0
    client_messages       = 0
    system_messages       = 0
    messages_with_files   = 0
}

foreach ($deal in $selectedDeals) {
    $conversationPath = Join-Path $conversationsDir ("deal_{0}.json" -f $deal.ID)
    $dealTimelineRawPath = Join-Path $rawDir ("deal_{0}.timeline.json" -f $deal.ID)
    $contactTimelineRawPath = Join-Path $rawDir ("deal_{0}.contact.timeline.json" -f $deal.ID)

    if ($SkipExisting -and (Test-Path -LiteralPath $conversationPath)) {
        $totals.skipped_existing++
        Write-Host "Skipped deal ID=$($deal.ID) because conversation already exists"
        continue
    }

    try {
        $dealTimeline = Get-BitrixList `
            -BaseUrl $baseUrl `
            -Method "crm.timeline.comment.list" `
            -Select @("ID", "CREATED", "ENTITY_ID", "ENTITY_TYPE", "AUTHOR_ID", "COMMENT", "FILES") `
            -Filter @{
                ENTITY_ID   = [int]$deal.ID
                ENTITY_TYPE = "deal"
            } `
            -Order @{ CREATED = "ASC" } `
            -RequestContext "deal ID=$($deal.ID) timeline"

        Write-JsonFile -Path $dealTimelineRawPath -Data $dealTimeline

        $conversation = Convert-DealTimelineToConversation `
            -Deal $deal `
            -TimelineComments $dealTimeline `
            -TimelineSource "deal" `
            -TimelineEntityType "deal" `
            -TimelineEntityId ([string]$deal.ID)

        $contactId = [string]$deal.CONTACT_ID
        $hasContact = -not [string]::IsNullOrWhiteSpace($contactId) -and $contactId -ne "0"
        if ($conversation.stats.total_messages -eq 0 -and $hasContact) {
            Write-Host "No WhatsApp messages found in deal timeline for deal ID=$($deal.ID). Trying contact timeline for contact ID=$contactId"

            $contactTimeline = Get-BitrixList `
                -BaseUrl $baseUrl `
                -Method "crm.timeline.comment.list" `
                -Select @("ID", "CREATED", "ENTITY_ID", "ENTITY_TYPE", "AUTHOR_ID", "COMMENT", "FILES") `
                -Filter @{
                    ENTITY_ID   = [int]$contactId
                    ENTITY_TYPE = "contact"
                } `
                -Order @{ CREATED = "ASC" } `
                -RequestContext "deal ID=$($deal.ID) contact ID=$contactId timeline"

            Write-JsonFile -Path $contactTimelineRawPath -Data $contactTimeline

            $contactConversation = Convert-DealTimelineToConversation `
                -Deal $deal `
                -TimelineComments $contactTimeline `
                -TimelineSource "contact" `
                -TimelineEntityType "contact" `
                -TimelineEntityId $contactId

            if ($contactConversation.stats.total_messages -gt 0) {
                $conversation = $contactConversation
                Write-Host "Using contact timeline for deal ID=$($deal.ID): $($conversation.stats.total_messages) WhatsApp messages"
            }
        }

        Write-JsonFile -Path $conversationPath -Data $conversation

        $totals.chats_exported++
        $totals.total_messages += $conversation.stats.total_messages
        $totals.manager_messages += $conversation.stats.manager_messages
        $totals.client_messages += $conversation.stats.client_messages
        $totals.system_messages += $conversation.stats.system_messages
        $totals.messages_with_files += $conversation.stats.messages_with_files

        [void]$reportRows.Add([pscustomobject]@{
            deal_id            = [string]$deal.ID
            deal_title         = [string]$deal.TITLE
            contact_id         = [string]$deal.CONTACT_ID
            source_id          = [string]$deal.SOURCE_ID
            total_messages     = $conversation.stats.total_messages
            manager_messages   = $conversation.stats.manager_messages
            client_messages    = $conversation.stats.client_messages
            system_messages    = $conversation.stats.system_messages
            messages_with_files = $conversation.stats.messages_with_files
            first_message_at   = $conversation.stats.first_message_at
            last_message_at    = $conversation.stats.last_message_at
            timeline_source    = [string]$conversation.timeline_source
            timeline_entity_type = [string]$conversation.timeline_entity_type
            timeline_entity_id = [string]$conversation.timeline_entity_id
            output_file        = $conversationPath
        })

        Write-Host "Exported WhatsApp timeline for deal ID=$($deal.ID) from $([string]$conversation.timeline_source) timeline: $($conversation.stats.total_messages) messages"
    }
    catch {
        $errorMessage = $_.Exception.Message
        [void]$errors.Add([pscustomobject]@{
            deal_id  = [string]$deal.ID
            message  = $errorMessage
        })

        Write-Host "Failed to export deal ID=$($deal.ID): $errorMessage"
    }
}

$report = [pscustomobject]@{
    generated_at = (Get-Date).ToString("o")
    totals       = [pscustomobject]$totals
    rows         = $reportRows.ToArray()
    errors_count = $errors.Count
}

Write-JsonFile -Path (Join-Path $OutputDir "report.json") -Data $report
Write-JsonFile -Path (Join-Path $OutputDir "errors.json") -Data $errors.ToArray()

Write-Host "WhatsApp export completed."
Write-Host "Deals scanned: $($totals.deals_scanned)"
Write-Host "Chats exported: $($totals.chats_exported)"
Write-Host "Messages exported: $($totals.total_messages)"
Write-Host "Errors: $($errors.Count)"
Write-Host "Files saved to $((Resolve-Path -LiteralPath $OutputDir).Path)"
