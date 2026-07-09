# Standup reference (agent)

Orchestration: [SKILL.md](SKILL.md). Spec for steps 2–7. No caching between runs.

## Config (`config.yaml`)

| Key | Required | Use |
|-----|----------|-----|
| `google_chat.webhook_url` | post | GChat webhook |
| `jira.board_id` | yes | Agile board ID (metadata); JQL scope comes from filter/jql_scope below |
| `jira.filter_id` | no* | Saved board filter ID — **preferred**; skips step 2b (set once in Jira UI) |
| `jira.filter_name` | no* | Saved filter name — alternative to `filter_id` |
| `jira.jql_scope` | no* | Raw JQL clause when filter ID/name unavailable (e.g. `project = FCCNS OR project = MX`) |
| `jira.status_columns` | yes | Fixed Jira `status.name` → four buckets (`todo`, `in_progress`, `in_review`, `done`). Hardcoded in config; same for every team on this board. |
| `jira.browse_base_url` | yes | Ticket links in cards |
| `jira.fields.sprint` | yes | Sprint custom field ID |
| `jira.fields.story_points` | no | Story-points field ID |
| `jira.statuses.*_for_jql` | no | Override first status used in activity JQL |
| `team` | no | `[email, …]` or `{pod: [email, …]}` — pod tag in GChat only when real pod names are set |
| `thresholds.close_out_risk_days` | yes | Close-out risk card threshold |

\*Set **one** of `jira.filter_id`, `jira.filter_name`, or `jira.jql_scope` in config for automations (skips step 2b). Otherwise step 2b must resolve scope via Agile REST API.

Do not store `accountId` or `name` in config — resolve each run (step 2). Do not edit `jira.status_columns` during a run.

### Scope modes

| Mode | Condition | JQL base | Roster at `build` |
|------|-----------|----------|-------------------|
| `board_only` | no `team` emails | `<SCOPE> AND sprint in openSprints()` | assignees from `jira_main.json` |
| `board_and_emails` | `team` emails set | above + `assignee in (<ASSIGNEES>)` | `roster_resolved.json` |

`<SCOPE>` = `filter = <id>`, `filter = "<name>"`, or `jira.jql_scope` from config.yaml (or `board_runtime.json` after step 2b). `<ASSIGNEES>` = quoted `accountId`s from `roster_resolved.json`.

### Team (`config.yaml`)

| Format | GChat dev line |
|--------|----------------|
| omitted | board-only roster; name only |
| flat email list | name only |
| `{PodName: [emails]}` | `Name [PodName]` |

Pod keys `members`, `team`, `default`, `_` are treated as no-pod (name only).

## Step 2 — roster (when `team` set)

1. `standup.py roster-emails <skill_root>`
2. `lookupJiraAccountId` per email → `{"email": {"accountId": "…", "name": "…"}, …}`
3. `standup.py apply-roster '<json>' --workspace <cwd>` → `./roster_resolved.json`

Skip when no `team` emails.

## Step 2b — board scope (skip when configured)

**Automations:** when `config.yaml` has `jira.filter_id`, `jira.filter_name`, or `jira.jql_scope`, go straight to step 3 — do not call the Agile board API.

**Manual / first-time setup** (no scope in config):

1. `GET /rest/agile/1.0/board/{jira.board_id}` → read `filter.id`, or use the board’s saved filter **name**
2. `standup.py apply-board-scope '{"filter_id": <id>}'`, `'{"filter_name": "<name>"}'`, or `'{"jql_scope": "<clause>"}' --workspace <cwd>`

To find `filter_id` in Jira UI: open the board → board settings / filter → “Edit filter query” — the URL contains the numeric filter ID. Add it to `config.yaml` as `jira.filter_id` and remove `filter_name` / `jql_scope` if set.

## Step 3 — MCP + JQL

| Action | Tool |
|--------|------|
| `cloudId` | `getAccessibleAtlassianResources` |
| Four JQL searches | `searchJiraIssuesUsingJql` (parallel); queries from `standup.py print-jql <skill_root> --workspace <cwd>` |
| Comments | `getJiraIssue` `fields:["comment"]` — `pipeline.in_progress_keys` only |
| Roster lookup | `lookupJiraAccountId` — step 2 emails |

Do not run extra JQL for `no_desc` — `build` derives it from `jira_main.json`.

### JQL output files

| File | Suffix |
|------|--------|
| `jira_main.json` | (base only) |
| `jira_act_ip.json` | `AND status changed to "<S_IP>" during (-1d, now())` |
| `jira_act_cr.json` | `AND status changed to "<S_CR>" during (-1d, now())` |
| `jira_act_done.json` | `AND status changed to "<S_DONE>" during (-1d, now())` |

`<S_IP>` / `<S_CR>` / `<S_DONE>` = first status in `jira.status_columns.in_progress` / `in_review` / `done`, unless overridden by `jira.statuses.in_progress_for_jql`, `review_for_jql`, `done_for_jql`.

### `jira_main.json` fields

`summary`, `assignee`, `status`, `updated`, `description`, `labels`, `<SPRINT_FIELD>` (+ `<SP_FIELD>` if set). Paginate (`maxResults` 100) until exhausted.

Activity files (`jira_act_*`): `summary`, `assignee` only. Do not cross-attribute transitions between `act_*` files.

## Build — status buckets

Fixed map in `config.yaml` → `jira.status_columns`. For each ticket, match `fields.status.name` to one of four GChat sections:

| GChat section | `team_roster.data` key | Match |
|---------------|------------------------|-------|
| To Do | `todo` | `jira.status_columns.todo` |
| In Progress | `in_progress` | `jira.status_columns.in_progress` |
| In Review | `in_review` | `jira.status_columns.in_review` |
| Done (sprint) | `sprint_done` | `jira.status_columns.done` |

| Extra key | Rule |
|-----------|------|
| `no_desc` | description empty or &lt; ~20 chars |
| `blockers` | label `blocker` or `impediment` |
| `done` | keys from `jira_act_done.json` (last 24h) **and** current status still in `sprint_done` |

Unknown statuses: exclude from sprint health; log warning. `jira.status_columns` is fixed — do not edit config mid-run.

### Roster mapping

- Keys = `name` from `roster_resolved.json` (team mode) or assignee `displayName` (board-only).
- Match assignee: `accountId` first, else `displayName`; else omit ticket from member.
- Every member gets all bucket arrays (`[]` when empty).

### Transitions

| Key | Source |
|-----|--------|
| `to_in_progress` | `jira_act_ip.json` |
| `to_in_review` | `jira_act_cr.json` |
| `to_done` | `jira_act_done.json`, excluding tickets no longer in a done status |

Sprint: `<SPRINT_FIELD>` on main issues → `sprint.number`, `sprint.days_left`.

## Comment decision rules

Apply per in-progress key. Prefer `recent_comments.json`; use `comments_by_key.json` only for full-thread context.

### Choose `status`

| Condition | `status` |
|-----------|----------|
| `comments_since_last_standup` non-empty | `summary` |
| `comments_since_last_standup` empty and `latest_comment` present | `stale` or `stale_blocker` |
| `comment_count` == 0 | `none` |
| Text exists, progress not inferable | `ambiguous` |

### Write `text`

- **`summary`:** Read every entry in `comments_since_last_standup` (oldest → newest). One combined line; if later comments supersede earlier ones, reflect final state.
- **`stale` / `stale_blocker`:** Summarize `latest_comment` only.
- **`none` / `ambiguous`:** Omit `text` unless `ambiguous` needs a short note.

`hhmm` = newest summarized comment (`summary` → newest in 24h window; `stale` → `latest_comment`). `comment_when` required for `stale` / `stale_blocker` from `latest_comment.comment_when`.

### Blockers

`stale` alone is not a blocker. Comment-derived blockers use **`latest_comment` only**.

| Condition | `is_blocker` |
|-----------|--------------|
| Dependency only in older comments, not `latest_comment` | `false` |
| Active wait in `latest_comment` (or newest in 24h window) | `true` + `blocker_summary` |
| `latest_comment` shows progress / unblocked | `false` |
| Jira `blocker`/`impediment` label but `latest_comment` contradicts | `false`; prefer `latest_comment` |

Set `is_blocker` explicitly on every key. Never set `blocker_summary` from a comment older than `latest_comment`.

### `ip_comment_decisions.json`

One object per in-progress key.

| `status` | GChat line |
|----------|------------|
| `summary` | Combined 24h update (`hhmm` optional) |
| `stale` | No update since last standup + `comment_when` + summarized `latest_comment` |
| `stale_blocker` | Same as `stale` when `latest_comment` has active dependency |
| `none` | No comments on ticket |
| `ambiguous` | Progress not inferable |

| Field | When required |
|-------|---------------|
| `text` | `summary`, `stale`, `stale_blocker` |
| `comment_when` | `stale`, `stale_blocker` |
| `hhmm` | optional (`summary`) |
| `blocker_summary` | `is_blocker: true` |
| `is_blocker` | always |

`comments_by_key.json`: `{ "KEY": { …full getJiraIssue… }, … }`.

### `export-comments` fields (per IP key)

| Field | Meaning |
|-------|---------|
| `comments_since_last_standup` | Last 24h, oldest → newest |
| `latest_comment` | Newest comment (any age) |
| `comment_count` | Total on issue |
| `ticket_title` | Context only |

## Workspace scratch

Pipeline steps write JSON and other files under workspace cwd during a run. **Step 8 (`standup.py cleanup <cwd>`) deletes every cwd entry except the permanent skill-root files** — whether GChat post succeeded or failed. Do not run cleanup at pipeline start.

**Preserved (never deleted):** `README.md`, `SKILL.md`, `config.yaml`, `reference.md`, `scripts/`, `.gitignore`

**Typical scratch files (removed by cleanup):**

| File | Step |
|------|------|
| `roster_resolved.json` | 2 |
| `board_runtime.json` | 2b |
| `board_scope.json` | legacy (older runs) |
| `jira_main.json` | 3 |
| `jira_act_ip.json` | 3 |
| `jira_act_cr.json` | 3 |
| `jira_act_done.json` | 3 |
| `standup_payload.json` | 5a |
| `comments_by_key.json` | 5b |
| `recent_comments.json` | 5c |
| `ip_comment_decisions.json` | 5d |
| `_verify_standup_payload.json` | 0 (self-removed if verify ran) |

Any other file or directory at workspace cwd (including agent workaround scripts, `.tmp_issues/`, etc.) is also removed.

## Payload (`standup_payload.json`)

After `finalize`: `pipeline.steps.ready_to_post` = true. Artifact SHA256s in `pipeline.artifacts`.

```json
{
  "webhook_url": "…",
  "jira_base_url": "…",
  "thresholds": { "close_out_risk_days": 3 },
  "sprint": { "number": 68, "days_left": 4 },
  "tickets": { "PROJ-1": { "id": "PROJ-1", "title": "verbatim summary", "story_points": 3 } },
  "team_roster": {
    "Name": {
      "pod": "…", "accountId": "…",
      "data": { "todo": [], "in_progress": [], "in_review": [], "done": [], "no_desc": [], "blockers": [] },
      "ip_comments": {}, "blocker_notes": {},
      "transitions": { "to_in_progress": [], "to_in_review": [], "to_done": [] }
    }
  },
  "pipeline": {
    "in_progress_keys": ["PROJ-1"],
    "steps": { "payload_built": true, "comments_fetched": true, "ip_comments_applied": true, "ready_to_post": true }
  }
}
```

Every bucket key must exist in `tickets` with verbatim `title`.
