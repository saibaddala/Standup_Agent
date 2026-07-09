"""Standup config load/normalize and setup checks."""
from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore

_CONFIG_NAME = "config.yaml"
_ROSTER_RESOLVED_FILE = "roster_resolved.json"  # keep in sync with payload.ROSTER_RESOLVED_FILE
BOARD_RUNTIME_FILE = "board_runtime.json"  # workspace scratch — optional when scope is in config.yaml
_PLACEHOLDER_WEBHOOK_MARKERS = ("/spaces/.../", "key=...&token=...")
# Dict keys treated as “no pod” — GChat shows dev name only (no [pod] tag).
_NO_POD_TEAM_KEYS = frozenset({"", "members", "team", "default", "_"})


# --- Jira status → sprint-health bucket (four GChat sections) ---

_BOARD_BUCKETS = ("todo", "in_progress", "in_review", "done")


def status_buckets_from_columns(columns: dict[str, list]) -> dict[str, str]:
    """Build {status_name: bucket} from config jira.status_columns."""
    buckets: dict[str, str] = {}
    for bucket, statuses in columns.items():
        if bucket not in _BOARD_BUCKETS:
            continue
        for status in statuses or []:
            name = (status or "").strip() if isinstance(status, str) else ""
            if name:
                buckets[name] = bucket
    return buckets


def jql_status_from_columns(columns: dict[str, list], bucket: str) -> str:
    for status in columns.get(bucket) or []:
        name = (status or "").strip() if isinstance(status, str) else ""
        if name:
            return name
    raise ValueError(
        f"no Jira status in jira.status_columns.{bucket!r} — update config.yaml",
    )


def classify_status(status: str, *, status_buckets: dict[str, str]) -> str | None:
    """Map Jira status.name to sprint-health bucket via config status_columns."""
    return status_buckets.get(status)


# --- config.yaml parse ---


def config_path(skill_root: Path) -> Path:
    return skill_root.resolve() / _CONFIG_NAME


def load_config_raw(path: Path) -> dict:
    if yaml is None:
        raise ValueError("PyYAML required (pip install -r scripts/requirements.txt)")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{_CONFIG_NAME}: expected a YAML mapping")
    return raw


def parse_webhook(raw: dict) -> str:
    gc = raw.get("google_chat") if isinstance(raw.get("google_chat"), dict) else {}
    return (gc.get("webhook_url") or "").strip()


def parse_board_id(raw: dict) -> int:
    jira = raw.get("jira")
    if not isinstance(jira, dict):
        raise ValueError(f"{_CONFIG_NAME}: jira section is required")
    board_id = jira.get("board_id")
    if board_id is None:
        raise ValueError(
            f"{_CONFIG_NAME}: jira.board_id is required (numeric Jira agile board ID)",
        )
    try:
        return int(board_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{_CONFIG_NAME}: jira.board_id must be an integer") from exc


def parse_board_filter(raw: dict) -> dict:
    """Optional JQL scope from config — skips step 2b when set (automation-friendly)."""
    jira = raw.get("jira")
    if not isinstance(jira, dict):
        return {}
    out: dict = {}
    filter_id = jira.get("filter_id")
    if filter_id is not None:
        try:
            out["filter_id"] = int(filter_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{_CONFIG_NAME}: jira.filter_id must be an integer") from exc
    filter_name = (jira.get("filter_name") or "").strip()
    if filter_name:
        out["filter_name"] = filter_name
    jql_scope = (jira.get("jql_scope") or "").strip()
    if jql_scope:
        out["jql_scope"] = jql_scope
    if sum(1 for k in ("filter_id", "filter_name", "jql_scope") if k in out) > 1:
        raise ValueError(
            f"{_CONFIG_NAME}: set only one of jira.filter_id, jira.filter_name, jira.jql_scope",
        )
    return out


def parse_status_columns(raw: dict) -> dict[str, list[str]]:
    jira = raw.get("jira")
    if not isinstance(jira, dict):
        raise ValueError(f"{_CONFIG_NAME}: jira section is required")
    columns = jira.get("status_columns")
    if not isinstance(columns, dict):
        raise ValueError(
            f"{_CONFIG_NAME}: jira.status_columns is required "
            f"(todo, in_progress, in_review, done)",
        )
    out: dict[str, list[str]] = {}
    for bucket in _BOARD_BUCKETS:
        entries = columns.get(bucket)
        if not isinstance(entries, list):
            raise ValueError(
                f"{_CONFIG_NAME}: jira.status_columns.{bucket} must be a list of Jira status names",
            )
        names = [str(s).strip() for s in entries if str(s).strip()]
        if not names:
            raise ValueError(
                f"{_CONFIG_NAME}: jira.status_columns.{bucket} must not be empty",
            )
        out[bucket] = names
    return out


def normalize_pod_name(pod: str) -> str:
    """Return display pod name, or '' when config has no pod grouping."""
    key = (pod or "").strip()
    if not key or key.lower() in _NO_POD_TEAM_KEYS:
        return ""
    return key


def _parse_email_entries(entries: list, context: str) -> list[str]:
    emails: list[str] = []
    for item in entries:
        if isinstance(item, str):
            email = item.strip()
        elif isinstance(item, dict):
            email = (item.get("email") or "").strip()
        else:
            raise ValueError(f"{_CONFIG_NAME}: {context} entries must be emails")
        if email:
            emails.append(email)
    return emails


def parse_team_emails(raw: dict) -> dict[str, list[str]]:
    """Parse config.yaml team → { pod: [email, ...] } (pod '' when no pods configured)."""
    team = raw.get("team")
    if team is None:
        return {}
    if isinstance(team, list):
        emails = _parse_email_entries(team, "team")
        return {"": emails} if emails else {}
    if not isinstance(team, dict):
        raise ValueError(
            f"{_CONFIG_NAME}: 'team' must be a list of emails or a mapping of pods to email lists",
        )

    pods: dict[str, list[str]] = {}
    for pod, entries in team.items():
        if not isinstance(entries, list):
            raise ValueError(f"{_CONFIG_NAME}: pod {pod!r} must be a list of emails")
        emails = _parse_email_entries(entries, f"pod {pod!r}")
        if not emails:
            continue
        normalized_pod = normalize_pod_name(str(pod))
        pods.setdefault(normalized_pod, []).extend(emails)
    return pods


def validate_raw_config(raw: dict) -> None:
    """Require standup settings in config.yaml — no Python fallbacks at runtime."""
    parse_board_id(raw)
    parse_status_columns(raw)

    thresholds = raw.get("thresholds")
    if not isinstance(thresholds, dict) or thresholds.get("close_out_risk_days") is None:
        raise ValueError(f"{_CONFIG_NAME}: thresholds.close_out_risk_days is required")

    jira = raw.get("jira")
    if not isinstance(jira, dict):
        raise ValueError(f"{_CONFIG_NAME}: jira section is required")

    for key in ("browse_base_url",):
        if not (jira.get(key) or "").strip():
            raise ValueError(f"{_CONFIG_NAME}: jira.{key} is required")

    fields = jira.get("fields")
    if not isinstance(fields, dict) or not fields.get("sprint"):
        raise ValueError(f"{_CONFIG_NAME}: jira.fields.sprint is required")

    statuses = jira.get("statuses")
    if statuses is not None and not isinstance(statuses, dict):
        raise ValueError(f"{_CONFIG_NAME}: jira.statuses must be a mapping when set")


def roster_email_entries(raw: dict) -> list[dict]:
    """Flat list of {email, pod} from config (no accountId/name)."""
    return [
        {"email": email, "pod": normalize_pod_name(pod)}
        for pod, emails in parse_team_emails(raw).items()
        for email in emails
    ]


def load_roster_resolved(path: Path) -> dict[str, dict]:
    if not path.is_file():
        raise ValueError(
            f"missing {path.name} — lookup Jira accountIds for team emails "
            f"(see reference.md step 2)",
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: expected JSON object email -> {{accountId, name}}")
    out: dict[str, dict] = {}
    for email, entry in data.items():
        if not isinstance(entry, dict):
            continue
        aid = (entry.get("accountId") or "").strip()
        name = (entry.get("name") or "").strip()
        if aid and name:
            out[email.lower()] = {"accountId": aid, "name": name}
    if not out:
        raise ValueError(f"{path.name}: no resolved accountId/name entries")
    return out


def apply_roster_resolved(workspace: Path, resolutions: dict[str, dict]) -> int:
    """Write runtime roster_resolved.json (workspace scratch; not persisted in config)."""
    normalized: dict[str, dict] = {}
    for email, data in resolutions.items():
        key = email.lower()
        aid = (data.get("accountId") or "").strip()
        name = (data.get("name") or "").strip()
        if not aid or not name:
            continue
        normalized[key] = {"accountId": aid, "name": name}
    if not normalized:
        print("ERROR: no valid accountId/name pairs in resolutions", file=sys.stderr)
        return 1
    workspace.mkdir(parents=True, exist_ok=True)
    out = workspace / _ROSTER_RESOLVED_FILE
    out.write_text(json.dumps(normalized, indent=2), encoding="utf-8")
    print(f"Wrote {out} ({len(normalized)} member(s))")
    return 0


def _build_roster(
    team_emails: dict[str, list[str]],
    resolved: dict[str, dict] | None,
) -> list[dict]:
    roster: list[dict] = []
    if not team_emails:
        return roster
    if not resolved:
        return roster
    for pod, emails in team_emails.items():
        for email in emails:
            hit = resolved.get(email.lower())
            if not hit:
                continue
            roster.append({
                "name": hit["name"],
                "pod": normalize_pod_name(pod),
                "accountId": hit["accountId"],
                "email": email,
            })
    return roster


def normalize_config(raw: dict, *, resolved: dict[str, dict] | None = None) -> dict:
    jira = raw.get("jira") or {}
    fields = jira.get("fields") or {}
    statuses = jira.get("statuses") or {}
    thresholds = raw.get("thresholds") or {}
    gc = raw.get("google_chat") or {}

    team_emails = parse_team_emails(raw)
    board_id = parse_board_id(raw)
    board_filter = parse_board_filter(raw)
    status_columns = parse_status_columns(raw)
    roster = _build_roster(team_emails, resolved)
    scope_mode = "board_and_emails" if team_emails else "board_only"

    return {
        "webhook": gc.get("webhook_url", ""),
        "jira_base": jira["browse_base_url"],
        "board_id": board_id,
        **board_filter,
        "status_columns": status_columns,
        "status_buckets": status_buckets_from_columns(status_columns),
        "scope_mode": scope_mode,
        "thresholds": {
            "close_out_risk_days": int(thresholds["close_out_risk_days"]),
        },
        "status_overrides": {
            "in_progress_for_jql": (statuses.get("in_progress_for_jql") or "").strip() or None,
            "review_for_jql": (statuses.get("review_for_jql") or "").strip() or None,
            "done_for_jql": (statuses.get("done_for_jql") or "").strip() or None,
        },
        "sprint_field": fields["sprint"],
        "story_points_field": fields.get("story_points"),
        "roster": roster,
        "account_id_to_name": {r["accountId"]: r["name"] for r in roster},
    }


def load_config(
    path: Path,
    *,
    workspace: Path | None = None,
    require_resolved_roster: bool = False,
) -> dict:
    cwd = (workspace or Path.cwd()).resolve()
    raw = load_config_raw(path)
    validate_raw_config(raw)
    resolved = None
    team_emails = parse_team_emails(raw)
    resolved_path = cwd / _ROSTER_RESOLVED_FILE
    if team_emails and (require_resolved_roster or resolved_path.is_file()):
        resolved = load_roster_resolved(resolved_path)

    return normalize_config(raw, resolved=resolved)


def payload_envelope(cfg: dict) -> dict:
    return {
        "webhook_url": cfg["webhook"],
        "jira_base_url": cfg["jira_base"],
        "thresholds": dict(cfg["thresholds"]),
    }


def jql_assignee_list(cfg: dict) -> str:
    ids = [r["accountId"] for r in cfg["roster"] if r.get("accountId")]
    return ", ".join(f'"{aid}"' for aid in ids)


def resolve_run_scope(cfg: dict, workspace: Path) -> dict:
    """
    Jira status → bucket mapping from config.yaml.
    JQL scope from config.yaml (filter_id / filter_name / jql_scope), overlaid by
    workspace board_runtime.json when step 2b ran.
    """
    scope = {
        "board_id": cfg["board_id"],
        "columns": cfg["status_columns"],
        "status_buckets": cfg["status_buckets"],
    }
    if cfg.get("filter_id") is not None:
        scope["filter_id"] = cfg["filter_id"]
    if cfg.get("filter_name"):
        scope["filter_name"] = cfg["filter_name"]
    if cfg.get("jql_scope"):
        scope["jql_scope"] = cfg["jql_scope"]

    runtime_path = workspace.resolve() / BOARD_RUNTIME_FILE
    if runtime_path.is_file():
        try:
            runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            runtime = {}
        if isinstance(runtime, dict):
            if runtime.get("filter_id") is not None:
                scope["filter_id"] = runtime["filter_id"]
                scope.pop("filter_name", None)
                scope.pop("jql_scope", None)
            filter_name = (runtime.get("filter_name") or "").strip()
            if filter_name:
                scope["filter_name"] = filter_name
                scope.pop("filter_id", None)
                scope.pop("jql_scope", None)
            jql_scope = (runtime.get("jql_scope") or "").strip()
            if jql_scope:
                scope["jql_scope"] = jql_scope
                scope.pop("filter_id", None)
                scope.pop("filter_name", None)
    return scope


def apply_board_scope(workspace: Path, scope: dict) -> int:
    """Write board_runtime.json with filter_id from Jira board lookup (step 2b)."""
    if not isinstance(scope, dict):
        print("ERROR: board scope must be a JSON object", file=sys.stderr)
        return 2

    data = dict(scope)
    filter_id = data.get("filter_id")
    filter_name = (data.get("filter_name") or "").strip()
    jql_scope = (data.get("jql_scope") or "").strip()
    if filter_id is None and not filter_name and not jql_scope:
        print(
            "ERROR: need filter_id, filter_name, or jql_scope "
            "(from GET /rest/agile/1.0/board/{board_id} or config.yaml)",
            file=sys.stderr,
        )
        return 1

    workspace.mkdir(parents=True, exist_ok=True)
    out = workspace / BOARD_RUNTIME_FILE
    runtime_out: dict = {}
    if filter_id is not None:
        runtime_out["filter_id"] = filter_id
    if filter_name:
        runtime_out["filter_name"] = filter_name
    if jql_scope:
        runtime_out["jql_scope"] = jql_scope
    out.write_text(json.dumps(runtime_out, indent=2), encoding="utf-8")
    print(f"Wrote {out} — status buckets from config.yaml jira.status_columns")
    return 0


def jql_base_clause(cfg: dict, run_scope: dict | None = None) -> str:
    scope_mode = cfg.get("scope_mode") or "board_only"
    if run_scope is None:
        raise ValueError("run scope is required for JQL")

    filter_id = run_scope.get("filter_id")
    filter_name = (run_scope.get("filter_name") or "").strip()
    jql_scope = (run_scope.get("jql_scope") or "").strip()
    if filter_id is not None:
        scope_clause = f"filter = {filter_id}"
    elif filter_name:
        scope_clause = f'filter = "{filter_name}"'
    elif jql_scope:
        scope_clause = jql_scope
    else:
        board_id = cfg.get("board_id")
        raise ValueError(
            f"no JQL board scope — set jira.filter_id (preferred), jira.filter_name, or "
            f"jira.jql_scope in config.yaml, or run step 2b: GET /rest/agile/1.0/board/{board_id} "
            f"and apply-board-scope",
        )

    parts = [scope_clause, "sprint in openSprints()"]

    if scope_mode == "board_and_emails":
        assignees = jql_assignee_list(cfg)
        if not assignees:
            raise ValueError(
                f"team emails require {_ROSTER_RESOLVED_FILE} with accountIds (step 2)",
            )
        parts.append(f"assignee in ({assignees})")

    return " AND ".join(parts)


def jql_queries(cfg: dict, run_scope: dict | None = None) -> dict[str, str]:
    base = jql_base_clause(cfg, run_scope)
    if run_scope is None:
        raise ValueError("run scope is required for JQL")
    columns = run_scope.get("columns") or cfg.get("status_columns") or {}
    if not columns:
        raise ValueError("config.yaml missing jira.status_columns")
    overrides = cfg.get("status_overrides") or {}
    ip_status = overrides.get("in_progress_for_jql") or jql_status_from_columns(
        columns, "in_progress",
    )
    cr_status = overrides.get("review_for_jql") or jql_status_from_columns(
        columns, "in_review",
    )
    done_status = overrides.get("done_for_jql") or jql_status_from_columns(
        columns, "done",
    )
    return {
        "main": base,
        "act_ip": f'{base} AND status changed to "{ip_status}" during (-1d, now())',
        "act_cr": f'{base} AND status changed to "{cr_status}" during (-1d, now())',
        "act_done": f'{base} AND status changed to "{done_status}" during (-1d, now())',
    }


def roster_assignee_name(issue: dict, account_id_to_name: dict[str, str]) -> str | None:
    assignee = (issue.get("fields") or {}).get("assignee") or {}
    aid = assignee.get("accountId")
    if aid and aid in account_id_to_name:
        return account_id_to_name[aid]
    return assignee.get("displayName")


# --- Setup ---


def skill_root_from_arg(path: Path | None) -> Path:
    if path:
        root = path.resolve()
        if (root / _CONFIG_NAME).exists():
            return root
        if (root.parent / _CONFIG_NAME).exists():
            return root.parent
    return Path(__file__).resolve().parent.parent


def webhook_configured(raw: dict) -> bool:
    webhook = parse_webhook(raw)
    return bool(webhook) and not any(m in webhook for m in _PLACEHOLDER_WEBHOOK_MARKERS)


def bootstrap_config(skill_root: Path) -> int:
    target = config_path(skill_root)
    if target.is_file():
        print(f"{target} already exists")
        return 0
    print(
        f"ERROR: create {_CONFIG_NAME} at {target} — copy structure from reference.md Config section.",
        file=sys.stderr,
    )
    return 2


def ensure_config(skill_root: Path, *, strict: bool = False, cwd: Path | None = None) -> int:
    from payload import list_temp_files

    target = config_path(skill_root)
    check_cwd = (cwd or Path.cwd()).resolve()
    stale = list_temp_files(check_cwd)
    if stale:
        print(
            f"WARNING: leftover scratch in {check_cwd}: {', '.join(stale)} "
            f"— will be overwritten or removed by final cleanup at end of run",
            file=sys.stderr,
        )
    if not target.is_file():
        print(
            f"ERROR: missing {target} — create {_CONFIG_NAME} (see reference.md Config section)",
            file=sys.stderr,
        )
        return 2
    try:
        raw = load_config_raw(target)
        validate_raw_config(raw)
        team = parse_team_emails(raw)
    except ValueError as exc:
        print(f"{_CONFIG_NAME}: {exc}", file=sys.stderr)
        return 1
    try:
        cfg = load_config(target, workspace=check_cwd)
    except Exception as exc:  # noqa: BLE001
        print(f"{_CONFIG_NAME}: unreadable ({exc})", file=sys.stderr)
        return 1

    scope = cfg.get("scope_mode", "board_only")
    email_count = sum(len(v) for v in team.values())
    print(
        f"{_CONFIG_NAME}: ok (scope={scope}, board_id={cfg.get('board_id')}, "
        f"team_emails={email_count}, resolved_roster={len(cfg.get('roster', []))})",
    )
    exit_code = 0
    if not parse_webhook(raw):
        print(f"{_CONFIG_NAME}: set google_chat.webhook_url before GChat post.", file=sys.stderr)
        if strict:
            exit_code = 1
    elif not webhook_configured(raw):
        print(f"{_CONFIG_NAME}: webhook_url looks like a placeholder.", file=sys.stderr)
        if strict:
            exit_code = 1

    n = len(cfg.get("status_buckets") or {})
    print(f"status_columns: ok ({n} fixed Jira status(es) → 4 buckets)")

    has_config_scope = (
        cfg.get("filter_id") is not None or cfg.get("filter_name") or cfg.get("jql_scope")
    )
    has_runtime_scope = (check_cwd / BOARD_RUNTIME_FILE).is_file()
    if has_config_scope:
        kind = (
            "filter_id" if cfg.get("filter_id") is not None
            else "filter_name" if cfg.get("filter_name")
            else "jql_scope"
        )
        print(f"board_scope: ok ({kind} from config.yaml — step 2b skippable)")
    elif has_runtime_scope:
        print("board_scope: ok (board_runtime.json present)")
    else:
        print(
            "board_scope: missing — set jira.filter_id in config.yaml (recommended for "
            "automations) or run step 2b before print-jql",
            file=sys.stderr,
        )
        if strict:
            exit_code = 1

    return exit_code
