"""Agent ticket form, ServiceNow-style (spec §7): fields, bot panel, suggested KB, checklist,
comments / work notes, activity timeline. Saves use optimistic locking (`version`)."""
import html as html_lib
import re

import streamlit as st

from ui import client
from ui.client import ApiError
from ui.common import PRIO_COLOR, STATE_LABEL, error, local, sla_pill

ss = st.session_state
user = ss.user
meta = client.meta()
cats = {c["id"]: c["name"] for c in meta["categories"]}

tid = ss.get("open_ticket")
pick_col, _ = st.columns([2, 5])
typed = pick_col.text_input("Open ticket", value=tid or "", placeholder="TKT-3001", label_visibility="collapsed")
if typed and typed.strip().upper() != (tid or ""):
    ss.open_ticket = tid = typed.strip().upper()
if not tid:
    st.info("Pick a ticket from **My queue**, or type a ticket number above.")
    st.stop()

try:
    d = client.get(f"/tickets/{tid}")
except ApiError as e:
    st.error("Ticket not found, or it isn't in your queues." if e.status == 404 else str(e.detail))
    st.stop()

t, sla, bot = d["ticket"], d["sla"], d["bot_panel"]
v = t["version"]
mine = t["owner"] == user["user_id"]
can_work = mine or user["role"] == "supervisor"   # checklist + resolve: the owner (or a supervisor)
can_edit = t["status"] not in ("CLOSED", "CANCELLED")  # fields: anyone who can see it; `version` stops lost updates
unacked = [a for a in d["alerts"] if not a["acked_at"]]
key = f"{tid}-{v}"

# ------------------------------------------------------------------ header strip
sub = f"{t['category']} → {t['queue']} &nbsp;|&nbsp; {t['ticket_type'] or 'Incident'}"
if t["is_incident"]:
    sub += " &nbsp;|&nbsp; incident parent"
if t["incident_parent"]:
    sub += f" &nbsp;|&nbsp; linked to {t['incident_parent']}"
# one line of HTML: indented lines would be read as a markdown code block
st.markdown(
    f'<div class="tk-head"><div class="tk-prio" style="background:{PRIO_COLOR.get(t["priority"], "#6B7B86")}">'
    f'{t["priority"]}</div><div class="tk-body"><span class="tk-id">{t["ticket_id"]}</span>'
    f'<span class="state">{STATE_LABEL.get(t["status"], t["status"])}</span><div class="tk-sub">{sub}</div></div>'
    f'<div class="tk-timers">{sla_pill("Response", sla.get("response_due"), sla.get("response_state"))}'
    f'{sla_pill("Resolve", sla.get("resolve_due"), sla.get("resolve_state"))}</div></div>',
    unsafe_allow_html=True)


def act(path: str, body: dict | None = None, ok: str | None = None) -> None:
    try:
        client.post(path, body)
    except ApiError as e:
        ss.ticket_error = e
    else:
        if ok:
            st.toast(ok)
    st.rerun()


if ss.get("ticket_error"):
    error(ss.pop("ticket_error"))

can_take = not (t["owner"] and not mine) and (t["status"] in ("ESCALATION_QUEUED", "HUMAN_ASSIGNED") or (
    t["status"] not in ("HUMAN_IN_PROGRESS", "RESOLVED_PENDING_CONFIRMATION", "RESOLVED", "CLOSED", "CANCELLED")
    and not t["owner"]))
lead = ["ack"] * bool(unacked) + (["take"] if can_take else ["owned"] if t["owner"] and not mine else [])
cols = st.columns([7 - len(lead)] + [1.2] * len(lead) + [1, 1, 0.6])  # actions sit right, like the header
note, slots = cols[0], dict(zip(lead + ["update", "resolve", "more"], cols[1:]))
if "ack" in slots and slots["ack"].button("Acknowledge", width="stretch"):
    for a in unacked:
        client.post(f"/alerts/{a['alert_id']}/ack")
    st.rerun()
if "take" in slots and slots["take"].button("Take over", type="primary", width="stretch"):
    act(f"/tickets/{tid}/takeover", ok=f"You own {tid}")
if "owned" in slots:
    slots["owned"].markdown(f"<span class='muted'>Owned by {t['owner_name']}</span>", unsafe_allow_html=True)
update_clicked = slots["update"].button("Update", width="stretch", disabled=not can_edit)
b = [None, None, None, slots["resolve"], slots["more"]]

checklist = d["checklist"]
req = [c for c in checklist if c["required"]]
req_done = sum(bool(c["done_at"]) for c in req)
with b[3].popover("Resolve", width="stretch", disabled=t["status"] != "HUMAN_IN_PROGRESS" or not can_work):
    code = st.selectbox("Resolution code", meta["agent_resolution_codes"], key=f"{key}-code")
    notes = st.text_area("Resolution notes", key=f"{key}-notes", placeholder="What fixed it, in a sentence or two")
    open_items = [c for c in req if not c["done_at"]]
    missing = [c["text"] for c in open_items] + ([] if notes.strip() else ["Resolution notes"])
    if open_items:  # tick them here; the same items are in the Checklist section below
        st.caption("Tick the required checks you've done:")
        for c in open_items:
            if st.checkbox(c["text"], key=f"{key}-rchk-{c['item_id']}"):
                act(f"/tickets/{tid}/checklist/{c['item_id']}", {"done": True})
    if missing:
        st.caption("Resolve unlocks when these are done: " +
                   ("all checks above" if open_items else "") +
                   (" and " if open_items and not notes.strip() else "") +
                   ("" if notes.strip() else "resolution notes") + ".")
    if st.button("Resolve", type="primary", disabled=bool(missing), key=f"{key}-resolve"):
        act(f"/tickets/{tid}/resolve", {"resolution_code": code, "notes": notes}, ok=f"{tid} resolved")
with b[4].popover("⋯", width="stretch"):
    st.markdown("**Reassign**")
    q = st.selectbox("Assignment group", meta["queues"], index=meta["queues"].index(t["queue"])
                     if t["queue"] in meta["queues"] else 0, key=f"{key}-rq")
    why = st.text_input("Reason", key=f"{key}-rwhy")
    if st.button("Reassign", disabled=not why.strip() or q == t["queue"], key=f"{key}-rbtn"):
        act(f"/tickets/{tid}/reassign", {"queue": q, "reason": why}, ok=f"Moved to {q}")
    st.divider()
    st.markdown("**Cancel or close as duplicate**")
    dup = st.text_input("Duplicate of (optional)", placeholder="TKT-3004", key=f"{key}-dup")
    cwhy = st.text_input("Reason", key=f"{key}-cwhy")
    if st.button("Close as duplicate" if dup.strip() else "Cancel ticket", disabled=not cwhy.strip(),
                 key=f"{key}-cbtn"):
        act(f"/tickets/{tid}/cancel", {"reason": cwhy, "duplicate_of": dup.strip().upper() or None})
    st.divider()
    if st.button("Watch this ticket", key=f"{key}-watch"):
        act(f"/tickets/{tid}/watch", ok="Added to watch list")
    st.caption("Tickets are never deleted.")

# ------------------------------------------------------------------ fields
imp_scale, urg_scale = meta["impact_scale"], meta["urgency_scale"]
left, right = st.columns(2, gap="large")
with left:
    st.text_input("Number", t["ticket_id"], disabled=True, key=f"{key}-num")
    st.text_input("Caller", f"{t['employee_id']} · {t['caller_name'] or ''}", disabled=True, key=f"{key}-caller")
    loc = st.text_input("Location", t["location"] or "", key=f"{key}-loc", disabled=not can_edit)
    cat_ids = list(cats)
    cat = st.selectbox("Category", cat_ids, index=cat_ids.index(t["category_id"]) if t["category_id"] in cats else 0,
                       format_func=cats.get, key=f"{key}-cat", disabled=not can_edit)
    subs = [""] + meta["issue_types"].get(cat, [])
    sub = st.selectbox("Subcategory", subs, index=subs.index(t["subcategory"]) if t["subcategory"] in subs else 0,
                       key=f"{key}-sub", disabled=not can_edit)
    ci = st.text_input("Configuration item", t["config_item"] or "", key=f"{key}-ci", disabled=not can_edit)
    imp = st.selectbox("Impact", list(imp_scale), index=list(imp_scale).index(t["impact"])
                       if t["impact"] in imp_scale else 3, format_func=imp_scale.get, key=f"{key}-imp",
                       disabled=not can_edit)
    urg = st.selectbox("Urgency", list(urg_scale), index=list(urg_scale).index(t["urgency"])
                       if t["urgency"] in urg_scale else 2, format_func=urg_scale.get, key=f"{key}-urg",
                       disabled=not can_edit)
    if user["role"] == "supervisor":
        prio = st.selectbox(f"Priority ({t['priority_source']}: {t['priority_reason']})", ["P1", "P2", "P3", "P4"],
                            index=["P1", "P2", "P3", "P4"].index(t["priority"]), key=f"{key}-prio")
    else:
        prio = t["priority"]
        st.text_input("Priority", f"{t['priority']} (computed: {t['priority_reason']})", disabled=True,
                      key=f"{key}-prio-ro")
with right:
    st.text_input("Opened", local(t["created_at"]), disabled=True, key=f"{key}-opened")
    st.text_input("Opened by", t["opened_by"] or "—", disabled=True, key=f"{key}-by")
    chans = meta["channels"]
    chan = st.selectbox("Contact type", chans, index=chans.index(t["channel"]) if t["channel"] in chans else 0,
                        key=f"{key}-chan", disabled=not can_edit)
    st.text_input("State", STATE_LABEL.get(t["status"], t["status"]), disabled=True, key=f"{key}-state")
    nxt = ", ".join(STATE_LABEL.get(x["state"], x["state"]) for x in d["legal_next"])
    st.caption(f"Next legal states: {nxt or 'none'} (use the buttons above)")
    st.text_input("Assignment group", t["queue"], disabled=True, key=f"{key}-q")
    st.text_input("Assigned to", t["owner_name"] or "—", disabled=True, key=f"{key}-owner")
    st.text_input("Type", t["ticket_type"] or "Incident", disabled=True, key=f"{key}-type")
    st.text_input("Watch list", ", ".join(d["watchers"]) or "—", disabled=True, key=f"{key}-watchers")
summary = st.text_input("Short description", t["summary"], key=f"{key}-summary", disabled=not can_edit)
reason = st.text_input("Reason for corrections (needed when you change category, impact, urgency or priority)",
                       key=f"{key}-reason", disabled=not can_edit)

if update_clicked:
    changes = {"location": loc, "category_id": cat, "subcategory": sub or None, "config_item": ci, "impact": imp,
               "urgency": urg, "channel": chan, "summary": summary}
    if user["role"] == "supervisor":
        changes["priority"] = prio
    act(f"/tickets/{tid}/update", {"version": v, "changes": changes, "reason": reason}, ok="Saved")

# ------------------------------------------------------------------ people on this ticket
with st.container(border=True):
    st.markdown("**👥 People on this ticket**")
    st.dataframe([{"Name": p["name"] + ("  (owner)" if p["current_owner"] else ""), "Employee ID": p["user_id"],
                   "Role": {"requester": "Requester", "agent": "Agent", "supervisor": "Supervisor"}[p["role"]],
                   "Job title / team": " · ".join(x for x in (p["job_title"], p["team"]) if x) or "—",
                   "What they did": ", ".join(p["did"]) or "Viewed",
                   "First activity": local(p["first_at"]) if p["first_at"] else "—",
                   "Last activity": local(p["last_at"]) if p["last_at"] else "—"} for p in d["people"]],
                 hide_index=True, width="stretch")

# ------------------------------------------------------------------ bot panel + suggested KB
with st.container(border=True):
    st.markdown("**🤖 Bot panel**")
    c1, c2 = st.columns([3, 2])
    with c1:
        for f, p in list(bot["flags"].items())[:6]:
            color = "#C0362C" if f in bot["raised"] else "#8FA3B6"
            st.markdown(f"<span class='muted'>{f.replace('_', ' ')} {p:.2f}</span><div class='flag-bar'>"
                        f"<div style='width:{p * 100:.0f}%;background:{color}'></div></div>", unsafe_allow_html=True)
        if not bot["flags"]:
            st.caption("No risk flags recorded.")
    with c2:
        conf = bot["category_confidence"]
        st.markdown(f"Category confidence **{conf:.0%}**" if conf is not None else "Category confidence —")
        if bot["top_categories"]:
            st.caption(" · ".join(f"{n} {p:.0%}" for n, p in bot["top_categories"]))
        if bot["user_category"] and bot["user_category"] != t["category"]:
            st.caption(f"Employee chose {bot['user_category']} on the form")
        st.markdown(f"Verification **{bot['verification'] or '—'}** · attempts **{bot['attempts'] or 0}** · "
                    f"KB **{bot['kb_id'] or '—'}**")
        if bot["escalation_reason"]:
            st.markdown(f"Reason: {bot['escalation_reason']}")
if d["suggested_kb"]:
    with st.expander("📚 Suggested KB  " + " · ".join(f"{k['kb_id']} {k['relevance']:.0%}"
                                                      for k in d["suggested_kb"])):
        for k in d["suggested_kb"]:
            st.markdown(f"**{k['kb_id']} {k['title']}** ({k['relevance']:.0%})  \n{k['steps']}")

# ------------------------------------------------------------------ checklist
with st.container(border=True):
    st.markdown(f"**☑ Checklist** ({req_done} of {len(req)} required done)")
    if not checklist:
        st.caption("The checklist appears when the ticket is escalated.")
    cols = st.columns(2)
    for i, c in enumerate(checklist):
        label = c["text"] + ("" if c["required"] else " (optional)")
        done = cols[i % 2].checkbox(label, value=bool(c["done_at"]), key=f"{key}-chk-{c['item_id']}",
                                    disabled=not can_work or t["status"] != "HUMAN_IN_PROGRESS")
        if done != bool(c["done_at"]):
            act(f"/tickets/{tid}/checklist/{c['item_id']}", {"done": done})
    if checklist and t["status"] != "HUMAN_IN_PROGRESS":
        st.caption("Take over the ticket to work the checklist.")

# ------------------------------------------------------------------ screenshots
if d.get("attachments"):
    with st.container(border=True):
        st.markdown(f"**🖼️ Screenshots** ({len(d['attachments'])})")
        for a in d["attachments"]:
            c1, c2 = st.columns(2)
            data = client.image(a["id"]) if a["available"] else None
            if data:
                c1.image(data)
            else:
                c1.caption("🖼️ Deleted after the retention period")
            c2.markdown("**What the bot read from this screenshot**")
            if a["error_codes"]:
                c2.markdown("Error codes: " + ", ".join(f"`{c}`" for c in a["error_codes"]))
            c2.code(a["ocr_text"] or "(no readable text)", language=None)
            note = f"Read with {a['ocr_confidence'] or 0:.0%} confidence"
            if a["secrets_blurred"]:
                note += f" · 🔒 {a['secrets_blurred']} line(s) blurred: they looked like a password or code"
            c2.caption(note + (" · the employee was asked to type the error" if not a["readable"] else ""))

# ------------------------------------------------------------------ comments / work notes
tab_c, tab_w = st.tabs(["Comments (customer visible)", "Work notes (internal)"])
with tab_c:
    replies = client.get("/saved-replies", ticket_id=tid)
    if replies:
        by_id = {r["id"]: r for r in replies}

        def _insert_reply() -> None:
            pick_ = st.session_state.get(f"{key}-saved")
            if pick_:
                st.session_state[f"{key}-customer-text"] = by_id[pick_]["filled"]

        st.selectbox("Start from a saved reply", list(by_id), index=None, key=f"{key}-saved",
                     format_func=lambda i: by_id[i]["title"], placeholder="Start from a saved reply (optional)",
                     on_change=_insert_reply, label_visibility="collapsed")
for tab, vis in ((tab_c, "customer"), (tab_w, "internal")):
    with tab, st.form(f"{key}-{vis}", clear_on_submit=True):
        text = st.text_area("Comment" if vis == "customer" else "Work note", label_visibility="collapsed",
                            key=f"{key}-{vis}-text",
                            placeholder="The employee sees this" if vis == "customer" else "Only IT staff see this")
        if st.form_submit_button("Post comment" if vis == "customer" else "Add work note") and text.strip():
            act(f"/tickets/{tid}/comment", {"text": text, "visibility": vis})

# ------------------------------------------------------------------ activity
st.markdown("#### Activity")
flt = st.segmented_control("Show", ["Everything", "Conversation", "Work notes", "Bot decisions", "System"],
                           default="Everything", key=f"{tid}-flt")
items = []
for m in d["messages"]:
    who = {"user": "🧑‍💻 Employee", "bot": "🤖 Bot", "agent": f"👤 {m['meta'].get('agent', 'Agent')}",
           "system": "ℹ️ System"}[m["role"]]
    kind = "Work notes" if m["visibility"] == "internal" else "Conversation"
    items.append((m["created_at"], m["id"] * 2, kind, who, m["text"], m["visibility"] == "internal"))
bot_steps = {"Intent router", "Ticket triage", "Safety gate", "Safety", "Retriever", "Understand", "Solve", "Tool",
             "Wrap up", "Escalation", "Confirm classification", "Fallback", "Category check", "Duplicate guard",
             "Incident", "Form", "Screenshot", "Language"}
for e in d["events"]:
    if e["event_type"] in ("Comment", "Work note"):
        continue  # already shown as messages
    kind = "Bot decisions" if e["event_type"] in bot_steps else "System"
    icon = "🤖 Bot" if kind == "Bot decisions" else ("🔔 Alert" if "Alert" in e["event_type"] else "🔁 Sys")
    if e.get("actor_name"):
        icon = f"👤 {e['actor_name']}"
    items.append((e["created_at"], e["id"] * 2 + 1, kind, icon,
                  f"**{e['event_type']}** {e['payload'].get('detail', '')}", False))
items.sort(key=lambda x: (x[0], x[1]))
html = []
for ts, _, kind, who, text, internal in items:
    if flt != "Everything" and kind != flt:
        continue
    # escape first: message text comes from employees and must never be rendered as HTML
    body = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html_lib.escape(text)).replace("\n", "<br>")
    html.append(f"<div class='tl-row'><span class='tl-time'>{local(ts)}</span><b>{who}</b> "
                f"<span class='{'tl-internal' if internal else ''}'>{body}</span></div>")
st.markdown("<div class='tl'>" + "".join(html) + "</div>", unsafe_allow_html=True)

with st.expander("Handoff package and form answers"):
    st.json(t["handoff"] or {"note": "not escalated by the bot"}, expanded=False)
    if t["form"]:
        st.json(t["form"], expanded=False)
    if d["linked"]:
        st.markdown("Linked tickets: " + ", ".join(d["linked"]))
