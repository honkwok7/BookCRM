# Public booking wizard

`/book/<slug>/` is where customers book online (M4.6). It replaced the old one-page form.

## Steps

| Step | URL | Skipped when |
|---|---|---|
| Location | `/book/<slug>/` | The organization has one bookable location (active, booking enabled) |
| Service | `/book/<slug>/service/` | Never. Lists public services offered at the location that at least one visible provider offers there, grouped by category |
| Provider | `/book/<slug>/provider/` | The service has one provider there. Otherwise "Anyone available" plus each provider's public name |
| Date & time | `/book/<slug>/time/` | Never. A week of days (days without free times are greyed out; earlier and later weeks up to the service's booking window) and the selected day's free times, grouped into morning, afternoon and evening |
| Your details | `/book/<slug>/details/` | Never. Name, email, phone, notes. Prefilled for signed-in users |
| Review | `/book/<slug>/review/` | Never. Summary, the cancellation policy, and Confirm |
| Confirmation | `/book/<slug>/confirmation/<public_uuid>/` | Shown after booking |

Times come from the availability engine with the online rules (minimum notice, booking
window), in the location's time zone, which the page states.

## How it works

- **State lives on the server**, in the session, keyed by organization (`bookings/wizard.py`),
  so two organizations' wizards in one browser don't mix. Only ids and the entered details
  are stored.
- **Every request re-checks every earlier answer** against current data. The location must
  still be bookable, the service still public there, and the provider still offering it.
  An answer that is no longer valid is dropped, and the visitor goes back to the first step
  that needs one. A step can't be skipped ahead of its answers, and ids from another
  organization, private services or hidden providers are refused (422).
- **Confirm calls `create_booking`** with `source=public_booking` and `public=True`, so the
  time is checked again under the provider's lock. With "Anyone available", the first
  provider still free at that time is booked. If the time was taken meanwhile, the visitor
  returns to the time step with an explanation, and nothing is booked.
- **A double click or retry books once.** Each wizard run has an idempotency key, and Confirm
  with a key that already booked shows that booking.
- **Progressive enhancement.** Every step is a plain page with plain forms (POST, then a
  redirect), so booking works without JavaScript. With JavaScript, htmx boosts the wizard
  (`hx-boost` on `#wizard`) and only that part swaps between steps. Form errors answer 422,
  which htmx swaps in.

## Protection

- Confirmations are rate limited per client IP per organization (`PUBLIC_BOOKING_RATE`,
  default 10 per hour) and per organization (`PUBLIC_BOOKING_ORG_RATE`, default 300 per
  hour). Over the limit, the page answers 429.
- A honeypot field (`website`) is hidden from people and assistive technology. Bots that fill
  it in are refused.
- Only public services and online-visible providers are shown, by public name only (never an
  email address).
- The confirmation page lives at the booking's unguessable `public_uuid`, not its reference.
  It shows what was booked, not who booked it.
- Organizations that are suspended or have the booking page off get 404 on every step.
- When guest booking is off (`allow_guest_booking`), visitors are asked to sign in before the
  details step. Confirming without signing in answers 403.

## Branding

`Organization.booking_instructions` is shown under the organization's name (parking, what to
bring, and so on). `Organization.brand_color` picks one of a fixed set of palettes
(`organizations/branding.py`), each with a button colour that keeps white text at WCAG AA
contrast. The palette is served as `/book/<slug>/theme.css`, a stylesheet, because the
Content Security Policy allows no inline styles. It overrides the `--color-brand-*`
variables, so buttons, links and focus rings follow the brand. Both fields can be set through
the organization API. A settings screen comes with the owner dashboard (M5.1).
