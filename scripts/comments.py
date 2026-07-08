from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

RECENT_COMMENT_HOURS = 24
_URL_RE = re.compile(r"https?://[^\s<>\"'\])]+", re.I)
IST = timezone(timedelta(hours=5, minutes=30))

AMBIGUOUS_MARKER = "Update ambiguous"
STALE_UPDATE_MARKER = "Last comment"
NO_UPDATE_SINCE_STANDUP = "No update since last standup"
NO_COMMENTS_ON_TICKET = "No comments on this ticket at all"
_STALE_FALLBACK_CHARS = 280
_MAX_LINKS = 8
_MAX_SUMMARY_CHARS = 220
_MAX_BLOCKER_CHARS = 260


def _trim_text(text: str, max_len: int) -> str:
    s = " ".join((text or "").split()).strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1].rstrip() + "…"


def _summarize_for_gchat(text: str) -> str:
    """Safety cap for agent-written summaries before GChat rendering."""
    return _trim_text(text, _MAX_SUMMARY_CHARS)


def adf_plain(node) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(adf_plain(n) for n in node).strip()
    if not isinstance(node, dict):
        return ""
    parts = []
    if node.get("type") == "text":
        parts.append(node.get("text", ""))
    elif node.get("type") == "hardBreak":
        parts.append(" ")
    for child in node.get("content", []):
        parts.append(adf_plain(child))
    return " ".join(p for p in parts if p).strip()


def _normalize_url(url: str) -> str:
    return url.rstrip(".,;:!?)\"]'")


def _dedupe_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in urls:
        u = _normalize_url(raw.strip())
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def urls_from_plain(text: str) -> list[str]:
    if not text:
        return []
    return _dedupe_urls(_URL_RE.findall(text))


def urls_from_adf(node) -> list[str]:
    if node is None:
        return []
    if isinstance(node, list):
        found: list[str] = []
        for item in node:
            found.extend(urls_from_adf(item))
        return _dedupe_urls(found)
    if not isinstance(node, dict):
        return []

    found: list[str] = []
    attrs = node.get("attrs") or {}
    if node.get("type") == "inlineCard" and attrs.get("url"):
        found.append(attrs["url"])
    if node.get("type") == "blockCard" and attrs.get("url"):
        found.append(attrs["url"])

    for mark in node.get("marks") or []:
        if mark.get("type") == "link":
            href = (mark.get("attrs") or {}).get("href")
            if href:
                found.append(href)

    for child in node.get("content") or []:
        found.extend(urls_from_adf(child))

    return _dedupe_urls(found)


def extract_urls_from_body(body) -> list[str]:
    if body is None:
        return []
    if isinstance(body, str):
        return urls_from_plain(body)
    if isinstance(body, dict):
        return _dedupe_urls(urls_from_adf(body) + urls_from_plain(adf_plain(body)))
    return []


def comment_text(body) -> str:
    if isinstance(body, str):
        return body.strip()
    return adf_plain(body)


def format_comment_when(created: datetime) -> str:
    return created.astimezone(IST).strftime("%d %b %Y, %H:%M IST")


def _comment_snapshot(created: datetime, comment: dict) -> dict | None:
    body = comment.get("body")
    text = comment_text(body)
    if not text:
        return None
    author = (comment.get("author") or {}).get("displayName") or ""
    return {
        "hhmm": created.astimezone(IST).strftime("%H:%M"),
        "comment_when": format_comment_when(created),
        "created_iso": created.isoformat(),
        "author": author,
        "text": text,
        "urls": extract_urls_from_body(body),
    }


def _sorted_comments(issue_data: dict) -> list[tuple[datetime, dict]]:
    comments = issue_data.get("fields", {}).get("comment", {}).get("comments", [])
    parsed = []
    for c in comments:
        created = datetime.fromisoformat(c["created"].replace("Z", "+00:00"))
        parsed.append((created, c))
    return sorted(parsed, key=lambda x: x[0])


def export_comment_context(issue_data: dict, hours: int = RECENT_COMMENT_HOURS) -> dict:
    """Raw Jira comment context for the agent (export only; no classification)."""
    parsed = _sorted_comments(issue_data)
    if not parsed:
        return {
            "comment_count": 0,
            "comments_since_last_standup": [],
            "latest_comment": None,
            "all_urls": [],
        }

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    since_standup: list[dict] = []
    all_urls: list[str] = []

    for created, comment in parsed:
        snap = _comment_snapshot(created, comment)
        if not snap:
            continue
        all_urls.extend(snap.get("urls") or [])
        if created >= cutoff:
            since_standup.append(snap)

    latest_created, latest_raw = parsed[-1]
    latest = _comment_snapshot(latest_created, latest_raw)

    return {
        "comment_count": len(parsed),
        "comments_since_last_standup": since_standup,
        "latest_comment": latest,
        "all_urls": _dedupe_urls(all_urls),
    }


def format_links_block(urls: list[str]) -> str:
    if not urls:
        return ""
    shown = urls[:_MAX_LINKS]
    parts = [f"<a href='{html.escape(u)}'>{html.escape(u)}</a>" for u in shown]
    extra = f" <i>(+{len(urls) - _MAX_LINKS} more)</i>" if len(urls) > _MAX_LINKS else ""
    return f"<br>🔗 {' · '.join(parts)}{extra}"


def format_summarized_update(
    summary: str,
    hhmm: str | None = None,
    urls: list[str] | None = None,
) -> str:
    s = _summarize_for_gchat(summary)
    if not s:
        return format_ambiguous_update()
    prefix = f"{hhmm} — " if hhmm else ""
    block = f"💬 <i>{prefix}{html.escape(s)}</i>"
    link_urls = _dedupe_urls(urls or [])
    if link_urls:
        block += format_links_block(link_urls)
    return block


def format_ambiguous_update() -> str:
    return (
        f"<font color='#E65100'><i>⚠️ {AMBIGUOUS_MARKER} — "
        "could not infer concrete progress from the latest comment.</i></font>"
    )


def format_no_comments_on_ticket() -> str:
    return (
        f"<font color='#D93025'><i>ℹ️ {NO_COMMENTS_ON_TICKET}.</i></font>"
    )


def _truncate_comment(text: str, max_len: int = _STALE_FALLBACK_CHARS) -> str:
    s = " ".join((text or "").split()).strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1].rstrip() + "…"


def format_stale_update(
    summary: str,
    comment_when: str,
    urls: list[str] | None = None,
) -> str:
    s = _summarize_for_gchat(summary)
    if not s:
        return format_ambiguous_update()
    when = (comment_when or "last comment").strip()
    block = (
        f"<font color='#E65100'><i>⚠️ {NO_UPDATE_SINCE_STANDUP} · "
        f"{STALE_UPDATE_MARKER} · {html.escape(when)}: "
        f"{html.escape(s)}</i></font>"
    )
    link_urls = _dedupe_urls(urls or [])
    if link_urls:
        block += format_links_block(link_urls)
    return block


def _urls_for_decision(decision: dict) -> list[str]:
    explicit = decision.get("urls")
    if isinstance(explicit, list) and explicit:
        return _dedupe_urls(str(u) for u in explicit)
    return []


def format_blocker_note(text: str) -> str:
    s = _trim_text(_summarize_for_gchat(text), _MAX_BLOCKER_CHARS)
    return html.escape(s) if s else ""


def build_ip_comment_from_decision(decision: dict) -> str | None:
    """Render in-progress update line (summary / stale / ambiguous). Not the blockers section."""
    status = (decision.get("status") or "").lower()
    if status == "none":
        return None
    if status == "ambiguous":
        return format_ambiguous_update()
    if status in ("stale", "stale_blocker"):
        return format_stale_update(
            decision.get("text") or "",
            (decision.get("comment_when") or "").strip(),
            _urls_for_decision(decision),
        )
    if status == "summary":
        return format_summarized_update(
            decision.get("text") or "",
            (decision.get("hhmm") or "").strip() or None,
            _urls_for_decision(decision),
        )
    return None


def resolve_ip_comment_html(
    decision: dict,
    issue_data: dict | None = None,
) -> str | None:
    """Map agent decision + Jira comment context to the in-progress update line."""
    status = (decision.get("status") or "").lower()
    if status == "none":
        ctx = export_comment_context(issue_data or {})
        if ctx.get("comment_count", 0) == 0:
            return format_no_comments_on_ticket()
        latest = ctx.get("latest_comment") or {}
        text = (decision.get("text") or "").strip() or _truncate_comment(latest.get("text") or "")
        when = (decision.get("comment_when") or "").strip() or (latest.get("comment_when") or "")
        urls = _urls_for_decision(decision) or latest.get("urls")
        return format_stale_update(text, when, urls)
    return build_ip_comment_from_decision(decision)


def _resolve_blocker_reason(decision: dict) -> str | None:
    """Blocker line uses blocker_summary only — never the progress text field."""
    override = (decision.get("blocker_summary") or "").strip()
    if override:
        return format_blocker_note(override)
    return None


def _apply_blocker_fields(cfg: dict, ticket_key: str, decision: dict) -> None:
    """
    Populate data.blockers + blocker_notes from agent decisions.
    Jira blocker/impediment labels (from build_payload) are never removed here.
    """
    notes = dict(cfg.get("blocker_notes") or {})
    data = cfg.setdefault("data", {})
    blockers = data.setdefault("blockers", [])
    jira_label_flagged = ticket_key in blockers and ticket_key not in notes

    if decision.get("is_blocker") is False:
        notes.pop(ticket_key, None)
        if ticket_key in blockers and not jira_label_flagged:
            blockers.remove(ticket_key)
        cfg["blocker_notes"] = notes
        return

    reason = _resolve_blocker_reason(decision)
    if reason:
        _ensure_blocker_on_roster(cfg, ticket_key, reason)
        return

    notes.pop(ticket_key, None)
    if ticket_key in blockers and not jira_label_flagged:
        blockers.remove(ticket_key)
    cfg["blocker_notes"] = notes


def _ensure_blocker_on_roster(cfg: dict, ticket_key: str, note: str) -> None:
    if not note:
        return
    data = cfg.setdefault("data", {})
    blockers = data.setdefault("blockers", [])
    if ticket_key not in blockers:
        blockers.append(ticket_key)
    notes = dict(cfg.get("blocker_notes") or {})
    notes[ticket_key] = note
    cfg["blocker_notes"] = notes


def apply_finalize_to_payload(
    payload: dict,
    agent_decisions: dict[str, dict],
    *,
    comments_by_key: dict[str, dict],
    comments_path: Path,
    decisions_path: Path | None = None,
) -> None:
    """Mark comments fetched, apply agent decisions, set ready_to_post."""
    from payload import collect_in_progress_keys, mark_comments_fetched, mark_ip_comments_applied

    ip_keys = collect_in_progress_keys(payload)
    pipeline = dict(payload.get("pipeline") or {})
    mark_comments_fetched(pipeline, comments_path.resolve(), ip_keys)
    payload["pipeline"] = pipeline
    apply_ip_comment_decisions(payload, agent_decisions, comments_by_key=comments_by_key)
    mark_ip_comments_applied(payload["pipeline"], ip_keys, decisions_path=decisions_path)


def run_finalize_gate(
    payload: dict,
    agent_decisions: dict[str, dict],
    *,
    comments_by_key: dict[str, dict],
    comments_path: Path,
    decisions_path: Path | None = None,
    cwd: Path | None = None,
    require_webhook: bool = False,
) -> list[str]:
    """Apply agent decisions and return post-validation errors (empty = ok)."""
    from payload import mark_validated, validate_ready_to_post

    apply_finalize_to_payload(
        payload,
        agent_decisions,
        comments_by_key=comments_by_key,
        comments_path=comments_path,
        decisions_path=decisions_path,
    )
    mark_validated(payload["pipeline"])
    return validate_ready_to_post(payload, cwd=cwd, require_webhook=require_webhook)


def apply_ip_comment_decisions(
    payload: dict,
    decisions: dict[str, dict],
    *,
    comments_by_key: dict[str, dict] | None = None,
) -> None:
    cbk = comments_by_key or {}
    for cfg in payload.get("team_roster", {}).values():
        ip_keys = cfg.get("data", {}).get("in_progress", [])
        ip_comments = dict(cfg.get("ip_comments") or {})
        for key in ip_keys:
            decision = decisions.get(key, {"status": "none"})
            issue = cbk.get(key)
            html_out = resolve_ip_comment_html(decision, issue)
            if html_out:
                ip_comments[key] = html_out
            else:
                ip_comments.pop(key, None)

            _apply_blocker_fields(cfg, key, decision)
        cfg["ip_comments"] = ip_comments
