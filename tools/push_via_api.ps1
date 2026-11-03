<#
把当前 HEAD 推到一个 GitHub 仓库：走 GitHub REST API（api.github.com），
不依赖 github.com:443 的 git 传输。

默认发布入口：本地相关验证完成后，通过 API 直接更新目标仓库。
认证走本机已登录的 gh CLI（`gh auth login`），脚本本身不保存任何密钥。
用户持久要求今后禁止跑 CI；本脚本不创建验证分支，不触发或等待 CI。

用法：
    pwsh tools/push_via_api.ps1 -Repo Whereis-Alice/astrbot_plugin_my_world

做的事：
1. 把 HEAD 里每个 blob 逐个上传（二进制安全，直接读 git 对象）；
   已经传过的按 blob SHA 记在 .git/mw_push_blobs.json 里，重跑不重复上传；
2. 以远端现在的 main 为父提交建一个新 commit（**不是空父提交**，历史照样接着长）；
3. 校验上传的 tree 与本地 HEAD 一致，确认源码和远端 main 没有变化；
4. 以 force=false 直接更新 main。

注意：它只能"以远端 HEAD 为父"往前推一个提交，做不了合并、rebase 之类的同步；
它建出来的 commit 与本地 HEAD 内容相同，但 commit SHA 不同（作者 / 时间戳由 API
决定），发布后 `git fetch` + 对齐一次即可。只发布已提交的干净 HEAD；疑似密钥只报告
文件名，不输出值。
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

function Assert-UnchangedHead {
    param([string]$Head)
    $currentHead = (git rev-parse HEAD).Trim()
    $dirty = @(git status --porcelain --untracked-files=all)
    if ($currentHead -ne $Head -or $dirty.Count -gt 0) {
        throw "源码已变化或尚未提交。请先提交本轮修改；本脚本只发布干净的 HEAD。"
    }
}

function Assert-PublishableHead {
    param([string]$Head, [string[]]$Rows)
    Assert-UnchangedHead -Head $Head
    foreach ($row in $Rows) {
        $meta, $path = $row -split "`t", 2
        $parts = $meta -split " ", 3
        if ($parts[1] -ne "blob") {
            throw "HEAD 包含不支持的对象：$path。停止发布以免遗漏源码。"
        }
        $text = [System.Text.Encoding]::UTF8.GetString((Get-GitBlobBytes -Sha $parts[2]))
        # 检查长密钥字面量，避免把测试里的短脱敏样本判为实际凭据。
        $secret = '(?:abk_[A-Za-z0-9_-]{40,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})'
        $privateKey = '(?s)-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----\s+[A-Za-z0-9+/=]{20,}'
        if ($text -match $secret -or $text -match $privateKey) {
            throw "HEAD 中存在疑似敏感信息：$path。请移除后重新提交；未向 GitHub 上传内容。"
        }
    }
}

$root = (git rev-parse --show-toplevel).Trim()
Push-Location $root
try {
    $head = (git rev-parse HEAD).Trim()
    $headTree = (git rev-parse "$head^{tree}").Trim()
    $message = (git log -1 --pretty=%B | Out-String).TrimEnd()
    $rows = @(git ls-tree -r $head)
    Assert-PublishableHead -Head $head -Rows $rows
    # 发布前不通过 Contents API 初始化 main。
    # 读取失败直接停止，也不把网络或权限错误当成空仓库。
    $refJson = Invoke-GhJson -Endpoint "repos/$Repo/git/ref/heads/main" -Method GET
    $parentSha = ($refJson | ConvertFrom-Json).object.sha
    if ($parentSha -notmatch '^[0-9a-f]{40}$') {
        throw "无法取得有效的远端 main 提交。请先确认仓库与访问权限；main 未更新。"
    }
    & git merge-base --is-ancestor $parentSha $head
    if ($LASTEXITCODE -ne 0) {
        throw "远端 main 不是本地 HEAD 的祖先。请先 fetch 并合并远端修改，再运行此脚本。"
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
            if ($blobSha -ne $sha) {
                throw "上传后的 blob 与本地对象不一致：$path。main 未更新。"
            }
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
    if ($treeSha -ne $headTree) {
        throw "上传后的 tree 与本地 HEAD 不一致。main 未更新。"
    }

    Assert-UnchangedHead -Head $head

    Write-Host "创建 commit…"
    $commitSha = (
        Invoke-GhJson -Endpoint "repos/$Repo/git/commits" -Method POST -Payload @{
            message = $message
            tree    = $treeSha
            parents = @($parentSha)
        } | ConvertFrom-Json
    ).sha

    $currentMain = (Invoke-GhJson -Endpoint "repos/$Repo/git/ref/heads/main" | ConvertFrom-Json).object.sha
    if ($currentMain -ne $parentSha) {
        throw "上传期间远端 main 已更新，停止发布。请同步远端修改后重试；已上传提交：$commitSha"
    }
    Assert-UnchangedHead -Head $head
    Write-Host "直接更新 refs/heads/main → $commitSha"
    Invoke-GhJson -Endpoint "repos/$Repo/git/refs/heads/main" -Method PATCH -Payload @{
        sha   = $commitSha
        force = $false
    } | Out-Null
    Write-Host "完成：https://github.com/$Repo/commit/$commitSha"
    Write-Host "（本地 HEAD $head 的树已通过 API 直发；未运行 CI）"
} finally {
    Pop-Location
}
