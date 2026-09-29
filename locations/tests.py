"""M3.1: locations, their opening hours and closures (service layer and data migration)."""

from datetime import date, time, timedelta
from importlib import import_module

from django.apps import apps as django_apps
from django.db import IntegrityError, transaction
from django.test import TestCase

from core.exceptions import ConflictError, DomainError
from core.models import AuditLog
from locations.models import Location, LocationClosure, LocationHours
from locations.selectors import weekly_hours
from locations.services import (
    create_closure,
    create_location,
    delete_closure,
    delete_location,
    ensure_default_location,
    set_default_location,
    set_location_hours,
    update_closure,
    update_location,
)
from subscriptions.models import Plan, Subscription
from subscriptions.services import enforce_plan_limit
from tests import factories as f


def subscribe(organization, *, maximum_locations):
    plan = Plan.objects.create(
        name=f"Plan {maximum_locations}",
        slug=f"plan-{organization.slug}",
        monthly_price=10,
        yearly_price=100,
        maximum_locations=maximum_locations,
    )
    return Subscription.objects.create(organization=organization, plan=plan)


class DefaultLocationTests(TestCase):
    def test_every_new_organization_gets_a_default_location_from_its_profile(self):
        organization = f.OrganizationFactory(
            timezone="America/Vancouver",
            address="12 Main Street\nSuite 4\nVancouver BC",
            phone="+1 604 555 0100",
            email="hello@example.test",
        )
        location = Location.objects.get(organization=organization)
        self.assertTrue(location.is_default)
        self.assertTrue(location.is_active)
        self.assertEqual(location.name, "Main")
        self.assertEqual(location.slug, "main")
        self.assertEqual(location.timezone, "America/Vancouver")
        self.assertEqual(location.address_line1, "12 Main Street")
        self.assertEqual(location.address_line2, "Suite 4, Vancouver BC")
        self.assertEqual(location.phone, "+1 604 555 0100")
        self.assertEqual(location.email, "hello@example.test")

    def test_unknown_organization_timezone_falls_back_to_utc(self):
        organization = f.OrganizationFactory(timezone="Mars/Olympus")
        self.assertEqual(Location.objects.get(organization=organization).timezone, "UTC")

    def test_ensure_default_location_is_idempotent(self):
        organization = f.OrganizationFactory()
        first = ensure_default_location(organization)
        self.assertEqual(ensure_default_location(organization), first)
        self.assertEqual(Location.objects.filter(organization=organization).count(), 1)

    def test_a_lost_default_is_restored_by_promoting_an_active_location(self):
        organization = f.OrganizationFactory()
        Location.objects.filter(organization=organization).delete()
        other = f.LocationFactory(organization=organization, name="Annex")
        self.assertEqual(ensure_default_location(organization), other)
        other.refresh_from_db()
        self.assertTrue(other.is_default)

    def test_data_migration_creates_main_for_organizations_without_one(self):
        organization = f.OrganizationFactory(timezone="Europe/Paris", address="1 Rue de Rivoli")
        Location.objects.filter(organization=organization).delete()
        untouched = f.OrganizationFactory()
        migration = import_module("locations.migrations.0002_default_locations")

        migration.create_default_locations(django_apps, None)
        migration.create_default_locations(django_apps, None)  # safe to run twice

        location = Location.objects.get(organization=organization)
        self.assertEqual(
            (location.name, location.is_default, location.timezone, location.address_line1),
            ("Main", True, "Europe/Paris", "1 Rue de Rivoli"),
        )
        self.assertEqual(Location.objects.filter(organization=untouched).count(), 1)

    def test_database_allows_only_one_default_per_organization(self):
        organization = f.OrganizationFactory()
        with self.assertRaises(IntegrityError), transaction.atomic():
            f.LocationFactory(organization=organization, is_default=True)

    def test_database_refuses_an_inactive_default(self):
        location = Location.objects.get(organization=f.OrganizationFactory())
        location.is_active = False
        with self.assertRaises(IntegrityError), transaction.atomic():
            location.save()


class LocationServiceTests(TestCase):
    def setUp(self):
        self.org = f.OrganizationFactory(timezone="America/Toronto")
        self.owner = f.MembershipFactory(organization=self.org).user
        self.main = Location.objects.get(organization=self.org)

    def test_create_location(self):
        location = create_location(
            organization=self.org, actor=self.owner, name="  Downtown ", country="ca"
        )
        self.assertEqual(location.name, "Downtown")
        self.assertEqual(location.slug, "downtown")
        self.assertEqual(location.country, "CA")
        self.assertEqual(location.timezone, "America/Toronto")
        self.assertFalse(location.is_default)
        log = AuditLog.objects.get(action="location.created")
        self.assertEqual((log.organization, log.user), (self.org, self.owner))

    def test_slugs_are_unique_per_organization(self):
        first = create_location(organization=self.org, name="Downtown")
        second = create_location(organization=self.org, name="Downtown")
        elsewhere = create_location(organization=f.OrganizationFactory(), name="Downtown")
        self.assertEqual(
            (first.slug, second.slug, elsewhere.slug), ("downtown", "downtown-2", "downtown")
        )

    def test_invalid_input(self):
        cases = [
            ({"name": " "}, "name_required"),
            ({"name": "X", "timezone": "Nowhere/City"}, "invalid_timezone"),
            ({"name": "X", "is_default": True}, "invalid"),
        ]
        for fields, code in cases:
            with self.subTest(fields=fields):
                with self.assertRaises(DomainError) as raised:
                    create_location(organization=self.org, **fields)
                self.assertEqual(raised.exception.code, code)

    def test_update_is_audited_with_the_changes(self):
        update_location(location=self.main, actor=self.owner, name="Head office", city="Toronto")
        self.main.refresh_from_db()
        self.assertEqual((self.main.name, self.main.slug), ("Head office", "main"))
        changes = AuditLog.objects.get(action="location.updated").metadata["changes"]
        self.assertEqual(changes["name"], ["Main", "Head office"])

    def test_default_location_cannot_be_deactivated_or_deleted(self):
        with self.assertRaises(ConflictError) as raised:
            update_location(location=self.main, is_active=False)
        self.assertEqual(raised.exception.code, "default_location")
        with self.assertRaises(ConflictError):
            delete_location(location=self.main)
        self.assertTrue(Location.objects.get(pk=self.main.pk).is_active)

    def test_set_default_moves_the_flag(self):
        annex = create_location(organization=self.org, name="Annex")
        set_default_location(location=annex, actor=self.owner)
        self.main.refresh_from_db()
        annex.refresh_from_db()
        self.assertFalse(self.main.is_default)
        self.assertTrue(annex.is_default)
        self.assertTrue(AuditLog.objects.filter(action="location.updated").exists())
        # The old default can now be deactivated.
        update_location(location=self.main, is_active=False)

    def test_inactive_location_cannot_become_default(self):
        annex = create_location(organization=self.org, name="Annex", is_active=False)
        with self.assertRaises(ConflictError) as raised:
            set_default_location(location=annex)
        self.assertEqual(raised.exception.code, "inactive")

    def test_delete_location(self):
        annex = create_location(organization=self.org, name="Annex")
        f.LocationHoursFactory(organization=self.org, location=annex)
        delete_location(location=annex, actor=self.owner)
        self.assertFalse(Location.objects.filter(pk=annex.pk).exists())
        self.assertEqual(AuditLog.objects.get(action="location.deleted").metadata["name"], "Annex")


class PlanLimitTests(TestCase):
    def setUp(self):
        self.org = f.OrganizationFactory()
        subscribe(self.org, maximum_locations=2)

    def test_active_locations_are_limited_by_the_plan(self):
        create_location(organization=self.org, name="Second")
        with self.assertRaises(ConflictError) as raised:
            create_location(organization=self.org, name="Third")
        self.assertEqual(raised.exception.code, "plan_limit")
        self.assertEqual(Location.objects.filter(organization=self.org).count(), 2)

    def test_inactive_locations_do_not_count(self):
        second = create_location(organization=self.org, name="Second")
        update_location(location=second, is_active=False)
        create_location(organization=self.org, name="Third")
        create_location(organization=self.org, name="Stored", is_active=False)
        with self.assertRaises(ConflictError):
            update_location(location=second, is_active=True)  # reactivating counts again

    def test_enforce_plan_limit_knows_locations(self):
        enforce_plan_limit(self.org, "locations")  # 1 of 2
        create_location(organization=self.org, name="Second")
        with self.assertRaises(ValueError):
            enforce_plan_limit(self.org, "locations")

    def test_no_subscription_means_no_limit(self):
        organization = f.OrganizationFactory()
        for n in range(3):
            create_location(organization=organization, name=f"Site {n}")
        self.assertEqual(Location.objects.filter(organization=organization).count(), 4)


class HoursTests(TestCase):
    def setUp(self):
        self.org = f.OrganizationFactory()
        self.location = Location.objects.get(organization=self.org)

    def test_set_and_replace_hours(self):
        set_location_hours(
            location=self.location,
            periods=[(0, time(13), time(18)), (0, time(9), time(12)), (5, time(10), time(14))],
        )
        week = weekly_hours(self.location)
        self.assertEqual(week[0]["periods"], [(time(9), time(12)), (time(13), time(18))])
        self.assertEqual(week[1]["periods"], [])
        self.assertEqual(week[5]["label"], "Saturday")
        self.assertEqual(
            set(LocationHours.objects.values_list("organization", flat=True)), {self.org.pk}
        )

        set_location_hours(location=self.location, periods=[(2, time(8), time(16))])
        self.assertEqual(list(self.location.hours.values_list("weekday", flat=True)), [2])
        set_location_hours(location=self.location, periods=[])
        self.assertFalse(self.location.hours.exists())

    def test_changes_are_audited_once(self):
        periods = [(0, time(9), time(17))]
        set_location_hours(location=self.location, periods=periods)
        set_location_hours(location=self.location, periods=periods)  # no change, no entry
        log = AuditLog.objects.get(action="location.hours_updated")
        self.assertEqual(log.metadata["changes"]["hours"], [[], ["Monday 09:00-17:00"]])

    def test_invalid_hours(self):
        cases = [
            ([(7, time(9), time(17))], "invalid_weekday"),
            ([(0, time(17), time(9))], "invalid_hours"),
            ([(0, time(9), time(9))], "invalid_hours"),
            ([(0, time(9), time(13)), (0, time(12), time(17))], "overlapping"),
            (
                [(0, time(8), time(9)), (0, time(10), time(11)), (0, time(12), time(13))],
                "too_many_periods",
            ),
        ]
        for periods, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(DomainError) as raised:
                    set_location_hours(location=self.location, periods=periods)
                self.assertEqual(raised.exception.code, code)
        self.assertFalse(self.location.hours.exists())

    def test_back_to_back_periods_are_allowed(self):
        set_location_hours(
            location=self.location, periods=[(0, time(9), time(12)), (0, time(12), time(17))]
        )
        self.assertEqual(self.location.hours.count(), 2)


class ClosureTests(TestCase):
    def setUp(self):
        self.org = f.OrganizationFactory()
        self.location = Location.objects.get(organization=self.org)
        self.day = date.today() + timedelta(days=10)

    def test_single_day_closure(self):
        closure = create_closure(location=self.location, start_date=self.day, reason=" Holiday ")
        self.assertEqual(
            (closure.end_date, closure.all_day, closure.reason), (self.day, True, "Holiday")
        )
        self.assertEqual(closure.organization, self.org)
        log = AuditLog.objects.get(action="location_closure.created")
        self.assertEqual(log.metadata["location"], str(self.location.pk))

    def test_partial_closure_over_several_days(self):
        closure = create_closure(
            location=self.location,
            start_date=self.day,
            end_date=self.day + timedelta(days=2),
            all_day=False,
            start_time=time(12),
            end_time=time(14),
        )
        self.assertEqual((closure.start_time, closure.end_time), (time(12), time(14)))

    def test_all_day_closure_drops_times(self):
        closure = create_closure(
            location=self.location, start_date=self.day, start_time=time(9), end_time=time(10)
        )
        self.assertIsNone(closure.start_time)

    def test_invalid_closures(self):
        cases = [
            ({"end_date": self.day - timedelta(days=1)}, "invalid_dates"),
            ({"all_day": False}, "times_required"),
            ({"all_day": False, "start_time": time(14), "end_time": time(12)}, "invalid_times"),
        ]
        for fields, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(DomainError) as raised:
                    create_closure(location=self.location, start_date=self.day, **fields)
                self.assertEqual(raised.exception.code, code)
        self.assertFalse(LocationClosure.objects.exists())

    def test_update_and_delete(self):
        closure = create_closure(location=self.location, start_date=self.day)
        update_closure(closure=closure, end_date=self.day + timedelta(days=1), reason="Works")
        closure.refresh_from_db()
        self.assertEqual(closure.end_date, self.day + timedelta(days=1))
        with self.assertRaises(DomainError):
            update_closure(closure=closure, location=f.LocationFactory())
        delete_closure(closure=closure)
        self.assertFalse(LocationClosure.objects.exists())
        self.assertEqual(
            set(AuditLog.objects.values_list("action", flat=True)),
            {"location_closure.created", "location_closure.updated", "location_closure.deleted"},
        )
