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
- Python: `<skill_root>/scripts/standup.py` (venv: `<skill_root>/scripts/.venv/bin/python3`)
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

## Pipeline (workspace cwd)

| Step | Command / action |
|------|------------------|
| 0 | `standup.py verify <skill_root> --workspace <cwd>` (optional) |
| 1 | `standup.py ensure-config <skill_root> --strict` |
| 2 | If `team` in config: `roster-emails` → `lookupJiraAccountId` per email → `apply-roster '<json>' --workspace <cwd>`. Skip when no `team`. |
| 2b | `GET /rest/agile/1.0/board/{jira.board_id}` → `filter_id` (or saved filter name) → `apply-board-scope '{"filter_id":N}'` or `'{"filter_name":"…"}' --workspace <cwd>` |
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

After step 7 (`post`), run `standup.py cleanup <cwd>`. Deletes **everything in workspace cwd except** `README.md`, `SKILL.md`, `config.yaml`, `reference.md`, and `scripts/`. Run cleanup even when post failed, was skipped, or errored.

## Step 1 failure

Report the missing/invalid `config.yaml` key from `ensure-config` stderr. Do not invent config values.

## GChat card order

Intro → Tickets Updates (one card per dev) → Today's Summary → Sprint Progress → Sprint Health → Closure Risks (if any).
