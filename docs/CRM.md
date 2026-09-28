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

## Reads (`crm/selectors.py`)

- `get_customer_for_org(organization, id)`
- `list_tags(organization)`: tags with their customer counts.
- `list_customers(organization, search=, status=, tag=, include_anonymized=)`: every search word
  must match a first name, last name, preferred name, email or phone.
- `customer_stats(customer)`: computed from appointments on every call, not stored as
  counters. It returns total appointments, completed, cancelled, no-shows, upcoming, last
  visit, next appointment and lifetime value (the total of completed appointment prices).
  An appointment that was rescheduled counts once and is not counted as a cancellation.

## API (`/api/v1/customers/`)

Needs `customers.view` to read and `customers.manage` to write. Retrieving a single customer
includes `stats`. You can filter with `?status=` and search with `?search=`. Clients may send a
full `name` instead of `first_name`/`last_name`; it is split with the same rule. Merge and
anonymize endpoints, and the full CRM search API, arrive in M2.4.
