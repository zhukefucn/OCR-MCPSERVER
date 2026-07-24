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
Add-Type -AssemblyName System.IO.Compression
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

namespace OcrTransfer
{
    public sealed class ArchiveEntryMetadata
    {
        public ArchiveEntryMetadata(uint crc32, bool containsBackslash)
        {
            Crc32 = crc32;
            ContainsBackslash = containsBackslash;
        }

        public uint Crc32 { get; private set; }
        public bool ContainsBackslash { get; private set; }
    }

    public static class ArchiveIntegrity
    {
        private static readonly uint[] Table = CreateTable();

        private static uint[] CreateTable()
        {
            var table = new uint[256];
            for (uint index = 0; index < table.Length; index++)
            {
                uint value = index;
                for (var bit = 0; bit < 8; bit++)
                {
                    value = (value & 1) == 1
                        ? 0xEDB88320U ^ (value >> 1)
                        : value >> 1;
                }
                table[index] = value;
            }
            return table;
        }

        public static uint ReadCrc32(Stream stream)
        {
            uint crc = 0xFFFFFFFFU;
            var buffer = new byte[64 * 1024];
            int count;
            while ((count = stream.Read(buffer, 0, buffer.Length)) > 0)
            {
                for (var index = 0; index < count; index++)
                {
                    crc = Table[(byte)(crc ^ buffer[index])] ^ (crc >> 8);
                }
            }
            return ~crc;
        }

        private static byte[] ReadExactly(Stream stream, int count)
        {
            var buffer = new byte[count];
            var offset = 0;
            while (offset < count)
            {
                var read = stream.Read(buffer, offset, count - offset);
                if (read == 0)
                {
                    throw new InvalidDataException("Truncated ZIP structure.");
                }
                offset += read;
            }
            return buffer;
        }

        public static ArchiveEntryMetadata[] ReadCentralDirectory(Stream stream)
        {
            if (!stream.CanRead || !stream.CanSeek || stream.Length < 22)
            {
                throw new InvalidDataException("Invalid ZIP stream.");
            }

            var tailLength = (int)Math.Min(stream.Length, 65557L);
            stream.Position = stream.Length - tailLength;
            var tail = ReadExactly(stream, tailLength);
            var endIndex = -1;
            for (var index = tail.Length - 22; index >= 0; index--)
            {
                if (tail[index] != 0x50 || tail[index + 1] != 0x4B ||
                    tail[index + 2] != 0x05 || tail[index + 3] != 0x06)
                {
                    continue;
                }
                var commentLength = tail[index + 20] |
                    (tail[index + 21] << 8);
                if (index + 22 + commentLength == tail.Length)
                {
                    endIndex = index;
                    break;
                }
            }
            if (endIndex < 0)
            {
                throw new InvalidDataException("Missing ZIP end record.");
            }

            var endOffset = stream.Length - tailLength + endIndex;
            stream.Position = endOffset + 4;
            using (var reader = new BinaryReader(stream, Encoding.UTF8, true))
            {
                var diskNumber = reader.ReadUInt16();
                var directoryDisk = reader.ReadUInt16();
                var entriesOnDisk = reader.ReadUInt16();
                var entryCount = reader.ReadUInt16();
                var directorySize = reader.ReadUInt32();
                var directoryOffset = reader.ReadUInt32();
                var commentLength = reader.ReadUInt16();

                if (diskNumber != 0 || directoryDisk != 0 ||
                    entriesOnDisk != entryCount || commentLength !=
                    tail.Length - endIndex - 22 ||
                    entryCount == UInt16.MaxValue ||
                    directorySize == UInt32.MaxValue ||
                    directoryOffset == UInt32.MaxValue)
                {
                    throw new InvalidDataException(
                        "Unsupported ZIP directory layout."
                    );
                }
                if (directoryOffset > endOffset ||
                    directorySize > endOffset - directoryOffset)
                {
                    throw new InvalidDataException(
                        "ZIP directory is outside the archive."
                    );
                }

                var directoryEnd = (long)directoryOffset + directorySize;
                stream.Position = directoryOffset;
                var metadata = new List<ArchiveEntryMetadata>(entryCount);
                for (var entryIndex = 0; entryIndex < entryCount; entryIndex++)
                {
                    if (reader.ReadUInt32() != 0x02014B50U)
                    {
                        throw new InvalidDataException(
                            "Invalid ZIP directory entry."
                        );
                    }
                    reader.ReadUInt16();
                    reader.ReadUInt16();
                    reader.ReadUInt16();
                    reader.ReadUInt16();
                    reader.ReadUInt16();
                    reader.ReadUInt16();
                    var crc32 = reader.ReadUInt32();
                    reader.ReadUInt32();
                    reader.ReadUInt32();
                    var nameLength = reader.ReadUInt16();
                    var extraLength = reader.ReadUInt16();
                    var entryCommentLength = reader.ReadUInt16();
                    reader.ReadUInt16();
                    reader.ReadUInt16();
                    reader.ReadUInt32();
                    reader.ReadUInt32();

                    var variableLength = (long)nameLength + extraLength +
                        entryCommentLength;
                    if (nameLength == 0 ||
                        variableLength > directoryEnd - stream.Position)
                    {
                        throw new InvalidDataException(
                            "Truncated ZIP directory entry."
                        );
                    }
                    var nameBytes = ReadExactly(stream, nameLength);
                    var containsBackslash =
                        Array.IndexOf(nameBytes, (byte)'\\') >= 0;
                    stream.Position += extraLength + entryCommentLength;
                    metadata.Add(
                        new ArchiveEntryMetadata(crc32, containsBackslash)
                    );
                }
                if (stream.Position != directoryEnd)
                {
                    throw new InvalidDataException(
                        "Unexpected ZIP directory data."
                    );
                }
                return metadata.ToArray();
            }
        }
    }
}
'@

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

function Assert-CanonicalArtifactId([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw (New-TransferException 'invalid_artifact')
    }

    $parsed = [guid]::Empty
    $parsedSuccessfully = [guid]::TryParseExact($Value, 'D', [ref]$parsed)
    if (
        -not $parsedSuccessfully -or
        $parsed.ToString('D') -cne $Value
    ) {
        throw (New-TransferException 'invalid_artifact')
    }
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

function Invoke-ArtifactDownload(
    [string]$ArtifactId,
    [string]$ApiKey,
    [uri]$Endpoint,
    [string]$TemporaryPath
) {
    $request = $null
    $response = $null
    $responseStream = $null
    $fileStream = $null
    $client = $null
    try {
        $request = [System.Net.Http.HttpRequestMessage]::new(
            [System.Net.Http.HttpMethod]::Get,
            $Endpoint
        )
        [void]$request.Headers.TryAddWithoutValidation('X-API-Key', $ApiKey)

        $client = [System.Net.Http.HttpClient]::new()
        $response = $client.SendAsync(
            $request,
            [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead
        ).GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) {
            throw (New-TransferException 'download_failed')
        }

        $responseStream =
            $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $fileStream = [System.IO.FileStream]::new(
            $TemporaryPath,
            [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
        $responseStream.CopyTo($fileStream)
        $fileStream.Flush($true)
    } catch {
        if ($_.Exception.Data['TransferCode']) {
            throw
        }
        throw (New-TransferException 'download_failed')
    } finally {
        if ($null -ne $fileStream) { $fileStream.Dispose() }
        if ($null -ne $responseStream) { $responseStream.Dispose() }
        if ($null -ne $response) { $response.Dispose() }
        if ($null -ne $request) { $request.Dispose() }
        if ($null -ne $client) { $client.Dispose() }
    }
}

function Get-SafeArchiveTarget(
    [string]$Name,
    [string]$ExtractionRoot
) {
    try {
        if (
            [string]::IsNullOrEmpty($Name) -or
            [System.IO.Path]::IsPathRooted($Name) -or
            $Name.Contains('\')
        ) {
            throw (New-TransferException 'unsafe_archive')
        }

        $segments = $Name.Split([char]'/')
        if ($segments -contains '..') {
            throw (New-TransferException 'unsafe_archive')
        }

        $root = [System.IO.Path]::GetFullPath($ExtractionRoot)
        $target = [System.IO.Path]::GetFullPath(
            [System.IO.Path]::Combine($root, $Name)
        )
        $trimCharacters = [char[]]@(
            [System.IO.Path]::DirectorySeparatorChar,
            [System.IO.Path]::AltDirectorySeparatorChar
        )
        $rootWithSeparator =
            $root.TrimEnd($trimCharacters) +
            [System.IO.Path]::DirectorySeparatorChar
        if (-not $target.StartsWith(
            $rootWithSeparator,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            throw (New-TransferException 'unsafe_archive')
        }
        return $target
    } catch {
        if ($_.Exception.Data['TransferCode'] -eq 'unsafe_archive') {
            throw
        }
        throw (New-TransferException 'unsafe_archive')
    }
}

function Test-SafeArchive(
    [string]$ZipPath,
    [string]$ExtractionRoot
) {
    $fileStream = $null
    $metadataStream = $null
    $archive = $null
    try {
        $fileStream = [System.IO.File]::Open(
            $ZipPath,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::Read
        )
        $metadataStream = [System.IO.File]::Open(
            $ZipPath,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::Read
        )
        $archive = [System.IO.Compression.ZipArchive]::new(
            $fileStream,
            [System.IO.Compression.ZipArchiveMode]::Read,
            $false
        )
        $centralDirectory =
            [OcrTransfer.ArchiveIntegrity]::ReadCentralDirectory(
                $metadataStream
            )
        if ($centralDirectory.Length -ne $archive.Entries.Count) {
            throw (New-TransferException 'invalid_artifact')
        }
        $entryCount = 0
        $hasFinalMarkdown = $false

        foreach ($entry in $archive.Entries) {
            $metadata = $centralDirectory[$entryCount]
            $entryCount += 1
            if ($metadata.ContainsBackslash) {
                throw (New-TransferException 'unsafe_archive')
            }
            [void](Get-SafeArchiveTarget $entry.FullName $ExtractionRoot)
            $isDirectory = $entry.FullName.EndsWith(
                '/',
                [System.StringComparison]::Ordinal
            )
            if (
                -not $isDirectory -and
                $entry.FullName -ceq 'final.md'
            ) {
                $hasFinalMarkdown = $true
            }

            if (-not $isDirectory) {
                $entryStream = $null
                try {
                    $entryStream = $entry.Open()
                    $actualCrc =
                        [OcrTransfer.ArchiveIntegrity]::ReadCrc32($entryStream)
                    if ($actualCrc -ne $metadata.Crc32) {
                        throw (New-TransferException 'invalid_artifact')
                    }
                } finally {
                    if ($null -ne $entryStream) { $entryStream.Dispose() }
                }
            }
        }

        if ($entryCount -eq 0 -or -not $hasFinalMarkdown) {
            throw (New-TransferException 'invalid_artifact')
        }
        return @{ entry_count = $entryCount }
    } catch {
        $code = [string]$_.Exception.Data['TransferCode']
        if ($code -eq 'unsafe_archive' -or $code -eq 'invalid_artifact') {
            throw
        }
        throw (New-TransferException 'invalid_artifact')
    } finally {
        if ($null -ne $archive) { $archive.Dispose() }
        if ($null -ne $metadataStream) { $metadataStream.Dispose() }
        if ($null -ne $fileStream) { $fileStream.Dispose() }
    }
}

function Expand-SafeArchive(
    [string]$ZipPath,
    [string]$ExtractionRoot
) {
    $fileStream = $null
    $archive = $null
    try {
        [void][System.IO.Directory]::CreateDirectory($ExtractionRoot)
        $fileStream = [System.IO.File]::Open(
            $ZipPath,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::Read
        )
        $archive = [System.IO.Compression.ZipArchive]::new(
            $fileStream,
            [System.IO.Compression.ZipArchiveMode]::Read,
            $false
        )

        foreach ($entry in $archive.Entries) {
            $target = Get-SafeArchiveTarget $entry.FullName $ExtractionRoot
            $isDirectory = $entry.FullName.EndsWith(
                '/',
                [System.StringComparison]::Ordinal
            )
            if ($isDirectory) {
                [void][System.IO.Directory]::CreateDirectory($target)
                continue
            }

            $parent = [System.IO.Path]::GetDirectoryName($target)
            [void][System.IO.Directory]::CreateDirectory($parent)
            $entryStream = $null
            $targetStream = $null
            try {
                $entryStream = $entry.Open()
                $targetStream = [System.IO.FileStream]::new(
                    $target,
                    [System.IO.FileMode]::CreateNew,
                    [System.IO.FileAccess]::Write,
                    [System.IO.FileShare]::None
                )
                $entryStream.CopyTo($targetStream)
                $targetStream.Flush($true)
            } finally {
                if ($null -ne $targetStream) { $targetStream.Dispose() }
                if ($null -ne $entryStream) { $entryStream.Dispose() }
            }
        }
    } catch {
        $code = [string]$_.Exception.Data['TransferCode']
        if ($code -eq 'unsafe_archive') {
            throw
        }
        throw (New-TransferException 'invalid_artifact')
    } finally {
        if ($null -ne $archive) { $archive.Dispose() }
        if ($null -ne $fileStream) { $fileStream.Dispose() }
    }
}

function Invoke-Download(
    [string]$ArtifactId,
    [string]$OutputRoot,
    [string]$ApiKey,
    [uri]$Endpoint
) {
    Assert-CanonicalArtifactId $ArtifactId
    if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
        throw (New-TransferException 'download_failed')
    }

    try {
        $root = [System.IO.Path]::GetFullPath($OutputRoot)
        [void][System.IO.Directory]::CreateDirectory($root)
    } catch {
        if ($_.Exception.Data['TransferCode']) {
            throw
        }
        throw (New-TransferException 'download_failed')
    }

    $zipPath = [System.IO.Path]::Combine($root, "$ArtifactId.zip")
    $partialPath = [System.IO.Path]::Combine(
        $root,
        "$ArtifactId.partial.zip"
    )
    $extractPath = [System.IO.Path]::Combine($root, $ArtifactId)
    $extractingPath = [System.IO.Path]::Combine(
        $root,
        "$ArtifactId.extracting"
    )
    $entryCount = 0
    $reusedZip = $false

    try {
        if (
            (Test-Path -LiteralPath $zipPath) -and
            -not (Test-Path -LiteralPath $zipPath -PathType Leaf)
        ) {
            throw (New-TransferException 'download_failed')
        }
        if (
            (Test-Path -LiteralPath $extractPath) -and
            -not (Test-Path -LiteralPath $extractPath -PathType Container)
        ) {
            throw (New-TransferException 'download_failed')
        }

        if (Test-Path -LiteralPath $zipPath -PathType Leaf) {
            try {
                $existingValidation =
                    Test-SafeArchive $zipPath $extractingPath
                $entryCount = [int]$existingValidation.entry_count
                $reusedZip = $true
            } catch {
                $reusedZip = $false
            }
        }

        if (
            $reusedZip -and
            (Test-Path -LiteralPath $extractPath -PathType Container) -and
            (Test-Path -LiteralPath (
                [System.IO.Path]::Combine($extractPath, 'final.md')
            ) -PathType Leaf)
        ) {
            return @{
                zip_path = $zipPath
                extract_path = $extractPath
                entry_count = $entryCount
                reused = $true
            }
        }

        if (-not $reusedZip) {
            if (Test-Path -LiteralPath $partialPath) {
                Remove-Item -LiteralPath $partialPath -Force
            }
            Invoke-ArtifactDownload `
                $ArtifactId $ApiKey $Endpoint $partialPath
            $downloadValidation =
                Test-SafeArchive $partialPath $extractingPath
            $entryCount = [int]$downloadValidation.entry_count

            if (Test-Path -LiteralPath $zipPath -PathType Leaf) {
                Remove-Item -LiteralPath $zipPath -Force
            }
            Move-Item -LiteralPath $partialPath -Destination $zipPath
        }

        if (Test-Path -LiteralPath $extractingPath) {
            Remove-Item -LiteralPath $extractingPath -Recurse -Force
        }
        Expand-SafeArchive $zipPath $extractingPath
        if (-not (Test-Path -LiteralPath (
            [System.IO.Path]::Combine($extractingPath, 'final.md')
        ) -PathType Leaf)) {
            throw (New-TransferException 'invalid_artifact')
        }

        if (Test-Path -LiteralPath $extractPath -PathType Container) {
            Remove-Item -LiteralPath $extractPath -Recurse -Force
        }
        Move-Item -LiteralPath $extractingPath -Destination $extractPath

        return @{
            zip_path = $zipPath
            extract_path = $extractPath
            entry_count = $entryCount
            reused = $reusedZip
        }
    } finally {
        if (Test-Path -LiteralPath $partialPath -PathType Leaf) {
            Remove-Item -LiteralPath $partialPath -Force
        }
        if (Test-Path -LiteralPath $extractingPath -PathType Container) {
            Remove-Item -LiteralPath $extractingPath -Recurse -Force
        }
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

    Assert-CanonicalArtifactId $ArtifactId
    $endpoint =
        [uri]"$($BaseUrl.TrimEnd('/'))/v1/artifacts/$ArtifactId"
    $download = Invoke-Download $ArtifactId $OutputRoot $apiKey $endpoint
    Write-SafeJson @{
        ok = $true
        action = 'download'
        artifact_id = $ArtifactId
        zip_path = [string]$download.zip_path
        extract_path = [string]$download.extract_path
        entry_count = [int]$download.entry_count
        reused = [bool]$download.reused
    } 0
} catch {
    $safe = Convert-ToSafeTransferError $_.Exception
    Write-SafeJson @{ ok = $false; error = $safe } 1
}
