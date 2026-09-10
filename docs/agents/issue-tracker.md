# Issue tracker: Jira (TRAN)

Issues, PRDs, and specs for this repo live as Jira tickets in project **`TRAN`** on
`https://procyoncreative.atlassian.net`. GitHub Issues is **not** the tracker — never
open one for planned work.

`docs/jira.md` is the authoritative description of the workflow; this file is the
skill-facing summary of it. If they disagree, `docs/jira.md` wins.

## Access

Use the `procyon_atlassian` MCP server (`mcp__procyon_atlassian__*`), registered in
`.mcp.json`. Do not shell out to `curl` against the REST API unless MCP is unavailable.

- **Create a ticket**: `createJiraIssue` (project key `TRAN`)
- **Read a ticket**: `getJiraIssue` — include comments
- **Search**: `searchJiraIssuesUsingJql`, e.g. `project = TRAN AND status != Done`
- **Comment**: `addCommentToJiraIssue`
- **Edit fields / labels**: `editJiraIssue`
- **Move state**: `getTransitionsForJiraIssue`, then `transitionJiraIssue`

## Ticket rules (from docs/jira.md)

- **No work without a ticket.** If one doesn't exist, create it before branching.
- **Branch name**: `TRAN-NNN-short-description`
- **One ticket = one branch = one PR.** Post-merge discoveries get a *new* ticket.
- Every ticket needs an hours estimate and Acceptance Criteria, including the
  mandatory line **"Use Red/Green TDD"**.
- PR opened → ticket moves to QA; PR merged → Done. Both are automated by
  `.github/workflows/jira.yml` — don't transition those by hand.

## When a skill says "publish to the issue tracker"

Create a `TRAN` Jira ticket via `createJiraIssue` and follow the formatting
requirements in `docs/jira.md`.

## When a skill says "fetch the relevant ticket"

Resolve the `TRAN-NNN` key from the current branch name, then `getJiraIssue` with
comments. Bare `#42` in this repo means a GitHub PR, not a ticket.

## Pull requests as a request surface

**No.** Pull requests originate from tickets, not the other way around. The `triage`
skill should not read pull requests.

## Wayfinding operations

The `wayfinder` skill uses one `TRAN` ticket as the **map** (label
`wayfinder:map`), holding the Notes / Decisions-so-far / Fog body. **Child tickets**
are Jira sub-tasks of the map, labeled `wayfinder:<type>`
(`research`/`prototype`/`grilling`/`task`).

- **Blocking**: Jira issue links — `createIssueLink` with the `Blocks` link type
  (see `getIssueLinkTypes`). A ticket is unblocked when every blocker is Done.
- **Frontier query**: `searchJiraIssuesUsingJql` for
  `project = TRAN AND parent = TRAN-NNN AND status != Done AND assignee IS EMPTY`,
  then drop any with an open blocker; first in map order wins.
- **Claim**: assign the ticket to yourself via `editJiraIssue` — the session's first write.
- **Resolve**: `addCommentToJiraIssue` with the answer, transition to Done, then append
  a context pointer to the map's Decisions-so-far.
