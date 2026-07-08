from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

from httplib2 import Http

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from payload import (  # noqa: E402
    COMMENTS_FILE,
    DECISIONS_FILE,
    ROSTER_MEMBER_BUCKETS,
    TRANSITION_KEYS,
    collect_in_progress_keys,
    list_temp_files,
    mark_posted,
    validate_agent_decisions,
    validate_comments_by_key,
    validate_ready_to_post,
    validate_structure,
)
from comments import resolve_ip_comment_html, run_finalize_gate  # noqa: E402

WEBHOOK_URL = ""
SPRINT_NO = None
SPRINT_DAYS_LEFT = None
CLOSE_OUT_RISK_DAYS = None
JIRA_BASE_URL = ""

TICKETS = {}
TEAM_ROSTER = {}
SPRINT_HEALTH = {}

_RISK_KEYS = ("no_desc",)

_CARD_RUN_ID = ""
_POST_INTERVAL_SEC = 1.15
_POST_MAX_RETRIES = 4
_MAX_WIDGETS_PER_ROSTER_CARD = 85
_MAX_TICKETS_PER_LIST = 12
_ROSTER_CARD_TITLE = "Tickets Updates"
_DEFAULT_TITLE = "No Title Found"
_DIVIDER_LINE = "―" * 40

_DASHBOARD_PREFIXES = (
    "sprintHealthSnapshotCard",
    "sprintProgressCard",
    "teamMetricsDashboardCard",
    "sprintCloseOutCard",
)
_MOVED_TO_IP_SINCE_STANDUP = "[moved to In Progress since last standup]"
_MOVED_TO_CR_SINCE_STANDUP = "[moved to In Review since last standup]"
_DEV_STATUS = (
    ("📋 To Do", "todo"),
    ("🔄 In Progress", "in_progress"),
    ("👀 In Review", "in_review"),
    ("✅ Done (Since Last Standup)", "done"),
)
_PIE_STATUS = (
    ("To Do", "todo"),
    ("In Progress", "in_progress"),
    ("In Review", "in_review"),
    ("Done", "done"),
)
_STATUS_KEYS = tuple(k for _, k in _PIE_STATUS)
# Bar chart: sprint_done = terminal statuses in sprint; per-dev "done" = last 24h only.
_BAR_STACK = (
    ("Done", "sprint_done"),
    ("In Review", "in_review"),
    ("In Progress", "in_progress"),
    ("To Do", "todo"),
)

_ICON = {
    "calendar": "https://www.gstatic.com/images/icons/material/system/2x/calendar_month_googblue_48dp.png",
    "summary": "https://www.gstatic.com/images/icons/material/system/2x/event_note_googblue_48dp.png",
    "analytics": "https://www.gstatic.com/images/icons/material/system/2x/analytics_googblue_48dp.png",
    "check": "https://www.gstatic.com/images/icons/material/system/2x/check_circle_googgreen_24dp.png",
    "warning": "https://www.gstatic.com/images/icons/material/system/2x/report_problem_black_48dp.png",
}

_HTTP = Http()
_JSON_HEADERS = {"Content-Type": "application/json; charset=UTF-8"}


class Color:
    DIVIDER = "#0AB6DD"
    SECTION = "#2196F3"
    MUTED = "#5f6368"
    UPDATE_LABEL = "#00796b"
    DEV_SEPARATOR = "#000000"
    DEV_NAME = "#1a73e8"
    WINS = "#1E8E3E"
    BLOCKER = "#FF9800"
    TRANSITIONS = "#E1E904"
    ACTION = "#f71500"
    RISK_LIST = "#941717"
    CLOSE_OUT = "#D93025"
    TODO_DEV = "#9056b0"
    IN_REVIEW = "#fbbc04"
    DONE = "#34a853"
    CHART_TITLE = "#202124"
    CHART_GRID = "#e8eaed"
    CHART_ZERO_LINE = "#dadce0"


STATUS_COLORS = {
    "todo": Color.BLOCKER,
    "in_progress": Color.SECTION,
    "in_review": Color.IN_REVIEW,
    "done": Color.DONE,
    "sprint_done": Color.DONE,
}

_DEV_COLORS = {
    "todo": Color.TODO_DEV,
    "in_progress": Color.SECTION,
    "in_review": Color.IN_REVIEW,
    "done": Color.DONE,
}


def _display_date():
    return date.today().strftime("%B %d, %Y")


def _date_subtitle(when=None):
    return f"Date: {when or _display_date()}"


def _roster_card_title():
    return f"{_ROSTER_CARD_TITLE} (Sprint {SPRINT_NO})"


def _ticket_meta(ticket_id):
    return TICKETS.get(ticket_id, {})


def _parse_sp(raw):
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _story_points_value(ticket_id):
    return _parse_sp(_ticket_meta(ticket_id).get("story_points")) or 0.0


def _format_sp(value):
    return str(int(value)) if value == int(value) else f"{value:g}"


def _title(ticket_id):
    return _ticket_meta(ticket_id).get("title") or _DEFAULT_TITLE


def _ticket_link(ticket_id):
    return f"<a href='{JIRA_BASE_URL}/{ticket_id}'><b>{ticket_id}</b></a>"


def _sp_badge(ticket_id):
    sp = _parse_sp(_ticket_meta(ticket_id).get("story_points"))
    return "" if sp is None else f" <font color='{Color.MUTED}'><i>({_format_sp(sp)} SP)</i></font>"


def _since_standup_badge(kind):
    """Transition marker placed after ticket id + SP."""
    label = (
        _MOVED_TO_IP_SINCE_STANDUP
        if kind == "ip"
        else _MOVED_TO_CR_SINCE_STANDUP
    )
    return f" <font color='{Color.MUTED}'><i>{label}</i></font>"


def _ticket_header(ticket_id, since_standup_kind=None):
    header = f"{_ticket_link(ticket_id)}{_sp_badge(ticket_id)}"
    if since_standup_kind:
        header += _since_standup_badge(since_standup_kind)
    return header


def _ticket_line(ticket_id, since_standup_kind=None):
    return f"{_ticket_header(ticket_id, since_standup_kind)}: {_title(ticket_id)}"


def _ticket_bullet(ticket_id):
    return f"  • {_ticket_line(ticket_id)}"


def _pod_tag(pod):
    return f"[{pod}]" if pod else ""


def _person_label(icon, name, pod=""):
    return f"{icon} {name}{f' {_pod_tag(pod)}' if pod else ''}"


def _text(text):
    return {"textParagraph": {"text": text}}


def _section_header(text, color=None):
    c = color or Color.SECTION
    return _text(f"<b><font color='{c}'>{text}</font></b>")


def _divider(color=None):
    c = color or Color.DIVIDER
    return _text(f"<b><font color='{c}'>{_DIVIDER_LINE}</font></b>")


def _muted_bold(text):
    return f"<font color='{Color.MUTED}'><b>{text}</b></font>"


def _centered_image(url):
    return {
        "columns": {
            "columnItems": [{
                "horizontalAlignment": "CENTER",
                "widgets": [{"image": {"imageUrl": url}}],
            }],
        },
    }


def _side_by_side_images(left_url, right_url, left_label="", right_label=""):
    def _column(url, label):
        widgets = []
        if label:
            widgets.append(_text(_muted_bold(label)))
        if url:
            widgets.append({"image": {"imageUrl": url}})
        return {"horizontalAlignment": "CENTER", "widgets": widgets}

    return {
        "columns": {
            "columnItems": [_column(left_url, left_label), _column(right_url, right_label)],
        },
    }


def _widgets_section(widgets):
    return {"widgets": widgets}


def _begin_card_run():
    global _CARD_RUN_ID
    _CARD_RUN_ID = datetime.now().strftime("%Y%m%d-%H%M%S")


def _is_dashboard(card):
    cid = card.get("cardId", "")
    return any(cid.startswith(p) for p in _DASHBOARD_PREFIXES)


def _partition_cards(cards):
    roster, dashboard = [], []
    for card in cards:
        (dashboard if _is_dashboard(card) else roster).append(card)
    return roster, dashboard


def _ticket_widget(
    key,
    title,
    update=None,
    blocker_note=None,
    since_standup_kind=None,
):
    line_title = _title(key) if title is None else title
    body = f"{_ticket_header(key, since_standup_kind)}: {line_title}"
    if update:
        body += f"<br>↳ <b><font color='{Color.UPDATE_LABEL}'>Update:</font></b> {update}"
    if blocker_note:
        body += (
            f"<br>↳ <b><font color='{Color.BLOCKER}'>⚠️ Blocker / dependency:</font></b> "
            f"{blocker_note}"
        )
    return _text(body)


def _ticket_list(
    label,
    keys,
    feedback=None,
    color=None,
    blocker_notes=None,
    since_standup_kinds=None,
):
    widgets = [_section_header(label, color or Color.SECTION)]
    if not keys:
        widgets.append(_text("<i>None</i>"))
        return widgets
    feedback = feedback or {}
    blocker_notes = blocker_notes or {}
    since_standup_kinds = since_standup_kinds or {}
    shown, extra = keys[:_MAX_TICKETS_PER_LIST], max(0, len(keys) - _MAX_TICKETS_PER_LIST)
    widgets.extend(
        _ticket_widget(
            k,
            _title(k),
            feedback.get(k),
            blocker_note=blocker_notes.get(k),
            since_standup_kind=since_standup_kinds.get(k),
        )
        for k in shown
    )
    if extra:
        widgets.append(_text(f"<i>+ {extra} more in Jira</i>"))
    return widgets


def _dev_block(dev_name, ticket_ids):
    return [f"<b>👤 {dev_name}</b>", *(_ticket_bullet(t) for t in ticket_ids), ""]


def _join_dev_blocks(blocks):
    if not blocks:
        return ""
    lines = [line for block in blocks for line in block]
    return "<br>".join(lines[:-1])


def _card_header(title, subtitle, image_url):
    return {"title": title, "subtitle": subtitle, "imageUrl": image_url, "imageType": "CIRCLE"}


def _card(card_id, title, subtitle, icon, sections, *, show_header=True):
    body = {"sections": sections if isinstance(sections, list) else [sections]}
    if show_header:
        body["header"] = _card_header(title, subtitle, icon)
    run_id = f"{card_id}-{_CARD_RUN_ID}" if _CARD_RUN_ID else card_id
    return {"cardId": run_id, "card": body}


def _dated_card(card_id, title, icon, section):
    return _card(card_id, title, _date_subtitle(), icon, section)


def _quickchart(chart_config, width=500, height=220, *, device_pixel_ratio=2):
    payload = quote(json.dumps(chart_config, separators=(",", ":")))
    return (
        f"https://quickchart.io/chart?c={payload}&w={width}&h={height}"
        f"&devicePixelRatio={device_pixel_ratio}&backgroundColor=white"
    )


def _short_member_label(name: str, max_len: int = 16) -> str:
    """Compact chart label: 'First L.' when possible, else truncated first token."""
    base = name.replace(" lnm", "").strip()
    parts = base.split()
    if len(parts) >= 2:
        label = f"{parts[0]} {parts[1][0]}."
    else:
        label = parts[0] if parts else base
    if len(label) <= max_len:
        return label
    return label[: max_len - 1] + "…"


def _accumulated_metrics():
    """Per-member bucket totals (done = sprint terminal statuses via sprint_done)."""
    counts = {k: 0 for k in _STATUS_KEYS}
    points = {k: 0.0 for k in _STATUS_KEYS}
    roster_key_for = {"done": "sprint_done", **{k: k for k in _STATUS_KEYS if k != "done"}}
    for data in (c.get("data", {}) for c in TEAM_ROSTER.values()):
        for key in _STATUS_KEYS:
            items = data.get(roster_key_for[key], [])
            counts[key] += len(items)
            points[key] += sum(_story_points_value(t) for t in items)
    return counts, points


def _sprint_health_metrics():
    """Full-sprint totals from payload (includes Done/Resolved/Closed in sprint)."""
    if SPRINT_HEALTH:
        counts = dict(SPRINT_HEALTH.get("ticket_counts") or {})
        points = {
            k: float((SPRINT_HEALTH.get("story_points") or {}).get(k, 0))
            for k in _STATUS_KEYS
        }
        return counts, points
    return _accumulated_metrics()


def _team_bar_chart_options(y_max):
    """Horizontal stacked bar — member names on Y-axis avoid x-label overlap."""
    return {
        "layout": {"padding": {"left": 8, "right": 24, "top": 16, "bottom": 8}},
        "title": {
            "display": True,
            "text": f"Sprint {SPRINT_NO} — Ticket Status per Member",
            "fontSize": 14,
            "fontColor": Color.CHART_TITLE,
            "fontStyle": "bold",
        },
        "legend": {
            "display": True,
            "position": "top",
            "labels": {"boxWidth": 14, "padding": 12, "fontSize": 11, "fontColor": Color.MUTED},
        },
        "scales": {
            "xAxes": [{
                "stacked": True,
                "gridLines": {"color": Color.CHART_GRID, "zeroLineColor": Color.CHART_ZERO_LINE},
                "ticks": {
                    "beginAtZero": True,
                    "precision": 0,
                    "stepSize": 1,
                    "max": y_max,
                    "fontSize": 11,
                    "fontColor": Color.MUTED,
                },
            }],
            "yAxes": [{
                "stacked": True,
                "barPercentage": 0.72,
                "categoryPercentage": 0.82,
                "gridLines": {"display": False},
                "ticks": {"fontSize": 11, "fontColor": Color.CHART_TITLE, "autoSkip": False},
            }],
        },
    }


def _team_bar_chart_url():
    labels, series, counts = [], {k: [] for _, k in _BAR_STACK}, []
    for name, cfg in TEAM_ROSTER.items():
        labels.append(_short_member_label(name))
        data = cfg.get("data", {})
        for _, key in _BAR_STACK:
            n = len(data.get(key, []))
            series[key].append(n)
            counts.append(n)
    y_max = max(max(counts) + 1, 4) if counts else 4
    n = len(labels)
    return _quickchart({
        "type": "horizontalBar",
        "data": {
            "labels": labels,
            "datasets": [
                {"label": lbl, "backgroundColor": STATUS_COLORS[key], "data": series[key]}
                for lbl, key in _BAR_STACK
            ],
        },
        "options": _team_bar_chart_options(y_max),
    }, width=560, height=max(280, 34 * n + 150))


def _doughnut_chart_options(*, chart_title=""):
    options = {
        "title": {"display": bool(chart_title), "text": chart_title, "fontSize": 13},
        "legend": {
            "display": True,
            "position": "bottom",
            "labels": {"boxWidth": 14, "padding": 10, "fontSize": 11},
        },
        "cutoutPercentage": 58,
        "plugins": {
            "datalabels": {
                "display": True,
                "color": "#ffffff",
                "font": {"weight": "bold", "size": 15},
                "anchor": "center",
                "align": "center",
            },
        },
    }
    return options


def _doughnut_chart_url(totals, *, chart_title="", include_all_slices=False):
    if include_all_slices:
        slices = [(lbl, k, totals.get(k, 0)) for lbl, k in _PIE_STATUS]
    else:
        slices = [(lbl, k, totals.get(k, 0)) for lbl, k in _PIE_STATUS if totals.get(k, 0) > 0]
    if not slices or not any(v for _, _, v in slices):
        return None
    return _quickchart({
        "type": "doughnut",
        "data": {
            "labels": [lbl for lbl, _, _ in slices],
            "datasets": [{
                "data": [v for _, _, v in slices],
                "backgroundColor": [STATUS_COLORS[k] for _, k, _ in slices],
            }],
        },
        "options": _doughnut_chart_options(chart_title=chart_title),
    }, width=400, height=300)


def _ensure_ip_comments(
    ip_comments,
    in_progress,
    *,
    comments_by_key=None,
    agent_decisions=None,
):
    out = dict(ip_comments)
    cbk = comments_by_key or {}
    decisions = agent_decisions or {}
    for ticket in in_progress:
        if out.get(ticket, "").strip():
            continue
        issue = cbk.get(ticket)
        html_out = resolve_ip_comment_html(decisions.get(ticket, {"status": "none"}), issue)
        if html_out:
            out[ticket] = html_out
    return out


def _aggregate_roster():
    done_by_dev, todo_by_dev = {}, {}
    blocker_map: dict[str, tuple[str, str]] = {}
    total_done = 0
    for dev, cfg in TEAM_ROSTER.items():
        data = cfg.get("data", {})
        notes = cfg.get("blocker_notes") or {}
        done, todo = data.get("done", []), data.get("todo", [])
        if done:
            done_by_dev[dev] = done
            total_done += len(done)
        if todo:
            todo_by_dev[dev] = todo
        # Active dependencies/blockers must have an explicit reason.
        # Python does not infer dependency reasons.
        for ticket_id in set(data.get("blockers", [])) | set(notes.keys()):
            reason = (notes.get(ticket_id) or "").strip()
            if reason:
                blocker_map[ticket_id] = (dev, reason)
    blockers = [(tid, dev, reason) for tid, (dev, reason) in blocker_map.items()]
    return {
        "done_by_dev": done_by_dev,
        "todo_by_dev": todo_by_dev,
        "blockers": blockers,
        "total_done": total_done,
    }


def _append_action_required(widgets, data):
    if not any(data.get(k) for k in _RISK_KEYS):
        return
    widgets.extend([_divider(), _section_header("🚨 ACTION REQUIRED", Color.ACTION)])
    items = data.get("no_desc", [])
    if items:
        hdr = f"📝 Missing description ({len(items)})"
        widgets.extend(_ticket_list(hdr, items, color=Color.RISK_LIST))


def _dev_widgets(cfg):
    data = cfg.get("data", {})
    activity = cfg.get("transitions", {})
    if not any(data.values()):
        return [_text("<i>No open-sprint tickets assigned.</i>")]

    ip_comments = _ensure_ip_comments(cfg.get("ip_comments", {}), data.get("in_progress", []))
    blocker_notes = cfg.get("blocker_notes") or {}
    since_standup = {k: "ip" for k in activity.get("to_in_progress", [])}
    since_standup.update({k: "cr" for k in activity.get("to_in_review", [])})
    widgets = []
    for label, key in _DEV_STATUS:
        fb = ip_comments if key == "in_progress" else {}
        bn = blocker_notes if key == "in_progress" else {}
        section_notes = (
            {k: since_standup[k] for k in data.get(key, []) if k in since_standup}
            if key in ("in_progress", "in_review")
            else {}
        )
        widgets.extend(
            _ticket_list(
                label,
                data.get(key, []),
                fb,
                _DEV_COLORS[key],
                bn,
                section_notes,
            ),
        )
    _append_action_required(widgets, data)
    return widgets


def _chunk_widget_list(widgets, limit):
    """Split a flat widget list when a single dev exceeds the per-card cap."""
    if not widgets:
        return [[]]
    chunks, cur = [], []
    for widget in widgets:
        if cur and len(cur) >= limit:
            chunks.append(cur)
            cur = []
        cur.append(widget)
    if cur:
        chunks.append(cur)
    return chunks


def build_roster_update_cards(display_date):
    sprint = f" · Sprint {SPRINT_NO}" if SPRINT_NO is not None else ""
    sub = f"Date: {display_date}{sprint}"
    if not TEAM_ROSTER:
        return [_card("teamStandupDetailsRosterCard0", _roster_card_title(), sub, _ICON["calendar"],
                     [_widgets_section([_text("<i>No developers in roster.</i>")])])]

    cards = []
    for i, (name, cfg) in enumerate(TEAM_ROSTER.items()):
        title = _person_label("👤", name, cfg.get("pod", ""))
        for j, chunk in enumerate(_chunk_widget_list(_dev_widgets(cfg), _MAX_WIDGETS_PER_ROSTER_CARD)):
            part_title = title if j == 0 else f"{title} (cont.)"
            card_id = f"teamStandupDevCard{i}" if j == 0 else f"teamStandupDevCard{i}p{j}"
            cards.append(_card(
                card_id,
                part_title,
                sub,
                _ICON["calendar"],
                [_widgets_section(chunk)],
            ))
    return cards


def build_sprint_summary_section(agg=None):
    agg = agg or _aggregate_roster()
    widgets = [_section_header("✅ KEY WINS (SINCE LAST STANDUP)", Color.WINS)]
    if agg["done_by_dev"]:
        widgets.extend([
            {
                "decoratedText": {
                    "text": f"<b>{agg['total_done']} Tickets Completed</b>",
                    "startIcon": {"iconUrl": _ICON["check"]},
                },
            },
            _text(_join_dev_blocks(_dev_block(d, t) for d, t in agg["done_by_dev"].items())),
        ])
    else:
        widgets.append(_text("none"))
    widgets.extend([
        _divider(Color.BLOCKER),
        _section_header("⚠️ ACTIVE DEPENDENCIES & BLOCKERS", Color.BLOCKER),
    ])
    if agg["blockers"]:
        for ticket_id, dev, reason in agg["blockers"]:
            widgets.append({
                "decoratedText": {
                    "topLabel": f"🛑 BLOCKED · {dev.upper()}",
                    "text": _ticket_line(ticket_id),
                    "bottomLabel": f"↳ Reason: {reason}",
                    "wrapText": True,
                },
            })
    else:
        widgets.append(_text("<i>No active blockers or dependency updates reported.</i>"))
    return _widgets_section(widgets)


def build_sprint_progress_section():
    return _widgets_section([_centered_image(_team_bar_chart_url())])


def build_sprint_health_section():
    counts, points = _sprint_health_metrics()
    total_tickets = SPRINT_HEALTH.get("total_tickets") or sum(counts.values())
    total_sp = SPRINT_HEALTH.get("total_story_points") or sum(points.values())
    if not total_tickets:
        return {"widgets": [_text("<i>No tickets in roster.</i>")]}

    # Always show all four workflow slices (incl. Done/Resolved/Closed in sprint).
    ticket_chart = _doughnut_chart_url(counts, include_all_slices=True)
    sp_chart = _doughnut_chart_url(points, include_all_slices=True) if total_sp > 0 else None
    widgets = []

    if ticket_chart and sp_chart:
        widgets.append(_side_by_side_images(
            ticket_chart,
            sp_chart,
            f"Tickets ({total_tickets})",
            f"Story points ({_format_sp(total_sp)})",
        ))
    elif ticket_chart:
        widgets.append(_text(_muted_bold(f"Tickets ({total_tickets})")))
        widgets.append(_centered_image(ticket_chart))
    elif sp_chart:
        widgets.append(_text(_muted_bold(f"Story points ({_format_sp(total_sp)})")))
        widgets.append(_centered_image(sp_chart))

    if total_sp <= 0:
        if any(m.get("story_points") is not None for m in TICKETS.values()):
            widgets.append(_text(_muted_bold("Story points: 0")))
        else:
            widgets.append(_text(
                "<i>Story points not shown — set <code>jira.fields.story_points</code> in config "
                "and include that field in Jira search.</i>"
            ))
    return {"widgets": widgets}


def build_close_out_card(agg=None):
    if SPRINT_DAYS_LEFT is None or CLOSE_OUT_RISK_DAYS is None:
        return None
    if SPRINT_DAYS_LEFT > CLOSE_OUT_RISK_DAYS:
        return None
    agg = agg or _aggregate_roster()
    sections = []

    sp_lines = []
    _sp_buckets = ("todo", "in_progress", "in_review", "sprint_done")
    for dev, cfg in TEAM_ROSTER.items():
        data = cfg.get("data", {})
        done_sp = sum(_story_points_value(t) for t in data.get("sprint_done", []))
        total_sp = sum(
            sum(_story_points_value(t) for t in data.get(k, []))
            for k in _sp_buckets
        )
        if total_sp == 0:
            continue
        pod = f" {_pod_tag(cfg['pod'])}" if cfg.get("pod") else ""
        sp_lines.append(
            f"• <b>{dev}</b>{pod} — {_muted_bold(f'{_format_sp(done_sp)}/{_format_sp(total_sp)} SP')}"
        )
    if sp_lines:
        sections.append(_widgets_section([
            _section_header("📊 Story Points — Done / Total", Color.DEV_NAME),
            _text("<br>".join(sp_lines)),
        ]))

    if agg["todo_by_dev"]:
        sections.append(_widgets_section([
            _section_header("🚨 CRITICAL CLOSE-OUT RISK", Color.CLOSE_OUT),
            _text("<b>Tickets still stuck in To-Do:</b>"),
            _text(_join_dev_blocks(_dev_block(d, t) for d, t in agg["todo_by_dev"].items())),
        ]))

    if not sections:
        return None
    days = "day" if SPRINT_DAYS_LEFT == 1 else "days"
    return _card(
        "sprintCloseOutCard",
        "Sprint Closure Risks",
        f"{SPRINT_DAYS_LEFT} {days} left · Sprint {SPRINT_NO}",
        _ICON["warning"],
        sections,
    )


def generate_standup_card():
    _begin_card_run()
    agg = _aggregate_roster()
    cards = build_roster_update_cards(_display_date())
    cards.append(_dated_card(
        "sprintHealthSnapshotCard", "Today's Summary", _ICON["summary"],
        build_sprint_summary_section(agg),
    ))
    cards.append(_dated_card(
        "sprintProgressCard", "Sprint Progress", _ICON["analytics"],
        build_sprint_progress_section(),
    ))
    cards.append(_dated_card(
        "teamMetricsDashboardCard", "Sprint Health", _ICON["analytics"],
        build_sprint_health_section(),
    ))
    close_out = build_close_out_card(agg)
    if close_out:
        cards.append(close_out)
    return cards


def _post_throttled(payload, thread, label, posted, *, first=False):
    if not first:
        time.sleep(_POST_INTERVAL_SEC)
    last = ""
    for attempt in range(_POST_MAX_RETRIES):
        if attempt:
            time.sleep(_POST_INTERVAL_SEC * (attempt + 1))
        resp, body = _HTTP.request(
            uri=WEBHOOK_URL, method="POST", headers=_JSON_HEADERS,
            body=json.dumps({**payload, "thread": thread}),
        )
        status = int(resp.get("status", 0))
        last = body.decode("utf-8")
        if status == 429 and attempt < _POST_MAX_RETRIES - 1:
            continue
        if status != 200:
            raise RuntimeError(f"Google Chat post failed for '{label}': HTTP {status}. {last[:500]}")
        posted.append(label)
        return status, last
    raise RuntimeError(f"Google Chat rate-limited '{label}' after {_POST_MAX_RETRIES} retries. {last[:500]}")


def post_message(cards, mention_all=False):
    if not WEBHOOK_URL:
        raise ValueError("A valid Google Chat Webhook URL must be provided.")

    when = _display_date()
    thread = {"threadKey": f"StandUp-{date.today().strftime('%B-%d-%Y')}"}
    posted = []
    first = True
    intro = f"{'<users/all> ' if mention_all else ''}📅 Today's Standup — {when}"
    _post_throttled({"text": intro}, thread, "intro", posted, first=first)
    first = False

    roster, dashboard = _partition_cards(cards)
    for card in roster:
        title = card.get("card", {}).get("header", {}).get("title", _roster_card_title())
        _post_throttled({"cardsV2": [card]}, thread, title, posted, first=first)
        first = False

    for card in dashboard:
        title = card.get("card", {}).get("header", {}).get("title", "Dashboard")
        _post_throttled({"cardsV2": [card]}, thread, title, posted, first=first)
        first = False

    return 200, posted


def _roster_entry(cfg):
    data, trans = cfg.get("data", {}), cfg.get("transitions", {})
    roster_data = {b: list(data.get(b, [])) for b in ROSTER_MEMBER_BUCKETS}
    roster_data["sprint_done"] = list(data.get("sprint_done", []))
    return {
        "pod": cfg.get("pod", ""),
        "accountId": cfg.get("accountId", ""),
        "data": roster_data,
        "ip_comments": dict(cfg.get("ip_comments") or {}),
        "blocker_notes": dict(cfg.get("blocker_notes") or {}),
        "transitions": {k: list(trans.get(k, [])) for k in TRANSITION_KEYS},
    }


def load_payload(path, *, require_webhook: bool = True, require_post_ready: bool = True):
    global WEBHOOK_URL, SPRINT_NO, SPRINT_DAYS_LEFT, CLOSE_OUT_RISK_DAYS
    global JIRA_BASE_URL, TICKETS, TEAM_ROSTER, SPRINT_HEALTH

    p = json.loads(Path(path).read_text(encoding="utf-8"))
    if require_post_ready:
        errors = validate_ready_to_post(
            p,
            cwd=Path.cwd(),
            require_webhook=require_webhook,
        )
    else:
        errors = validate_structure(p, require_webhook=require_webhook)
    if errors:
        raise ValueError("Invalid standup payload:\n- " + "\n- ".join(errors))

    if p.get("webhook_url"):
        WEBHOOK_URL = p["webhook_url"]

    base = p.get("jira_base_url", "").rstrip("/")
    if base:
        JIRA_BASE_URL = base if base.endswith("/browse") else f"{base}/browse"

    th = p.get("thresholds") or {}
    if "close_out_risk_days" not in th:
        raise ValueError("Invalid standup payload:\n- thresholds.close_out_risk_days is missing")
    CLOSE_OUT_RISK_DAYS = th["close_out_risk_days"]

    sprint = p.get("sprint") or {}
    SPRINT_NO = sprint.get("number")
    SPRINT_DAYS_LEFT = sprint.get("days_left")

    TICKETS.clear()
    TICKETS.update({
        k: {
            "id": m.get("id", k),
            "title": m.get("title", ""),
            "story_points": m.get("story_points"),
        }
        for k, m in p.get("tickets", {}).items()
    })

    TEAM_ROSTER.clear()
    sorted_roster = sorted(
        p.get("team_roster", {}).items(),
        key=lambda item: (item[1].get("pod", ""), item[0]),
    )
    TEAM_ROSTER.update({n: _roster_entry(c) for n, c in sorted_roster})
    SPRINT_HEALTH.clear()
    SPRINT_HEALTH.update(p.get("sprint_health") or {})


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _prepare_payload_for_post(
    payload_path: Path,
    *,
    comments_by_key_path: Path | None,
    decisions_path: Path | None,
    cwd: Path,
) -> dict:
    stale = list_temp_files(cwd)
    if stale and not (comments_by_key_path and comments_by_key_path.is_file()):
        raise SystemExit(
            "ERROR: workspace has scratch files but --comments-by-key missing. "
            "Re-run the full pipeline with fresh comments_by_key.json.",
        )

    payload = _load_json(payload_path)

    if not comments_by_key_path or not comments_by_key_path.is_file():
        raise SystemExit(
            f"ERROR: --comments-by-key is required for post (e.g. ./{COMMENTS_FILE}). "
            "Fetch fresh getJiraIssue(comment) JSON for every in-progress key this run.",
        )

    ip_keys = collect_in_progress_keys(payload)
    comment_errors = validate_comments_by_key(comments_by_key_path, ip_keys)
    if comment_errors:
        for err in comment_errors:
            print(f"ERROR: {err}", file=sys.stderr)
        raise SystemExit(1)

    if not decisions_path or not decisions_path.is_file():
        raise SystemExit(
            f"ERROR: --decisions is required for post (e.g. ./{DECISIONS_FILE}). "
            "Agent must write ip_comment_decisions.json after standup.py export-comments.",
        )

    decision_errors = validate_agent_decisions(decisions_path.resolve(), ip_keys)
    if decision_errors:
        for err in decision_errors:
            print(f"ERROR: {err}", file=sys.stderr)
        raise SystemExit(1)

    agent_decisions = _load_json(decisions_path)
    comments_by_key = _load_json(comments_by_key_path)

    errors = run_finalize_gate(
        payload,
        agent_decisions,
        comments_by_key=comments_by_key,
        comments_path=comments_by_key_path,
        decisions_path=decisions_path.resolve(),
        cwd=cwd,
        require_webhook=True,
    )
    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        raise SystemExit(1)

    payload_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build and post standup cards to Google Chat.")
    parser.add_argument("--payload", required=True, help="JSON from standup skill Jira step")
    parser.add_argument(
        "--comments-by-key",
        type=Path,
        default=None,
        help="Required for post: map issue key -> getJiraIssue JSON from this run",
    )
    parser.add_argument(
        "--decisions",
        type=Path,
        default=Path(DECISIONS_FILE),
        help=f"Required for post: agent decisions (default ./{DECISIONS_FILE})",
    )
    parser.add_argument("--dry-run", action="store_true", help="Build cards only; do not POST")
    parser.add_argument(
        "--preview-chart",
        action="store_true",
        help="Print QuickChart URL for Sprint Progress bar chart; do not POST",
    )
    parser.add_argument("--no-mention-all", action="store_true", help="Skip <users/all> in intro")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Directory where jira_*.json and payload live (default: current directory)",
    )
    args = parser.parse_args(argv)

    posting = not (args.dry_run or args.preview_chart)
    cwd = (args.workspace or Path.cwd()).resolve()
    payload_path = Path(args.payload)
    if not payload_path.is_absolute():
        payload_path = cwd / payload_path
    if posting:
        _prepare_payload_for_post(
            payload_path,
            comments_by_key_path=args.comments_by_key,
            decisions_path=args.decisions,
            cwd=cwd,
        )

    load_payload(
        str(payload_path),
        require_webhook=posting,
        require_post_ready=posting,
    )

    if args.preview_chart:
        print("Sprint Progress chart (open in browser to preview):")
        print(_team_bar_chart_url())
        counts, points = _sprint_health_metrics()
        ticket_chart = _doughnut_chart_url(counts, include_all_slices=True)
        if ticket_chart:
            print("\nTicket status doughnut:")
            print(ticket_chart)
        if sum(points.values()) > 0:
            sp_chart = _doughnut_chart_url(points)
            if sp_chart:
                print("\nStory points doughnut:")
                print(sp_chart)
        raise SystemExit(0)

    cards = generate_standup_card()

    if args.dry_run:
        print(json.dumps(cards, indent=2)[:8000])
        print(f"... ({len(cards)} cards, dry-run — not posted)")
    else:
        try:
            status, labels = post_message(cards, mention_all=not args.no_mention_all)
            print(f"HTTP Status : {status}")
            for lbl in labels:
                print(f"  posted: {lbl}")
            if status == 200:
                payload_path = Path(args.payload)
                if not payload_path.is_absolute():
                    payload_path = cwd / payload_path
                if payload_path.is_file():
                    payload_doc = _load_json(payload_path)
                    if payload_doc.get("pipeline"):
                        mark_posted(payload_doc["pipeline"])
                        payload_path.write_text(
                            json.dumps(payload_doc, indent=2),
                            encoding="utf-8",
                        )
        except RuntimeError as exc:
            print("HTTP Status : failed")
            print(exc)
            raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
