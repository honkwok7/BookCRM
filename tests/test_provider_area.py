"""M5.3: the provider area - access, strict own scope (whatever the role), the provider's day,
time-off requests and their approval, blocked time, and the query budget."""

from datetime import time, timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from bookings.models import Booking
from bookings.services import create_booking
from core.audit import AuditAction
from core.exceptions import DomainError
from core.models import AuditLog
from organizations.models import OrganizationRole
from scheduling.availability import AvailabilityService
from scheduling.models import TimeOff
from scheduling.services import block_time, decide_time_off, request_time_off
from tests import factories as f

Status = TimeOff.ApprovalStatus
PAGES = ("staff-dashboard", "staff-calendar", "staff-customers", "staff-availability")


class Fixtures:
    def make_fixtures(self):
        self.org = f.OrganizationFactory(slug="glow", timezone="UTC")
        self.service = f.ServiceFactory(organization=self.org, name="Massage", duration_minutes=60)
        self.maya = self.provider("Maya", OrganizationRole.STAFF)
        self.sam = self.provider("Sam", OrganizationRole.STAFF)
        self.noon = f.future(1, hour=12)

    def provider(self, name, role):
        user = f.UserFactory(first_name=name)
        f.MembershipFactory(organization=self.org, user=user, role=role)
        profile = f.StaffProfileFactory(organization=self.org, user=user, display_name=name)
        f.make_bookable(profile, self.service, start=time(8), end=time(18))
        return profile

    def book(self, staff, hour, email):
        return create_booking(
            organization=self.org,
            service=self.service,
            staff_profile=staff,
            customer_name=f"{staff.display_name} customer {hour}",
            customer_email=email,
            start_datetime=self.noon.replace(hour=hour),
            notify=False,
        )

    def at(self, moment):
        return patch("django.utils.timezone.now", return_value=moment)


class AccessTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def status(self, user, url_name):
        self.client.force_login(user)
        return self.client.get(reverse(url_name)).status_code

    def test_providers_of_any_role_open_every_page(self):
        owner = self.provider("Olga", OrganizationRole.OWNER)
        for profile in (self.maya, owner):
            for url_name in PAGES:
                with self.subTest(role=profile.display_name, page=url_name):
                    self.assertEqual(self.status(profile.user, url_name), 200)

    def test_members_without_a_profile_are_refused(self):
        receptionist = f.MembershipFactory(
            organization=self.org, role=OrganizationRole.RECEPTIONIST
        ).user
        for url_name in PAGES:
            with self.subTest(page=url_name):
                self.assertEqual(self.status(receptionist, url_name), 403)
        # Staff-role members without a profile land on their (empty) day, nothing else.
        newcomer = f.MembershipFactory(organization=self.org, role=OrganizationRole.STAFF).user
        self.client.force_login(newcomer)
        self.assertContains(self.client.get(reverse("staff-dashboard")), "take appointments here")
        for url_name in PAGES[1:]:
            with self.subTest(page=url_name):
                self.assertEqual(self.status(newcomer, url_name), 403)

    def test_customers_cannot(self):
        customer = f.MembershipFactory(organization=self.org, role=OrganizationRole.CUSTOMER).user
        for url_name in PAGES:
            with self.subTest(page=url_name):
                self.assertEqual(self.status(customer, url_name), 403)


class OwnScopeTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        # An owner who also takes appointments sees everything in /app/, only theirs here.
        self.olga = self.provider("Olga", OrganizationRole.OWNER)
        self.mine = self.book(self.olga, 10, "mine@x.test")
        self.theirs = self.book(self.maya, 11, "theirs@x.test")
        self.client.force_login(self.olga.user)

    def test_the_day_shows_only_their_appointments(self):
        with self.at(self.noon.replace(hour=8)):
            response = self.client.get(reverse("staff-dashboard"))
        self.assertContains(response, "Olga customer 10")
        self.assertNotContains(response, "Maya customer 11")

    def test_the_calendar_shows_only_their_appointments(self):
        day = self.noon.date().isoformat()
        response = self.client.get(reverse("staff-calendar"), {"view": "day", "date": day})
        self.assertContains(response, "Olga customer 10")
        self.assertNotContains(response, "Maya customer 11")
        # Asking for another provider changes nothing.
        response = self.client.get(
            reverse("staff-calendar"), {"view": "day", "date": day, "staff": str(self.maya.pk)}
        )
        self.assertNotContains(response, "Maya customer 11")
        # The organization calendar still shows everyone to the owner.
        response = self.client.get(reverse("app-calendar"), {"view": "day", "date": day})
        self.assertContains(response, "Maya customer 11")

    def test_customers_are_only_theirs(self):
        assigned = f.CustomerFactory(
            organization=self.org, assigned_staff=self.olga, first_name="Assigned", last_name="One"
        )
        stranger = f.CustomerFactory(organization=self.org, first_name="Nobody", last_name="Here")
        response = self.client.get(reverse("staff-customers"))
        self.assertContains(response, self.mine.customer.name)
        self.assertContains(response, assigned.name)
        self.assertNotContains(response, self.theirs.customer.name)
        self.assertNotContains(response, stranger.name)
        self.assertNotContains(response, reverse("crm-customer-new"))

    def test_another_providers_appointment_is_a_404_for_a_provider(self):
        self.client.force_login(self.maya.user)
        mine = self.client.get(reverse("app-appointment", args=[self.theirs.pk]))
        self.assertEqual(mine.status_code, 200)
        theirs = self.client.get(reverse("app-appointment", args=[self.mine.pk]))
        self.assertEqual(theirs.status_code, 404)
        action = self.client.post(
            reverse("app-appointment-action", args=[self.mine.pk]), {"action": "confirm"}
        )
        self.assertEqual(action.status_code, 404)


class DayTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.first = self.book(self.maya, 9, "a@x.test")
        self.second = self.book(self.maya, 14, "b@x.test")
        self.client.force_login(self.maya.user)

    def test_greeting_next_and_counts(self):
        with self.at(self.noon.replace(hour=8)):
            response = self.client.get(reverse("staff-dashboard"))
        self.assertContains(response, "Good morning, Maya")
        day = response.context["day"]
        self.assertEqual(day["next"], self.first)
        self.assertEqual([b.pk for b in day["today_rows"]], [self.first.pk, self.second.pk])
        self.assertEqual((day["week_count"], day["seen_count"]), (2, 0))
        with self.at(self.noon.replace(hour=19)):
            self.assertContains(self.client.get(reverse("staff-dashboard")), "Good evening, Maya")

    def test_check_in_from_the_day_comes_back_to_it(self):
        with self.at(self.first.start_datetime - timedelta(minutes=10)):
            response = self.client.post(
                reverse("app-appointment-action", args=[self.first.pk]),
                {"action": "check_in", "next": reverse("staff-dashboard")},
            )
        self.assertRedirects(response, reverse("staff-dashboard"), fetch_redirect_response=False)
        self.first.refresh_from_db()
        self.assertEqual(self.first.status, Booking.Status.CHECKED_IN)

    def test_the_day_stays_within_budget(self):
        with self.at(self.noon.replace(hour=8)), CaptureQueriesContext(connection) as queries:
            self.client.get(reverse("staff-dashboard"))
        self.assertLessEqual(len(queries), 14, [q["sql"][:80] for q in queries])


class TimeOffTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.manager = f.MembershipFactory(
            organization=self.org, role=OrganizationRole.MANAGER
        ).user
        self.client.force_login(self.maya.user)
        self.day = self.noon.date()

    def request_days(self, first, last, **extra):
        return self.client.post(
            reverse("staff-availability"),
            {
                "form": "request",
                "request-first_day": first.isoformat(),
                "request-last_day": last.isoformat(),
                "request-reason": "Holiday",
                **extra,
            },
        )

    def slots(self):
        engine = AvailabilityService(self.org, self.service)
        return engine.get_available_slots(self.day, self.day, staff=self.maya)

    def test_a_request_blocks_nothing_until_approved(self):
        response = self.request_days(self.day, self.day)
        self.assertRedirects(response, reverse("staff-availability"))
        entry = TimeOff.objects.get(staff=self.maya)
        self.assertEqual(entry.approval_status, Status.PENDING)
        self.assertTrue(self.slots())
        # Providers can't approve their own; the manager's button does.
        url = reverse("app-staff-time-off-decide", args=[self.maya.pk, entry.pk])
        self.assertEqual(self.client.post(url, {"decision": "approve"}).status_code, 403)
        self.client.force_login(self.manager)
        self.assertContains(
            self.client.get(reverse("app-staff-tab", args=[self.maya.pk, "time-off"])), "Approve"
        )
        self.client.post(url, {"decision": "approve"})
        entry.refresh_from_db()
        self.assertEqual(entry.approval_status, Status.APPROVED)
        self.assertEqual(self.slots(), [])
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditAction.TIME_OFF_APPROVED, object_identifier=str(entry.pk)
            ).exists()
        )

    def test_rejected_and_decided_twice(self):
        entry = request_time_off(
            staff=self.maya,
            start=self.noon,
            end=self.noon + timedelta(hours=2),
            actor=self.maya.user,
        )
        decide_time_off(entry=entry, approve=False, actor=self.manager)
        entry.refresh_from_db()
        self.assertEqual(entry.approval_status, Status.REJECTED)
        with self.assertRaises(ValidationError):
            decide_time_off(entry=entry, approve=True, actor=self.manager)

    def test_last_day_before_first_is_an_error(self):
        response = self.request_days(self.day, self.day - timedelta(days=1))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(TimeOff.objects.exists())

    def test_cancel_own_but_not_someone_elses(self):
        mine = request_time_off(
            staff=self.maya, start=self.noon, end=self.noon + timedelta(hours=1)
        )
        theirs = request_time_off(
            staff=self.sam, start=self.noon, end=self.noon + timedelta(hours=1)
        )
        response = self.client.post(reverse("staff-time-off-cancel", args=[theirs.pk]))
        self.assertEqual(response.status_code, 404)
        self.client.post(reverse("staff-time-off-cancel", args=[mine.pk]))
        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertEqual(
            (mine.approval_status, theirs.approval_status), (Status.CANCELLED, Status.PENDING)
        )

    def test_the_decide_url_checks_the_staff_member(self):
        entry = request_time_off(
            staff=self.sam, start=self.noon, end=self.noon + timedelta(hours=1)
        )
        self.client.force_login(self.manager)
        url = reverse("app-staff-time-off-decide", args=[self.maya.pk, entry.pk])
        self.assertEqual(self.client.post(url, {"decision": "approve"}).status_code, 404)


class BlockTimeTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.client.force_login(self.maya.user)

    def block(self, start, end):
        return self.client.post(
            reverse("staff-availability"),
            {
                "form": "block",
                "block-date": self.noon.date().isoformat(),
                "block-start": start,
                "block-end": end,
                "block-reason": "Lunch",
            },
        )

    def test_blocked_time_is_approved_at_once_and_closes_the_calendar(self):
        response = self.block("12:00", "13:00")
        self.assertRedirects(response, reverse("staff-availability"))
        entry = TimeOff.objects.get(staff=self.maya)
        self.assertEqual(
            (entry.approval_status, entry.start_datetime, entry.reason),
            (Status.APPROVED, self.noon, "Lunch"),
        )
        with self.assertRaises(DomainError):
            self.book(self.maya, 12, "late@x.test")
        self.assertTrue(
            AuditLog.objects.filter(action=AuditAction.TIME_BLOCKED, user=self.maya.user).exists()
        )

    def test_it_is_refused_over_an_appointment(self):
        self.book(self.maya, 12, "a@x.test")
        response = self.block("11:30", "12:30")
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "You have 1 appointment then", status_code=400)
        self.assertFalse(TimeOff.objects.exists())

    def test_limits(self):
        with self.assertRaises(ValidationError):  # longer than 12 hours
            block_time(staff=self.maya, start=self.noon, end=self.noon + timedelta(hours=13))
        with self.assertRaises(ValidationError):  # already over
            block_time(
                staff=self.maya,
                start=self.noon - timedelta(days=3),
                end=self.noon - timedelta(days=3, hours=-1),
            )
        with self.assertRaises(ValidationError):  # backwards
            block_time(staff=self.maya, start=self.noon, end=self.noon - timedelta(hours=1))
