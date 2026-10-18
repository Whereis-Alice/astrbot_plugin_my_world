<#
把当前 HEAD 推到一个 GitHub 仓库：走 GitHub REST API（api.github.com），
不依赖 github.com:443 的 git 传输。

什么时候用：本机 / 网络只能访问 api.github.com、`git push` 被重置的时候。
认证走本机已登录的 gh CLI（`gh auth login`），脚本本身不保存任何密钥。

用法：
    pwsh tools/push_via_api.ps1 -Repo Whereis-Alice/astrbot_plugin_my_world

做的事：
1. 把 HEAD 里每个 blob 逐个上传（二进制安全，直接读 git 对象）；
   已经传过的按 blob SHA 记在 .git/mw_push_blobs.json 里，重跑不重复上传；
2. 以远端现在的 main 为父提交建一个新 commit（**不是空父提交**，历史照样接着长）；
3. 更新 refs/heads/main 指向这个新 commit。

注意：它只能"以远端 HEAD 为父"往前推一个提交，做不了合并、rebase 之类的同步；
能 `git push` 的时候还是用 git push。另外它建出来的 commit 与本地 HEAD 内容相同、
但 commit SHA 不同（作者 / 时间戳由 API 决定），网络恢复后 `git fetch` + 对齐一次即可。
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$Repo
)

$ErrorActionPreference = "Stop"
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Invoke-GhJson {
    param(
        [string]$Endpoint,
        [string]$Method = "GET",
        [object]$Payload = $null
    )
    $ghArgs = @("api", $Endpoint, "--method", $Method)
    $temp = $null
    if ($null -ne $Payload) {
        $temp = Join-Path ([System.IO.Path]::GetTempPath()) ("mw-push-" + [guid]::NewGuid().ToString("N") + ".json")
        [System.IO.File]::WriteAllText($temp, ($Payload | ConvertTo-Json -Depth 8 -Compress), $utf8)
        $ghArgs += @("--input", $temp)
    }
    try {
        $attempt = 0
        while ($true) {
            $attempt++
            $output = & gh @ghArgs 2>&1
            if ($LASTEXITCODE -eq 0) {
                return ($output | Out-String).Trim()
            }
            if ($attempt -ge 3) {
                throw "gh api $Endpoint 失败（exit $LASTEXITCODE）：$($output | Out-String)"
            }
            Write-Host "  网络抖动，重试 $attempt / 2：$Endpoint"
            Start-Sleep -Seconds (2 * $attempt)
        }
    } finally {
        if ($temp -and (Test-Path $temp)) {
            Remove-Item -LiteralPath $temp -Force
        }
    }
}

function Get-GitBlobBytes {
    param([string]$Sha)
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = "git"
    foreach ($item in @("cat-file", "blob", $Sha)) {
        $psi.ArgumentList.Add($item)
    }
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $process = [System.Diagnostics.Process]::Start($psi)
    $stream = [System.IO.MemoryStream]::new()
    $process.StandardOutput.BaseStream.CopyTo($stream)
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) {
        throw "git cat-file blob $Sha 失败：$($process.StandardError.ReadToEnd())"
    }
    return $stream.ToArray()
}

$root = (git rev-parse --show-toplevel).Trim()
Push-Location $root
try {
    $head = (git rev-parse HEAD).Trim()
    $message = (git log -1 --pretty=%B | Out-String).TrimEnd()
    # 空仓库（一个提交都没有）时 Git Data API 会 409：先用 Contents API 建一次初始化提交。
    $parentSha = $null
    try {
        $refJson = Invoke-GhJson -Endpoint "repos/$Repo/git/ref/heads/main" -Method GET
        if ($refJson) {
            $parentSha = ($refJson | ConvertFrom-Json).object.sha
        }
    } catch {
        $parentSha = $null
    }
    if (-not $parentSha) {
        Write-Host "远端仓库还是空的：先建一个初始化提交…"
        Invoke-GhJson -Endpoint "repos/$Repo/contents/.gitkeep" -Method PUT -Payload @{
            message = "chore: 初始化仓库"
            content = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes(""))
        } | Out-Null
        $refJson = Invoke-GhJson -Endpoint "repos/$Repo/git/ref/heads/main" -Method GET
        $parentSha = ($refJson | ConvertFrom-Json).object.sha
    } else {
        & git merge-base --is-ancestor $parentSha $head
        if ($LASTEXITCODE -ne 0) {
            throw "远端 main 不是本地 HEAD 的祖先。请先 fetch 并合并远端修改，再运行此脚本。"
        }
    }
    # 断点续传：已经上传过的 blob 记在 .git 里，重跑时不再重复上传
    $gitDir = (git rev-parse --git-dir).Trim()
    if (-not [System.IO.Path]::IsPathRooted($gitDir)) {
        $gitDir = Join-Path $root $gitDir
    }
    $cachePath = Join-Path $gitDir "mw_push_blobs.json"
    $uploaded = @{}
    if (Test-Path $cachePath) {
        try {
            $saved = Get-Content -LiteralPath $cachePath -Raw | ConvertFrom-Json
            foreach ($item in $saved.PSObject.Properties) {
                $uploaded[$item.Name] = $true
            }
        } catch {
            $uploaded = @{}
        }
    }
    $rows = @(git ls-tree -r HEAD)
    $entries = New-Object System.Collections.Generic.List[object]
    $index = 0
    foreach ($row in $rows) {
        $index++
        $meta, $path = $row -split "`t", 2
        $parts = $meta -split " ", 3
        $mode, $type, $sha = $parts[0], $parts[1], $parts[2]
        if ($type -ne "blob") {
            continue
        }
        if ($uploaded.ContainsKey($sha)) {
            # blob 的 SHA 就是 git 的对象名，上传过就能直接引用
            $blobSha = $sha
        } else {
            $bytes = Get-GitBlobBytes -Sha $sha
            $payload = @{
                content  = [Convert]::ToBase64String($bytes)
                encoding = "base64"
            }
            $blobSha = (
                Invoke-GhJson -Endpoint "repos/$Repo/git/blobs" -Method POST -Payload $payload |
                    ConvertFrom-Json
            ).sha
            $uploaded[$sha] = $true
            if ($index % 20 -eq 0) {
                [System.IO.File]::WriteAllText(
                    $cachePath,
                    ($uploaded | ConvertTo-Json -Compress),
                    $utf8
                )
            }
        }
        $entries.Add(@{ path = $path; mode = $mode; type = "blob"; sha = $blobSha })
        if ($index % 20 -eq 0) {
            Write-Host "  已上传 $index / $($rows.Count) 个文件…"
        }
    }
    [System.IO.File]::WriteAllText(
        $cachePath,
        ($uploaded | ConvertTo-Json -Compress),
        $utf8
    )

    Write-Host "创建 tree（$($entries.Count) 个文件）…"
    $treeSha = (
        Invoke-GhJson -Endpoint "repos/$Repo/git/trees" -Method POST -Payload @{ tree = $entries } |
            ConvertFrom-Json
    ).sha

    Write-Host "创建 commit…"
    $commitSha = (
        Invoke-GhJson -Endpoint "repos/$Repo/git/commits" -Method POST -Payload @{
            message = $message
            tree    = $treeSha
            parents = @($parentSha)
        } | ConvertFrom-Json
    ).sha

    Write-Host "更新 refs/heads/main → $commitSha"
    Invoke-GhJson -Endpoint "repos/$Repo/git/refs/heads/main" -Method PATCH -Payload @{
        sha   = $commitSha
        force = $false
    } | Out-Null
    Write-Host "完成：https://github.com/$Repo/commit/$commitSha"
    Write-Host "（本地 HEAD $head 的树已推送；日常同步请直接 git push）"
} finally {
    Pop-Location
}
