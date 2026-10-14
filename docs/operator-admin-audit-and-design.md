# Operator admin: audit of today's Django admin, and a design for "see everything, safely"

Scope: the staff-only Django admin at `/admin/` (`ENABLE_ADMIN`, default on) in this repo. Sections 1 to 2 describe the admin as audited, before any changes.
Assumption: Hawkeye is operated for several customer organizations. If every customer runs their own copy, most of the "content access" section collapses into the customer's own org-owner view.

## 0. Status

**Operating model decided: a single superuser, no staff.** That removes the need for staff roles, a staff audit log and support-access grants for now. Sections 3.2 to 3.5 are deferred, not dropped: revisit them before anyone other than the superuser gets admin access.

**Phase 0 is implemented** (code, with tests):
- Finding 1: the admin shows only the last four characters of an API token, and `APIToken.__str__` no longer contains the token. The plaintext column and the API behaviour are unchanged; hashing is still phase 3.
- Finding 5: `UserResource` no longer exports or imports `is_superuser` or `is_staff`.
- Finding 6: the `Issue` admin is view-only and shows id, project, type, short id, first seen and public flag. A related leak is also closed: Django prints `str(issue)`, which is the title, in the page title and breadcrumb, so the change page overrides that. `Issue.__str__` itself is unchanged because alert webhooks use it.

**Phase 0 is configuration, not code**, and is tracked in [deployment-checklist.md](deployment-checklist.md): `ALLOWED_HOSTS`, MFA on the superuser account, a proxy allowlist for `/admin/` or `ENABLE_ADMIN=false`.

**Operator console v1 is built** (section 3.7): a read-only UI at `/operator` in `hawkeye-ui`, backed by superuser-only endpoints in `apps/operator`.

**Still open:** findings 2, 3 and 7 are not material with one superuser. Finding 4 is covered by the checklist. Finding 5's export and import remain available to the superuser by design.

## 1. What an operator can see today

| Area | Registered in admin | What staff see |
|---|---|---|
| Orgs, members, owners | `Organization`, `OrganizationUser`, owner inline | Names, members, emails, roles; event/transaction/log counts and storage per org |
| Users | `User` | Email, name, flags (`is_staff`, `is_superuser`), groups, permissions, last login |
| Projects, teams, alerts, monitors, releases | yes | Config and metadata |
| API tokens | `APIToken` | **Full token value in the list** (see finding 1) |
| Issues | `Issue` | List shows id and first-seen; **change form exposes `title`, `culprit`, `metadata`** (finding 6) |
| Events, transactions, logs | **not registered** | Not browsable in admin |
| Files | `File`, `FileBlob`, `DebugSymbolBundle`, `DebugInformationFile` | Names, checksums, sizes, release; no download |
| Billing | `Stripe*` (only when `BILLING_ENABLED`), `SupportLicense` | Read-only; licence key restricted to superusers (a good pattern to copy) |

What is already good:
- Event content is not in the admin.
- `is_superuser` does **not** grant cross-org access in the product API. The only superuser check in `apps/` outside admin and models is the organization-creation gate (`apps/organizations_ext/api.py:109`). There is no impersonation.
- Password hashes are excluded from user export.
- Admin login is routed through allauth (`secure_admin_login`), so its rate limits and MFA apply when a user has MFA.

## 2. Findings (most serious first)

1. **API tokens are stored in plaintext and shown to staff.** `apps/api_tokens/admin.py` has `list_display = ("token", "user", "label")`. `APIToken.__str__` returns the token, so it also lands in admin dropdowns and in `django_admin_log.object_repr` on any change or delete. Tokens carry scopes up to `org:admin` and `event:read`, so a staff member who can view them can read a customer's events through the normal API. This defeats any rule that customer content is access-by-exception.
2. **Staff can silently grant themselves content access.** `OrganizationAdmin` has editable `OrganizationUser` and owner inlines (and `OrganizationUserAdmin`, `TeamAdmin.members`). A staff user with change permission can add themselves as owner of any org and read everything through the normal UI. The only trace is `django_admin_log`, which records writes only, lives in the same database, and is editable by superusers.
3. **No read audit.** Django's admin log records add, change and delete. Nothing records who opened which org, user or token page, or who exported data.
4. **Nothing enforces staff MFA, and `/admin/` is on the public app host.** MFA (TOTP, WebAuthn, recovery codes) is supported but I found no requirement for staff. `ENABLE_ADMIN` defaults to true, there is no network allowlist, and `ALLOWED_HOSTS` defaults to `*` (it only logs a warning, `settings.py:123`).
5. **Bulk export and import on user and org data.** `ImportExportModelAdmin` on `User`, `Organization`, `OrganizationUser`, `Project` and `Team` lets any staff with the permission export emails and memberships in bulk. `UserResource` also includes `is_superuser` and `is_staff`, so an import is a privilege-escalation path.
6. **`Issue` change form exposes title, culprit and metadata JSON.** Issue titles and metadata often embed values from customer data, such as keys, ids and emails. The page title and breadcrumb also print `str(issue)`, which is the title. (Confirmed when the fix was tested.)
7. **All-or-nothing roles.** `createsuperuser` sets both flags, and nothing defines support or ops groups with narrower model permissions.

Not audited: database and backup access, worker logs, the in-product self-error reporting, and the HTTP API's own authorization (beyond the superuser check above).

## 3. Design

### 3.1 Principles
- Two tiers. **Metadata** (who, what plan, how much, is ingest healthy) is open to staff. **Content** (events, logs, stack traces, request data) is closed by default and opened per organization, per ticket, for a limited time.
- Staff never get content access by editing membership.
- Every staff action is attributable, and customers can see when staff touched their org.
- Fewer people hold more power: a small superuser group, everyone else on narrow groups.

### 3.2 Roles (Django groups, not `is_superuser`)
| Role | Can do | Cannot do |
|---|---|---|
| Support | Read org, user, project, usage, monitor state; open a support grant | Edit users or membership, export, see tokens |
| Ops | Support, plus suspend or unsuspend an org, set quota, toggle `is_accepting_events` | Content access without a grant |
| Owner (2 people max) | Everything, including role changes and exports | n/a, but every action is audited |

### 3.3 Staff access hardening
- Require MFA for any `is_staff` user (middleware on `/admin/` using allauth MFA state; redirect to MFA setup).
- Network allowlist for `/admin/` at the proxy, or an `ADMIN_ALLOWED_CIDRS` middleware; optionally a separate admin hostname.
- Shorter staff sessions and re-auth before sensitive actions (grants, exports, role changes).

### 3.4 Audit log
New app, e.g. `apps/staff_audit`, with one append-only table:
`StaffAuditEvent(id, created, actor, action, model, object_id, organization, reason, ip, user_agent, diff)`
- `action` is one of `view`, `change`, `export`, `import`, `grant_open`, `grant_close`, `login`.
- An `AuditedAdminMixin` logs detail-page views (not every list refresh), changes with a field diff, and every export or import.
- Append-only in practice: the app DB role gets INSERT and SELECT only on this table, and a copy is shipped to an external sink (object storage with retention lock, or syslog).
- Customer-facing: an org-level "Hawkeye staff activity" list built from rows where `organization` matches.

### 3.5 Support access grants (the break-glass path)
`SupportAccessGrant(organization, requested_by, approved_by, reason, ticket_ref, scope, starts_at, expires_at, revoked_at)`
1. **Request:** staff pick an org and give a reason and ticket reference. **Approval:** a customer owner approves from an email link or an org-settings toggle ("Allow Hawkeye support to view this organization for 24 h"). Without approval there is no content access. An emergency override exists only for Owners and is flagged in the audit log and in the customer notification.
2. **Use:** while a grant is active, a staff user is treated as a synthetic read-only "support" member of that one org. They are **not** added to membership. Writes are refused.
3. **Visibility:** the customer UI shows a banner ("Hawkeye support has read access until 14:32"), and the customer gets an email at start and end. The staff UI shows who they are acting for and when it expires.
4. **Limits:** maximum duration (suggest 24 h), single org per grant, auto-expiry, instant revoke by the customer.
5. **Closes finding 2:** make the membership and owner inlines read-only for everyone except Owners, and notify org owners on any staff membership change.

### 3.6 Secrets and sensitive fields
- Stop displaying `APIToken.token`; show label, user, scopes, created, and the last four characters. Change `__str__` to something non-secret. Immediate fix.
- Longer term, hash tokens at rest: add `token_hash` and `token_prefix`, backfill, accept both for a transition period, then drop the plaintext column. Show the token once at creation.
- Make the `Issue` admin read-only and limit it to id, organization, project and first-seen. Hide title, culprit and metadata.
- Restrict export and import to Owners; remove `is_superuser` and `is_staff` from `UserResource`.
- Keep the existing pattern of restricting credential-like fields to superusers (`SupportLicense`).

### 3.7 Where it lives
- Short term: keep Django admin as the operator tool. Optionally apply a minimal Hawkeye theme and set `GLITCHTIP_INSTANCE_NAME`.
- **Decision: the console lives inside `hawkeye-ui` at `/operator`.** The original recommendation was a separate app on its own hostname, so it could be firewalled. The trade-off accepted instead: it is on the public host. What keeps that acceptable:
  - The security gate is the backend, not the UI. `/api/0/operator/*` answers only an active superuser's browser session. API tokens never qualify, so a leaked token cannot read it. Hiding the UI is cosmetic.
  - The whole console is one lazy chunk (about 13 kB), so ordinary users never download it.
  - `operator` is a reserved organization slug, so no customer can take the URL.
  - It shows metadata only, never customer content. It now has two write actions (below), so if it gains more, revisit the separate-host option first.
- Built: Overview (organization, user and project counts, ingest volume for 24 h and 30 days, organizations not accepting events), Organizations (search, status, members, projects, 30-day events), organization detail (current-period usage, members with roles, projects), Users (search, status, last sign-in). Backend: `apps/operator`.
- Actions, each behind a confirmation dialog and logged with the acting superuser (a `WARNING` line from `apps.operator.api`, not a durable audit table):
  - **Pause or resume an organization's ingest** (`is_accepting_events`). Pausing takes effect on the next uncached request. Resuming can lag up to 30 seconds while the block cache expires.
  - **Deactivate or reactivate a user.** You cannot deactivate yourself or another superuser (remove the superuser flag in Django admin first). Deactivating also ends the user's API sessions, see below.
- **Side change to shared auth:** the API's `SessionAuth` read the user id from the session without loading the user, so deactivating a user did not end their signed-in API sessions (found by test: 200 instead of 401). Sessions live in the cache and cannot be listed per user, so deactivation now sets a cache marker that `SessionAuth` checks. It costs one cache read per session-authenticated request. Deactivating a user in Django admin does not set the marker, so use the console for that.
- Not built: queue and worker health, a durable audit log. Django admin remains the tool for other edits.

## 4. Suggested order (rough estimates)
| Phase | Work | Rough size |
|---|---|---|
| 0 | **Done (code):** hide token in admin and fix `__str__`; make `Issue` admin read-only; drop privilege fields from `UserResource`. **Deploy checklist:** `ALLOWED_HOSTS`, allowlist and MFA | 1 to 2 days |
| 1 | Audit log with detail-view logging and exports; lock membership inlines; staff roles via groups | about 1 week |
| 2 | Support access grants with customer approval, banner and emails; customer-visible staff activity | 1 to 2 weeks |
| 3 | Token hashing migration; read-only operator console | 1 to 2 weeks |

## 5. Open questions
- Do you operate Hawkeye as hosted multi-tenant for customers, or do customers run it themselves?
- Compliance target (GDPR processor terms, SOC 2, customer security reviews)? It sets how strict retention and approval must be.
- ~~How many staff?~~ Answered: none, only the superuser. Reopen if that changes; phases 1 and 2 then apply.
- Does support need a self-serve customer toggle or ticket-based approval only, once there is staff?
- Should an emergency override exist at all, and who can use it?
