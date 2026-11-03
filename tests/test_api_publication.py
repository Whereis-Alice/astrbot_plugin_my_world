"""Exercise API publication with isolated Git objects and a local API recorder."""

import base64
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "push_via_api.ps1"
BINARY_SAMPLE = bytes(range(256)) + b"\x00\xff\r\n"

HARNESS = r'''
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$global:mode = MODE
$global:parent = PARENT
$global:originalHead = ORIGINAL_HEAD
$global:ledger = LEDGER
$global:sample = SAMPLE
$global:mainReads = 0
function global:Start-Sleep { }
function Get-ApiBlobSha {
    param([byte[]]$Bytes)
    $header = [System.Text.Encoding]::UTF8.GetBytes("blob $($Bytes.Length)" + [char]0)
    $object = [byte[]](@($header) + @($Bytes))
    $hash = [System.Security.Cryptography.SHA1]::Create().ComputeHash($object)
    return [Convert]::ToHexString($hash).ToLowerInvariant()
}
function Get-ApiTreeSha {
    param([object[]]$Entries)
    $lines = [System.Collections.Generic.List[string]]::new()
    foreach($entry in @($Entries | Where-Object {$_.path -notlike '*/*'})) {
        $lines.Add("$($entry.mode) $($entry.type) $($entry.sha)" + [char]9 + $entry.path)
    }
    $directories = @($Entries | Where-Object {$_.path -like '*/*'} |
        ForEach-Object {($_.path -split '/', 2)[0]} | Sort-Object -Unique)
    foreach($directory in $directories) {
        $children = @($Entries | Where-Object {$_.path.StartsWith("$directory/")} | ForEach-Object {
            @{path=$_.path.Substring($directory.Length + 1);mode=$_.mode;type=$_.type;sha=$_.sha}
        })
        $sha = Get-ApiTreeSha -Entries $children
        $lines.Add("040000 tree $sha" + [char]9 + $directory)
    }
    # PowerShell's native pipeline writes CRLF on Windows. Git mktree treats
    # the CR as part of a path; write the protocol's LF bytes explicitly.
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = 'git'
    $psi.ArgumentList.Add('mktree')
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $process = [System.Diagnostics.Process]::Start($psi)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes(($lines -join "`n") + "`n")
    $process.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
    $process.StandardInput.Close()
    $tree = $process.StandardOutput.ReadToEnd().Trim()
    $errorText = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    if($process.ExitCode -ne 0) { throw "Fixture could not build uploaded tree entries: $errorText" }
    return $tree
}
function global:gh {
    $items = @($args)
    $endpoint = $items[1]
    $method = $items[[array]::IndexOf($items, '--method') + 1]
    $inputIndex = [array]::IndexOf($items, '--input')
    $payload = if($inputIndex -ge 0) { Get-Content -LiteralPath $items[$inputIndex + 1] -Raw | ConvertFrom-Json } else {$null}
    @{endpoint=$endpoint;method=$method;payload=$payload} | ConvertTo-Json -Depth 12 -Compress |
        Add-Content -LiteralPath $global:ledger -Encoding utf8
    $global:LASTEXITCODE = 0
    if($endpoint.Contains('/actions/')) { throw 'Actions API is forbidden in publication' }
    if($endpoint.EndsWith('/git/ref/heads/main')) {
        if($global:mode -eq 'ref-unavailable') {
            $global:LASTEXITCODE = 1
            return 'Main reference temporarily unavailable'
        }
        $global:mainReads++
        if($global:mode -eq 'late-source-drift' -and $global:mainReads -gt 1) {
            [System.IO.File]::WriteAllText($global:sample, 'changed before ref update')
        }
        $sha = if($global:mode -eq 'main-drift' -and $global:mainReads -gt 1) {$global:originalHead} else {$global:parent}
        if($global:mode -eq 'invalid-parent') { $sha = 'not-a-git-sha' }
        return (@{object=@{sha=$sha}} | ConvertTo-Json -Compress)
    }
    if($endpoint.EndsWith('/git/blobs') -and $method -eq 'POST') {
        $sha = Get-ApiBlobSha -Bytes ([Convert]::FromBase64String($payload.content))
        if($global:mode -eq 'bad-blob') { $sha = 'e' * 40 }
        return (@{sha=$sha} | ConvertTo-Json -Compress)
    }
    if($endpoint.EndsWith('/git/trees') -and $method -eq 'POST') {
        $sha = Get-ApiTreeSha -Entries $payload.tree
        @{endpoint='fixture/tree-response';method='LOCAL';payload=@{sha=$sha;head_tree=(git rev-parse 'HEAD^{tree}' | Out-String).Trim()}} |
            ConvertTo-Json -Depth 5 -Compress | Add-Content -LiteralPath $global:ledger -Encoding utf8
        if($global:mode -eq 'source-drift') {
            [System.IO.File]::WriteAllText($global:sample, 'changed during upload')
        }
        if($global:mode -eq 'bad-tree') { $sha = 'e' * 40 }
        return (@{sha=$sha} | ConvertTo-Json -Compress)
    }
    if($endpoint.EndsWith('/git/commits') -and $method -eq 'POST') {
        $sha = ($payload.message | & git commit-tree $payload.tree -p $payload.parents[0] | Out-String).Trim()
        if($LASTEXITCODE -ne 0) { throw 'Fixture could not create the API commit' }
        return (@{sha=$sha} | ConvertTo-Json -Compress)
    }
    if($endpoint.EndsWith('/git/refs/heads/main') -and $method -eq 'PATCH') {
        return (@{object=@{sha=$payload.sha}} | ConvertTo-Json -Depth 3 -Compress)
    }
    throw "Unexpected API: $method $endpoint"
}
if((Get-Command gh).CommandType -ne 'Function') { throw 'Local fake API is missing' }
try { & SCRIPT_PATH -Repo 'example/publication-test'; exit 0 }
catch { [Console]::Error.WriteLine($_); exit 1 }
'''


@unittest.skipUnless(shutil.which("pwsh") and shutil.which("git"), "PowerShell and Git required")
class ApiPublicationTests(unittest.TestCase):
    def run_publisher(self, mode, *, sensitive=None):
        artifact_base = Path(os.environ.get("MY_WORLD_TEST_ARTIFACTS", tempfile.gettempdir())) / "api-publication"
        artifact_base.mkdir(parents=True, exist_ok=True)
        artifact = Path(tempfile.mkdtemp(prefix=f"{mode}-", dir=artifact_base))
        with tempfile.TemporaryDirectory(prefix="working-", dir=artifact) as folder:
            workspace = Path(folder).resolve()
            self.assertTrue(workspace.is_relative_to(artifact_base.resolve()))
            root = workspace / "repo"
            root.mkdir()

            def git(*args):
                return subprocess.check_output(["git", *args], cwd=root, text=True).strip()

            git("init", "-q", "--initial-branch=main")
            git("config", "user.name", "Local publication test")
            git("config", "user.email", "publication-test@example.invalid")
            git("config", "core.hooksPath", str(workspace / "no-hooks"))
            git("config", "core.autocrlf", "false")
            git("config", "commit.gpgsign", "false")
            sample = root / "sample.txt"
            sample.write_text("before\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-qm", "baseline")
            parent = git("rev-parse", "HEAD")
            sample.write_text("after\n", encoding="utf-8")
            (root / "binary.bin").write_bytes(BINARY_SAMPLE)
            (root / "details").mkdir()
            (root / "details" / "note.txt").write_text("nested source\n", encoding="utf-8")
            if sensitive:
                (root / "sensitive.txt").write_text(sensitive, encoding="utf-8")
            if mode == "safe-redaction":
                (root / "redaction.txt").write_text("abk_" + "short_redaction_sample", encoding="utf-8")
            git("add", ".")
            git("commit", "-qm", "change")
            head = git("rev-parse", "HEAD")
            tree = git("rev-parse", "HEAD^{tree}")
            expected = {"parent": parent, "head": head, "tree": tree, "blobs": {}}
            for row in git("ls-tree", "-r", "HEAD").splitlines():
                meta, path = row.split("\t", 1)
                mode_bits, _, sha = meta.split()
                expected["blobs"][path] = {"mode": mode_bits, "sha": sha}
            if mode == "nonancestor":
                git("checkout", "-q", "--orphan", "unrelated")
                git("commit", "-qm", "unrelated parent")
                parent = git("rev-parse", "HEAD")
                git("checkout", "-q", "--detach", head)
            if mode == "dirty-tracked":
                sample.write_text("uncommitted\n", encoding="utf-8")
            if mode == "dirty-untracked":
                (root / "untracked.txt").write_text("not committed\n", encoding="utf-8")
            if mode == "cache-success":
                cached = expected["blobs"]["binary.bin"]["sha"]
                (root / ".git" / "mw_push_blobs.json").write_text(json.dumps({cached: True}), encoding="utf-8")
            ledger = workspace / "api.jsonl"
            harness = workspace / "runner.ps1"

            def ps_literal(value):
                return "'" + str(value).replace("'", "''") + "'"

            text = HARNESS
            for token, value in {"MODE": mode, "PARENT": parent, "ORIGINAL_HEAD": head,
                                 "LEDGER": ledger, "SAMPLE": sample, "SCRIPT_PATH": SCRIPT}.items():
                text = text.replace(token, ps_literal(value))
            harness.write_text(text, encoding="utf-8")
            temp_dir = workspace / "tmp"
            temp_dir.mkdir()
            env = {**os.environ, "TEMP": str(temp_dir), "TMP": str(temp_dir), "GIT_TERMINAL_PROMPT": "0"}
            result = subprocess.run(["pwsh", "-NoProfile", "-File", str(harness)], cwd=root, env=env,
                                    capture_output=True, text=True, encoding="utf-8", timeout=20)
            ledger_text = ledger.read_text(encoding="utf-8-sig") if ledger.exists() else ""
            calls = [json.loads(line) for line in ledger_text.splitlines()]
            updates = self.main_updates(calls)
            if updates:
                info = git("show", "-s", "--format=%T %P", updates[-1]["payload"]["sha"]).split()
                expected["published_tree"], expected["published_parents"] = info[0], info[1:]
            (artifact / "api.jsonl").write_text(ledger_text, encoding="utf-8")
            (artifact / "result.json").write_text(json.dumps({"returncode": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr, "expected": expected}, indent=2), encoding="utf-8")
            self.assertFalse(any("/actions/" in call["endpoint"] for call in calls), "No Actions API is allowed")
            self.assertFalse(any(call["endpoint"].endswith("/git/refs") and call["method"] == "POST"
                                 for call in calls), "No CI validation branch is allowed")
            return result, calls, expected

    @staticmethod
    def main_updates(calls):
        return [call for call in calls if call["method"] == "PATCH"
                and call["endpoint"].endswith("/git/refs/heads/main")]

    def test_success_directly_updates_main_with_the_exact_source_tree(self):
        result, calls, expected = self.run_publisher("success")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        updates = self.main_updates(calls)
        self.assertEqual(len(updates), 1)
        self.assertFalse(updates[0]["payload"]["force"])
        self.assertEqual(expected["published_tree"], expected["tree"])
        self.assertEqual(expected["published_parents"], [expected["parent"]])
        tree_call = next(call for call in calls if call["endpoint"].endswith("/git/trees"))
        self.assertEqual({entry["path"]: {"mode": entry["mode"], "sha": entry["sha"]}
                          for entry in tree_call["payload"]["tree"]}, expected["blobs"])
        uploaded = [base64.b64decode(call["payload"]["content"]) for call in calls
                    if call["endpoint"].endswith("/git/blobs")]
        self.assertIn(BINARY_SAMPLE, uploaded)
        main_reads = [index for index, call in enumerate(calls) if call["endpoint"].endswith("/git/ref/heads/main")]
        self.assertEqual(len(main_reads), 2)
        self.assertLess(main_reads[-1], calls.index(updates[0]))

    def test_cached_blob_still_produces_the_exact_tree(self):
        result, calls, expected = self.run_publisher("cache-success")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(expected["published_tree"], expected["tree"])
        uploads = [call for call in calls if call["endpoint"].endswith("/git/blobs")]
        self.assertEqual(len(uploads), len(expected["blobs"]) - 1)

    def test_changed_blob_or_tree_cannot_create_a_commit_or_update_main(self):
        for mode, message in (("bad-blob", "blob 与本地对象不一致"), ("bad-tree", "tree 与本地 HEAD 不一致")):
            with self.subTest(mode=mode):
                result, calls, _ = self.run_publisher(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertFalse(any(call["endpoint"].endswith("/git/commits") for call in calls))
                self.assertEqual(self.main_updates(calls), [])

    def test_main_race_stops_before_ref_update(self):
        result, calls, _ = self.run_publisher("main-drift")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("远端 main 已更新", result.stderr)
        self.assertTrue(any(call["endpoint"].endswith("/git/commits") for call in calls))
        self.assertEqual(self.main_updates(calls), [])

    def test_failed_main_read_or_invalid_parent_cannot_upload_content(self):
        for mode in ("ref-unavailable", "invalid-parent", "nonancestor"):
            with self.subTest(mode=mode):
                result, calls, _ = self.run_publisher(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(calls)
                self.assertEqual([call for call in calls if call["method"] != "GET"], [])

    def test_dirty_workspace_stops_before_any_api_call(self):
        for mode in ("dirty-tracked", "dirty-untracked"):
            with self.subTest(mode=mode):
                result, calls, _ = self.run_publisher(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("只发布干净的 HEAD", result.stderr)
                self.assertEqual(calls, [])

    def test_source_drift_during_upload_or_before_ref_update_stops_publication(self):
        for mode in ("source-drift", "late-source-drift"):
            with self.subTest(mode=mode):
                result, calls, _ = self.run_publisher(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("源码已变化", result.stderr)
                self.assertEqual(self.main_updates(calls), [])
                commits = [call for call in calls if call["endpoint"].endswith("/git/commits")]
                self.assertEqual(len(commits), int(mode == "late-source-drift"))

    def test_sensitive_literals_stop_before_api_and_do_not_echo_the_value(self):
        secrets = ("abk_" + "Q" * 43, "ghp_" + "R" * 36,
                   "-----BEGIN " + "PRIVATE KEY-----\n" + "QUJD" * 12 + "\n-----END PRIVATE KEY-----")
        for index, secret in enumerate(secrets):
            with self.subTest(kind=index):
                result, calls, _ = self.run_publisher("sensitive", sensitive=secret)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, [])
                self.assertIn("sensitive.txt", result.stderr)
                self.assertNotIn(secret, result.stdout + result.stderr)

    def test_short_redaction_fixture_is_not_treated_as_a_real_key(self):
        result, calls, _ = self.run_publisher("safe-redaction")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.main_updates(calls)), 1)
