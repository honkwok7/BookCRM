# Platform administration (M5.5)

`/saas/` is for the people who run BookCRM, not for organizations. Code: `saas/` (services,
metrics, views, forms, tasks); templates in `templates/saas/`.

## Who

- **Platform staff** (`User.is_platform_staff`) and **superusers** (`is_superuser`) open
  `/saas/`; they land there after signing in. Everyone else gets 403.
- Only a superuser grants or removes platform staff, deactivates another platform account, or
  sees the Django admin link. Nobody changes their own account here.
- Account APIs can't set either flag (tested in `accounts/test_privilege_escalation.py`).

## What they see

Platform facts and counts, never an organization's customers or appointments:

| Page | What it does |
|---|---|
| Overview | Organizations (total, active, suspended, new in 30 days), trialing and paying, trials ending in 7 days, MRR, accounts (total, signed in and joined in 30 days), appointments booked (30 and 7 days), failed notifications (24 h), the background worker's heartbeat, subscriptions by plan, and alerts. Cached for a minute. |
| Organizations | Search and filter (active, suspended, trialing, past due); create one (a trial or active subscription and an owner invitation); per organization: owners, members by role, counts of staff, customers and appointments, open invitations, subscription, suspend or reactivate with a reason, and its platform actions. |
| Subscriptions | Every subscription with its monthly value; filter by status. Change plan, status, billing cycle, trial end and period end from the organization's page. |
| Plans | Prices and limits; create, edit, retire. |
| Users | Search accounts; per account: memberships, deactivate or reactivate with a reason, platform access. |
| Audit log | Every audited action, filterable by action, organization and platform actions only. Details are shown for platform actions only, because an organization's own entries can describe its customers. |

**MRR** is from plan prices of active subscriptions (a yearly plan counts as a twelfth of its
yearly price). There is no payment provider yet, so it is what the plans say, not what was
collected.

**Heartbeat:** celery beat runs `saas.tasks.heartbeat` every minute (`CELERY_BEAT_SCHEDULE`);
the overview warns when there is none, or when the last one is more than 5 minutes old.

## Audit

Every change is audited with `actor_type = platform_admin`: `organization.created`,
`.suspended`, `.reactivated` (with the reason), `subscription.changed` (with a from/to diff),
`plan.created`, `plan.updated`, `user.deactivated`, `user.reactivated`,
`platform_staff.granted`, `platform_staff.revoked`.

Suspending an organization blocks its members at once (M1.1): the app refuses them and its
booking page closes, until it is reactivated.
