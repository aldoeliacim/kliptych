"""Exercise the actual workflow scripts using local Git remotes and a fake HTTP API.

Run with: python .github/mirror-sync/test_sync.py
Requires PyYAML; does not import the application or use live credentials/network.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = Path(os.environ.get("TEST_WORKFLOW", ROOT / ".github/workflows/ci.yml"))
STEPS = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["sync"]["steps"]
SCRIPTS = {step["name"]: step["run"] for step in STEPS if "run" in step}


def command(*args: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    """Run an argv without a shell.

    Returns:
        Stripped stdout from the command.

    Raises:
        AssertionError: The command failed; includes stdout and stderr.
    """
    result = subprocess.run(args, check=False, cwd=cwd, env=env, text=True, capture_output=True)
    if result.returncode:
        message = result.stdout + result.stderr
        raise AssertionError(message)
    return result.stdout.strip()


class MirrorSyncFixture(unittest.TestCase):
    """Black-box coverage of settings, Git writes, and notification payloads."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        command("git", "init", "-b", "main", cwd=self.source)
        command("git", "config", "user.name", "Fixture", cwd=self.source)
        command("git", "config", "user.email", "fixture@example.invalid", cwd=self.source)
        self.put("app.txt", "before\n")
        self.put(".github/workflows/ci.yml", "local controller\n")
        self.base = self.commit()
        self.origin = self.root / "origin.git"
        self.upstream = self.root / "upstream.git"
        command("git", "clone", "--bare", str(self.source), str(self.origin), cwd=self.root)
        command("git", "clone", "--bare", str(self.source), str(self.upstream), cwd=self.root)
        command("git", "remote", "add", "upstream-fixture", str(self.upstream), cwd=self.source)
        self.work = self.root / "work"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "curl").write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "with open(os.environ['HTTP_LOG'], 'a') as log:\n"
            "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "args = sys.argv[1:]\n"
            "if os.environ.get('HTTP_FAIL') == 'true':\n"
            "    raise SystemExit(22)\n"
            "if any('/commits/' in arg for arg in args):\n"
            "    print(json.dumps({'commit': {'verification': {'verified': "
            "os.environ.get('VERIFIED') == 'true'}}}))\n"
            "elif any('issues?state=' in arg for arg in args):\n"
            "    print(os.environ.get('ISSUES', '[]'))\n"
            "else:\n"
            "    print(json.dumps({'source': {'full_name': 'fixture/upstream'}}))\n"
        )
        (self.bin / "curl").chmod(0o755)
        self.env = os.environ.copy()
        for key in tuple(self.env):
            if key.startswith("GIT_") or key.endswith("TOKEN"):
                self.env.pop(key)
        self.env.update(
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            GIT_CONFIG_GLOBAL=str(self.root / "gitconfig"),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
            GITHUB_REPOSITORY="fixture/mirror",
            GITHUB_TOKEN="fixture-api-token",
            MIRROR_SYNC_TOKEN="fixture-push-token",
            UPSTREAM_REPOSITORY="fixture/upstream",
            UPSTREAM_BRANCH="main",
            TARGET_BRANCH="main",
            PROTECTED_PATHS_CSV=".github/workflows/ci.yml",
            DRY_RUN="false",
            MAX_CHANGED_FILES="400",
            MAX_DELETIONS="20000",
            FORCE_LARGE_REWRITE="false",
            REQUIRE_VERIFIED_COMMIT="false",
            ARCHIVE_KEEP="50",
            GITHUB_STEP_SUMMARY=str(self.root / "summary"),
            GITHUB_OUTPUT=str(self.root / "output"),
            HTTP_LOG=str(self.root / "http"),
            DEFAULT_BRANCH="main",
            INPUT_UPSTREAM_REPOSITORY="fixture/upstream",
            NOTIFY_ON_FAILURE="true",
            REPOSITORY="fixture/mirror",
            RUN_ID="123",
            WORKFLOW="Mirror Sync",
            JOB="sync",
        )
        Path(self.env["GIT_CONFIG_GLOBAL"]).write_text(
            f'[url "{self.upstream}"]\n    insteadOf = https://github.com/fixture/upstream.git\n',
            encoding="utf-8",
        )

    def put(self, path: str, text: str) -> None:
        destination = self.source / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text)

    def commit(self) -> str:
        command("git", "add", "-A", cwd=self.source)
        command("git", "commit", "-m", "fixture", cwd=self.source)
        return command("git", "rev-parse", "HEAD", cwd=self.source)

    def update(self) -> str:
        sha = self.commit()
        command("git", "push", "upstream-fixture", "main", cwd=self.source)
        return sha

    def refs(self) -> str:
        return command("git", "for-each-ref", "--format=%(refname) %(objectname)", cwd=self.origin)

    def diverge(self) -> None:
        fork = self.root / "fork"
        command("git", "clone", str(self.origin), str(fork), cwd=self.root)
        command("git", "config", "user.name", "Fixture", cwd=fork)
        command("git", "config", "user.email", "fixture@example.invalid", cwd=fork)
        (fork / "mirror-only.txt").write_text("local change\n")
        command("git", "add", "-A", cwd=fork)
        command("git", "commit", "-m", "local divergence", cwd=fork)
        command("git", "push", "origin", "main", cwd=fork)
        self.base = command("git", "rev-parse", "HEAD", cwd=fork)

    def script(self, name: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        env = self.env | overrides
        if name == "Mirror upstream commit":
            if self.work.exists():
                shutil.rmtree(self.work)
            command("git", "clone", str(self.origin), str(self.work), cwd=self.root, env=env)
            cwd = self.work
        else:
            cwd = self.source
        return subprocess.run(
            ["bash", "-c", SCRIPTS[name]],
            check=False,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
        )

    def sync(self, expected: str = "fast_forward", **overrides: str) -> str:
        result = self.script("Mirror upstream commit", **overrides)
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"| action | {expected} |" in (self.root / "summary").read_text()
        return command("git", "rev-parse", "refs/heads/main", cwd=self.origin)


class TestMirrorGit(MirrorSyncFixture):
    def test_no_change_without_push_token(self) -> None:
        assert self.sync("already_synchronized", MIRROR_SYNC_TOKEN="") == self.base

    def test_fast_forward(self) -> None:
        self.put("app.txt", "after\n")
        tip = self.update()
        assert self.sync() == tip

    def test_initial_push(self) -> None:
        command("git", "update-ref", "-d", "refs/heads/main", cwd=self.origin)
        assert self.sync("initial_push") == self.base

    def test_initial_dry_run(self) -> None:
        command("git", "update-ref", "-d", "refs/heads/main", cwd=self.origin)
        before = self.refs()
        self.script("Mirror upstream commit", DRY_RUN="true", MIRROR_SYNC_TOKEN="")
        assert self.refs() == before
        assert "dry_run_initial_push" in (self.root / "summary").read_text()

    def test_modified_workflow_restored_and_tip_archived(self) -> None:
        self.diverge()
        self.put("app.txt", "after\n")
        self.put(".github/workflows/ci.yml", "upstream controller\n")
        upstream = self.update()
        tip = self.sync("archive_and_force")
        assert tip != upstream
        assert (
            command("git", "show", "main:.github/workflows/ci.yml", cwd=self.origin)
            == "local controller"
        )
        assert command("git", "show", "main:app.txt", cwd=self.origin) == "after"
        assert command("git", "rev-parse", "main^", cwd=self.origin) == upstream
        assert f" {self.base}" in self.refs()
        before = self.refs()
        self.sync("already_synchronized_preserved")
        assert self.refs() == before

    def test_deleted_workflow_restored(self) -> None:
        self.put("app.txt", "after\n")
        (self.source / ".github/workflows/ci.yml").unlink()
        self.update()
        self.sync()
        assert (
            command("git", "show", "main:.github/workflows/ci.yml", cwd=self.origin)
            == "local controller"
        )

    def test_new_protected_file_removed(self) -> None:
        self.put("app.txt", "after\n")
        self.put("new-protected.txt", "not in mirror\n")
        self.update()
        self.sync(PROTECTED_PATHS_CSV=".github/workflows/ci.yml,new-protected.txt")
        assert "new-protected.txt" not in command(
            "git", "ls-tree", "-r", "--name-only", "main", cwd=self.origin
        )

    def test_protected_directory_and_symlink(self) -> None:
        self.put("config/old.txt", "local\n")
        (self.source / "link").symlink_to("app.txt")
        local = self.commit()
        command("git", "push", str(self.origin), "main", cwd=self.source)
        self.put("config/old.txt", "upstream\n")
        self.put("config/new.txt", "new\n")
        (self.source / "link").unlink()
        (self.source / "link").symlink_to("config/new.txt")
        self.put("app.txt", "after\n")
        self.update()
        self.sync(PROTECTED_PATHS_CSV="config,link")
        assert command("git", "rev-parse", "main:config", cwd=self.origin) == command(
            "git", "rev-parse", f"{local}:config", cwd=self.origin
        )
        assert command("git", "show", "main:link", cwd=self.origin) == "app.txt"

    def test_dry_run_has_no_remote_writes(self) -> None:
        self.diverge()
        self.put("app.txt", "after\n")
        self.put(".github/workflows/ci.yml", "upstream\n")
        self.update()
        before = self.refs()
        self.sync("dry_run_archive_and_force", DRY_RUN="true", MIRROR_SYNC_TOKEN="")
        assert self.refs() == before

    def test_circuit_breaker_rejects_and_override_allows(self) -> None:
        self.put("app.txt", "after\n")
        self.update()
        before = self.refs()
        result = self.script("Mirror upstream commit", MAX_CHANGED_FILES="0")
        assert result.returncode != 0
        assert "Circuit breaker blocked" in result.stdout
        assert self.refs() == before
        self.sync(MAX_CHANGED_FILES="0", FORCE_LARGE_REWRITE="true")

    def test_deletion_threshold_rejects(self) -> None:
        (self.source / "app.txt").unlink()
        self.update()
        before = self.refs()
        result = self.script("Mirror upstream commit", MAX_DELETIONS="0")
        assert result.returncode != 0
        assert self.refs() == before
        assert "Circuit breaker blocked" in result.stdout

    def test_missing_push_token_rejects_without_writes(self) -> None:
        self.put("app.txt", "after\n")
        self.update()
        before = self.refs()
        result = self.script("Mirror upstream commit", MIRROR_SYNC_TOKEN="")
        assert result.returncode != 0
        assert "Set MIRROR_SYNC_TOKEN" in result.stdout
        assert self.refs() == before

    def test_unverified_commit_rejects_without_writes(self) -> None:
        self.put("app.txt", "after\n")
        self.update()
        before = self.refs()
        result = self.script("Mirror upstream commit", REQUIRE_VERIFIED_COMMIT="true")
        assert result.returncode != 0
        assert "failed provenance verification" in result.stdout
        assert self.refs() == before
        self.sync(REQUIRE_VERIFIED_COMMIT="true", VERIFIED="true")

    def test_archive_pruning(self) -> None:
        self.diverge()
        for index in range(3):
            command(
                "git",
                "update-ref",
                f"refs/heads/archive/main/2000010{index}",
                self.base,
                cwd=self.origin,
            )
        self.put("app.txt", "after\n")
        self.put(".github/workflows/ci.yml", "upstream\n")
        self.update()
        self.sync("archive_and_force", ARCHIVE_KEEP="2")
        archives = command(
            "git", "for-each-ref", "--format=%(refname)", "refs/heads/archive/", cwd=self.origin
        ).splitlines()
        assert len(archives) == 2
        assert "archive_refs_pruned | 2" in (self.root / "summary").read_text()

    def test_rejected_atomic_push_keeps_archive_and_main_unchanged(self) -> None:
        self.diverge()
        hook = self.origin / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(493)
        self.put("app.txt", "after\n")
        self.put(".github/workflows/ci.yml", "upstream\n")
        self.update()
        before = self.refs()
        assert self.script("Mirror upstream commit").returncode != 0
        assert self.refs() == before


class TestMirrorSettings(MirrorSyncFixture):
    def test_settings_defaults_and_normalization(self) -> None:
        result = self.script(
            "Resolve sync settings",
            INPUT_UPSTREAM_REPOSITORY="",
            INPUT_ARCHIVE_KEEP="008",
            INPUT_DRY_RUN="YES",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        output = (self.root / "output").read_text()
        assert "upstream_repository=fixture/upstream" in output
        assert "archive_keep=8" in output
        assert "dry_run=true" in output
        assert ".github/mirror-sync" in output
        calls = [json.loads(line) for line in (self.root / "http").read_text().splitlines()]
        assert "Authorization: Bearer fixture-api-token" in calls[0]

    def test_settings_reject_invalid_inputs(self) -> None:
        cases = [
            {"INPUT_PROTECTED_PATHS": "../escape"},
            {"INPUT_PROTECTED_PATHS": ".git/config"},
            {"INPUT_PROTECTED_PATHS": "/absolute"},
            {"INPUT_PROTECTED_PATHS": "*"},
            {"INPUT_PROTECTED_PATHS": "."},
            {"INPUT_PROTECTED_PATHS": ":(top)app.txt"},
            {"INPUT_UPSTREAM_BRANCH": "main\nINJECTED=true"},
            {"INPUT_UPSTREAM_REPOSITORY": "fixture/mirror"},
            {"INPUT_UPSTREAM_REPOSITORY": "https://invalid"},
            {"INPUT_MAX_CHANGED_FILES": "-1"},
            {"INPUT_ARCHIVE_KEEP": "2147483648"},
            {"INPUT_DRY_RUN": "typo"},
            {"INPUT_FORCE_LARGE_REWRITE": "typo"},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                result = self.script("Resolve sync settings", **overrides)
                assert result.returncode != 0, result.stdout + result.stderr


class TestMirrorNotifications(MirrorSyncFixture):
    def test_notification_create_and_existing_issue(self) -> None:
        for issues, endpoint in [
            ("[]", "/issues"),
            (
                json.dumps([{"number": 7, "title": "[Mirror Sync] Failure alert"}]),
                "/issues/7/comments",
            ),
        ]:
            with self.subTest(endpoint=endpoint):
                (self.root / "http").write_text("")
                result = self.script("Notify sync failure", ISSUES=issues)
                assert result.returncode == 0, result.stdout + result.stderr
                calls = [json.loads(line) for line in (self.root / "http").read_text().splitlines()]
                last = calls[-1]
                assert f"https://api.github.com/repos/fixture/mirror{endpoint}" in last
                assert "Authorization: Bearer fixture-api-token" in last
                payload = json.loads(last[last.index("-d") + 1])
                assert "https://github.com/fixture/mirror/actions/runs/123" in payload["body"]
                if endpoint == "/issues":
                    assert payload["title"] == "[Mirror Sync] Failure alert"

    def test_notification_disabled(self) -> None:
        result = self.script("Notify sync failure", NOTIFY_ON_FAILURE="false")
        assert result.returncode == 0
        assert not (self.root / "http").exists()


if __name__ == "__main__":
    unittest.main(verbosity=2)
