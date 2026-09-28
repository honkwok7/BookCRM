# CRM: customers

The CRM customer is `bookings.Customer` (extended in place; see the implementation plan,
decision 3). The `crm` app holds the logic: `crm/services.py` for writes, `crm/selectors.py`
for reads. The API, the web app, the booking engine and future AI agents all go through them.

## Rules

| Rule | Where it is enforced |
|---|---|
| A customer needs an **email or a phone number**. Phone-only customers are allowed. | Service (`contact_required`) and a database check constraint. Anonymized customers are exempt. |
| Emails are **unique per organization, ignoring case**. Blank emails don't collide. The same person can be a customer of two organizations; the records are separate and invisible to each other. | Service (`duplicate_email`, 409) and a partial unique index on `(organization, lower(email))`. |
| `name` is always "First Last". Callers that only have a full name get it split: the last word is the last name. | `Customer.save()`, migration `bookings/0003`. |
| `display_name` is the preferred name if one is set. | `Customer.display_name` |
| Assigned and preferred staff must belong to the customer's organization. | Service (`invalid_staff`) and the API's tenant-scoped relation field. |
| Status is `active`, `inactive`, `archived` or `anonymized`. Only anonymization sets `anonymized`. | Service and API. |

## Writes (`crm/services.py`)

| Function | What it does |
|---|---|
| `create_customer` | Validates, sets `created_by`, stamps `consent_updated_at` if any consent is given, and audits `customer.created`. |
| `update_customer` | Locks the row and validates. Audits `customer.updated`, with personal fields recorded as "changed" without values. If a consent flag changed, it stamps `consent_updated_at` and audits `customer.consent_changed`, recording which flags and their new values. Anonymized customers can't be changed. |
| `find_or_create_customer` | Used by the booking engine. It matches by email, then by phone, or else creates a new customer (source `public_booking` or `reception`). A match **never** links the booking user's account to the existing record, because knowing someone's email must not give you their history. |
| `merge_customers` | Moves the duplicate's appointments to the target, fills the target's blank fields, appends notes, combines tags, then deletes the duplicate. **Consent is never copied.** It refuses merges across organizations, merges into itself, and records linked to two different user accounts. Audited as `customer.merged`. |
| `anonymize_customer` | Irreversible. See below. Audited as `customer.anonymized`. |

## Anonymization

Anonymization replaces a customer's personal data but keeps their appointments, so reports
and revenue totals stay correct. It clears:

- **The customer record:** names become "Anonymized"; contact details, address, birthday, gender, pronouns, notes, alerts and tags are cleared; the linked account, staff links, preferences and all consents are removed. `anonymized_at` is set.
- **Their appointments:** the name, email and phone copies, customer and internal notes, and cancellation reasons. Status, time, service, staff and price are kept.
- **Status-history notes and cancellation reasons** in the booking activity log.
- **Notification logs** for their appointments: the recipient email and account link.
- **Waitlist entries** under their old email address.

Audit history already stores personal fields without values, so it needs no rewrite. Anonymized
customers are hidden from `list_customers` by default.

Not yet covered: free text that staff typed about the customer somewhere else (for example
another customer's notes). CRM notes (M2.3) will be included when they exist.

## Tags

Each organization defines its own tags (`crm.Tag`: name, slug, color). Customers carry them
through `crm.CustomerTag`, which records who tagged them.

| Rule | Where |
|---|---|
| Slugs are unique per organization, so "VIP" and "vip" are the same tag. Renaming a tag changes its slug. | `create_tag` / `update_tag` (`duplicate_tag`, 409) and a database constraint |
| A tag from another organization is refused exactly like a tag that doesn't exist. | Service (`not_found`), the API's tenant-scoped `tag_ids`, and the `?tag=` filter |
| Tagging and untagging are audited as `customer.tag_added` / `customer.tag_removed`, recording the **tag id only**. A label like "diabetic" is personal data once it is attached to a person. | `add_customer_tag` / `remove_customer_tag` |
| Anonymized customers can't be tagged, and anonymization removes their tags. Merging combines both customers' tags. | Service |
| Deleting a tag removes it from every customer, and the audit entry records how many customers had it. | `delete_tag` |

The old free-text `Customer.tags` JSON was copied into tags by migration `crm/0002` (one tag
per distinct slug per organization). The JSON is now read-only and is removed in M11.3.

API:
- `/api/v1/tags/` returns each tag with `customer_count` (anonymized customers not counted).
  Reading needs `customers.view`; creating, renaming and deleting need `customers.manage`.
- On `/api/v1/customers/`, `tags` lists the customer's tags. Sending `tag_ids` replaces them,
  and `?tag=<id>` filters the list.

## Notes

`crm.CustomerNote` stores the author, a type (general, call, follow-up, alert), a
**visibility**, the text, a pinned flag and `edited_at`.

| Visibility | Who can read it | Who can write it |
|---|---|---|
| `internal` (the default) | Team members with `customers.notes.private` (owner and manager by default; it can be granted to others), and the note's author | The same, plus providers (their own notes) |
| `customer_visible` | Anyone who can see the customer, and the customer themselves (portal, M5) | Anyone with `customers.manage`, plus providers on their own customers |

- **Enforcement:** visibility is enforced in the selectors (`notes_visible_to`,
  `notes_for_customer`). A note the caller may not read answers 404, as if it didn't exist.
- **Providers** (staff role) read and write notes only on their own customers (see "Who
  sees which customers"). They read customer-visible notes and the notes they wrote
  themselves.
- **Editing and deleting:** allowed for the author, or anyone holding
  `customers.notes.private`. Changing the text sets `edited_at`. The audit log records that
  the content changed, never the text itself.
- **Anonymized and merged customers:** anonymization deletes a customer's notes, and merging
  moves the duplicate's notes to the kept record.

API: `/api/v1/customer-notes/`, filterable with `?customer=`, `?visibility=`,
`?note_type=` and `?pinned=`.

## Activity timeline

`crm.CustomerActivity` is the customer's history, written only by
`crm.activity.record_activity()` inside the same transaction as the change it describes.

| Written by | Kinds |
|---|---|
| Booking service | `appointment_booked`, `appointment_rescheduled`, `appointment_cancelled`, `appointment_completed`, `appointment_no_show` |
| CRM service | `customer_created`, `profile_updated` (changed field names only), `consent_changed`, `tag_added`, `tag_removed`, `note_created`, `customer_merged` |
| Notification task | `email_sent` (after the email was delivered) |
| Later milestones | `sms_sent`, `form_completed`, `payment_recorded` |

- **No free text in entries:** entries hold ids and codes (booking reference, statuses, tag
  id, field names), never names, emails, reasons or note text. The timeline therefore needs
  no rewriting when a customer is anonymized.
- **Internal entries:** an entry about an internal note is marked `internal` and hidden from
  users without `customers.notes.private`.
- **History for older data:** migration `crm/0004` created entries from the bookings that
  existed before the timeline. A booking that was rescheduled appears once, as the reschedule.

API: `GET /api/v1/customers/{id}/timeline/` returns entries newest first, paginated.

## Reads (`crm/selectors.py`)

- `customers_visible_to(request)`, `notes_visible_to(request)`: the access rules below.
- `search_filter(customers, query)`: phone-aware search (see "Search").
- `get_customer_for_org(organization, id)`
- `list_tags(organization)`: tags with their customer counts.
- `team_notes(organization, include_internal=, customer=)`, `notes_for_customer(customer)`.
- `customer_timeline(customer, include_internal=)`.
- `list_customers(organization, search=, status=, tag=, include_anonymized=)`.
- `customer_stats(customer)`: computed from appointments on every call, not stored as
  counters. It returns total appointments, completed, cancelled, no-shows, upcoming, last
  visit, next appointment and lifetime value (the total of completed appointment prices).
  An appointment that was rescheduled counts once and is not counted as a cancellation.

## Who sees which customers

| Caller | Customers | Can change them |
|---|---|---|
| `customers.view` (owner, manager, receptionist) | All of the organization | With `customers.manage` |
| Provider (staff role) | Assigned to them, or with whom they have or had an appointment | No (read-only) |
| Anyone else | None | No |

Deleting and anonymizing need `customers.erase` (owner and manager). Both are irreversible.
Merging needs `customers.manage`.

## Search

- **Phone numbers** match in any format: "(416) 555-0101", "416-555-0101", "+1 416 555
  0101" and "5550101" all find "+14165550101". An 11-digit number starting with 1 also finds
  the same number stored without the country code. Each customer stores a digits-only copy
  of their phone numbers (`phone_search`) for this.
- **Words:** every word must match a first, last or preferred name, or the email.
- **Speed (PostgreSQL):** trigram indexes (migration `bookings/0007`, `pg_trgm`) keep this
  fast. `python manage.py benchmark_customer_search` measured, at 50,000 customers:

  | Query | p95 |
  |---|---|
  | Name words | 76–87 ms |
  | Email fragment | 3 ms |
  | Phone | 3 ms |

## API

`/api/v1/customers/`:

- **Filters:** `?search=`, `?status=`, `?tag=`, `?assigned_staff=`, `?preferred_staff=`,
  `?staff=` (assigned, preferred or ever booked with), `?last_visit_before=` /
  `?last_visit_after=` (dates), and `?never_visited=true`. `?ordering=` accepts
  `last_name`, `first_name`, `created_at` and `last_visit`.
- **Fields:** each row includes `last_visit`, the latest completed appointment. Retrieving a
  single customer adds `stats`.
- **Name shortcut:** clients may send a full `name` instead of `first_name`/`last_name`; it
  is split with the same rule.

| Endpoint | What it does |
|---|---|
| `GET /customers/search/?q=` | Quick lookup, at most 20 matches, excludes anonymized customers |
| `GET /customers/{id}/timeline/` | History, newest first (paginated) |
| `GET /customers/{id}/notes/` | Notes the caller may read, pinned first |
| `GET /customers/{id}/appointments/` | Appointments the caller may see, newest first |
| `POST /customers/{id}/merge/` `{"duplicate": id}` | Fold a duplicate into this customer |
| `POST /customers/{id}/anonymize/` `{"confirm": true}` | Irreversible; needs `customers.erase` |

`GET /api/v1/search/?q=&limit=5` (maximum 20 per section) searches the whole organization:
customers, appointments (by reference or customer name), staff and services. Each section
uses the same access rules as browsing. Staff and services appear only with `staff.view` /
`services.view`.
