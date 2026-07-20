"""Standup payload pipeline: Jira bundle gates, build, validate, workspace cleanup."""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

JIRA_ARTIFACTS = {
    "main": "jira_main.json",
    "act_ip": "jira_act_ip.json",
    "act_cr": "jira_act_cr.json",
    "act_done": "jira_act_done.json",
}
COMMENTS_FILE = "comments_by_key.json"
DECISIONS_FILE = "ip_comment_decisions.json"
PAYLOAD_FILE = "standup_payload.json"
RECENT_COMMENTS_FILE = "recent_comments.json"
VERIFY_PAYLOAD_FILE = "_verify_standup_payload.json"

# Every JSON artifact the standup pipeline may write under workspace cwd.
# Keep in sync with SKILL.md step 8 and reference.md **Workspace scratch**.
# board_scope.json removed — fixed status_columns live in config.yaml jira.status_columns.
ROSTER_RESOLVED_FILE = "roster_resolved.json"
WORKSPACE_SCRATCH_ARTIFACTS = frozenset({
    PAYLOAD_FILE,
    DECISIONS_FILE,
    RECENT_COMMENTS_FILE,
    COMMENTS_FILE,
    VERIFY_PAYLOAD_FILE,
    "board_runtime.json",
    "board_scope.json",  # legacy workspace file from older runs
    ROSTER_RESOLVED_FILE,
    *JIRA_ARTIFACTS.values(),
})

# Permanent entries at workspace cwd (skill root). Cleanup deletes everything else.
WORKSPACE_PRESERVE_NAMES = frozenset({
    "README.md",
    "SKILL.md",
    "config.yaml",
    "reference.md",
    "scripts",
    ".gitignore",
})

ALLOWED_DECISION_STATUS = frozenset({
    "summary", "stale", "stale_blocker", "ambiguous", "none",
})

ROSTER_MEMBER_BUCKETS = (
    "todo", "in_progress", "in_review", "done",
    "no_desc", "blockers",
)
ROSTER_BUCKETS = ROSTER_MEMBER_BUCKETS + ("sprint_done",)
TRANSITION_KEYS = ("to_in_progress", "to_in_review", "to_done")
_THRESHOLD_KEYS = ("close_out_risk_days",)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_jira_issues(path: Path) -> list:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: expected JSON object")
    issues = data.get("issues")
    if not isinstance(issues, list):
        raise ValueError(f"{path.name}: missing 'issues' array")
    return issues


def _read_json_dict(path: Path) -> tuple[dict | None, str | None]:
    """Load a JSON object file; return (data, error_message)."""
    if not path.is_file():
        return None, f"missing {path.name}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return None, f"{path.name}: {exc}"
    if not isinstance(data, dict):
        return None, f"{path.name}: expected object"
    return data, None


def collect_in_progress_keys(payload: dict) -> list[str]:
    keys: list[str] = []
    for cfg in payload.get("team_roster", {}).values():
        keys.extend((cfg.get("data") or {}).get("in_progress", []))
    return sorted(set(keys))


def validate_jira_bundle(cwd: Path) -> list[str]:
    """Gate after step 4 — all four JQL files present, valid, and not duplicates/swapped."""
    errors: list[str] = []
    digests: dict[str, str] = {}
    empty_activity: dict[str, bool] = {}

    for label, name in JIRA_ARTIFACTS.items():
        path = cwd / name
        if not path.is_file():
            errors.append(f"missing {name} — run four separate JQL searches and save to cwd")
            continue
        try:
            issues = load_jira_issues(path)
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            errors.append(f"{name}: {exc}")
            continue
        digests[label] = file_sha256(path)
        if label == "main" and not issues:
            errors.append(f"{name}: main query returned zero issues")
        empty_activity[label] = label != "main" and not issues

    seen_digest: dict[str, str] = {}
    activity_pair = frozenset({"act_ip", "act_cr"})
    for label, digest in digests.items():
        if digest in seen_digest.values():
            other = next(k for k, v in digests.items() if v == digest and k != label)
            if frozenset({label, other}) == activity_pair:
                # Same tickets may transition IP → CR within 24h; JQL overlap is valid.
                continue
            if empty_activity.get(label) and empty_activity.get(other):
                # Two independently-empty activity results are trivially identical
                # (no issues means no content to differ on) — not a duplicate fetch.
                continue
            errors.append(
                f"{JIRA_ARTIFACTS[label]} is identical to {JIRA_ARTIFACTS[other]} — "
                "re-fetch with distinct JQL",
            )
        seen_digest[label] = digest

    return errors


def validate_comments_by_key(path: Path, expected_ip_keys: list[str]) -> list[str]:
    data, err = _read_json_dict(path)
    if err:
        if err.endswith("expected object"):
            return [f"{path.name}: expected object map key -> getJiraIssue JSON"]
        return [err]
    errors: list[str] = []
    missing = [k for k in expected_ip_keys if k not in data]
    extra = sorted(set(data) - set(expected_ip_keys))
    if missing:
        errors.append(
            f"{path.name}: missing getJiraIssue data for {', '.join(missing[:8])}"
            f"{'...' if len(missing) > 8 else ''}",
        )
    if extra:
        errors.append(
            f"{path.name}: stale keys not in current in_progress: {', '.join(extra[:8])}"
            f"{'...' if len(extra) > 8 else ''}",
        )
    for key in expected_ip_keys:
        entry = data.get(key)
        if entry is not None and not isinstance(entry, dict):
            errors.append(f"{path.name}[{key}]: expected object")
    return errors


def validate_agent_decisions(path: Path, expected_ip_keys: list[str]) -> list[str]:
    """Gate before finalize/post — every in-progress key needs a decision."""
    if not path.is_file():
        return [
            f"missing {path.name} — agent must write {DECISIONS_FILE} "
            f"for all {len(expected_ip_keys)} in-progress key(s) "
            "(analyze Jira comments; see reference.md)",
        ]
    data, err = _read_json_dict(path)
    if err:
        if err.endswith("expected object"):
            return [f"{path.name}: expected object map key -> decision"]
        return [err]
    errors: list[str] = []
    extra = sorted(set(data) - set(expected_ip_keys))
    if extra:
        errors.append(
            f"{path.name}: stale keys not in current in_progress: {', '.join(extra[:8])}"
            f"{'...' if len(extra) > 8 else ''}",
        )
    errors.extend(validate_agent_decisions_on_map(data, expected_ip_keys, label=path.name))
    return errors


def artifact_record(path: Path, *, issue_count: int | None = None) -> dict:
    rec = {"sha256": file_sha256(path), "path": path.name}
    if issue_count is not None:
        rec["issue_count"] = issue_count
    return rec


def init_pipeline(
    cfg: dict,
    config_path: Path,
    *,
    roster_names: list[str] | None = None,
) -> dict:
    names = roster_names if roster_names is not None else sorted(m["name"] for m in cfg["roster"])
    return {
        "version": 1,
        "started_at": utc_now(),
        "config_sha256": file_sha256(config_path),
        "scope_mode": cfg.get("scope_mode", "board_only"),
        "board_id": cfg.get("board_id"),
        "roster_count": len(names),
        "roster_names": names,
        "steps": {
            "jira_fetched": False,
            "payload_built": False,
            "comments_fetched": False,
            "ip_comments_applied": False,
            "validated": False,
            "posted": False,
        },
        "artifacts": {},
        "in_progress_keys": [],
    }


def mark_jira_fetched(pipeline: dict, cwd: Path) -> None:
    artifacts = {}
    for label, name in JIRA_ARTIFACTS.items():
        path = cwd / name
        issues = load_jira_issues(path)
        artifacts[label] = artifact_record(path, issue_count=len(issues))
    pipeline["artifacts"] = {**pipeline.get("artifacts", {}), **artifacts}
    pipeline["steps"]["jira_fetched"] = True


def mark_payload_built(
    pipeline: dict,
    *,
    cwd: Path,
    ip_keys: list[str],
    ticket_count: int,
    sprint: dict,
) -> None:
    mark_jira_fetched(pipeline, cwd)
    pipeline["in_progress_keys"] = ip_keys
    pipeline["ticket_count"] = ticket_count
    pipeline["sprint"] = sprint
    pipeline["steps"]["payload_built"] = True
    pipeline["built_at"] = utc_now()


def mark_comments_fetched(pipeline: dict, comments_path: Path, ip_keys: list[str]) -> None:
    pipeline["artifacts"]["comments_by_key"] = artifact_record(comments_path)
    pipeline["artifacts"]["comments_by_key"]["ip_key_count"] = len(ip_keys)
    pipeline["steps"]["comments_fetched"] = True
    pipeline["comments_fetched_at"] = utc_now()


def mark_ip_comments_applied(
    pipeline: dict,
    ip_keys: list[str],
    *,
    decisions_path: Path | None = None,
) -> None:
    pipeline["steps"]["ip_comments_applied"] = True
    pipeline["ip_comment_keys"] = ip_keys
    pipeline["ip_comments_applied_at"] = utc_now()
    pipeline["steps"]["ready_to_post"] = True
    if decisions_path and decisions_path.is_file():
        artifacts = dict(pipeline.get("artifacts") or {})
        artifacts["ip_comment_decisions"] = artifact_record(decisions_path)
        artifacts["ip_comment_decisions"]["ip_key_count"] = len(ip_keys)
        pipeline["artifacts"] = artifacts


def _mark_pipeline_step(pipeline: dict, step: str) -> None:
    pipeline["steps"][step] = True
    pipeline[f"{step}_at"] = utc_now()


def mark_validated(pipeline: dict) -> None:
    _mark_pipeline_step(pipeline, "validated")


def mark_posted(pipeline: dict) -> None:
    _mark_pipeline_step(pipeline, "posted")


def _check_artifact_freshness(pipeline: dict, cwd: Path | None) -> list[str]:
    """If scratch files still exist, fingerprints must match pipeline (no stale reuse)."""
    if cwd is None:
        return []
    errors: list[str] = []
    artifacts = pipeline.get("artifacts") or {}
    scratch = {**JIRA_ARTIFACTS, "comments_by_key": COMMENTS_FILE, "ip_comment_decisions": DECISIONS_FILE}
    for label, name in scratch.items():
        rec = artifacts.get(label)
        if not rec:
            continue
        path = cwd / rec.get("path", name)
        if not path.is_file():
            continue
        current = file_sha256(path)
        if current != rec.get("sha256"):
            errors.append(
                f"{path.name} on disk changed after pipeline recorded it — "
                "re-run build/finalize from fresh Jira data",
            )
    return errors


def validate_structure(
    payload: dict,
    *,
    require_webhook: bool = True,
) -> list[str]:
    """Checks after standup.py build (before IP finalize)."""
    errors: list[str] = []
    warnings: list[str] = []

    if require_webhook and not (payload.get("webhook_url") or "").strip():
        errors.append("webhook_url is missing")

    if not (payload.get("jira_base_url") or "").strip():
        errors.append("jira_base_url is missing")

    th = payload.get("thresholds") or {}
    for key in _THRESHOLD_KEYS:
        if key not in th:
            errors.append(f"thresholds.{key} is missing")

    roster = payload.get("team_roster") or {}
    if not roster:
        errors.append("team_roster is empty")

    pipeline = payload.get("pipeline") or {}
    expected_names = pipeline.get("roster_names") or []
    if expected_names:
        missing_members = sorted(set(expected_names) - set(roster.keys()))
        extra_members = sorted(set(roster.keys()) - set(expected_names))
        if missing_members:
            errors.append(f"team_roster missing config members: {', '.join(missing_members)}")
        if extra_members:
            errors.append(f"team_roster has unexpected names: {', '.join(extra_members)}")
    if pipeline.get("roster_count") and len(roster) != pipeline["roster_count"]:
        errors.append(
            f"team_roster has {len(roster)} members, expected {pipeline['roster_count']} from config",
        )

    sh = payload.get("sprint_health") or {}
    if not sh:
        errors.append("sprint_health block is missing")
    else:
        for key in ("ticket_counts", "story_points", "total_tickets", "total_story_points"):
            if key not in sh:
                errors.append(f"sprint_health.{key} is missing")
        counts = sh.get("ticket_counts") or {}
        if counts and sum(counts.values()) != sh.get("total_tickets"):
            errors.append("sprint_health.total_tickets does not match sum of ticket_counts")
        roster_n = sh.get("roster_ticket_count")
        if roster_n is not None and counts and sum(counts.values()) != roster_n:
            warnings.append(
                f"sprint_health total ({sum(counts.values())}) != roster_ticket_count ({roster_n})",
            )

    sprint = payload.get("sprint") or {}
    if sprint.get("number") is None:
        warnings.append("sprint.number is null — check sprint field on main JQL issues")

    tickets = payload.get("tickets") or {}
    missing_tickets: list[str] = []
    seen_in_buckets: dict[str, list[str]] = {}

    for name, cfg in roster.items():
        data = cfg.get("data") or {}
        for bucket in ROSTER_BUCKETS:
            if bucket not in data:
                errors.append(f"{name}: missing data bucket '{bucket}'")
            else:
                for tid in data.get(bucket, []):
                    if tid not in tickets:
                        missing_tickets.append(f"{name}/{bucket}/{tid}")
                    seen_in_buckets.setdefault(tid, []).append(f"{name}/{bucket}")

        if "blocker_notes" not in cfg:
            errors.append(f"{name}: missing blocker_notes (use {{}} when empty)")
        if "ip_comments" not in cfg:
            errors.append(f"{name}: missing ip_comments (use {{}} when empty)")

        trans = cfg.get("transitions") or {}
        for key in TRANSITION_KEYS:
            if key not in trans:
                errors.append(f"{name}: missing transition '{key}'")

        tip = set(trans.get("to_in_progress", []))
        tcr = set(trans.get("to_in_review", []))
        if tip and tip == tcr:
            warnings.append(
                f"{name}: to_in_progress and to_in_review are identical — "
                "check jira_act_ip.json vs jira_act_cr.json were not swapped",
            )

    for tid, places in seen_in_buckets.items():
        if len(places) > 1:
            warnings.append(f"{tid}: listed in multiple buckets: {', '.join(places)}")

    if missing_tickets:
        sample = ", ".join(missing_tickets[:8])
        suffix = "..." if len(missing_tickets) > 8 else ""
        errors.append(f"ticket keys not in tickets map: {sample}{suffix}")

    steps = pipeline.get("steps") or {}
    if not steps.get("payload_built"):
        errors.append("pipeline.steps.payload_built is false — run standup.py build first")

    if not steps.get("jira_fetched"):
        errors.append("pipeline.steps.jira_fetched is false — save four jira_*.json files first")

    ip_expected = sorted(pipeline.get("in_progress_keys") or collect_in_progress_keys(payload))
    ip_actual = collect_in_progress_keys(payload)
    if ip_expected and ip_actual != ip_expected:
        errors.append("in_progress keys changed since build — rebuild payload")

    for w in warnings:
        print(f"WARNING: {w}", file=__import__("sys").stderr)

    return errors


def validate_ready_to_post(
    payload: dict,
    *,
    cwd: Path | None = None,
    require_webhook: bool = True,
) -> list[str]:
    """Full gate before GChat post."""
    errors = validate_structure(payload, require_webhook=require_webhook)
    pipeline = payload.get("pipeline") or {}
    steps = pipeline.get("steps") or {}

    if not steps.get("comments_fetched"):
        errors.append(
            "pipeline.steps.comments_fetched is false — write comments_by_key.json "
            "with getJiraIssue for every in_progress key",
        )
    if not steps.get("ip_comments_applied"):
        errors.append(
            "pipeline.steps.ip_comments_applied is false — run standup.py finalize "
            f"with agent-written {DECISIONS_FILE}",
        )
    artifacts = pipeline.get("artifacts") or {}
    if steps.get("ip_comments_applied") and "ip_comment_decisions" not in artifacts:
        errors.append(
            f"pipeline missing {DECISIONS_FILE} artifact record — "
            "finalize must apply agent decisions, not heuristics",
        )
    if not steps.get("ready_to_post"):
        errors.append("pipeline.steps.ready_to_post is false — finalize payload before post")

    expected = sorted(pipeline.get("in_progress_keys") or collect_in_progress_keys(payload))
    processed = sorted(pipeline.get("ip_comment_keys") or [])
    if expected != processed:
        errors.append(
            f"pipeline.ip_comment_keys mismatch: expected {len(expected)}, got {len(processed)}",
        )

    errors.extend(_check_artifact_freshness(pipeline, cwd))
    return errors


def summarize_agent_decisions(agent_decisions: dict[str, dict]) -> tuple[int, int, int]:
    """Return (update_lines, stale, none) counts for CLI summary."""
    n_stale = sum(
        1
        for d in agent_decisions.values()
        if (d.get("status") or "").lower() in ("stale", "stale_blocker")
    )
    n_none = sum(1 for d in agent_decisions.values() if (d.get("status") or "").lower() == "none")
    n_other = len(agent_decisions) - n_stale - n_none
    return n_other, n_stale, n_none


# --- Workspace scratch cleanup ---


def list_temp_files(cwd: Path) -> list[str]:
    """Return cwd entries that are not permanent skill-root files (pipeline scratch)."""
    cwd = cwd.resolve()
    extras: list[str] = []
    for path in sorted(cwd.iterdir()):
        if path.name in WORKSPACE_PRESERVE_NAMES:
            continue
        extras.append(f"{path.name}/" if path.is_dir() else path.name)
    return extras


def cleanup_temp(cwd: Path, *, quiet: bool = False) -> list[str]:
    """Delete every non-permanent entry in workspace cwd. Safe to call multiple times."""
    cwd = cwd.resolve()
    removed: list[str] = []
    for path in sorted(cwd.iterdir()):
        if path.name in WORKSPACE_PRESERVE_NAMES:
            continue
        name = f"{path.name}/" if path.is_dir() else path.name
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        removed.append(name)
        if not quiet:
            print(f"removed: {name}")
    return removed


# --- Build standup_payload.json from Jira JSON ---


def description_empty(description) -> bool:
    if description is None:
        return True
    if isinstance(description, str):
        return len(description.strip()) < 20
    return len(json.dumps(description)) < 40


def story_points_value(fields: dict, sp_field: str | None) -> float | None:
    if not sp_field:
        return None
    raw = fields.get(sp_field)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def has_blocker_label(labels) -> bool:
    for label in labels or []:
        name = label.get("name", label) if isinstance(label, dict) else str(label)
        if str(name).lower() in ("blocker", "impediment"):
            return True
    return False


def aggregate_sprint_health_from_roster(
    team: dict,
    tickets: dict[str, dict],
) -> tuple[dict[str, int], dict[str, float]]:
    """Derive sprint-health totals from per-member roster buckets (single source of truth)."""
    counts = {"todo": 0, "in_progress": 0, "in_review": 0, "done": 0}
    points = {k: 0.0 for k in counts}
    roster_to_health = {
        "todo": "todo",
        "in_progress": "in_progress",
        "in_review": "in_review",
        "sprint_done": "done",
    }
    for cfg in team.values():
        data = cfg.get("data") or {}
        for roster_bucket, health_bucket in roster_to_health.items():
            for key in data.get(roster_bucket, []):
                counts[health_bucket] += 1
                raw_sp = (tickets.get(key) or {}).get("story_points")
                points[health_bucket] += float(raw_sp) if raw_sp is not None else 0.0
    return counts, points


def sprint_from_issues(issues: list, sprint_field: str) -> dict:
    import re

    for issue in issues:
        raw = issue.get("fields", {}).get(sprint_field) or []
        for sprint in raw if isinstance(raw, list) else [raw]:
            if not isinstance(sprint, dict) or sprint.get("state") not in (None, "", "active"):
                continue
            match = re.search(r"\d+", sprint.get("name", "") or "")
            days_left = None
            if sprint.get("endDate"):
                end = datetime.fromisoformat(sprint["endDate"].replace("Z", "+00:00"))
                days_left = (end.date() - datetime.now(timezone.utc).date()).days
            return {"number": int(match.group()) if match else None, "days_left": days_left}
    return {"number": None, "days_left": None}


def _empty_team_member(pod: str, account_id: str = "") -> dict:
    data_buckets = {k: [] for k in ROSTER_MEMBER_BUCKETS}
    data_buckets["sprint_done"] = []
    return {
        "pod": pod,
        "accountId": account_id,
        "data": data_buckets,
        "ip_comments": {},
        "blocker_notes": {},
        "transitions": {k: [] for k in TRANSITION_KEYS},
    }


def build_team(
    *,
    cfg: dict | None = None,
    issues: list | None = None,
    pod: str = "",
) -> tuple[dict, dict[str, str]]:
    """Roster from config roster (team mode) or Jira assignees (board-only)."""
    if cfg is not None:
        team = {
            member["name"]: _empty_team_member(member["pod"], member["accountId"])
            for member in cfg["roster"]
        }
        return team, dict(cfg["account_id_to_name"])

    team: dict = {}
    aid_map: dict[str, str] = {}
    for issue in issues or []:
        assignee = (issue.get("fields") or {}).get("assignee") or {}
        name = (assignee.get("displayName") or "").strip()
        aid = (assignee.get("accountId") or "").strip()
        if not name:
            continue
        if name not in team:
            team[name] = _empty_team_member(pod, aid)
        elif aid and not team[name].get("accountId"):
            team[name]["accountId"] = aid
        if aid:
            aid_map[aid] = name
    return team, aid_map


def _sorted_roster(team: dict) -> dict:
    return {
        name: team[name]
        for name, _ in sorted(team.items(), key=lambda x: (x[1].get("pod", ""), x[0]))
    }


def apply_activity(
    team: dict,
    issues: list,
    transition_key: str,
    account_id_to_name: dict[str, str],
    *,
    done_bucket: bool = False,
) -> None:
    from config import roster_assignee_name

    for issue in issues:
        key = issue["key"]
        name = roster_assignee_name(issue, account_id_to_name)
        if name not in team:
            continue
        team[name]["transitions"][transition_key].append(key)
        if done_bucket:
            team[name]["data"]["done"].append(key)


def reconcile_done_since_standup(team: dict) -> None:
    """
    Keep Done (Since Last Standup) aligned with current status.
    Tickets that transitioned to Done in the last 24h but were reopened
    (e.g. back to To Do) must not appear in data.done or transitions.to_done.
    """
    for cfg in team.values():
        data = cfg.get("data") or {}
        still_done = set(data.get("sprint_done") or [])
        data["done"] = [k for k in data.get("done", []) if k in still_done]
        transitions = cfg.get("transitions") or {}
        transitions["to_done"] = [
            k for k in transitions.get("to_done", []) if k in still_done
        ]


def cmd_check_jira(cwd: Path) -> int:
    import sys

    errors = validate_jira_bundle(cwd.resolve())
    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1
    print(f"OK: Jira bundle in {cwd} ({', '.join(JIRA_ARTIFACTS.values())})")
    return 0


def cmd_build(
    *,
    main: Path,
    act_ip: Path,
    act_cr: Path,
    act_done: Path,
    config: Path,
    output: Path,
) -> int:
    import sys

    from config import classify_status, load_config, payload_envelope, resolve_run_scope, roster_assignee_name

    cwd = Path.cwd()
    jira_errors = validate_jira_bundle(cwd)
    if jira_errors:
        for err in jira_errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    cfg = load_config(config, workspace=cwd, require_resolved_roster=True)
    run_scope = resolve_run_scope(cfg, cwd)
    status_buckets = run_scope["status_buckets"]
    scope_mode = cfg.get("scope_mode", "board_only")

    main_issues = load_jira_issues(main)
    if scope_mode == "board_only":
        team, aid_map = build_team(issues=main_issues, pod="")
    else:
        team, aid_map = build_team(cfg=cfg)

    tickets = {}
    roster_ticket_count = 0
    excluded_ticket_count = 0
    unknown_statuses: set[str] = set()

    for issue in main_issues:
        key = issue["key"]
        fields = issue["fields"]
        title = fields.get("summary", "")
        tickets[key] = {
            "id": key,
            "title": title,
            "story_points": story_points_value(fields, cfg["story_points_field"]),
        }
        name = roster_assignee_name(issue, aid_map)
        if name not in team:
            continue
        roster_ticket_count += 1
        status = fields.get("status", {}).get("name", "")
        data = team[name]["data"]

        bucket = classify_status(status, status_buckets=status_buckets)
        if bucket is None:
            if status:
                unknown_statuses.add(status)
                excluded_ticket_count += 1
            continue

        if bucket == "done":
            data["sprint_done"].append(key)
        elif bucket == "in_progress":
            data["in_progress"].append(key)
        elif bucket == "in_review":
            data["in_review"].append(key)
        elif bucket == "todo":
            data["todo"].append(key)

        if description_empty(fields.get("description")):
            data["no_desc"].append(key)
        if has_blocker_label(fields.get("labels")):
            data["blockers"].append(key)

    apply_activity(team, load_jira_issues(act_ip), "to_in_progress", aid_map)
    apply_activity(team, load_jira_issues(act_cr), "to_in_review", aid_map)
    apply_activity(team, load_jira_issues(act_done), "to_done", aid_map, done_bucket=True)
    reconcile_done_since_standup(team)

    sh_counts, sh_points = aggregate_sprint_health_from_roster(team, tickets)
    sprint = sprint_from_issues(main_issues, cfg["sprint_field"])
    pipeline = init_pipeline(cfg, config.resolve(), roster_names=sorted(team.keys()))
    if unknown_statuses:
        pipeline["unknown_statuses"] = sorted(unknown_statuses)
        print(
            f"WARNING: statuses not on board columns (excluded from Sprint Health): "
            f"{', '.join(sorted(unknown_statuses))}",
            file=sys.stderr,
        )
    mark_payload_built(
        pipeline,
        cwd=cwd,
        ip_keys=collect_in_progress_keys({"team_roster": _sorted_roster(team)}),
        ticket_count=len(tickets),
        sprint=sprint,
    )

    payload = {
        **payload_envelope(cfg),
        "sprint": sprint,
        "sprint_health": {
            "ticket_counts": sh_counts,
            "story_points": sh_points,
            "total_tickets": sum(sh_counts.values()),
            "total_story_points": sum(sh_points.values()),
            "roster_ticket_count": roster_ticket_count,
            "excluded_ticket_count": excluded_ticket_count,
            "jql_issue_count": len(tickets),
        },
        "tickets": tickets,
        "team_roster": _sorted_roster(team),
        "pipeline": pipeline,
    }

    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    ip_keys = pipeline["in_progress_keys"]
    print(
        f"Wrote {output} — sprint {payload['sprint']}, "
        f"jql {len(tickets)} / roster {roster_ticket_count} tickets, "
        f"sprint_health {sum(sh_counts.values())}, ip_keys {len(ip_keys)} "
        "(finalize required before post)",
    )
    return 0


def cmd_export_comments(payload_path: Path, comments_path: Path, output: Path | None) -> int:
    from comments import export_comment_context

    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    comments_by_key = json.loads(comments_path.read_text(encoding="utf-8"))
    ticket_titles = {
        k: (m.get("title") or "").strip()
        for k, m in (payload.get("tickets") or {}).items()
    }

    tickets: dict[str, dict] = {}
    for key in collect_in_progress_keys(payload):
        issue = comments_by_key.get(key)
        if not issue:
            tickets[key] = {
                "comment_count": 0,
                "error": "no_issue_data",
                "ticket_title": ticket_titles.get(key, ""),
            }
            continue
        ctx = export_comment_context(issue)
        ctx["ticket_title"] = ticket_titles.get(key, "")
        tickets[key] = ctx

    text = json.dumps({"tickets": tickets}, indent=2)
    if output:
        output.write_text(text, encoding="utf-8")
        print(f"Wrote {output} — write {DECISIONS_FILE} next (agent judgment)")
    else:
        print(text)
    return 0


def validate_agent_decisions_on_map(
    data: dict,
    expected_ip_keys: list[str],
    *,
    label: str = "decisions",
) -> list[str]:
    """Same rules as validate_agent_decisions but for an in-memory map."""
    errors: list[str] = []
    missing = [k for k in expected_ip_keys if k not in data]
    if missing:
        errors.append(
            f"{label}: missing {', '.join(missing[:8])}"
            f"{'...' if len(missing) > 8 else ''}",
        )
    for key in expected_ip_keys:
        dec = data.get(key)
        if not isinstance(dec, dict):
            errors.append(f"{label}[{key}]: expected object")
            continue
        status = (dec.get("status") or "").lower()
        if status not in ALLOWED_DECISION_STATUS:
            errors.append(
                f"{label}[{key}]: invalid status {status!r} "
                f"(allowed: {', '.join(sorted(ALLOWED_DECISION_STATUS))})",
            )
            continue
        text = (dec.get("text") or "").strip()
        if status in ("summary", "stale", "stale_blocker") and not text:
            errors.append(f"{label}[{key}]: status {status} requires text")
        if text and len(text) > 260:
            errors.append(
                f"{label}[{key}]: text too long ({len(text)} chars) — summarize, do not paste",
            )
        if status in ("stale", "stale_blocker") and not (dec.get("comment_when") or "").strip():
            errors.append(f"{label}[{key}]: {status} requires comment_when")
        if dec.get("is_blocker") is True and not (dec.get("blocker_summary") or "").strip():
            errors.append(f"{label}[{key}]: is_blocker requires blocker_summary")
    return errors


def cmd_finalize(
    payload_path: Path,
    comments_path: Path,
    decisions_path: Path,
    output: Path | None,
) -> int:
    import sys

    from comments import run_finalize_gate

    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    ip_keys = collect_in_progress_keys(payload)

    for err in validate_structure(payload, require_webhook=False):
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    for err in validate_comments_by_key(comments_path, ip_keys):
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    for err in validate_agent_decisions(decisions_path.resolve(), ip_keys):
        print(f"ERROR: {err}", file=sys.stderr)
        return 1

    agent_decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    comments_by_key = json.loads(comments_path.read_text(encoding="utf-8"))

    post_errors = run_finalize_gate(
        payload,
        agent_decisions,
        comments_by_key=comments_by_key,
        comments_path=comments_path,
        decisions_path=decisions_path.resolve(),
        cwd=Path.cwd(),
        require_webhook=False,
    )
    if post_errors:
        for err in post_errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    out = output or payload_path
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    n_ip = sum(
        1
        for cfg in payload["team_roster"].values()
        for k in cfg["data"]["in_progress"]
        if (cfg.get("ip_comments") or {}).get(k, "").strip()
    )
    n_other, n_stale, n_none = summarize_agent_decisions(agent_decisions)
    print(
        f"Finalized {out} — {len(agent_decisions)} decisions, "
        f"{n_ip} update lines ({n_other} fresh, {n_stale} stale, {n_none} no comments)",
    )
    return 0


def cmd_validate(
    payload_path: Path,
    *,
    no_webhook: bool,
    pre_finalize: bool,
    workspace: Path,
) -> int:
    import sys

    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    if pre_finalize:
        errors = validate_structure(payload, require_webhook=not no_webhook)
    else:
        errors = validate_ready_to_post(
            payload,
            cwd=workspace.resolve(),
            require_webhook=not no_webhook,
        )
    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    steps = (payload.get("pipeline") or {}).get("steps") or {}
    print(
        f"OK: {payload_path} ({len(payload.get('team_roster', {}))} members, "
        f"{len(payload.get('tickets', {}))} tickets, "
        f"ready_to_post={steps.get('ready_to_post', False)})",
    )
    return 0
