"""Demo data for local development: ``python manage.py seed_demo``.

Idempotent: safe to run repeatedly. Future appointments go through the real booking service
(same validation as the API) with notifications off; a handful of *past* appointments are
written directly because the booking service rightly refuses times in the past.

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
from bookings.services import create_booking
from organizations.models import Organization, OrganizationMembership, OrganizationRole
from scheduling.models import WeeklyAvailability
from services.models import Service, ServiceCategory
from staff.models import StaffProfile
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
    },
]

PLANS = [
    ("starter", "Starter", 29, 290, 3, 10, 300, False),
    ("professional", "Professional", 79, 790, 15, 50, 2000, True),
    ("business", "Business", 149, 1490, 100, 500, 20000, True),
]
PASSWORD = "Demo12345!"


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
        for slug, name, monthly, yearly, staff, services, bookings, analytics in PLANS:
            plans[slug], _ = Plan.objects.get_or_create(
                slug=slug,
                defaults={
                    "name": name,
                    "monthly_price": monthly,
                    "yearly_price": yearly,
                    "maximum_staff": staff,
                    "maximum_services": services,
                    "maximum_monthly_bookings": bookings,
                    "analytics_enabled": analytics,
                    "api_access_enabled": True,
                },
            )
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

        staff_by_email = {}
        for email, first, last, role, job_title in spec["members"]:
            user = self._user(email, first, last)
            OrganizationMembership.objects.get_or_create(
                organization=organization, user=user, defaults={"role": role}
            )
            if role == OrganizationRole.STAFF:
                profile, _ = StaffProfile.objects.get_or_create(
                    organization=organization, user=user, defaults={"job_title": job_title}
                )
                staff_by_email[email] = profile
                for day in range(5):  # Monday-Friday, 09:00-17:00 local time
                    WeeklyAvailability.objects.get_or_create(
                        organization=organization,
                        staff=profile,
                        day_of_week=day,
                        start_time=time(9),
                        end_time=time(17),
                    )

        services = []
        for category_name, items in spec["categories"].items():
            category, _ = ServiceCategory.objects.get_or_create(
                organization=organization,
                slug=category_name.lower().replace(" ", "-"),
                defaults={"name": category_name},
            )
            for name, minutes, price, provider_email in items:
                service, _ = Service.objects.get_or_create(
                    organization=organization,
                    slug=name.lower().replace(" ", "-"),
                    defaults={
                        "name": name,
                        "category": category,
                        "duration_minutes": minutes,
                        "price": price,
                        "currency": spec["currency"],
                        "buffer_after_minutes": 10,
                    },
                )
                service.assigned_staff_members.add(staff_by_email[provider_email])
                services.append((service, staff_by_email[provider_email]))

        customers = []
        for name, email, phone in spec["customers"]:
            customer, _ = Customer.objects.get_or_create(
                organization=organization, email=email, defaults={"name": name, "phone": phone}
            )
            customers.append(customer)

        if not Booking.objects.filter(organization=organization).exists():
            self._appointments(organization, services, customers)

    def _appointments(self, organization, services, customers):
        tz = ZoneInfo(organization.timezone)
        today = timezone.now().astimezone(tz).date()

        def weekday_at(days_ahead, hour):
            day = today + timedelta(days=days_ahead)
            while day.weekday() >= 5:
                day += timedelta(days=1)
            return datetime.combine(day, time(hour), tzinfo=tz)

        # Upcoming: through the booking service (validation, history, audit; no emails).
        for index, customer in enumerate(customers):
            service, provider = services[index % len(services)]
            create_booking(
                organization=organization,
                service=service,
                staff_profile=provider,
                customer_name=customer.name,
                customer_email=customer.email,
                customer_phone=customer.phone,
                start_datetime=weekday_at(1 + index // 2, 10 + 2 * (index % 3)),
                customer_timezone=organization.timezone,
                notify=False,
            )

        # History: written directly (the service refuses past times) with final statuses.
        outcomes = [Booking.Status.COMPLETED, Booking.Status.COMPLETED, Booking.Status.NO_SHOW]
        outcomes += [Booking.Status.CANCELLED]
        for index, customer in enumerate(customers):
            service, provider = services[index % len(services)]
            start = weekday_at(-14 + index, 11) if index < 14 else weekday_at(-3, 11)
            if start >= timezone.now():
                continue
            Booking.objects.create(
                reference=f"SCH-{start.year}-{9000 + index:06d}-{organization.slug[:3]}"[:20],
                organization=organization,
                customer=customer,
                customer_name=customer.name,
                customer_email=customer.email,
                customer_phone=customer.phone,
                service=service,
                staff=provider,
                start_datetime=start,
                end_datetime=start + timedelta(minutes=service.duration_minutes),
                organization_timezone=organization.timezone,
                customer_timezone=organization.timezone,
                price_snapshot=service.price,
                duration_snapshot_minutes=service.duration_minutes,
                status=outcomes[index % len(outcomes)],
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
