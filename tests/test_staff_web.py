"""M3.2: the staff screens, under the same access rules as the API."""

from django.test import TestCase
from django.urls import reverse

from locations.models import Location
from locations.services import create_location
from organizations.models import OrganizationRole
from staff.forms import StaffServicesForm
from staff.models import StaffProfile, StaffServiceOffering
from staff.services import add_offering, create_staff_profile
from tests import factories as f

HTMX = {"HX-Request": "true"}


def member(role, organization, **kwargs):
    return f.MembershipFactory(organization=organization, role=role, **kwargs).user


class StaffWebTestCase(TestCase):
    def setUp(self):
        self.org = f.OrganizationFactory()
        self.main = Location.objects.get(organization=self.org)
        self.downtown = create_location(organization=self.org, name="Downtown")
        self.owner = member(OrganizationRole.OWNER, self.org)
        self.maya_user = f.UserFactory(first_name="Maya", last_name="Chen")
        f.MembershipFactory(organization=self.org, user=self.maya_user, role=OrganizationRole.STAFF)
        self.maya = create_staff_profile(
            organization=self.org, user=self.maya_user, locations=[self.main, self.downtown]
        )
        self.massage = f.ServiceFactory(organization=self.org, name="Massage")
        self.facial = f.ServiceFactory(organization=self.org, name="Facial")
        self.outsider = f.StaffProfileFactory()


class StaffPagesTests(StaffWebTestCase):
    def test_list_and_tabs(self):
        add_offering(staff=self.maya, service=self.massage, location=self.downtown)
        self.client.force_login(self.owner)
        page = self.client.get(reverse("app-staff-list")).content.decode()
        self.assertIn("Maya Chen", page)
        self.assertIn(reverse("app-staff-new"), page)
        self.assertNotIn(str(self.outsider.pk), page)

        for tab, text in (
            ("profile", "Name shown to customers"),
            ("services", "Massage"),
            ("locations", "Downtown"),
            ("availability", "No availability set"),
            ("time-off", "No upcoming time off"),
        ):
            with self.subTest(tab=tab):
                url = (
                    reverse("app-staff-detail", args=[self.maya.pk])
                    if tab == "profile"
                    else reverse("app-staff-tab", args=[self.maya.pk, tab])
                )
                response = self.client.get(url, headers=HTMX)
                self.assertEqual(response.status_code, 200)
                self.assertIn(text, response.content.decode())
        self.assertEqual(
            self.client.get(reverse("app-staff-tab", args=[self.maya.pk, "nope"])).status_code, 404
        )

    def test_filters(self):
        daniel_user = member(OrganizationRole.STAFF, self.org, user__first_name="Daniel")
        create_staff_profile(organization=self.org, user=daniel_user)  # Main only
        self.client.force_login(self.owner)
        page = self.client.get(
            reverse("app-staff-list"), {"location": str(self.downtown.pk)}, headers=HTMX
        ).content.decode()
        self.assertIn("Maya", page)
        self.assertNotIn("Daniel", page)

    def test_read_only_roles(self):
        for role in (OrganizationRole.RECEPTIONIST, OrganizationRole.STAFF):
            with self.subTest(role=role):
                self.client.force_login(member(role, self.org))
                page = self.client.get(reverse("app-staff-detail", args=[self.maya.pk]))
                self.assertEqual(page.status_code, 200)
                self.assertNotIn(
                    reverse("app-staff-edit", args=[self.maya.pk]), page.content.decode()
                )
                for name in ("app-staff-edit", "app-staff-services", "app-staff-locations"):
                    url = reverse(name, args=[self.maya.pk])
                    self.assertEqual(self.client.get(url).status_code, 403)
                    self.assertEqual(self.client.post(url, {}).status_code, 403)
        self.client.force_login(member(OrganizationRole.CUSTOMER, self.org))
        self.assertEqual(self.client.get(reverse("app-staff-list")).status_code, 403)

    def test_other_organization_staff_is_404(self):
        self.client.force_login(self.owner)
        for name in ("app-staff-detail", "app-staff-edit", "app-staff-services"):
            with self.subTest(page=name):
                self.assertEqual(
                    self.client.get(reverse(name, args=[self.outsider.pk])).status_code, 404
                )


class StaffFormsTests(StaffWebTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_add_staff_member(self):
        daniel = member(OrganizationRole.STAFF, self.org, user__first_name="Daniel")
        form = self.client.get(reverse("app-staff-new"), headers=HTMX).content.decode()
        self.assertIn("Daniel", form)
        self.assertNotIn("Maya", form)  # already a staff member
        response = self.client.post(
            reverse("app-staff-new"),
            {"user": str(daniel.pk), "job_title": "Chiropractor", "locations": [str(self.main.pk)]},
            headers=HTMX,
        )
        self.assertEqual(response.status_code, 204)
        staff = StaffProfile.objects.get(user=daniel)
        self.assertEqual(
            response["HX-Redirect"], reverse("app-staff-tab", args=[staff.pk, "services"])
        )
        self.assertEqual(list(staff.locations.all()), [self.main])

    def test_add_form_rejects_people_outside_the_team(self):
        response = self.client.post(
            reverse("app-staff-new"), {"user": str(f.UserFactory().pk)}, headers=HTMX
        )
        self.assertEqual(response.status_code, 422)

    def test_nobody_left_to_add(self):
        create_staff_profile(organization=self.org, user=self.owner)  # owners can see customers too
        response = self.client.get(reverse("app-staff-new"), headers=HTMX).content.decode()
        self.assertIn("Everyone on your team is already listed", response)

    def test_edit_profile(self):
        response = self.client.post(
            reverse("app-staff-edit", args=[self.maya.pk]),
            {
                "display_name": "Maya C.",
                "is_active": "on",
                "is_accepting_bookings": "on",
                "max_daily_appointments": "6",
            },
            headers=HTMX,
        )
        self.assertEqual(response.status_code, 204)
        self.maya.refresh_from_db()
        self.assertEqual(
            (
                self.maya.public_name,
                self.maya.online_booking_visible,
                self.maya.max_daily_appointments,
            ),
            ("Maya C.", False, 6),
        )

    def test_edit_locations(self):
        add_offering(staff=self.maya, service=self.massage, location=self.downtown)
        response = self.client.post(
            reverse("app-staff-locations", args=[self.maya.pk]),
            {"locations": [str(self.main.pk)]},
            headers=HTMX,
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(list(self.maya.locations.all()), [self.main])
        self.assertFalse(StaffServiceOffering.objects.exists())

    def test_edit_services(self):
        url = reverse("app-staff-services", args=[self.maya.pk])
        form = self.client.get(url, headers=HTMX).content.decode()
        self.assertIn("Facial", form)
        layout = StaffServicesForm(staff=self.maya)
        facial = layout.services.index(self.facial)
        massage = layout.services.index(self.massage)
        main, downtown = layout.locations.index(self.main), layout.locations.index(self.downtown)
        response = self.client.post(
            url,
            {
                f"s{facial}_l{downtown}": "on",
                f"s{massage}_l{main}": "on",
                f"s{massage}_l{downtown}": "on",
                f"s{massage}_price": "80",
            },
            headers=HTMX,
        )
        self.assertEqual(response.status_code, 204)
        offerings = set(
            self.maya.offerings.values_list("service__name", "location__name", "custom_price")
        )
        self.assertEqual(offerings, {("Facial", "Downtown", None), ("Massage", None, 80)})

        form = self.client.get(url, headers=HTMX).content.decode()
        self.assertIn(f'name="s{massage}_l{main}" id="id_s{massage}_l{main}" checked', form)

        response = self.client.post(
            url, {f"s{massage}_l{main}": "on", f"s{massage}_duration": "0"}, headers=HTMX
        )
        self.assertEqual(response.status_code, 422)
