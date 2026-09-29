# Staff

A staff member (provider) is a team member customers can book with. `staff/services.py` holds
the writes and `staff/selectors.py` the reads; the API, the web app, `seed_demo` and future AI
agents all go through them.

## Who offers what, where

| Model | Meaning |
|---|---|
| `StaffProfile` | A team member who sees customers. `display_name` is the name customers see (empty: their full name; never their email). `provider_type` is a free-form kind, e.g. "Massage therapist" (services can require one from M3.3). `online_booking_visible` off means only the team can book them. `max_daily_appointments` is stored now and enforced by the booking engine from M4.1. |
| `StaffProfile.locations` | The locations they work at. |
| `StaffServiceOffering` | They offer a service at one location, or at **all their locations** when `location` is empty, optionally with their own duration and price. |

`list_providers_for(service, location=None, public=False)` answers "who can be booked for this
service here": active, accepting bookings, an active offering that applies at the location,
and working at that location. `public=True` also requires `online_booking_visible`.
`offering_for(staff, service, location)` gives the offering that applies (a location-specific
one wins over "all locations"), with the effective `duration_minutes` and `price`.

The booking engine does not check offerings yet: that arrives with the availability rewrite
and the booking service consolidation (M3.4, M4.1), which use these selectors.

## Rules

| Rule | Where it is enforced |
|---|---|
| A staff profile belongs to an **active, non-customer member** of the organization, one per person. | Service (`not_a_member` 400, `duplicate` 409) and a unique constraint. |
| **Active** staff count towards the plan's `maximum_staff`. Creating or reactivating beyond it is refused. | Service (`plan_limit`, 409), with the organization row locked. |
| Staff work only at **active locations of their own organization**. A new profile works at the default location unless told otherwise. | Service (`invalid_location`). |
| An offering's service must belong to the organization and not be archived; a location-specific offering needs a location the person works at. | Service (`invalid_service`, `location_not_assigned`). |
| One offering per person, service and location (and one "all locations" offering). | Service (`duplicate`, 409) and partial unique indexes. |
| Custom durations are at least one minute; custom prices aren't negative. | Service and check constraints. |
| Removing a location from someone also removes the offerings tied to that location. | `set_staff_locations`. |
| A location that offerings point to can't be deleted (deactivate it instead). | `delete_location` (`in_use`, 409). |
| Someone with appointments can't be deleted (deactivate them instead). | `delete_staff_profile` (`in_use`, 409). |
| The public booking page and slot API list only staff visible online, by their public name. | `bookings.selectors.bookable_staff`, `scheduling` slot view. |

`Service.assigned_staff_members` is the older way to say who offers a service. It is kept as a
mirror of the offerings (staff with an active offering) and is only written by
`staff/services.py`. Writing it through the services API still works: each listed person
offers the service at all their locations, and anyone left out stops offering it. It will be
removed in M11.3.

## Writes (`staff/services.py`)

| Function | Audit action |
|---|---|
| `create_staff_profile` | `staff.created` |
| `update_staff_profile` | `staff.updated` (phone number recorded as "changed" only) |
| `delete_staff_profile` | `staff.deleted` |
| `set_staff_locations` | `staff.locations_changed`, with the number of offerings removed |
| `add_offering`, `update_offering`, `remove_offering` | `staff.service_added/updated/removed` |
| `set_staff_offerings` | Replaces everything a person offers; writes and audits only real changes. |
| `set_service_providers` | The older per-service list, as offerings. |

## Screens

- `/app/staff/`: search, location and status filters, each person's locations, number of
  services and booking status. Managers see how many active staff their plan allows.
- `/app/staff/<id>/<tab>/`: tabs Profile, Services, Locations, Availability and Time off; each
  is its own URL and htmx swaps only the tab. Managers edit the profile, locations and services
  in dialogs. The services editor has a box per service and location (ticking every location
  stores "all their locations") and optional duration and price. Availability and time off
  are read-only here until the availability rewrite (M3.4).

Viewing needs `staff.view` (every team role); changes need `staff.manage` (owners and
managers).

## API

See [API.md](API.md#services-staff-scheduling).
