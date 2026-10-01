# Pilot plan: 2–3 weeks, one team

Real people phrase problems in ways 43 test cases can't. A pilot finds those gaps while the stakes are low.

## Set-up (day 0)

- **Team:** 30–80 people from one department that raises varied IT issues (not IT itself). Tell them it's a
  pilot, that a person is always one click away ("Talk to a human"), and how to give feedback (👍/👎).
- **Accounts:** import only the pilot team (User accounts → Import from HR). Everyone else keeps using the
  current process.
- **Content:** approve the employee-friendly articles first (see GO-LIVE.md §3).
- **Support:** the normal service desk handles escalations; one named lead reviews the numbers weekly.
- **Baseline:** note last month's ticket count, average first response time and satisfaction for the same
  team, so there's something to compare with.

## Weekly review (30 minutes, service desk lead)

| Look at | Where | Act on |
|---|---|---|
| Answers rated helpful, satisfaction | Charts (KPI tiles) | Falling or below target → read the 👎 answers first |
| Knowledge gaps | Knowledge → Knowledge gaps | Write or extend an article for the top 3 |
| Helpful % per article | Knowledge → Articles | Rewrite the lowest (draft → check → approve) |
| Corrections log | Corrections log | Repeated category mistakes → add evaluation cases, re-run `evaluate.py` |
| Critical recall, unauthorised actions, secret leakage | Charts → C12 safety scorecard | Anything off target: stop and investigate before continuing |
| Escalation reasons, SLA met | Charts C4, C5 | Unexpected spikes: check routing and queue staffing |
| Low ratings (1–2) | Notifications to supervisors | Call the person back |

Add each week's real phrasings that went wrong to `evaluate.py` as new cases, and keep the scorecard in
`eval_runs/` so changes can be compared.

## Exit criteria (all must hold for the last full week)

| Measure | Target |
|---|---|
| Critical safety recall (C12) | 100%, no exceptions |
| Unauthorised actions / secret leakage (C12) | 0 / 0 |
| Category accuracy (C9) | ≥ 90% |
| Answers rated helpful | ≥ 70% |
| Satisfaction | ≥ 4.0 / 5 |
| SLA met (C5) | At or above the team's baseline |
| Watchdog / backups | No unexplained downtime; one restore test done |

If a target is missed, extend the pilot by a week with a specific fix, rather than widening it.

## Rollout after the pilot

Add departments in waves of a few hundred people a week, watching the same numbers. Keep the old channel
available for the first month.
