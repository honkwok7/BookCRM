"""factory_boy factories for tests.

Related objects default to the *same* organization as their parent (``SelfAttribute``), so a
factory never creates an accidental cross-tenant reference. Pass ``organization=`` explicitly to
place an object in a given tenant.
"""

from datetime import time, timedelta

import factory
from django.contrib.auth import get_user_model
from django.utils import timezone

from bookings.models import Booking, Customer, WaitlistEntry
from core.models import AuditLog
from crm.models import CustomerActivity, CustomerNote, CustomerTag, Tag
from locations.models import Location, LocationClosure, LocationHours
from notifications.models import NotificationLog
from organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationRole,
)
from scheduling.models import (
    AvailabilityException,
    OrganizationHoliday,
    TimeOff,
    WeeklyAvailability,
)
from services.models import Service, ServiceCategory
from staff.models import StaffProfile, StaffServiceOffering

DEFAULT_PASSWORD = "Password12345!"
SAME_ORG = factory.SelfAttribute("..organization")


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = get_user_model()

    email = factory.Sequence(lambda n: f"user{n}@example.test")
    first_name = factory.Faker("first_name")
    last_name = factory.Faker("last_name")

    @classmethod
    def _create(cls, model_class, *args, **kwargs):
        password = kwargs.pop("password", DEFAULT_PASSWORD)
        return model_class.objects.create_user(*args, password=password, **kwargs)


class OrganizationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Organization

    name = factory.Sequence(lambda n: f"Organization {n}")
    slug = factory.Sequence(lambda n: f"org-{n}")


class MembershipFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = OrganizationMembership

    organization = factory.SubFactory(OrganizationFactory)
    user = factory.SubFactory(UserFactory)
    role = OrganizationRole.OWNER


class InvitationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = OrganizationInvitation

    organization = factory.SubFactory(OrganizationFactory)
    email = factory.Sequence(lambda n: f"invitee{n}@example.test")
    role = OrganizationRole.STAFF
    token = factory.LazyFunction(OrganizationInvitation.generate_token)
    expires_at = factory.LazyFunction(OrganizationInvitation.default_expiry)


class StaffProfileFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = StaffProfile

    organization = factory.SubFactory(OrganizationFactory)
    user = factory.SubFactory(UserFactory)
    job_title = "Therapist"

    @factory.post_generation
    def locations(self, create, extracted, **kwargs):
        """Like create_staff_profile: the default location unless ``locations=[...]``."""
        if not create:
            return
        if extracted is not None:
            self.locations.set(extracted)
        else:
            self.locations.set(
                Location.objects.filter(organization=self.organization, is_default=True)
            )


class StaffServiceOfferingFactory(factory.django.DjangoModelFactory):
    """``staff`` offers ``service`` at all their locations."""

    class Meta:
        model = StaffServiceOffering

    organization = factory.SubFactory(OrganizationFactory)
    staff = factory.SubFactory(StaffProfileFactory, organization=SAME_ORG)
    service = factory.SubFactory("tests.factories.ServiceFactory", organization=SAME_ORG)


class ServiceCategoryFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ServiceCategory

    organization = factory.SubFactory(OrganizationFactory)
    name = factory.Sequence(lambda n: f"Category {n}")
    slug = factory.Sequence(lambda n: f"category-{n}")


class ServiceFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Service

    organization = factory.SubFactory(OrganizationFactory)
    category = factory.SubFactory(ServiceCategoryFactory, organization=SAME_ORG)
    name = factory.Sequence(lambda n: f"Service {n}")
    slug = factory.Sequence(lambda n: f"service-{n}")
    price = 80
    duration_minutes = 60


class CustomerFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Customer

    organization = factory.SubFactory(OrganizationFactory)
    first_name = factory.Faker("first_name")
    last_name = factory.Faker("last_name")
    email = factory.Sequence(lambda n: f"customer{n}@example.test")
    phone = factory.Sequence(lambda n: f"+1555000{n:04d}")


class TagFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Tag

    organization = factory.SubFactory(OrganizationFactory)
    name = factory.Sequence(lambda n: f"Tag {n}")
    slug = factory.Sequence(lambda n: f"tag-{n}")
    color = "#3b82f6"


class CustomerTagFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = CustomerTag

    organization = factory.SubFactory(OrganizationFactory)
    customer = factory.SubFactory(CustomerFactory, organization=SAME_ORG)
    tag = factory.SubFactory(TagFactory, organization=SAME_ORG)


class CustomerNoteFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = CustomerNote

    organization = factory.SubFactory(OrganizationFactory)
    customer = factory.SubFactory(CustomerFactory, organization=SAME_ORG)
    content = factory.Sequence(lambda n: f"Note {n}")


class CustomerActivityFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = CustomerActivity

    organization = factory.SubFactory(OrganizationFactory)
    customer = factory.SubFactory(CustomerFactory, organization=SAME_ORG)
    kind = CustomerActivity.Kind.PROFILE_UPDATED


class BookingFactory(factory.django.DjangoModelFactory):
    """Creates the row directly (no validation, no notifications). Use
    ``bookings.services.create_booking`` when a test is about booking behaviour."""

    class Meta:
        model = Booking

    organization = factory.SubFactory(OrganizationFactory)
    service = factory.SubFactory(ServiceFactory, organization=SAME_ORG)
    staff = factory.SubFactory(StaffProfileFactory, organization=SAME_ORG)
    customer = factory.SubFactory(CustomerFactory, organization=SAME_ORG)
    reference = factory.Sequence(lambda n: f"SCH-2099-{n:06d}")
    customer_name = factory.SelfAttribute("customer.name")
    customer_email = factory.SelfAttribute("customer.email")
    start_datetime = factory.LazyFunction(lambda: timezone.now() + timedelta(days=2))
    end_datetime = factory.LazyAttribute(lambda o: o.start_datetime + timedelta(minutes=60))
    status = Booking.Status.CONFIRMED
    price_snapshot = 80
    duration_snapshot_minutes = 60


class WaitlistEntryFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = WaitlistEntry

    organization = factory.SubFactory(OrganizationFactory)
    service = factory.SubFactory(ServiceFactory, organization=SAME_ORG)
    customer_name = factory.Faker("name")
    customer_email = factory.Sequence(lambda n: f"waiting{n}@example.test")


class WeeklyAvailabilityFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = WeeklyAvailability

    organization = factory.SubFactory(OrganizationFactory)
    staff = factory.SubFactory(StaffProfileFactory, organization=SAME_ORG)
    day_of_week = 0
    start_time = time(9)
    end_time = time(17)


class AvailabilityExceptionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = AvailabilityException

    organization = factory.SubFactory(OrganizationFactory)
    staff = factory.SubFactory(StaffProfileFactory, organization=SAME_ORG)
    date = factory.LazyFunction(lambda: timezone.localdate() + timedelta(days=10))
    unavailable_all_day = True


class TimeOffFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = TimeOff

    organization = factory.SubFactory(OrganizationFactory)
    staff = factory.SubFactory(StaffProfileFactory, organization=SAME_ORG)
    start_datetime = factory.LazyFunction(lambda: timezone.now() + timedelta(days=20))
    end_datetime = factory.LazyAttribute(lambda o: o.start_datetime + timedelta(days=1))


class OrganizationHolidayFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = OrganizationHoliday

    organization = factory.SubFactory(OrganizationFactory)
    date = factory.LazyFunction(lambda: timezone.localdate() + timedelta(days=30))
    name = factory.Sequence(lambda n: f"Holiday {n}")


class LocationFactory(factory.django.DjangoModelFactory):
    """An extra, non-default location (every organization already has its default "Main")."""

    class Meta:
        model = Location

    organization = factory.SubFactory(OrganizationFactory)
    name = factory.Sequence(lambda n: f"Location {n}")
    slug = factory.Sequence(lambda n: f"location-{n}")
    timezone = "America/Toronto"


class LocationHoursFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = LocationHours

    organization = factory.SubFactory(OrganizationFactory)
    location = factory.SubFactory(LocationFactory, organization=SAME_ORG)
    weekday = 0
    opens_at = time(9)
    closes_at = time(17)


class LocationClosureFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = LocationClosure

    organization = factory.SubFactory(OrganizationFactory)
    location = factory.SubFactory(LocationFactory, organization=SAME_ORG)
    start_date = factory.LazyFunction(lambda: timezone.localdate() + timedelta(days=14))
    end_date = factory.SelfAttribute("start_date")
    reason = factory.Sequence(lambda n: f"Closure {n}")


class AuditLogFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = AuditLog

    organization = factory.SubFactory(OrganizationFactory)
    action = "system.test"


class NotificationLogFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = NotificationLog

    organization = factory.SubFactory(OrganizationFactory)
    recipient_email = factory.Sequence(lambda n: f"notify{n}@example.test")
    notification_type = "booking_confirmation"


def make_bookable(staff, *services, days=(0, 1, 2, 3, 4, 5, 6), start=time(0), end=time(23, 59)):
    """Let ``staff`` be booked for ``services``: an offering for each (unless one exists) and
    weekly hours on ``days`` (00:00-23:59 by default, any location), and the default location
    if they work nowhere yet. For tests about booking
    behaviour rather than availability rules; the booking service checks both."""
    if not staff.locations.exists():  # created without the factory: works at the default
        staff.locations.add(Location.objects.get(organization=staff.organization, is_default=True))
    for service in services:
        if not StaffServiceOffering.objects.filter(staff=staff, service=service).exists():
            StaffServiceOfferingFactory(
                organization=staff.organization, staff=staff, service=service
            )
    for day in days:
        WeeklyAvailabilityFactory(
            organization=staff.organization,
            staff=staff,
            day_of_week=day,
            start_time=start,
            end_time=end,
        )
    return staff


def future(days=2, hour=10, minute=0):
    """``hour:minute`` UTC, ``days`` from today: a booking time that is always in the future
    and never crosses midnight (weekly hours are per day)."""
    moment = timezone.now() + timedelta(days=days)
    return moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
