"""Execute the publisher with a temporary Git repo and a recording GitHub API."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "push_via_api.ps1"


@unittest.skipUnless(shutil.which("pwsh") and shutil.which("git"), "PowerShell and Git required")
class ApiPublicationTests(unittest.TestCase):
    def run_publisher(self, mode):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def git(*args):
                return subprocess.check_output(["git", *args], cwd=root, text=True).strip()
            git("init", "-q")
            git("config", "user.name", "CI test")
            git("config", "user.email", "ci-test@example.org")
            git("config", "core.hooksPath", str(root / "no-hooks"))
            sample = root / "sample.txt"
            sample.write_text("before\n", encoding="utf-8")
            git("add", "sample.txt")
            git("commit", "-qm", "baseline")
            parent = git("rev-parse", "HEAD")
            sample.write_text("after\n", encoding="utf-8")
            git("add", "sample.txt")
            git("commit", "-qm", "change")
            ledger = root / "api.jsonl"
            def ps_literal(value):
                return "'" + str(value).replace("'", "''") + "'"
            harness = root / "runner.ps1"
            harness.write_text(r'''
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$global:mode = MODE
$global:parent = PARENT
$global:ledger = LEDGER
$global:mainReads = 0
$global:ciReads = 0
$global:validationBranch = ""
function global:Start-Sleep { }
function global:gh {
    $items = @($args)
    $endpoint = $items[1]
    $method = $items[[array]::IndexOf($items, '--method') + 1]
    $inputIndex = [array]::IndexOf($items, '--input')
    $payload = if ($inputIndex -ge 0) { Get-Content -LiteralPath $items[$inputIndex + 1] -Raw | ConvertFrom-Json } else { $null }
    @{endpoint=$endpoint;method=$method;payload=$payload} | ConvertTo-Json -Depth 12 -Compress | Add-Content -LiteralPath $global:ledger -Encoding utf8
    $global:LASTEXITCODE = 0
    if ($endpoint.EndsWith('/git/ref/heads/main')) {
        if ($global:mode -eq 'ref-unavailable') {
            $global:LASTEXITCODE = 1
            return 'Main reference temporarily unavailable'
        }
        $global:mainReads++
        $sha = if ($global:mode -eq 'drift' -and $global:mainReads -gt 1) { 'e' * 40 } else { $global:parent }
        return (@{object=@{sha=$sha}} | ConvertTo-Json -Compress)
    }
    if ($endpoint.EndsWith('/git/blobs')) { return (@{sha=('a' * 40)} | ConvertTo-Json -Compress) }
    if ($endpoint.EndsWith('/git/trees')) { return (@{sha=('b' * 40)} | ConvertTo-Json -Compress) }
    if ($endpoint.EndsWith('/git/commits')) { return (@{sha=('c' * 40)} | ConvertTo-Json -Compress) }
    if ($endpoint.EndsWith('/git/refs') -and $method -eq 'POST') {
        $global:validationBranch = $payload.ref.Substring('refs/heads/'.Length)
        return (@{ref=$payload.ref;object=@{sha=$payload.sha}} | ConvertTo-Json -Depth 3 -Compress)
    }
    if ($endpoint.Contains('/actions/workflows/ci.yml/runs?')) {
        $global:ciReads++
        if ($global:mode -eq 'wrong-sha' -or $global:mode -eq 'wrong-branch' -or $global:mode -eq 'wrong-event') {
            if ($global:ciReads -eq 1) {
                $sha = if ($global:mode -eq 'wrong-sha') { 'd' * 40 } else { 'c' * 40 }
                $branch = if ($global:mode -eq 'wrong-branch') { 'main' } else { $global:validationBranch }
                $event = if ($global:mode -eq 'wrong-event') { 'pull_request' } else { 'push' }
                return (@{workflow_runs=@(@{id=2;head_sha=$sha;head_branch=$branch;event=$event;status='completed';conclusion='success';html_url='https://example.org/ci/unrelated'})} | ConvertTo-Json -Depth 5 -Compress)
            }
        }
        $conclusion = if ($global:mode -eq 'failed') { 'failure' } elseif ($global:mode -eq 'cancelled') { 'cancelled' } else { 'success' }
        if ($global:mode.StartsWith('wrong-')) { $conclusion = 'failure' }
        return (@{workflow_runs=@(@{id=1;head_sha=('c' * 40);head_branch=$global:validationBranch;event='push';status='completed';conclusion=$conclusion;html_url='https://example.org/ci/1'})} | ConvertTo-Json -Depth 5 -Compress)
    }
    if ($endpoint.EndsWith('/git/refs/heads/main') -and $method -eq 'PATCH') {
        return (@{object=@{sha=$payload.sha}} | ConvertTo-Json -Depth 3 -Compress)
    }
    throw "Unexpected API: $method $endpoint"
}
try { & SCRIPT -Repo 'Whereis-Alice/publication-test'; exit 0 }
catch { [Console]::Error.WriteLine($_); exit 1 }
'''.replace("MODE", ps_literal(mode)).replace("PARENT", ps_literal(parent))
                .replace("LEDGER", ps_literal(ledger)).replace("SCRIPT", ps_literal(SCRIPT)), encoding="utf-8")
            result = subprocess.run(["pwsh", "-NoProfile", "-File", str(harness)], cwd=root,
                                    capture_output=True, text=True, encoding="utf-8", timeout=20)
            calls = [json.loads(line) for line in ledger.read_text(encoding="utf-8-sig").splitlines()]
            return result, calls

    def main_updates(self, calls):
        return [call for call in calls if call["method"] == "PATCH" and call["endpoint"].endswith("/git/refs/heads/main")]

    def test_success_promotes_the_same_commit_only_after_validation(self):
        result, calls = self.run_publisher("success")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        updates = self.main_updates(calls)
        self.assertEqual(len(updates), 1)
        validation = next(call for call in calls if call["method"] == "POST" and call["endpoint"].endswith("/git/refs"))
        self.assertTrue(validation["payload"]["ref"].startswith("refs/heads/codex/ci-"))
        self.assertEqual(updates[0]["payload"]["sha"], validation["payload"]["sha"])
        self.assertFalse(updates[0]["payload"]["force"])
        ci = next(call for call in calls if "/actions/workflows/" in call["endpoint"])
        self.assertLess(calls.index(ci), calls.index(updates[0]))

    def test_failed_ci_cannot_update_main(self):
        result, calls = self.run_publisher("failed")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.main_updates(calls), [])
        self.assertIn("main 未更新", result.stderr)

    def test_cancelled_ci_cannot_update_main(self):
        result, calls = self.run_publisher("cancelled")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.main_updates(calls), [])

    def test_concurrent_main_update_stops_promotion_even_after_successful_ci(self):
        result, calls = self.run_publisher("drift")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.main_updates(calls), [])
        self.assertIn("验证期间远端 main 已更新", result.stderr)

    def test_success_for_another_commit_cannot_authorize_publication(self):
        self.assert_unrelated_success_is_ignored("wrong-sha")

    def test_success_on_another_branch_cannot_authorize_publication(self):
        self.assert_unrelated_success_is_ignored("wrong-branch")

    def test_success_from_another_event_cannot_authorize_publication(self):
        self.assert_unrelated_success_is_ignored("wrong-event")

    def test_unavailable_main_cannot_trigger_unvalidated_initialization(self):
        result, calls = self.run_publisher("ref-unavailable")
        self.assertNotEqual(result.returncode, 0)
        writes = [call for call in calls if call["method"] != "GET"]
        self.assertEqual(writes, [], "A failed main lookup must not write repository content")

    def assert_unrelated_success_is_ignored(self, mode):
        result, calls = self.run_publisher(mode)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.main_updates(calls), [])
        ci_reads = [call for call in calls if "/actions/workflows/" in call["endpoint"]]
        self.assertEqual(len(ci_reads), 2)
        self.assertIn("main 未更新", result.stderr)
