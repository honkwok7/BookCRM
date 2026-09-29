"""M3.2: staff profiles, the locations people work at and the services they offer where."""

from decimal import Decimal
from importlib import import_module

from django.apps import apps as django_apps
from django.db import IntegrityError, transaction
from django.test import TestCase

from core.exceptions import ConflictError, DomainError
from core.models import AuditLog
from locations.models import Location
from locations.services import create_location, delete_location, update_location
from locations.tests import subscribe
from organizations.models import OrganizationRole
from staff.models import StaffProfile, StaffServiceOffering
from staff.selectors import list_providers_for, offering_for
from staff.services import (
    OfferingChoice,
    add_offering,
    create_staff_profile,
    delete_staff_profile,
    remove_offering,
    set_service_providers,
    set_staff_locations,
    set_staff_offerings,
    update_offering,
    update_staff_profile,
)
from tests import factories as f


class StaffTestCase(TestCase):
    def setUp(self):
        self.org = f.OrganizationFactory()
        self.main = Location.objects.get(organization=self.org)
        self.downtown = create_location(organization=self.org, name="Downtown")
        self.owner = f.MembershipFactory(organization=self.org, role=OrganizationRole.OWNER).user

    def provider(self, locations=None, **fields):
        user = f.MembershipFactory(organization=self.org, role=OrganizationRole.STAFF).user
        return create_staff_profile(organization=self.org, user=user, locations=locations, **fields)


class StaffProfileTests(StaffTestCase):
    def test_create_defaults_to_the_default_location(self):
        staff = self.provider(display_name="  Maya ")
        self.assertEqual(list(staff.locations.all()), [self.main])
        self.assertEqual(staff.display_name, "Maya")
        log = AuditLog.objects.get(action="staff.created")
        self.assertEqual(log.metadata["locations"], [str(self.main.pk)])

    def test_only_active_team_members_can_become_staff(self):
        outsider = f.UserFactory()
        customer = f.MembershipFactory(organization=self.org, role=OrganizationRole.CUSTOMER).user
        former = f.MembershipFactory(organization=self.org, is_active=False).user
        for user in (outsider, customer, former):
            with self.subTest(user=user.email):
                with self.assertRaises(DomainError) as raised:
                    create_staff_profile(organization=self.org, user=user)
                self.assertEqual(raised.exception.code, "not_a_member")
        with self.assertRaises(ConflictError):
            create_staff_profile(organization=self.org, user=self.provider().user)

    def test_locations_must_be_own_and_active(self):
        other = Location.objects.get(organization=f.OrganizationFactory())
        update_location(location=self.downtown, is_active=False)
        for location in (other, self.downtown):
            with self.subTest(location=location.name):
                with self.assertRaises(DomainError) as raised:
                    self.provider(locations=[location])
                self.assertEqual(raised.exception.code, "invalid_location")

    def test_plan_limit(self):
        plan = subscribe(self.org, maximum_locations=5).plan
        plan.maximum_staff = 1
        plan.save()
        first = self.provider()
        with self.assertRaises(ConflictError) as raised:
            self.provider()
        self.assertEqual(raised.exception.code, "plan_limit")
        update_staff_profile(staff=first, is_active=False)
        second = self.provider()  # inactive staff don't count
        with self.assertRaises(ConflictError):
            update_staff_profile(staff=first, is_active=True)
        self.assertTrue(second.is_active)

    def test_update_is_audited_without_the_phone_number(self):
        staff = self.provider()
        update_staff_profile(staff=staff, phone_number="+1 555 0100", job_title="Therapist II")
        changes = AuditLog.objects.get(action="staff.updated").metadata["changes"]
        self.assertEqual(changes["phone_number"], "changed")
        self.assertEqual(changes["job_title"][1], "Therapist II")
        with self.assertRaises(DomainError):
            update_staff_profile(staff=staff, organization=f.OrganizationFactory())
        with self.assertRaises(DomainError):
            update_staff_profile(staff=staff, max_daily_appointments=0)

    def test_public_name_never_uses_the_email(self):
        user = f.UserFactory(first_name="", last_name="", email="secret@example.test")
        staff = StaffProfile(user=user, job_title="")
        self.assertEqual(staff.public_name, "Team member")
        staff.display_name = "Dr. Park"
        self.assertEqual(staff.public_name, "Dr. Park")

    def test_staff_with_appointments_cannot_be_deleted(self):
        booking = f.BookingFactory(organization=self.org)
        with self.assertRaises(ConflictError) as raised:
            delete_staff_profile(staff=booking.staff)
        self.assertEqual(raised.exception.code, "in_use")
        staff = self.provider()
        delete_staff_profile(staff=staff)
        self.assertFalse(StaffProfile.objects.filter(pk=staff.pk).exists())


class OfferingTests(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.massage = f.ServiceFactory(organization=self.org, name="Massage", price=100)
        self.facial = f.ServiceFactory(organization=self.org, name="Facial")

    def test_offering_everywhere_and_custom_values(self):
        staff = self.provider(locations=[self.main, self.downtown])
        offering = add_offering(
            staff=staff,
            service=self.massage,
            custom_duration_minutes=90,
            custom_price=Decimal("120"),
        )
        self.assertIsNone(offering.location)
        self.assertEqual((offering.duration_minutes, offering.price), (90, Decimal("120")))
        self.assertEqual(list(self.massage.assigned_staff_members.all()), [staff])
        with self.assertRaises(ConflictError):
            add_offering(staff=staff, service=self.massage)
        self.assertTrue(AuditLog.objects.filter(action="staff.service_added").exists())

    def test_invalid_offerings(self):
        staff = self.provider()  # works at Main only
        other_service = f.ServiceFactory()
        archived = f.ServiceFactory(organization=self.org, is_archived=True)
        cases = [
            ({"service": other_service}, "invalid_service"),
            ({"service": archived}, "invalid_service"),
            ({"service": self.massage, "location": self.downtown}, "location_not_assigned"),
            ({"service": self.massage, "custom_duration_minutes": 0}, "invalid_duration"),
            ({"service": self.massage, "custom_price": Decimal("-1")}, "invalid_price"),
        ]
        for kwargs, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(DomainError) as raised:
                    add_offering(staff=staff, **kwargs)
                self.assertEqual(raised.exception.code, code)
        self.assertFalse(StaffServiceOffering.objects.exists())

    def test_database_refuses_duplicate_all_location_offerings(self):
        staff = self.provider()
        add_offering(staff=staff, service=self.massage)
        with self.assertRaises(IntegrityError), transaction.atomic():
            StaffServiceOffering.objects.create(
                organization=self.org, staff=staff, service=self.massage
            )

    def test_update_and_remove(self):
        staff = self.provider()
        offering = add_offering(staff=staff, service=self.massage)
        update_offering(offering=offering, is_active=False)
        self.assertFalse(self.massage.assigned_staff_members.exists())  # mirror follows
        with self.assertRaises(DomainError):
            update_offering(offering=offering, location=self.downtown)
        remove_offering(offering=offering)
        self.assertFalse(StaffServiceOffering.objects.exists())

    def test_dropping_a_location_removes_offerings_tied_to_it(self):
        staff = self.provider(locations=[self.main, self.downtown])
        add_offering(staff=staff, service=self.massage, location=self.downtown)
        add_offering(staff=staff, service=self.facial)  # everywhere: kept
        set_staff_locations(staff=staff, locations=[self.main])
        self.assertEqual(list(staff.offerings.values_list("service__name", flat=True)), ["Facial"])
        self.assertFalse(self.massage.assigned_staff_members.exists())
        log = AuditLog.objects.get(action="staff.locations_changed")
        self.assertEqual(log.metadata["offerings_removed"], 1)

    def test_a_location_with_offerings_cannot_be_deleted(self):
        staff = self.provider(locations=[self.main, self.downtown])
        add_offering(staff=staff, service=self.massage, location=self.downtown)
        with self.assertRaises(ConflictError) as raised:
            delete_location(location=self.downtown)
        self.assertEqual(raised.exception.code, "in_use")

    def test_set_staff_offerings_writes_only_changes(self):
        staff = self.provider(locations=[self.main, self.downtown])
        set_staff_offerings(
            staff=staff,
            choices=[
                OfferingChoice(service=self.massage),
                OfferingChoice(service=self.facial, locations=(self.downtown,)),
            ],
        )
        self.assertEqual(
            set(staff.offerings.values_list("service__name", "location__name")),
            {("Massage", None), ("Facial", "Downtown")},
        )
        before = AuditLog.objects.count()
        set_staff_offerings(
            staff=staff,
            choices=[
                OfferingChoice(service=self.massage),
                OfferingChoice(service=self.facial, locations=(self.downtown,)),
            ],
        )
        self.assertEqual(AuditLog.objects.count(), before)  # nothing changed

        set_staff_offerings(
            staff=staff,
            choices=[OfferingChoice(service=self.massage, custom_price=Decimal("80"))],
        )
        self.assertEqual(
            list(staff.offerings.values_list("service__name", "custom_price")),
            [("Massage", Decimal("80.00"))],
        )
        self.assertEqual(list(self.facial.assigned_staff_members.all()), [])

    def test_set_service_providers_keeps_the_old_assignment_api_working(self):
        maya, daniel = self.provider(), self.provider()
        add_offering(staff=daniel, service=self.massage, location=self.main)
        set_service_providers(service=self.massage, staff_members=[maya, daniel])
        self.assertEqual(set(self.massage.assigned_staff_members.all()), {maya, daniel})
        # Daniel keeps his location-specific offering; Maya gets an "everywhere" one.
        self.assertEqual(daniel.offerings.get().location, self.main)
        self.assertIsNone(maya.offerings.get().location)
        set_service_providers(service=self.massage, staff_members=[maya])
        self.assertFalse(daniel.offerings.exists())
        with self.assertRaises(DomainError):
            set_service_providers(service=self.massage, staff_members=[f.StaffProfileFactory()])


class ProviderMatrixTests(StaffTestCase):
    """Acceptance: a provider can offer service X only at location Y."""

    def setUp(self):
        super().setUp()
        self.massage = f.ServiceFactory(organization=self.org, name="Massage", price=100)
        self.maya = self.provider(locations=[self.main, self.downtown])
        self.daniel = self.provider(locations=[self.main])
        self.ines = self.provider(locations=[self.downtown], online_booking_visible=False)
        add_offering(staff=self.maya, service=self.massage, location=self.downtown)
        add_offering(staff=self.daniel, service=self.massage)
        add_offering(staff=self.ines, service=self.massage, custom_price=Decimal("90"))

    def providers(self, location=None, **kwargs):
        return set(list_providers_for(self.massage, location, **kwargs))

    def test_matrix(self):
        self.assertEqual(self.providers(self.main), {self.daniel})
        self.assertEqual(self.providers(self.downtown), {self.maya, self.ines})
        self.assertEqual(self.providers(), {self.maya, self.daniel, self.ines})
        self.assertEqual(self.providers(self.downtown, public=True), {self.maya})

    def test_inactive_or_unavailable_providers_are_left_out(self):
        update_staff_profile(staff=self.maya, is_accepting_bookings=False)
        update_offering(offering=self.ines.offerings.get(), is_active=False)
        self.assertEqual(self.providers(self.downtown), set())

    def test_offering_for_prefers_the_location_specific_one(self):
        add_offering(
            staff=self.maya, service=self.massage, custom_price=Decimal("150")
        )  # everywhere
        downtown = offering_for(self.maya, self.massage, self.downtown)
        self.assertEqual(downtown.location, self.downtown)
        self.assertEqual(offering_for(self.maya, self.massage, self.main).price, Decimal("150"))
        self.assertIsNone(offering_for(self.daniel, self.massage, self.downtown))
        self.assertEqual(offering_for(self.ines, self.massage).price, Decimal("90"))


class BackfillMigrationTests(TestCase):
    def test_backfill_links_default_location_and_copies_assignments(self):
        org = f.OrganizationFactory()
        main = Location.objects.get(organization=org)
        staff = f.StaffProfileFactory(organization=org)
        service = f.ServiceFactory(organization=org)
        service.assigned_staff_members.add(staff)
        foreign = f.ServiceFactory()
        foreign.assigned_staff_members.add(staff)  # an invalid cross-tenant link: skipped
        migration = import_module("staff.migrations.0003_backfill_locations_and_offerings")

        migration.backfill(django_apps, None)
        migration.backfill(django_apps, None)  # safe to run twice

        self.assertEqual(list(staff.locations.all()), [main])
        offering = StaffServiceOffering.objects.get()
        self.assertEqual(
            (offering.staff, offering.service, offering.location, offering.organization),
            (staff, service, None, org),
        )
