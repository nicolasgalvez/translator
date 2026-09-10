# Public Readiness and Project Setup Report

Status date: September 9, 2026

Tracking issue: [TRAN-71](https://procyoncreative.atlassian.net/browse/TRAN-71)

Delivery: [pull request #81](https://github.com/nicolasgalvez/translator/pull/81)

## Result

The repository is ready to remain public. The current tree and the complete
reachable history passed secret scanning. Local commit policy is now repository-owned, and
the pull request workflow exposes an always-present `required` gate for the main
branch ruleset.

## Public-readiness checks

| Check | Result | Evidence |
| --- | --- | --- |
| Current and historical secrets | Pass | Gitleaks scanned the complete reachable history and reported no leaks. |
| Environment files | Pass | No `.env` file appears in Git history; `.env*` is ignored and `.env.example` is tracked. |
| Local and Docker contexts | Pass | `scripts/check-public-context.sh` confirmed that both contexts exclude local secrets and local-only files. |
| User-specific paths | Pass | No tracked `/Users/`, `/home/`, or Windows user path was found. |
| Private keys and provider tokens | Pass | No tracked private-key marker or common AWS, GitHub, Slack, or OpenAI token pattern was found. |
| Provisioned hosting domains | Pass | No tracked WP Engine, Kinsta, SiteGround, Pantheon, or Flywheel hostname was found. |
| Commit attribution | Pass | No AI session URL, AI co-author trailer, or generated-by attribution was found. |
| Author addresses | Pass | Commit authors use GitHub noreply addresses. |
| Network exposure | Pass | The default listener is `127.0.0.1`; the documented `0.0.0.0` option is explicit opt-in behavior. |

## Project-setup enforcement

| Control | Local enforcement | Pull request enforcement |
| --- | --- | --- |
| Whitespace, YAML, merge markers, and file size | `pre-commit` stage | Project policy workflow |
| Python lint | `pre-commit` stage | Dedicated Pylint workflow |
| Secret scanning | Gitleaks at `pre-commit` | Official Gitleaks action scans the candidate and pull request history |
| Conventional Commits | `conventional-pre-commit` at `commit-msg` | Every commit in the pull request range |
| Existing user-level hooks | Chained after repository policy | Not applicable |
| Merge gate | Not applicable | Stable `required` job on every pull request |

`run.sh` installs the tracked `.githooks` directory after dependency setup. A
developer who does not start the app can run `uv sync --group dev` and then
`./scripts/install-git-hooks.sh`.

Git does not execute repository files automatically when a clone is created.
The setup command is therefore the local bootstrap boundary; the required CI
gate is the server-side backstop for a clone that has not run setup yet.

## Verification commands

```text
gitleaks git --redact --verbose .
./scripts/check-public-context.sh
uv run --only-group dev pytest tests/test_project_setup_enforcement.py -q
uv run --only-group dev pre-commit run --all-files
git hook run commit-msg -- <message-file>
```

The delivery pull request completed Pylint, Pytest, project-policy, and `required`
successfully. Ruleset `22227639` was then read back as active with no bypass
actors and `required` pinned to the GitHub Actions integration (`15368`).
