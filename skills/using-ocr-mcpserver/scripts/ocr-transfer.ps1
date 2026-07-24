[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('upload', 'download')]
    [string]$Action,
    [string]$Path,
    [string]$ArtifactId,
    [string]$OutputRoot,
    [string]$BaseUrl = 'http://127.0.0.1:18011'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Net.Http

$script:MaximumUploadBytes = 60L * 1024L * 1024L

function Write-SafeJson([hashtable]$Value, [int]$ExitCode) {
    $json = ConvertTo-Json -InputObject $Value -Compress -Depth 5
    [Console]::Out.WriteLine($json)
    exit $ExitCode
}

function New-TransferException([string]$Code) {
    $exception = [System.InvalidOperationException]::new('Transfer operation failed.')
    $exception.Data['TransferCode'] = $Code
    return $exception
}

function Get-ApiKey() {
    $apiKey = [Environment]::GetEnvironmentVariable('OCR_MCP_API_KEY')
    if ([string]::IsNullOrWhiteSpace($apiKey)) {
        throw (New-TransferException 'authentication_unavailable')
    }
    return $apiKey
}

function Resolve-UploadFile([string]$InputPath) {
    if ([string]::IsNullOrWhiteSpace($InputPath)) {
        throw (New-TransferException 'file_not_found')
    }

    try {
        $fullPath = [System.IO.Path]::GetFullPath($InputPath)
    } catch {
        throw (New-TransferException 'file_not_found')
    }

    if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
        throw (New-TransferException 'file_not_found')
    }

    $file = Get-Item -LiteralPath $fullPath
    if (-not ($file -is [System.IO.FileInfo])) {
        throw (New-TransferException 'file_not_found')
    }
    if ($file.Length -gt $script:MaximumUploadBytes) {
        throw (New-TransferException 'file_too_large')
    }

    [void](Get-MediaType $file)
    return $file
}

function Get-MediaType([System.IO.FileInfo]$File) {
    switch ($File.Extension.ToLowerInvariant()) {
        '.pdf' { return 'application/pdf' }
        '.png' { return 'image/png' }
        '.jpg' { return 'image/jpeg' }
        '.jpeg' { return 'image/jpeg' }
        default { throw (New-TransferException 'unsupported_media_type') }
    }
}

function Get-SafeDisplayName([System.IO.FileInfo]$File) {
    $displayName = [System.IO.Path]::GetFileName($File.Name)
    if (
        [string]::IsNullOrWhiteSpace($displayName) -or
        $displayName.Length -gt 255 -or
        $displayName.IndexOfAny([char[]]@('/', '\', [char]0)) -ge 0
    ) {
        throw (New-TransferException 'upload_failed')
    }
    return $displayName
}

function Invoke-Upload(
    [System.IO.FileInfo]$File,
    [string]$ApiKey,
    [uri]$Endpoint
) {
    $fileStream = $null
    $content = $null
    $request = $null
    $response = $null
    $client = $null
    try {
        $fileStream = [System.IO.File]::OpenRead($File.FullName)
        $content = [System.Net.Http.StreamContent]::new($fileStream)
        $content.Headers.ContentType =
            [System.Net.Http.Headers.MediaTypeHeaderValue]::new((Get-MediaType $File))
        $content.Headers.ContentLength = $File.Length

        $request = [System.Net.Http.HttpRequestMessage]::new(
            [System.Net.Http.HttpMethod]::Post,
            $Endpoint
        )
        $request.Content = $content
        [void]$request.Headers.TryAddWithoutValidation('X-API-Key', $ApiKey)
        [void]$request.Headers.TryAddWithoutValidation(
            'X-Document-Name',
            (Get-SafeDisplayName $File)
        )

        $client = [System.Net.Http.HttpClient]::new()
        $response = $client.SendAsync(
            $request,
            [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead
        ).GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) {
            throw (New-TransferException 'upload_failed')
        }

        $responseBody = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        try {
            $receipt = ConvertFrom-Json -InputObject $responseBody
            if (
                [string]::IsNullOrWhiteSpace([string]$receipt.file_id) -or
                $null -eq $receipt.size_bytes -or
                [string]::IsNullOrWhiteSpace([string]$receipt.media_type)
            ) {
                throw (New-TransferException 'upload_failed')
            }
        } catch {
            if ($_.Exception.Data['TransferCode'] -eq 'upload_failed') {
                throw
            }
            throw (New-TransferException 'upload_failed')
        }
        return $receipt
    } catch {
        if ($_.Exception.Data['TransferCode']) {
            throw
        }
        throw (New-TransferException 'upload_failed')
    } finally {
        if ($null -ne $response) { $response.Dispose() }
        if ($null -ne $request) { $request.Dispose() }
        if ($null -ne $content) { $content.Dispose() }
        if ($null -ne $fileStream) { $fileStream.Dispose() }
        if ($null -ne $client) { $client.Dispose() }
    }
}

function Convert-ToSafeTransferError([System.Exception]$Exception) {
    $messages = @{
        authentication_unavailable = 'OCR authentication is not configured.'
        file_not_found = 'The upload file was not found.'
        unsupported_media_type = 'The file type is not supported.'
        file_too_large = 'The file exceeds the 60 MiB limit.'
        batch_limit_exceeded = 'The batch limit was exceeded.'
        upload_failed = 'The file could not be uploaded.'
        download_failed = 'The artifact could not be downloaded.'
        unsafe_archive = 'The artifact archive is unsafe.'
        invalid_artifact = 'The artifact is invalid.'
    }
    $code = [string]$Exception.Data['TransferCode']
    if (-not $messages.ContainsKey($code)) {
        if ($Action -eq 'download') {
            $code = 'download_failed'
        } else {
            $code = 'upload_failed'
        }
    }
    return @{
        code = $code
        message = $messages[$code]
    }
}

try {
    $apiKey = Get-ApiKey
    if ($Action -eq 'upload') {
        $file = Resolve-UploadFile $Path
        $endpoint = [uri]"$($BaseUrl.TrimEnd('/'))/v1/uploads"
        $receipt = Invoke-Upload $file $apiKey $endpoint
        Write-SafeJson @{
            ok = $true
            action = 'upload'
            file_id = [string]$receipt.file_id
            size_bytes = [long]$receipt.size_bytes
            media_type = [string]$receipt.media_type
        } 0
    }
    throw (New-TransferException 'download_failed')
} catch {
    $safe = Convert-ToSafeTransferError $_.Exception
    Write-SafeJson @{ ok = $false; error = $safe } 1
}
