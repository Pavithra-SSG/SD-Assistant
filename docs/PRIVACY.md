# IT Service Desk: how personal data is handled

**Status: draft for review by your data protection officer or legal team. It describes what the software
does; it is not legal advice.** It is written with India's Digital Personal Data Protection Act, 2023 (DPDP
Act) in mind: collect what's needed for a stated purpose, keep it no longer than needed, protect it, and let
people see what's held about them. Replace the bracketed parts with your company's details.

## Purpose

The service desk uses what employees tell it to resolve their IT problems, to route work to the right
support team, to meet service commitments, and to improve the help it gives (anonymous counts only).

## What is collected

| Data | From | Why |
|---|---|---|
| Name, employee ID, work e-mail, department, location, device asset tag, job title | HR import / account creation | To identify the requester, route tickets, and find the right device |
| Chat messages and ticket form answers | The employee | To understand and fix the problem |
| Screenshots | The employee, optionally | To read the error; see "Screenshots" below |
| Ratings and feedback on answers | The employee, optionally | To improve answers and service |
| Sign-in records, and a record of which staff opened which ticket | The system | Security and accountability |

Passwords and one-time codes are **not** wanted: anything that looks like one is hidden before it is stored,
in chat, on the form, in feedback, and in screenshots (blurred in the image and removed from the text).

## Who can see it

| Who | Sees |
|---|---|
| The employee | Their own tickets, conversations, screenshots and ratings |
| IT agents | Tickets in their team's queues, and every P1 (critical) ticket, including internal work notes |
| IT supervisors | All tickets; reports; account administration |
| Reporting tools (optional read-only account) | Counts and dates only: no names, messages or screenshots |

Every time a staff member opens a ticket or a screenshot, it is recorded (who, when). The audit log records
what happened, not what people wrote.

## Where it is processed

- The application and its database run on [company server / region].
- **Screenshots are read on the same server** (on-server OCR); they are not sent to any outside service.
- To understand typed messages, message text is sent to the AI provider TypeSafe (model "Jev") over HTTPS.
  [Confirm TypeSafe's data processing terms, retention and location before go-live.]
- Optional notifications go through [your mail server / Microsoft 365] and [Microsoft Teams]. Teams alerts
  contain only the ticket number, category and team — never the employee's words.

## How long it is kept

| Data | Kept for | Then |
|---|---|---|
| Screenshots | 90 days (`ATTACHMENT_RETENTION_DAYS`) | Image file deleted; a record that a screenshot existed remains |
| Conversation text, ticket summaries, form answers, feedback comments | 24 months after the ticket closes (`RETENTION_MONTHS`) | Replaced with "[removed after the retention period]"; ticket number, dates, category, priority and SLA remain for reporting |
| Notifications and e-mail copies | 24 months | Text removed |
| Sign-in sessions | Until expiry + 1 day | Deleted (sign-in history remains in the audit log) |
| Accounts of people who leave | Disabled at once; [decide: kept for N months for the audit trail] | [Decide] |
| Backups | 14 days (`BACKUP_KEEP_DAYS`), plus off-server copies per your backup policy | Overwritten |

## Employees' rights

- **See your data:** My account → **Prepare my data** → download (a JSON file with your profile, tickets,
  conversations, ratings, feedback and screenshot records). Each download is recorded.
- **Correct it:** profile details come from HR; ask HR or the service desk.
- **Questions or complaints:** [data protection officer name and contact].

## Security

Encrypted connections (HTTPS); passwords stored only as salted hashes; accounts lock after 5 wrong
attempts; sessions end after 30 minutes idle; each part of the system has only the database access it needs;
the audit log cannot be edited or deleted by the application; daily backups.
