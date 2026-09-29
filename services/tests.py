"""M3.3: services and categories (service layer, selectors, location and provider-type rules)."""

from decimal import Decimal

from django.test import TestCase

from bookings.selectors import bookable_services
from core.exceptions import ConflictError, DomainError
from core.models import AuditLog
from locations.models import Location
from locations.services import create_location
from locations.tests import subscribe
from organizations.models import OrganizationRole
from services.models import Service, ServiceCategory
from services.selectors import services_bookable_at
from services.services import (
    create_category,
    create_service,
    delete_category,
    delete_service,
    update_category,
    update_service,
)
from staff.selectors import list_providers_for
from staff.services import add_offering, create_staff_profile
from tests import factories as f


class ServiceTestCase(TestCase):
    def setUp(self):
        self.org = f.OrganizationFactory(currency="CAD")
        self.main = Location.objects.get(organization=self.org)
        self.downtown = create_location(organization=self.org, name="Downtown")
        self.owner = f.MembershipFactory(organization=self.org, role=OrganizationRole.OWNER).user

    def service(self, name="Massage", **fields):
        fields.setdefault("duration_minutes", 60)
        fields.setdefault("price", Decimal("100"))
        return create_service(organization=self.org, name=name, **fields)


class ServiceWriteTests(ServiceTestCase):
    def test_create(self):
        service = create_service(
            organization=self.org,
            actor=self.owner,
            name="  Deep Tissue ",
            duration_minutes=60,
            price=Decimal("110"),
            locations=[self.downtown],
            required_provider_type=" Massage therapist ",
            tax_rate=Decimal("13"),
        )
        self.assertEqual(
            (service.name, service.slug, service.currency), ("Deep Tissue", "deep-tissue", "CAD")
        )
        self.assertEqual(list(service.locations.all()), [self.downtown])
        self.assertEqual(service.required_provider_type, "Massage therapist")
        self.assertEqual(AuditLog.objects.get(action="service.created").user, self.owner)

    def test_slugs_are_unique_per_organization(self):
        first, second = self.service("Massage"), self.service("Massage")
        self.assertEqual((first.slug, second.slug), ("massage", "massage-2"))

    def test_invalid_values(self):
        other_category = f.ServiceCategoryFactory()
        cases = [
            ({"name": " "}, "name_required"),
            ({"duration_minutes": 0}, "invalid_duration"),
            ({"price": Decimal("-1")}, "invalid_price"),
            ({"tax_rate": Decimal("101")}, "invalid_tax_rate"),
            ({"capacity": 0}, "invalid_capacity"),
            ({"category": other_category}, "invalid_category"),
            (
                {"locations": [Location.objects.get(organization=f.OrganizationFactory())]},
                "invalid_location",
            ),
            ({"slug": "chosen"}, "invalid"),
        ]
        for fields, code in cases:
            with self.subTest(code=code):
                name = fields.pop("name", "X")
                with self.assertRaises(DomainError) as raised:
                    self.service(name, **fields)
                self.assertEqual(raised.exception.code, code)

    def test_plan_limit_counts_services_that_are_not_archived(self):
        plan = subscribe(self.org, maximum_locations=5).plan
        plan.maximum_services = 1
        plan.save()
        first = self.service("One")
        with self.assertRaises(ConflictError) as raised:
            self.service("Two")
        self.assertEqual(raised.exception.code, "plan_limit")
        update_service(service=first, is_archived=True)
        second = self.service("Two")
        with self.assertRaises(ConflictError):
            update_service(service=first, is_archived=False)
        self.assertFalse(second.is_archived)

    def test_update_locations_and_audit(self):
        service = self.service(locations=[self.main])
        update_service(service=service, actor=self.owner, price=Decimal("120"), locations=[])
        self.assertFalse(service.locations.exists())  # every location
        changes = AuditLog.objects.get(action="service.updated").metadata["changes"]
        self.assertEqual(changes["price"], ["100.00", "120"])
        self.assertEqual(changes["locations"], [[str(self.main.pk)], []])
        update_service(service=service, name="Renamed")
        service.refresh_from_db()
        self.assertEqual(service.slug, "massage")  # stable

    def test_delete(self):
        booked = f.BookingFactory(organization=self.org).service
        with self.assertRaises(ConflictError) as raised:
            delete_service(service=booked)
        self.assertEqual(raised.exception.code, "in_use")
        unused = self.service()
        delete_service(service=unused)
        self.assertFalse(Service.objects.filter(pk=unused.pk).exists())


class CategoryTests(ServiceTestCase):
    def test_create_update_delete(self):
        category = create_category(
            organization=self.org, name="Massage", color="#10B981", sort_order=2
        )
        self.assertEqual((category.slug, category.color), ("massage", "#10b981"))
        with self.assertRaises(ConflictError):
            create_category(organization=self.org, name="massage")
        with self.assertRaises(DomainError) as raised:
            update_category(category=category, color="green")
        self.assertEqual(raised.exception.code, "invalid_color")
        service = self.service(category=category)
        delete_category(category=category)
        service.refresh_from_db()
        self.assertIsNone(service.category)
        self.assertFalse(ServiceCategory.objects.filter(pk=category.pk).exists())

    def test_ordering(self):
        create_category(organization=self.org, name="B", sort_order=1)
        create_category(organization=self.org, name="A", sort_order=2)
        create_category(organization=self.org, name="C", sort_order=1)
        names = list(
            ServiceCategory.objects.filter(organization=self.org).values_list("name", flat=True)
        )
        self.assertEqual(names, ["B", "C", "A"])


class AvailabilityFilteringTests(ServiceTestCase):
    """Acceptance: only services bookable online at the chosen location are listed."""

    def setUp(self):
        super().setUp()
        self.everywhere = self.service("Everywhere")
        self.downtown_only = self.service("Downtown only", locations=[self.downtown])
        self.team_only = self.service("Team only", is_public=False)
        self.inactive = self.service("Inactive", is_active=False)
        self.archived = self.service("Archived", is_archived=True)

    def names(self, location=None, public=True):
        return set(
            services_bookable_at(self.org, location, public=public).values_list("name", flat=True)
        )

    def test_public_lists_by_location(self):
        self.assertEqual(self.names(self.main), {"Everywhere"})
        self.assertEqual(self.names(self.downtown), {"Everywhere", "Downtown only"})
        self.assertEqual(self.names(), {"Everywhere", "Downtown only"})

    def test_team_sees_services_not_bookable_online(self):
        self.assertEqual(self.names(self.main, public=False), {"Everywhere", "Team only"})
        self.assertEqual(
            set(bookable_services(self.org, public=False).values_list("name", flat=True)),
            {"Everywhere", "Downtown only", "Team only"},
        )


class ProviderRulesTests(ServiceTestCase):
    def provider(self, provider_type="", locations=None):
        user = f.MembershipFactory(organization=self.org, role=OrganizationRole.STAFF).user
        return create_staff_profile(
            organization=self.org,
            user=user,
            provider_type=provider_type,
            locations=locations or [self.main, self.downtown],
        )

    def test_required_provider_type(self):
        service = self.service(required_provider_type="Massage therapist")
        therapist = self.provider("massage therapist")
        chiropractor = self.provider("Chiropractor")
        add_offering(staff=therapist, service=service)
        with self.assertRaises(DomainError) as raised:
            add_offering(staff=chiropractor, service=service)
        self.assertEqual(raised.exception.code, "provider_type_mismatch")
        # A type added after the fact hides offerings that no longer fit.
        other = self.service("Other")
        add_offering(staff=chiropractor, service=other)
        other = update_service(service=other, required_provider_type="Massage therapist")
        self.assertEqual(list(list_providers_for(other)), [])
        self.assertEqual(list(list_providers_for(service)), [therapist])

    def test_service_locations_limit_offerings_and_providers(self):
        service = self.service(locations=[self.downtown])
        staff = self.provider()
        with self.assertRaises(DomainError) as raised:
            add_offering(staff=staff, service=service, location=self.main)
        self.assertEqual(raised.exception.code, "service_not_at_location")
        add_offering(staff=staff, service=service)  # all their locations
        self.assertEqual(list(list_providers_for(service, self.downtown)), [staff])
        self.assertEqual(list(list_providers_for(service, self.main)), [])
