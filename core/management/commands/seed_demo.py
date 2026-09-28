from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from bookings.services import create_booking
from organizations.models import Organization, OrganizationMembership, OrganizationRole
from scheduling.models import WeeklyAvailability
from services.models import Service
from staff.models import StaffProfile
from subscriptions.models import Plan, Subscription


class Command(BaseCommand):
    help = "Seed demo data for local development"

    def handle(self, *args, **options):
        user_model = get_user_model()

        admin_user, _ = user_model.objects.get_or_create(
            email="admin@schedula.local",
            defaults={"is_staff": True, "is_superuser": True, "email_verified": True},
        )
        admin_user.set_password("Admin12345!")
        admin_user.save()

        owner, _ = user_model.objects.get_or_create(
            email="owner@demo.local", defaults={"email_verified": True}
        )
        owner.set_password("Owner12345!")
        owner.save()

        manager, _ = user_model.objects.get_or_create(
            email="manager@demo.local", defaults={"email_verified": True}
        )
        manager.set_password("Manager12345!")
        manager.save()

        staff_user, _ = user_model.objects.get_or_create(
            email="staff@demo.local", defaults={"email_verified": True}
        )
        staff_user.set_password("Staff12345!")
        staff_user.save()

        customer_user, _ = user_model.objects.get_or_create(
            email="customer@demo.local", defaults={"email_verified": True}
        )
        customer_user.set_password("Customer12345!")
        customer_user.save()

        organization, _ = Organization.objects.get_or_create(
            slug="demo-clinic",
            defaults={
                "name": "Demo Clinic",
                "email": "hello@demo.local",
                "timezone": "UTC",
                "currency": "USD",
            },
        )

        OrganizationMembership.objects.get_or_create(
            organization=organization, user=owner, defaults={"role": OrganizationRole.OWNER}
        )
        OrganizationMembership.objects.get_or_create(
            organization=organization, user=manager, defaults={"role": OrganizationRole.MANAGER}
        )
        OrganizationMembership.objects.get_or_create(
            organization=organization, user=staff_user, defaults={"role": OrganizationRole.STAFF}
        )

        free_plan, _ = Plan.objects.get_or_create(
            slug="free",
            defaults={
                "name": "Free",
                "monthly_price": 0,
                "yearly_price": 0,
                "maximum_staff": 2,
                "maximum_services": 3,
                "maximum_monthly_bookings": 100,
                "analytics_enabled": False,
                "api_access_enabled": True,
            },
        )
        pro_plan, _ = Plan.objects.get_or_create(
            slug="professional",
            defaults={
                "name": "Professional",
                "monthly_price": 49,
                "yearly_price": 490,
                "maximum_staff": 10,
                "maximum_services": 25,
                "maximum_monthly_bookings": 1000,
                "analytics_enabled": True,
                "api_access_enabled": True,
            },
        )
        Plan.objects.get_or_create(
            slug="business",
            defaults={
                "name": "Business",
                "monthly_price": 99,
                "yearly_price": 990,
                "maximum_staff": 100,
                "maximum_services": 200,
                "maximum_monthly_bookings": 10000,
                "analytics_enabled": True,
                "api_access_enabled": True,
            },
        )

        Subscription.objects.get_or_create(
            organization=organization,
            defaults={
                "plan": pro_plan,
                "status": Subscription.Status.ACTIVE,
                "billing_cycle": Subscription.BillingCycle.MONTHLY,
                "current_period_start": timezone.now(),
                "current_period_end": timezone.now() + timedelta(days=30),
            },
        )

        staff_profile, _ = StaffProfile.objects.get_or_create(
            organization=organization,
            user=staff_user,
            defaults={"job_title": "Consultant", "is_active": True, "is_accepting_bookings": True},
        )

        service, _ = Service.objects.get_or_create(
            organization=organization,
            slug="general-consultation",
            defaults={
                "name": "General Consultation",
                "description": "30-minute consultation",
                "price": 50,
                "currency": "USD",
                "duration_minutes": 30,
                "buffer_before_minutes": 5,
                "buffer_after_minutes": 5,
            },
        )
        service.assigned_staff_members.add(staff_profile)

        for day in [0, 1, 2, 3, 4]:
            WeeklyAvailability.objects.get_or_create(
                organization=organization,
                staff=staff_profile,
                day_of_week=day,
                start_time="09:00",
                end_time="17:00",
                defaults={"is_active": True},
            )

        start_dt = timezone.now() + timedelta(days=1)
        start_dt = start_dt.replace(hour=10, minute=0, second=0, microsecond=0)
        create_booking(
            organization=organization,
            service=service,
            staff_profile=staff_profile,
            customer_name="Demo Customer",
            customer_email=customer_user.email,
            customer_phone="+10000000000",
            start_datetime=start_dt,
            customer_timezone="UTC",
            actor=owner,
            customer_user=customer_user,
        )

        self.stdout.write(self.style.SUCCESS("Demo data seeded."))
        self.stdout.write("Admin: admin@schedula.local / Admin12345!")
        self.stdout.write("Owner: owner@demo.local / Owner12345!")
        self.stdout.write("Manager: manager@demo.local / Manager12345!")
        self.stdout.write("Staff: staff@demo.local / Staff12345!")
        self.stdout.write("Customer: customer@demo.local / Customer12345!")
