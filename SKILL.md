---
name: standup
description: >-
  Run standup: Jira MCP (read-only) → build payload → write ip_comment_decisions.json →
  post to GChat. Config at skill root (config.yaml). Triggers: /standup,
  run standup, daily standup.
---

## Runbook

Execute the full pipeline without confirmation. Do not read [README.md](README.md).

- Skill root = this directory
- Python: `<skill_root>/scripts/standup.py` (venv: `<skill_root>/scripts/.venv/bin/python3`; fresh VM: `scripts/bootstrap_venv.sh`)
- Config: `<skill_root>/config.yaml`
- Read [reference.md](reference.md) before step 3

## Must not

1. Jira/Confluence **writes** on MCP.
2. Reuse cached `jira_*.json` or payload from a prior run — fetch fresh Jira data every run.
3. Skip `cleanup` (step 8).
4. End the run with any workspace scratch file still present — see reference.md **Workspace scratch**.
5. Paraphrase ticket `summary` in `tickets` — verbatim only.
6. Expose stack traces or tool errors in GChat.
7. Skip gates: `check-jira` → `build` → `comments_by_key.json` → `export-comments` → `ip_comment_decisions.json` → `finalize` → `validate` → `post`.
8. Paste raw Jira comment bodies into GChat or `ip_comment_decisions.json`.
9. Set `blocker_summary` from any comment older than `latest_comment`.
10. Summarize only one comment when `comments_since_last_standup` has multiple entries — combine all (oldest → newest). See reference.md **Comment decision rules**.
11. Edit `config.yaml` during a run.
12. Hand-author GChat card text.
13. Create helper scripts (`.py`), temp dirs (`.tmp_issues`, `.staging`, etc.), or subagents for step 5b — use parallel `getJiraIssue` MCP calls only.
14. **Hand-write, condense, or simplify any MCP JSON response** — save the exact raw response to file verbatim; never reconstruct it manually.
15. **Skip pagination check** — after every `searchJiraIssuesUsingJql` call, verify `isLast: true` in the saved file before proceeding. If `isLast: false`, make additional calls with increasing `startAt` offset until `isLast: true`, then merge all `issues` arrays into one file. `check-jira` will error on `isLast: false`.
16. **Read only partial content of a large MCP response file** — always read or copy the full file; never rely on a truncated preview to judge completeness.

## Pipeline (workspace cwd)

| Step | Command / action |
|------|------------------|
| 0 | Fresh VM only: `scripts/bootstrap_venv.sh`. Optional: `standup.py verify <skill_root> --workspace <cwd>` |
| 1 | `standup.py ensure-config <skill_root> --strict` |
| 2 | If `team` in config: `roster-emails` → `lookupJiraAccountId` per email → `apply-roster '<json>' --workspace <cwd>`. Skip when no `team`. |
| 2b | **Skip when** `jira.filter_id`, `jira.filter_name`, or `jira.jql_scope` is in `config.yaml`. Otherwise: `GET /rest/agile/1.0/board/{jira.board_id}` → `apply-board-scope` with `filter_id`, `filter_name`, or `jql_scope` |
| 3 | `print-jql` → `getAccessibleAtlassianResources` → `cloudId`; four JQL searches → `jira_main.json`, `jira_act_ip.json`, `jira_act_cr.json`, `jira_act_done.json` |
| 4 | `standup.py check-jira [cwd]` |
| 5a | `standup.py build --main jira_main.json --act-ip jira_act_ip.json --act-cr jira_act_cr.json --act-done jira_act_done.json --config <skill_root>/config.yaml -o ./standup_payload.json` |
| 5b | `getJiraIssue` `fields:["comment"]` for each key in `pipeline.in_progress_keys` → `comments_by_key.json` (parallel MCP batches of ~10–15; no helper scripts) |
| 5c | `standup.py export-comments ./standup_payload.json ./comments_by_key.json -o ./recent_comments.json` |
| 5d | Write `ip_comment_decisions.json` per reference.md **Comment decision rules** (no script) |
| 5e | `standup.py finalize ./standup_payload.json ./comments_by_key.json ./ip_comment_decisions.json` |
| 6 | `standup.py validate ./standup_payload.json` |
| 7 | `standup.py post --workspace <cwd> --payload ./standup_payload.json --comments-by-key ./comments_by_key.json --decisions ./ip_comment_decisions.json` |
| 8 | `standup.py cleanup <cwd>` — **mandatory final step** (after step 7, whether post succeeded or failed) |

Step 7 re-runs finalize before POST; step 5e is still required.

## Cleanup (step 8)

Run `standup.py cleanup <cwd>` after step 7 (even on failure). Preserved files: see reference.md **Workspace scratch**.

## Step 1 failure

Report the missing/invalid `config.yaml` key from `ensure-config` stderr. Do not invent config values.

## Automations (cloud) — fail fast

Single-shot only. One forward pass through the pipeline; then `cleanup` and stop.

| Rule | Limit |
|------|-------|
| Retries per step | **1** retry max; then `cleanup` → `STANDUP_FAILED` → stop |
| JQL / filter guessing | **Forbidden** — use `print-jql` output only; never invent filters or board API workarounds when scope is in config |
| MCP tools | Atlassian read-only only — no `fetch`, `search` (Rovo), or extra tools |
| Step 2b | **Skip** when `jira.filter_id`, `jira.filter_name`, or `jira.jql_scope` is in config |
| Subagents / helper scripts | **Forbidden** |
| After termination | No debugging, no “let me try”, no waiting for the next schedule |

Success: `STANDUP_COMPLETE`. Failure: `STANDUP_FAILED: <one line>`. Either ends the run.

## GChat card order

Intro → Tickets Updates (one card per dev) → Today's Summary → Sprint Progress → Sprint Health → Closure Risks (if any).
