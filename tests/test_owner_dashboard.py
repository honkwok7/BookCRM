"""M5.1: the owner and manager dashboard - numbers, filters, isolation, permissions, empty
states, caching and the query budget."""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from bookings.models import Booking
from dashboard.reporting import MIN_SAMPLE, Filters, dashboard_report
from locations.models import Location
from organizations.models import OrganizationRole
from tests import factories as f

UTC_NOON = time(12)


class Fixtures:
    def make_fixtures(self):
        cache.clear()
        self.org = f.OrganizationFactory(slug="glow", timezone="UTC")
        self.main = Location.objects.get(organization=self.org, is_default=True)
        self.annex = f.LocationFactory(organization=self.org, name="Annex")
        self.massage = f.ServiceFactory(organization=self.org, name="Massage")
        self.facial = f.ServiceFactory(organization=self.org, name="Facial")
        self.maya = f.StaffProfileFactory(organization=self.org, display_name="Maya")
        self.sam = f.StaffProfileFactory(organization=self.org, display_name="Sam")
        self.now = timezone.now()
        self.today = self.now.date()

    def at(self, days_ago, **fields):
        start = datetime.combine(self.today - timedelta(days=days_ago), UTC_NOON, tzinfo=UTC)
        options = {
            "organization": self.org,
            "service": self.massage,
            "staff": self.maya,
            "location": self.main,
            "start_datetime": start,
            "end_datetime": start + timedelta(hours=1),
        }
        options.update(fields)
        return f.BookingFactory(**options)

    def report(self, **filters):
        return dashboard_report(self.org, Filters(**filters), now=self.now)


class NumbersTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.at(0)  # today, still on
        self.at(0, status=Booking.Status.CANCELLED)  # today, cancelled: not counted as on
        statuses = (
            [Booking.Status.CANCELLED] * 2
            + [Booking.Status.NO_SHOW]
            + [Booking.Status.COMPLETED] * (MIN_SAMPLE - 3)
        )
        self.past = [
            self.at(
                days,
                status=status,
                price_snapshot=50,
                service=self.facial if days % 2 else self.massage,
            )
            for days, status in enumerate(statuses, start=1)
        ]
        f.BookingFactory(organization=f.OrganizationFactory())  # another organization

    def test_headline_numbers(self):
        report = self.report()
        self.assertEqual(report["today"], 1)
        week_start = self.today - timedelta(days=self.today.weekday())
        on_this_week = [
            b
            for b in self.past
            if b.start_datetime.date() >= week_start and b.status != Booking.Status.CANCELLED
        ]
        self.assertEqual(report["this_week"], 1 + len(on_this_week))
        self.assertEqual(
            report["past_appointments"], MIN_SAMPLE + (1 if self.now.time() > UTC_NOON else 0) * 2
        )

    def test_rates_need_enough_data(self):
        report = self.report()
        past = report["past_appointments"]
        self.assertGreaterEqual(past, MIN_SAMPLE)
        cancelled_today = 1 if self.now.time() > UTC_NOON else 0
        self.assertEqual(report["cancellation_rate"], round(100 * (2 + cancelled_today) / past, 1))
        self.assertEqual(report["no_show_rate"], round(100 * 1 / past, 1))
        Booking.objects.filter(pk__in=[b.pk for b in self.past[:10]]).delete()
        cache.clear()
        thin = self.report()
        self.assertIsNone(thin["cancellation_rate"])
        self.assertIsNone(thin["no_show_rate"])

    def test_revenue_popular_services_top_providers_and_volume(self):
        report = self.report()
        month_start = self.today.replace(day=1)
        completed = [b for b in self.past if b.status == Booking.Status.COMPLETED]
        self.assertEqual(
            report["revenue_month"],
            Decimal(50) * sum(1 for b in completed if b.start_datetime.date() >= month_start),
        )
        self.assertEqual(report["revenue_recent"], Decimal(50) * len(completed))
        services = {row["service__name"]: row["count"] for row in report["popular_services"]}
        on = [b for b in self.past if b.status != Booking.Status.CANCELLED]
        self.assertEqual(services["Facial"], sum(1 for b in on if b.service_id == self.facial.pk))
        self.assertEqual(report["top_providers"][0]["name"], "Maya")
        self.assertEqual(len(report["volume"]), 30)
        self.assertEqual(report["volume"][-1], {"day": self.today, "count": 1})
        self.assertEqual(report["volume_max"], 1)

    def test_other_organizations_never_count(self):
        other = f.OrganizationFactory()
        report = dashboard_report(other, Filters(), now=self.now)
        self.assertEqual((report["today"], report["past_appointments"]), (0, 0))


class FilterTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.at(0)
        self.at(0, staff=self.sam, location=self.annex)

    def test_location_and_provider_narrow_every_number(self):
        self.assertEqual(self.report()["today"], 2)
        self.assertEqual(self.report(location=str(self.annex.pk))["today"], 1)
        self.assertEqual(self.report(staff=str(self.maya.pk))["today"], 1)
        self.assertEqual(
            self.report(staff=str(self.maya.pk), location=str(self.annex.pk))["today"], 0
        )

    def test_the_page_ignores_ids_from_another_organization(self):
        owner = f.MembershipFactory(organization=self.org, role=OrganizationRole.OWNER).user
        self.client.force_login(owner)
        foreign = f.LocationFactory(organization=f.OrganizationFactory())
        response = self.client.get(
            reverse("app-dashboard"), {"location": str(foreign.pk), "days": "999"}
        )
        self.assertEqual(response.status_code, 200)
        filters = response.context["filters"]
        self.assertEqual((filters.location, filters.days), (None, 30))
        self.assertEqual(response.context["report"]["today"], 2)

    def test_a_linked_period_stays_selected_and_the_filter_replaces_the_widgets(self):
        owner = f.MembershipFactory(organization=self.org, role=OrganizationRole.OWNER).user
        self.client.force_login(owner)
        page = self.client.get(reverse("app-dashboard"), {"days": "7"}).content.decode()
        self.assertIn('<option value="7" selected>', page)
        self.assertIn('hx-target="#dashboard" hx-swap="outerHTML"', page)
        self.assertEqual(page.count('id="dashboard"'), 1)

    def test_recent_revenue_stops_at_the_end_of_today(self):
        # Completed early (allowed up to an hour before the start), but it is tomorrow's.
        self.at(-1, status=Booking.Status.COMPLETED, price_snapshot=70)
        self.at(0, status=Booking.Status.COMPLETED, price_snapshot=50)
        self.assertEqual(self.report()["revenue_recent"], Decimal(50))

    def test_htmx_refresh_returns_only_the_widgets(self):
        owner = f.MembershipFactory(organization=self.org, role=OrganizationRole.OWNER).user
        self.client.force_login(owner)
        response = self.client.get(
            reverse("app-dashboard"), {"staff": str(self.sam.pk)}, HTTP_HX_REQUEST="true"
        )
        body = response.content.decode()
        self.assertIn('id="dashboard"', body)
        self.assertNotIn("<html", body)


class AccessTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.at(-1)  # tomorrow: an upcoming appointment

    def page(self, role):
        self.client.force_login(f.MembershipFactory(organization=self.org, role=role).user)
        return self.client.get(reverse("app-dashboard"))

    def test_owners_and_managers_see_the_analytics(self):
        for role in (OrganizationRole.OWNER, OrganizationRole.MANAGER):
            with self.subTest(role=role):
                response = self.page(role)
                self.assertIsNotNone(response.context["report"])
                self.assertContains(response, "Estimated from completed appointments")

    def test_reception_sees_only_the_operational_part(self):
        response = self.page(OrganizationRole.RECEPTIONIST)
        self.assertIsNone(response.context["report"])
        self.assertNotContains(response, "Estimated revenue")
        self.assertNotContains(response, "Top providers")
        self.assertEqual(len(response.context["upcoming"]), 1)

    def test_empty_organization_shows_empty_states(self):
        empty = f.OrganizationFactory()
        self.client.force_login(
            f.MembershipFactory(organization=empty, role=OrganizationRole.OWNER).user
        )
        response = self.client.get(reverse("app-dashboard"))
        self.assertContains(response, "No appointments in this period")
        self.assertContains(response, "No upcoming appointments")
        self.assertContains(response, "Not enough data yet")


class BudgetTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        for days in range(-3, 25):
            self.at(days, staff=self.sam if days % 2 else self.maya)
        self.owner = f.MembershipFactory(organization=self.org, role=OrganizationRole.OWNER).user
        self.client.force_login(self.owner)

    def test_the_page_renders_in_twelve_queries_or_fewer(self):
        cache.clear()
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse("app-dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(queries), 12, [q["sql"][:80] for q in queries])

    def test_the_numbers_are_cached_per_organization_and_filter(self):
        cache.clear()
        self.report()
        with CaptureQueriesContext(connection) as queries:
            self.report()
        self.assertEqual(len(queries), 0)
        with CaptureQueriesContext(connection) as queries:
            self.report(staff=str(self.sam.pk))
        self.assertGreater(len(queries), 0)
