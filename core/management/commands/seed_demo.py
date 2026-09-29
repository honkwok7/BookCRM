"""Demo data for local development: ``python manage.py seed_demo``.

Idempotent: safe to run repeatedly. Future appointments go through the real booking service
(same validation as the API, so each lands on a free time) with notifications off; past
appointments go through ``record_past_booking`` (the service refuses to book the past).

Two organizations demonstrate tenant isolation, including one customer email that exists in
both. Locations (Downtown / North York) arrive with M3.1.
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from bookings.models import Booking, Customer
from bookings.services import create_booking, record_past_booking
from crm.activity import Kind, booking_metadata, record_activity
from crm.models import CustomerNote, Tag
from crm.services import add_customer_tag, create_customer, create_note, create_tag
from locations.models import Location
from locations.services import create_location, ensure_default_location, set_location_hours
from organizations.models import Organization, OrganizationMembership, OrganizationRole
from scheduling.availability import AvailabilityService
from scheduling.models import WeeklyAvailability
from services.models import Service, ServiceCategory
from services.services import create_category, create_service
from staff.models import StaffProfile
from staff.services import add_offering, create_staff_profile
from subscriptions.models import Plan, Subscription

User = get_user_model()

PLATFORM_ADMIN = ("admin@bookcrm.local", "Admin12345!")

ORGANIZATIONS = [
    {
        "slug": "harmony-wellness",
        "name": "Harmony Wellness Centre",
        "timezone": "America/Toronto",
        "currency": "CAD",
        "email": "hello@harmony.local",
        "plan": "professional",
        "staff_locations": {"massage@harmony.local": ["Main", "Downtown"]},
        # Maya works at Downtown on Thursdays and Fridays.
        "staff_hours": {"massage@harmony.local": {3: "Downtown", 4: "Downtown"}},
        # Chiropractic is only offered at the main clinic; massage at both.
        "service_locations": {
            "Initial Chiropractic Assessment": ["Main"],
            "Follow-up Chiropractic Visit": ["Main"],
        },
        "locations": [
            (
                "Downtown",
                {
                    "address_line1": "200 King Street West",
                    "city": "Toronto",
                    "region": "ON",
                    "postal_code": "M5H 3T4",
                    "country": "CA",
                    "phone": "+1 416 555 0142",
                },
            )
        ],
        "members": [
            ("owner@harmony.local", "Olivia", "Owner", OrganizationRole.OWNER, None),
            ("manager@harmony.local", "Marcus", "Manager", OrganizationRole.MANAGER, None),
            ("reception@harmony.local", "Rita", "Reception", OrganizationRole.RECEPTIONIST, None),
            ("massage@harmony.local", "Maya", "Chen", OrganizationRole.STAFF, "Massage Therapist"),
            ("chiro@harmony.local", "Daniel", "Park", OrganizationRole.STAFF, "Chiropractor"),
        ],
        "categories": {
            "Massage Therapy": [
                ("60 Minute Massage", 60, 110, "massage@harmony.local"),
                ("90 Minute Massage", 90, 150, "massage@harmony.local"),
            ],
            "Chiropractic": [
                ("Initial Chiropractic Assessment", 60, 120, "chiro@harmony.local"),
                ("Follow-up Chiropractic Visit", 30, 75, "chiro@harmony.local"),
            ],
        },
        "customers": [
            ("Alex Morgan", "alex@example.test", "+14165550101"),
            ("Priya Shah", "priya@example.test", "+14165550102"),
            ("Tom Becker", "tom@example.test", "+14165550103"),
            ("Grace Liu", "grace@example.test", "+14165550104"),
            ("Samuel Ortiz", "samuel@example.test", "+14165550105"),
            ("Hannah Kim", "hannah@example.test", "+14165550106"),
            ("Leo Martin", "", "+14165550107"),  # phone-only walk-in client
        ],
        "tags": {
            ("VIP", "#f59e0b"): ["alex@example.test", "grace@example.test"],
            ("New client", "#10b981"): ["+14165550107"],
            ("Prefers mornings", "#6366f1"): ["priya@example.test"],
        },
        "notes": [
            (
                "grace@example.test",
                "internal",
                "alert",
                "Sensitive to deep pressure; ask before increasing intensity.",
            ),
            (
                "alex@example.test",
                "customer_visible",
                "follow_up",
                "Stretch routine sent after the last visit; review at the next appointment.",
            ),
            ("+14165550107", "internal", "call", "Walk-in; prefers a phone call over email."),
        ],
    },
    {
        "slug": "serenity-spa",
        "name": "Serenity Spa",
        "timezone": "America/Vancouver",
        "currency": "CAD",
        "email": "hello@serenity.local",
        "plan": "starter",
        "members": [
            ("owner@serenity.local", "Sofia", "Owner", OrganizationRole.OWNER, None),
            ("esthetician@serenity.local", "Elena", "Rossi", OrganizationRole.STAFF, "Esthetician"),
        ],
        "categories": {
            "Facials": [("Signature Facial", 60, 130, "esthetician@serenity.local")],
            "Body": [("Hot Stone Massage", 75, 140, "esthetician@serenity.local")],
        },
        "customers": [
            # Same email as a Harmony customer: two separate records, invisible to each other.
            ("Alex Morgan", "alex@example.test", "+16045550101"),
            ("Nora White", "nora@example.test", "+16045550102"),
        ],
        "tags": {("Member", "#ec4899"): ["nora@example.test"]},
    },
]

PLANS = [
    ("starter", "Starter", 29, 290, 3, 10, 300, 1, False),
    ("professional", "Professional", 79, 790, 15, 50, 2000, 3, True),
    ("business", "Business", 149, 1490, 100, 500, 20000, 20, True),
]
PASSWORD = "Demo12345!"
CATEGORY_COLORS = ["#10b981", "#3b82f6", "#f59e0b", "#ec4899"]
PAST_OUTCOME_ACTIVITY = {
    Booking.Status.COMPLETED: Kind.APPOINTMENT_COMPLETED,
    Booking.Status.NO_SHOW: Kind.APPOINTMENT_NO_SHOW,
    Booking.Status.CANCELLED: Kind.APPOINTMENT_CANCELLED,
}


class Command(BaseCommand):
    help = "Seed idempotent demo data (Harmony Wellness Centre and Serenity Spa)."

    def handle(self, *args, **options):
        with transaction.atomic():
            self._platform_admin()
            plans = self._plans()
            for spec in ORGANIZATIONS:
                self._organization(spec, plans)
        self._report()

    # -- building blocks ---------------------------------------------------------------------

    def _user(self, email, first_name="", last_name="", password=PASSWORD, **extra):
        user, created = User.objects.get_or_create(
            email=email,
            defaults={
                "first_name": first_name,
                "last_name": last_name,
                "email_verified": True,
                **extra,
            },
        )
        if created:
            user.set_password(password)
            user.save(update_fields=["password"])
        return user

    def _platform_admin(self):
        email, password = PLATFORM_ADMIN
        self._user(email, "Platform", "Admin", password, is_staff=True, is_superuser=True)

    def _plans(self):
        plans = {}
        for slug, name, monthly, yearly, staff, services, bookings, locations, analytics in PLANS:
            plans[slug], _ = Plan.objects.get_or_create(
                slug=slug,
                defaults={
                    "name": name,
                    "monthly_price": monthly,
                    "yearly_price": yearly,
                    "maximum_staff": staff,
                    "maximum_services": services,
                    "maximum_monthly_bookings": bookings,
                    "maximum_locations": locations,
                    "analytics_enabled": analytics,
                    "api_access_enabled": True,
                },
            )
            if plans[slug].maximum_locations < locations:
                # Plans seeded before locations existed got the default limit of one.
                plans[slug].maximum_locations = locations
                plans[slug].save(update_fields=["maximum_locations", "updated_at"])
        return plans

    def _organization(self, spec, plans):
        organization, _ = Organization.objects.get_or_create(
            slug=spec["slug"],
            defaults={
                "name": spec["name"],
                "timezone": spec["timezone"],
                "currency": spec["currency"],
                "email": spec["email"],
            },
        )
        now = timezone.now()
        Subscription.objects.get_or_create(
            organization=organization,
            defaults={
                "plan": plans[spec["plan"]],
                "status": Subscription.Status.ACTIVE,
                "current_period_start": now,
                "current_period_end": now + timedelta(days=30),
            },
        )

        self._locations(organization, spec.get("locations", []))

        staff_by_email = {}
        for email, first, last, role, job_title in spec["members"]:
            user = self._user(email, first, last)
            OrganizationMembership.objects.get_or_create(
                organization=organization, user=user, defaults={"role": role}
            )
            if role == OrganizationRole.STAFF:
                profile = StaffProfile.objects.filter(organization=organization, user=user).first()
                if profile is None:
                    names = spec.get("staff_locations", {}).get(email, ["Main"])
                    profile = create_staff_profile(
                        organization=organization,
                        user=user,
                        job_title=job_title,
                        provider_type=job_title,
                        locations=Location.objects.filter(
                            organization=organization, name__in=names
                        ),
                    )
                staff_by_email[email] = profile
                # Monday-Friday, 09:00-17:00, at the location given for that weekday (default:
                # the main location). Only for someone who has no weekly hours yet.
                if not profile.weekly_availabilities.exists():
                    days = spec.get("staff_hours", {}).get(email, {})
                    for day in range(5):
                        WeeklyAvailability.objects.create(
                            organization=organization,
                            staff=profile,
                            location=Location.objects.get(
                                organization=organization, name=days.get(day, "Main")
                            ),
                            day_of_week=day,
                            start_time=time(9),
                            end_time=time(17),
                        )

        services = []
        service_locations = spec.get("service_locations", {})
        for position, (category_name, items) in enumerate(spec["categories"].items()):
            category = ServiceCategory.objects.filter(
                organization=organization, name=category_name
            ).first() or create_category(
                organization=organization,
                name=category_name,
                color=CATEGORY_COLORS[position % len(CATEGORY_COLORS)],
                sort_order=position,
            )
            for name, minutes, price, provider_email in items:
                service = Service.objects.filter(organization=organization, name=name).first()
                if service is None:
                    service = create_service(
                        organization=organization,
                        name=name,
                        category=category,
                        duration_minutes=minutes,
                        price=price,
                        currency=spec["currency"],
                        buffer_after_minutes=10,
                        cancellation_policy="Please give at least 24 hours' notice.",
                        locations=Location.objects.filter(
                            organization=organization, name__in=service_locations.get(name, [])
                        ),
                    )
                provider = staff_by_email[provider_email]
                if not provider.offerings.filter(service=service).exists():
                    add_offering(staff=provider, service=service)  # at all their locations
                services.append((service, staff_by_email[provider_email]))

        customers = []
        for name, email, phone in spec["customers"]:
            existing = Customer.objects.filter(organization=organization)
            customer = (
                existing.filter(email=email) if email else existing.filter(phone=phone)
            ).first()
            if customer is None:
                first_name, last_name = Customer.split_name(name)
                customer = create_customer(
                    organization=organization,
                    first_name=first_name,
                    last_name=last_name,
                    email=email,
                    phone=phone,
                    source=Customer.Source.WALK_IN if not email else Customer.Source.REFERRAL,
                    email_consent=bool(email),
                    sms_consent=True,
                )
            customers.append(customer)

        for (tag_name, color), contacts in spec["tags"].items():
            tag = Tag.objects.filter(organization=organization, name=tag_name).first()
            if tag is None:
                tag = create_tag(organization=organization, name=tag_name, color=color)
            for customer in customers:
                if customer.email in contacts or customer.phone in contacts:
                    add_customer_tag(customer=customer, tag=tag)  # no-op if already tagged

        if not Booking.objects.filter(organization=organization).exists():
            self._appointments(organization, services, customers)

        owner = User.objects.get(email=spec["members"][0][0])
        for contact, visibility, note_type, content in spec.get("notes", []):
            customer = next(c for c in customers if contact in (c.email, c.phone))
            if not customer.customer_notes.exists():
                create_note(
                    customer=customer,
                    author=owner,
                    visibility=visibility,
                    note_type=note_type,
                    content=content,
                    pinned=note_type == CustomerNote.NoteType.ALERT,
                )

    def _locations(self, organization, specs):
        """The default "Main" location (created with the organization) plus any extra ones,
        each with weekday opening hours. Hours are only set on a location that has none."""
        weekdays = [(day, time(9), time(18)) for day in range(5)] + [(5, time(10), time(15))]
        locations = [ensure_default_location(organization)]
        for name, fields in specs:
            location = Location.objects.filter(organization=organization, name=name).first()
            if location is None:
                location = create_location(organization=organization, name=name, **fields)
            locations.append(location)
        for location in locations:
            if not location.hours.exists():
                set_location_hours(location=location, periods=weekdays)

    def _appointments(self, organization, services, customers):
        tz = ZoneInfo(organization.timezone)
        today = timezone.now().astimezone(tz).date()

        def weekday_at(days_ahead, hour):
            day = today + timedelta(days=days_ahead)
            while day.weekday() >= 5:
                day += timedelta(days=1)
            return datetime.combine(day, time(hour), tzinfo=tz)

        # Upcoming: through the booking service (validation, history, audit; no emails). Each
        # appointment takes the provider's first free time from its preferred day and hour.
        for index, customer in enumerate(customers):
            service, provider = services[index % len(services)]
            preferred = weekday_at(1 + index // 2, 10 + 2 * (index % 3))
            engine = AvailabilityService(organization, service)
            slot = next(
                (
                    slot
                    for slot in engine.get_available_slots(
                        preferred.date(), preferred.date() + timedelta(days=14), staff=provider
                    )
                    if slot.start >= preferred
                ),
                None,
            )
            if slot is None:
                continue
            create_booking(
                organization=organization,
                service=service,
                staff_profile=provider,
                customer_name=customer.name,
                customer_email=customer.email,
                customer_phone=customer.phone,
                start_datetime=slot.start,
                customer_timezone=organization.timezone,
                source=Booking.Source.RECEPTION,
                customer=customer,
                notify=False,
            )

        # History: past appointments with final statuses.
        outcomes = [Booking.Status.COMPLETED, Booking.Status.COMPLETED, Booking.Status.NO_SHOW]
        outcomes += [Booking.Status.CANCELLED]
        for index, customer in enumerate(customers):
            service, provider = services[index % len(services)]
            start = weekday_at(-14 + index, 11) if index < 14 else weekday_at(-3, 11)
            if start >= timezone.now():
                continue
            booking = record_past_booking(
                organization=organization,
                service=service,
                staff_profile=provider,
                customer=customer,
                start_datetime=start,
                status=outcomes[index % len(outcomes)],
                reference=f"SCH-{start.year}-{9000 + index:06d}-{organization.slug[:3]}"[:20],
            )
            # Timeline entries the booking service would have written at the time.
            record_activity(
                Kind.APPOINTMENT_BOOKED,
                customer=customer,
                subject=booking,
                metadata=booking_metadata(booking),
                occurred_at=start - timedelta(days=7),
            )
            record_activity(
                PAST_OUTCOME_ACTIVITY[booking.status],
                customer=customer,
                subject=booking,
                metadata=booking_metadata(booking),
                occurred_at=start + timedelta(minutes=service.duration_minutes),
            )

    def _report(self):
        self.stdout.write(self.style.SUCCESS("Demo data ready."))
        self.stdout.write(f"  Platform admin: {PLATFORM_ADMIN[0]} / {PLATFORM_ADMIN[1]}")
        for spec in ORGANIZATIONS:
            self.stdout.write(f"  {spec['name']} (/book/{spec['slug']}/), password {PASSWORD}:")
            for email, _, _, role, job_title in spec["members"]:
                self.stdout.write(
                    f"    {role:<13} {email}" + (f"  ({job_title})" if job_title else "")
                )
        self.stdout.write("  Development-only credentials.")
