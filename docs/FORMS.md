# Forms

Businesses build intake, consent and questionnaire forms (M6.1), give them to customers, and
read the answers (M6.2).

The app is `customer_forms` (not `forms`, so it can't be confused with `django.forms`); the
pages are under `/app/forms/`.

## Building a form

- **Who.** Everything needs `forms.manage` (owners and managers by default). The sidebar shows
  "Forms" only to them.
- **List** (`/app/forms/`): each form's kind, latest published version, the services it is
  given with, and whether it is published, a draft or inactive.
- **New form**: a name (unique per organization, ignoring case), a kind (intake, consent,
  questionnaire) and an optional introduction shown above the questions.
- **Builder** (`/app/forms/<id>/`): add, edit, move up or down and remove questions. Moves and
  removals update the page in place (htmx); without JavaScript they are ordinary posts.
- **Settings**: name, kind, introduction, the services that give the form to customers who book
  them (M6.2), active or inactive, and delete.

### Question types

| Type | Stored as | Notes |
| --- | --- | --- |
| Short answer | `text` | |
| Paragraph | `textarea` | |
| Number | `number` | |
| Date | `date` | |
| Yes or no | `yes_no` | |
| One choice | `select` | 2 to 50 different options |
| Several choices | `multi_select` | 2 to 50 different options |
| Signature (typed name) | `signature_placeholder` | A typed full name for now |

Each question has a label (up to 300 characters), optional help text (500) and "required". A
form has at most 100 questions. Options are trimmed, empty lines dropped, and duplicates
(ignoring case) refused; only choice questions have options.

## Versions

- A form has numbered versions (`FormVersion`). The builder always edits the **draft**;
  **Publish** freezes it, and only published versions are given to customers.
- Editing a published form starts a new draft copied from the latest published version
  (`customer_forms.services.draft_for`). Until it is published, customers keep getting the
  previous version; the builder says so. **Discard changes** throws the draft away.
- Each question has a `key` that stays the same in every version it is copied into, so a
  question can be followed across versions (and its answers compared, M6.2). Changing a
  published question through the API returns its copy in the draft: a new `id`, the same `key`.
- A database constraint keeps one draft per form, and every write locks the form row, so two
  editors can't create two drafts or clash on positions.
- Published versions never change. A draft identical to the published version (e.g. after a
  move that changed nothing) shows no "unpublished changes".

## Auditing

Creating, changing (including linked services), publishing (with the version and question
count), discarding a draft and deleting a form are audited (`form.*`). Single question edits
aren't: they are draft changes until published.

## API

All endpoints need `forms.manage` and are scoped to the request's organization.

- `GET/POST /api/v1/forms/`, `GET/PATCH/DELETE /api/v1/forms/{id}/`: forms, each with its
  `published_version` and `draft_version` (null when there are no unpublished changes), both
  with their questions.
- `POST /api/v1/forms/{id}/publish/`, `POST /api/v1/forms/{id}/discard-draft/`.
- `POST /api/v1/form-questions/` (`form`, `label`, `type`, `help_text`, `required`,
  `options`), `GET /api/v1/form-questions/?form=<id>&version=<n>`,
  `GET/PATCH/DELETE /api/v1/form-questions/{id}/`,
  `POST /api/v1/form-questions/{id}/move/` (`{"direction": "up" | "down"}`).
- Errors carry a `code`: `name_required`, `duplicate`, `invalid_service`, `invalid_type`,
  `label_required`, `invalid_options`, `too_many`, `no_questions`, `nothing_to_publish`,
  `nothing_to_discard`, `stale` (the question was removed from the draft).

## Giving forms to customers (M6.2)

A `FormAssignment` gives one customer the latest **published** version of an **active** form.
There is at most one waiting copy of a form per customer (a database constraint).

- **When they book.** Booking a service linked to forms gives them those forms, in the booking
  transaction (`customer_forms.assignments.assign_for_booking`, called by
  `bookings.services.create_booking`). An **intake** form is asked for once per customer: not
  again while one is waiting or after it was completed. **Consent** forms and
  **questionnaires** are asked for each appointment (not while one is still waiting). A
  rescheduled appointment asks for nothing new. When the booking sends its confirmation, the
  customer also gets a "please fill in" email.
- **By hand.** On the customer's record, Forms tab, "Send a form" (`customers.manage`), with or
  without the email. A waiting form can be cancelled there; its link then stops working.
- **Email link.** `/forms/<signed link>/` needs no account. The link is signed with the site's
  secret and dated (`django.core.signing`), works for `FORM_LINK_DAYS` days (default 30), and
  only while the form is waiting; nothing secret is stored. An expired link shows "This link has
  expired" (410); a forged one is a 404; so is one for a suspended business.
- **Portal.** Signed-in customers see a "Forms" tab at each business (with the number to do)
  and fill them in there. It lists only forms given to *their* customer records at that
  business.

### Answers

- Validated against the version's questions (required, option lists, numbers, dates), stored as
  `FormSubmission` (time, account if signed in, IP address) and one `FormAnswer` per question
  (`value` JSON: text, decimal as text, ISO date, true/false, a choice or a list of choices).
- A form is completed once; a second submission is refused (409 `not_waiting`).
- Completing adds a "Form completed" entry to the customer's timeline. Neither the timeline nor
  the audit log (`form.assigned`, `form.assignment_cancelled`, `form.completed`) ever holds
  the answers: only the form id and version.
- **Who reads them.** The Forms tab on the customer record lists their forms. The answers page
  needs `customers.view` (owners, managers, receptionists); a provider sees that a form
  exists, not what was answered.
- **Privacy.** Anonymizing (or deleting) a customer deletes their answers and cancels their
  waiting forms. Merging two records moves the duplicate's forms; where both waited for the
  same form, the duplicate's copy is cancelled.
- A form customers were given can't be deleted (409 `in_use`): make it inactive instead.

## Demo data

`seed_demo` gives Harmony Wellness Centre a published "New client intake" form (7 questions),
linked to the Initial Chiropractic Assessment.
