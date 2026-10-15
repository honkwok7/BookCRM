"""The calendar's team panel: tick the providers to show; time off labelled in the day view."""

from datetime import timedelta

from django.test import TestCase

from organizations.models import OrganizationRole
from scheduling.models import TimeOff
from tests import factories as f
from tests.test_calendar_working_hours import MONDAY, Fixtures, at


class TeamPanelTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.client.force_login(self.owner)

    def calendar(self, **params):
        return self.client.get("/app/calendar/", {"date": MONDAY.isoformat(), **params})

    def test_everyone_is_listed_and_ticked_by_default(self):
        response = self.calendar(view="day")
        self.assertContains(response, "All team (2)")
        for member in (self.maya, self.sam):
            self.assertContains(
                response, f'name="staff" value="{member.pk}" data-cal-member checked'
            )
        self.assertContains(response, "data-cal-all checked")
        self.assertContains(response, "data-cal-column", count=2)

    def test_ticked_providers_only(self):
        response = self.calendar(view="day", picked="1", staff=[str(self.sam.pk)])
        self.assertContains(response, "data-cal-column", count=1)
        self.assertContains(response, f'value="{self.sam.pk}" data-cal-member checked')
        self.assertContains(response, f'value="{self.maya.pk}" data-cal-member>')
        self.assertNotContains(response, "data-cal-all checked")
        both = self.calendar(view="day", picked="1", staff=[str(self.sam.pk), str(self.maya.pk)])
        self.assertContains(both, "data-cal-column", count=2)

    def test_ticking_nobody_shows_nobody(self):
        response = self.calendar(view="week", picked="1")
        self.assertContains(response, "Nobody is ticked")
        self.assertNotContains(response, "data-cal-column")

    def test_week_shading_follows_the_ticked_providers(self):
        response = self.calendar(view="week", picked="1", staff=[str(self.sam.pk)])
        # Sam 12-20 only: rows from 12:00 (row 17 from 08:00), 32 rows.
        self.assertContains(response, "cal-open col-span-full row-start-17 row-span-32", count=5)

    def test_providers_without_view_all_get_no_team_list(self):
        provider = f.MembershipFactory(organization=self.org, role=OrganizationRole.STAFF).user
        self.maya.user = provider
        self.maya.save()
        self.client.force_login(provider)
        response = self.calendar(view="week")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "data-cal-team")

    def test_initials_stand_in_for_photos(self):
        response = self.calendar(view="day")
        self.assertContains(response, ">M</span>")
        self.assertContains(response, ">S</span>")


class AwayBlockTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.client.force_login(self.owner)

    def day(self, day=MONDAY):
        return self.client.get("/app/calendar/", {"view": "day", "date": day.isoformat()})

    def test_time_off_is_labelled(self):
        f.TimeOffFactory(
            organization=self.org,
            staff=self.sam,
            start_datetime=at(MONDAY, 13),
            end_datetime=at(MONDAY, 15),
            reason="Dentist",
            approval_status=TimeOff.ApprovalStatus.APPROVED,
        )
        f.TimeOffFactory(
            organization=self.org,
            staff=self.maya,
            start_datetime=at(MONDAY, 9),
            end_datetime=at(MONDAY, 10),
            reason="Not approved yet",
            approval_status=TimeOff.ApprovalStatus.PENDING,
        )
        response = self.day()
        # 13:00 is row 21 from 08:00; two hours is 8 rows.
        self.assertContains(
            response,
            'cal-away col-span-full row-start-21 row-span-8"><span class="truncate">Dentist',
        )
        self.assertNotContains(response, "Not approved yet")

    def test_a_day_off_covers_the_column(self):
        tuesday = MONDAY + timedelta(days=1)
        f.AvailabilityExceptionFactory(
            organization=self.org, staff=self.maya, date=tuesday, reason="Vacation"
        )
        response = self.day(tuesday)
        self.assertContains(response, "cal-away col-span-full row-start-1 ")
        self.assertContains(response, "Vacation")
