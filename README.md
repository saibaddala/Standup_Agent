# Standup accuracy — team guidelines

**For developers.** Not for the Cursor standup agent — agent runbook: [SKILL.md](SKILL.md) + [reference.md](reference.md).

Standup is built from **Jira** (open sprint tickets + comments). What you put in Jira is what the team sees in Google Chat.

---

## 1. Jira comments = your standup update

For every **In Progress** ticket, the bot reads comments from the **last 24 hours**. Multiple comments in that window are summarized into one line.

| Jira | Standup |
|------|---------|
| Comment(s) today with clear updates | Fresh update line |
| No comment since yesterday | “No update since last standup” + latest comment date/text |
| No comments ever | “No comments on this ticket at all” |

**Do:** Comment when you move to In Progress or when something meaningful changes.

**Don't:** Rely on DMs, PR-only updates, or verbal standup — if it's not on the ticket, standup won't show it.

---

## 2. Write summarizable comments

**Good:** “Deployed AuthN fix to INT; testing order-create with Sam Altman.” / “Blocked on Toss for SDK v2 — ETA June 15.”

**Weak:** “Working on it” / “Update” / link with no context.

**Format:** Done since last update → current focus → blocker (if any).

---

## 3. Blockers on the ticket

Blockers in **ACTIVE DEPENDENCIES** come from labels **`blocker`** / **`impediment`** and/or your **latest comment**.

Only the **latest comment** counts for dependency detection.

**Do:** Label real blockers; name who/what you're waiting on. When unblocked, add a new comment and remove the label.

**Don't:** Leave stale blocker narrative when a newer comment already moved things forward.

---

## 4. Status and assignee

Buckets follow **status** and **assignee**. Move tickets IP → CR → Done when state changes. Reassign if you're no longer driving it.

---

## 5. Titles and descriptions

**Summary** appears **verbatim** in standup. Empty descriptions may surface under hygiene signals.

---

## 6. Sprint hygiene

Active work belongs in the **current open sprint**. **Done yesterday** comes from status transitions in the last 24h.

---

## 7. Pre-standup checklist

- [ ] Every **In Progress** ticket has a **comment in the last 24h**, or accept “no update”
- [ ] Blockers have **label + comment** with owner and next step
- [ ] Status matches reality
- [ ] Links in comments include **one line of context**
- [ ] Completed work is **Done**

---

## For admins

- Config: `config.yaml` — webhook, `jira.board_id`, team emails, Jira field IDs;
- Share [README.md](README.md) with the team; keep [SKILL.md](SKILL.md) for Cursor only
