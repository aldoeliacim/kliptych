# Mirror Sync

## Push authentication

`GITHUB_TOKEN` supports `contents: write`, but it has no `workflows` permission.
Adding `workflows: write` to an Actions `permissions` block is invalid and cannot
fix GitHub's rejection of commits that update workflow files.

Create a repository Actions secret named **`MIRROR_SYNC_TOKEN`** containing either:

- A fine-grained personal access token restricted to this repository, with
  **Contents: read and write** and **Workflows: read and write**.
- A classic personal access token with `repo` and `workflow` scopes.

Use a dedicated account/token with the narrowest available access and an expiry.
The checkout uses this token for Git pushes. The ordinary `GITHUB_TOKEN` remains
responsible for repository discovery, commit verification and failure issues.
No token is required for a dry run or an already-synchronized no-op; a real sync
fails before any remote mutation if `MIRROR_SYNC_TOKEN` is missing. Neither the
regression tests nor the PR check need this secret.

## Safety behavior

- Runs are serialized and not cancelled halfway through a sync.
- The mirror controller, its check workflow and its test directory are always
  protected; extra configured paths are literal repository-relative paths.
- Protected files, directories and symlinks retain the target branch's version.
- Change-volume limits and optional GitHub-verified provenance fail closed.
- Divergent updates archive the old tip and update the target atomically, with
  an explicit force-with-lease expectation. A rejected push changes neither ref.
- Dry runs never write remote refs. Test fixtures use temporary local bare Git
  repositories and a fake HTTP client, never a live repository or real token.

## Verification

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run basedpyright
uv run --with pyyaml python .github/mirror-sync/test_sync.py
```

`Mirror Sync Checks` runs these gates on pull requests and pushes to `main`,
alongside checksum-verified, version-pinned actionlint workflow validation.
The harness executes the actual Bash extracted from `ci.yml`, not a separate
Python implementation of the sync logic. It covers initial sync, fast-forward,
restoration, idempotence, dry run, circuit breakers, provenance, atomic rejection,
archive pruning, input validation and failure notifications.

After merging and configuring the token, manually dispatch **Mirror Sync** with
`dry_run=true` first. Inspect the job summary before authorizing a real sync.
Do not run a live history rewrite solely to test this PR.
