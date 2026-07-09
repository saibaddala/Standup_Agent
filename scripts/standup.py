#!/usr/bin/env python3
"""Standup skill — single CLI for cleanup, config, payload, and GChat post."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def _run(cmd: list[str], *, cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def cmd_cleanup(args: argparse.Namespace) -> int:
    from payload import cleanup_temp

    cwd = args.cwd.resolve()
    removed = cleanup_temp(cwd, quiet=args.quiet)
    if not args.quiet:
        if removed:
            print(f"cleanup: {len(removed)} file(s) removed from {cwd}")
        else:
            print(f"cleanup: no scratch files to remove in {cwd}")
    return 0


def cmd_ensure_config(args: argparse.Namespace) -> int:
    from config import ensure_config, skill_root_from_arg

    cwd = getattr(args, "workspace", None) or Path.cwd()
    return ensure_config(skill_root_from_arg(args.skill_root), strict=args.strict, cwd=cwd)


def cmd_init(args: argparse.Namespace) -> int:
    from config import bootstrap_config, skill_root_from_arg

    return bootstrap_config(skill_root_from_arg(args.skill_root))


def cmd_roster_emails(args: argparse.Namespace) -> int:
    from config import config_path, load_config_raw, roster_email_entries, skill_root_from_arg

    root = skill_root_from_arg(args.skill_root)
    raw = load_config_raw(config_path(root))
    entries = roster_email_entries(raw)
    print(json.dumps({"emails": entries}, indent=2))
    return 0 if entries else 0


def cmd_apply_roster(args: argparse.Namespace) -> int:
    from config import apply_roster_resolved, skill_root_from_arg

    cwd = (getattr(args, "workspace", None) or Path.cwd()).resolve()
    try:
        resolutions = json.loads(args.roster_json)
    except json.JSONDecodeError as exc:
        print(f"ERROR: invalid JSON: {exc}", file=sys.stderr)
        return 2
    return apply_roster_resolved(cwd, resolutions)


def cmd_apply_board_scope(args: argparse.Namespace) -> int:
    from config import apply_board_scope

    cwd = (getattr(args, "workspace", None) or Path.cwd()).resolve()
    try:
        scope = json.loads(args.board_scope_json)
    except json.JSONDecodeError as exc:
        print(f"ERROR: invalid JSON: {exc}", file=sys.stderr)
        return 2
    return apply_board_scope(cwd, scope)


def cmd_check_jira(args: argparse.Namespace) -> int:
    from payload import cmd_check_jira as run
    return run(args.cwd)


def cmd_build(args: argparse.Namespace) -> int:
    from payload import cmd_build as run
    return run(
        main=args.main,
        act_ip=args.act_ip,
        act_cr=args.act_cr,
        act_done=args.act_done,
        config=args.config,
        output=Path(args.output),
    )


def cmd_export_comments(args: argparse.Namespace) -> int:
    from payload import cmd_export_comments as run
    return run(args.payload, args.comments_by_key, args.output)


def cmd_finalize(args: argparse.Namespace) -> int:
    from payload import DECISIONS_FILE, cmd_finalize as run
    return run(args.payload, args.comments_by_key, args.decisions or Path(DECISIONS_FILE), args.output)


def cmd_validate(args: argparse.Namespace) -> int:
    from payload import cmd_validate as run
    return run(
        args.payload,
        no_webhook=args.no_webhook,
        pre_finalize=args.pre_finalize,
        workspace=args.workspace,
    )


def cmd_print_jql(args: argparse.Namespace) -> int:
    from config import config_path, jql_queries, load_config, resolve_run_scope, skill_root_from_arg

    root = skill_root_from_arg(args.skill_root)
    config_file = config_path(root)
    cwd = (getattr(args, "workspace", None) or Path.cwd()).resolve()
    try:
        cfg = load_config(
            config_file,
            workspace=cwd,
            require_resolved_roster=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: config: {exc}", file=sys.stderr)
        return 1

    scope = cfg.get("scope_mode", "board_only")
    run_scope = resolve_run_scope(cfg, cwd)

    queries = jql_queries(cfg, run_scope)
    print(json.dumps({"scope_mode": scope, "board_id": cfg.get("board_id"), "jql": queries}, indent=2))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    from config import jql_assignee_list, load_config, payload_envelope, skill_root_from_arg
    from payload import ROSTER_BUCKETS, TRANSITION_KEYS, VERIFY_PAYLOAD_FILE, list_temp_files, validate_structure

    root = skill_root_from_arg(args.skill_root)
    workspace = (getattr(args, "workspace", None) or Path.cwd()).resolve()
    config_file = root / "config.yaml"
    errors: list[str] = []

    code, out = _run(
        [sys.executable, str(_SCRIPTS / "standup.py"), "ensure-config", str(root)],
        cwd=workspace,
    )
    print(out)
    if code != 0:
        errors.append("ensure-config failed")

    if not config_file.exists():
        errors.append(f"missing {config_file}")
        for err in errors:
            print(f"FAIL: {err}", file=sys.stderr)
        return 1

    stale = list_temp_files(workspace)
    if stale:
        print(
            f"WARNING: leftover scratch in workspace ({', '.join(stale[:5])}"
            f"{', ...' if len(stale) > 5 else ''}) — removed by final cleanup step",
            file=sys.stderr,
        )

    try:
        cfg = load_config(config_file, workspace=workspace)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"config: {exc}")
        cfg = None

    if cfg:
        scope = cfg.get("scope_mode", "board_only")
        n = len(cfg["roster"])
        print(f"config: scope={scope}, roster={n}, board_id={cfg.get('board_id')}")
        print(f"workspace: {workspace}")
        if cfg.get("board_id") is None:
            errors.append("jira.board_id is required in config.yaml")
        if scope == "board_and_emails" and not cfg.get("roster"):
            errors.append(
                f"roster empty — write roster_resolved.json via apply-roster after Jira lookup",
            )
        if scope == "board_and_emails" and not jql_assignee_list(cfg):
            errors.append("no assignee accountIds for JQL")

        roster = {}
        if scope == "board_only":
            sample_names = ["Board Dev"]
        else:
            sample_names = [m["name"] for m in cfg["roster"][:1]] or ["Sample Dev"]
        for name in sample_names:
            member = next((m for m in cfg["roster"] if m["name"] == name), None)
            roster[name] = {
                "pod": (member or {}).get("pod", ""),
                "accountId": (member or {}).get("accountId", ""),
                "data": {b: [] for b in ROSTER_BUCKETS},
                "ip_comments": {},
                "blocker_notes": {},
                "transitions": {k: [] for k in TRANSITION_KEYS},
            }
        sh_counts = {"todo": 0, "in_progress": 0, "in_review": 0, "done": 0}
        payload = {
            **payload_envelope(cfg),
            "sprint": {"number": 1, "days_left": 10},
            "sprint_health": {
                "ticket_counts": dict(sh_counts),
                "story_points": {k: 0.0 for k in sh_counts},
                "total_tickets": 0,
                "total_story_points": 0.0,
            },
            "tickets": {},
            "team_roster": roster,
            "pipeline": {
                "version": 1,
                "roster_count": len(roster),
                "roster_names": sorted(roster.keys()),
                "in_progress_keys": [],
                "steps": {
                    "jira_fetched": True,
                    "payload_built": True,
                    "comments_fetched": False,
                    "ip_comments_applied": False,
                    "ready_to_post": False,
                },
            },
        }
        val_errors = validate_structure(payload, require_webhook=False)
        if val_errors:
            errors.append(f"validate synthetic payload: {val_errors[0]}")

        tmp = workspace / VERIFY_PAYLOAD_FILE
        try:
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            code, out = _run(
                [
                    sys.executable,
                    str(_SCRIPTS / "gchat.py"),
                    "--payload",
                    str(tmp),
                    "--dry-run",
                ],
                cwd=workspace,
            )
            if code != 0:
                errors.append(f"gchat dry-run: {out[:200]}")
            else:
                print("gchat: dry-run OK")
        finally:
            tmp.unlink(missing_ok=True)

    if errors:
        for err in errors:
            print(f"FAIL: {err}", file=sys.stderr)
        return 1
    print("verify: all checks passed")
    return 0


def cmd_post(args: argparse.Namespace) -> int:
    from gchat import main as gchat_main

    argv: list[str] = ["--payload", str(args.payload)]
    if args.comments_by_key:
        argv.extend(["--comments-by-key", str(args.comments_by_key)])
    if args.decisions:
        argv.extend(["--decisions", str(args.decisions)])
    if args.dry_run:
        argv.append("--dry-run")
    if args.preview_chart:
        argv.append("--preview-chart")
    if args.no_mention_all:
        argv.append("--no-mention-all")
    if args.workspace:
        argv.extend(["--workspace", str(args.workspace)])
    gchat_main(argv)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Standup skill — Jira (read-only) to Google Chat",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "cleanup",
        help="Delete all standup scratch files in workspace (mandatory step 8 after post)",
    )
    p.add_argument("cwd", nargs="?", type=Path, default=Path.cwd())
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(func=cmd_cleanup)

    p = sub.add_parser("init", help="Check config.yaml exists (create manually if missing)")
    p.add_argument("skill_root", nargs="?", type=Path, default=None)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("ensure-config", help="Verify config.yaml (step 1)")
    p.add_argument("skill_root", nargs="?", type=Path, default=None)
    p.add_argument("--strict", action="store_true")
    p.add_argument("--workspace", type=Path, default=None)
    p.set_defaults(func=cmd_ensure_config)

    p = sub.add_parser("roster-emails", help="List team emails from config.yaml (step 2)")
    p.add_argument("skill_root", nargs="?", type=Path, default=None)
    p.set_defaults(func=cmd_roster_emails)

    p = sub.add_parser("apply-roster", help="Write roster_resolved.json from Jira lookup (step 2)")
    p.add_argument("roster_json", help='JSON map email -> {accountId, name}')
    p.add_argument("--workspace", type=Path, default=None)
    p.set_defaults(func=cmd_apply_roster)

    p = sub.add_parser(
        "apply-board-scope",
        help="Write board_runtime.json from board scope JSON (step 2b; skip when config has scope)",
    )
    p.add_argument(
        "board_scope_json",
        help='JSON with one of filter_id, filter_name, jql_scope (step 2b)',
    )
    p.add_argument("--workspace", type=Path, default=None)
    p.set_defaults(func=cmd_apply_board_scope)

    p = sub.add_parser("check-jira", help="Gate after four JQL files (step 4)")
    p.add_argument("cwd", nargs="?", type=Path, default=Path.cwd())
    p.set_defaults(func=cmd_check_jira)

    p = sub.add_parser("build", help="Build standup_payload.json (step 5a)")
    p.add_argument("--main", required=True, type=Path)
    p.add_argument("--act-ip", required=True, type=Path)
    p.add_argument("--act-cr", required=True, type=Path)
    p.add_argument("--act-done", required=True, type=Path)
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("-o", "--output", default="./standup_payload.json")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("export-comments", help="Raw comments for agent (step 5c)")
    p.add_argument("payload", type=Path)
    p.add_argument("comments_by_key", type=Path)
    p.add_argument("-o", "--output", type=Path, default=Path("./recent_comments.json"))
    p.set_defaults(func=cmd_export_comments)

    p = sub.add_parser("finalize", help="Apply ip_comment_decisions.json (step 5e)")
    p.add_argument("payload", type=Path)
    p.add_argument("comments_by_key", type=Path)
    p.add_argument("decisions", type=Path, nargs="?", default=None)
    p.add_argument("-o", "--output", type=Path, default=None)
    p.set_defaults(func=cmd_finalize)

    p = sub.add_parser("validate", help="Validate standup_payload.json (step 6)")
    p.add_argument("payload", type=Path)
    p.add_argument("--no-webhook", action="store_true")
    p.add_argument("--pre-finalize", action="store_true")
    p.add_argument("--workspace", type=Path, default=Path.cwd())
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("print-jql", help="Print four JQL queries for current scope (step 3)")
    p.add_argument("skill_root", nargs="?", type=Path, default=None)
    p.add_argument("--workspace", type=Path, default=None)
    p.set_defaults(func=cmd_print_jql)

    p = sub.add_parser("verify", help="End-to-end wiring check (optional pipeline step 0)")
    p.add_argument("skill_root", nargs="?", type=Path, default=None)
    p.add_argument("--workspace", type=Path, default=None)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("post", help="Finalize, validate, POST to GChat (step 7)")
    p.add_argument("--payload", required=True, type=Path)
    p.add_argument("--comments-by-key", type=Path, default=None)
    p.add_argument("--decisions", type=Path, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--preview-chart", action="store_true")
    p.add_argument("--no-mention-all", action="store_true")
    p.add_argument("--workspace", type=Path, default=None)
    p.set_defaults(func=cmd_post)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
