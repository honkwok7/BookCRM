"""M3.3: the services screen, under the same access rules as the API."""

from django.test import TestCase
from django.urls import reverse

from locations.models import Location
from locations.services import create_location
from organizations.models import OrganizationRole
from services.models import Service, ServiceCategory
from services.services import create_category, create_service
from tests import factories as f

HTMX = {"HX-Request": "true"}


def member(role, organization):
    return f.MembershipFactory(organization=organization, role=role).user


def service_post(**overrides):
    data = {
        "name": "Hot Stone",
        "duration_minutes": "75",
        "price": "140",
        "is_public": "on",
        "is_active": "on",
        "min_notice_minutes": "60",
        "max_advance_days": "30",
        "cancellation_deadline_hours": "24",
        "rescheduling_deadline_hours": "24",
    }
    data.update(overrides)
    return data


class ServiceScreenTests(TestCase):
    def setUp(self):
        self.org = f.OrganizationFactory()
        self.main = Location.objects.get(organization=self.org)
        self.downtown = create_location(organization=self.org, name="Downtown")
        self.owner = member(OrganizationRole.OWNER, self.org)
        self.body = create_category(organization=self.org, name="Body", sort_order=1)
        self.face = create_category(organization=self.org, name="Face", sort_order=0)
        self.massage = create_service(
            organization=self.org,
            name="Massage",
            category=self.body,
            duration_minutes=60,
            price=100,
        )
        self.facial = create_service(
            organization=self.org,
            name="Facial",
            category=self.face,
            duration_minutes=45,
            price=90,
            locations=[self.downtown],
        )
        self.outsider = f.ServiceFactory(name="Outsider service")

    def test_list_groups_by_category_in_order(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("app-service-list")).content.decode()
        self.assertLess(page.index("Face"), page.index("Body"))
        self.assertIn("Downtown", page)
        self.assertIn("All locations", page)
        self.assertNotIn("Outsider service", page)
        self.assertIn(reverse("app-service-new"), page)

    def test_filters(self):
        self.client.force_login(self.owner)
        page = self.client.get(
            reverse("app-service-list"), {"location": str(self.main.pk)}, headers=HTMX
        ).content.decode()
        self.assertIn("Massage", page)
        self.assertNotIn("Facial", page)

    def test_read_only_roles(self):
        for role in (OrganizationRole.RECEPTIONIST, OrganizationRole.STAFF):
            with self.subTest(role=role):
                self.client.force_login(member(role, self.org))
                page = self.client.get(reverse("app-service-list"))
                self.assertEqual(page.status_code, 200)
                self.assertNotIn(reverse("app-service-new"), page.content.decode())
                for url in (
                    reverse("app-service-new"),
                    reverse("app-service-edit", args=[self.massage.pk]),
                    reverse("app-service-category-new"),
                    reverse("app-service-category-delete", args=[self.body.pk]),
                ):
                    self.assertEqual(self.client.post(url, service_post()).status_code, 403)
        self.client.force_login(member(OrganizationRole.CUSTOMER, self.org))
        self.assertEqual(self.client.get(reverse("app-service-list")).status_code, 403)

    def test_other_organization_is_404(self):
        self.client.force_login(self.owner)
        url = reverse("app-service-edit", args=[self.outsider.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        url = reverse("app-service-category-edit", args=[self.outsider.category.pk])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_add_and_edit_service(self):
        self.client.force_login(self.owner)
        form = self.client.get(
            reverse("app-service-new"), {"category": str(self.body.pk)}, headers=HTMX
        ).content.decode()
        self.assertIn(f'<option value="{self.body.pk}" selected>', form)
        response = self.client.post(
            reverse("app-service-new"),
            service_post(category=str(self.body.pk), locations=[str(self.main.pk)]),
            headers=HTMX,
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["HX-Redirect"], reverse("app-service-list"))
        service = Service.objects.get(name="Hot Stone")
        self.assertEqual(list(service.locations.all()), [self.main])
        self.assertEqual((service.tax_rate, service.buffer_before_minutes), (0, 0))

        response = self.client.post(
            reverse("app-service-edit", args=[service.pk]),
            service_post(name="Hot Stone 90", duration_minutes="90", is_archived="on"),
            headers=HTMX,
        )
        self.assertEqual(response.status_code, 204)
        service.refresh_from_db()
        self.assertEqual((service.duration_minutes, service.is_archived), (90, True))
        self.assertFalse(service.locations.exists())

    def test_invalid_service_is_422(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("app-service-new"), service_post(duration_minutes="0"), headers=HTMX
        )
        self.assertEqual(response.status_code, 422)

    def test_categories(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("app-service-category-new"),
            {"name": "Hair", "color": "#3b82f6", "sort_order": "5"},
            headers=HTMX,
        )
        self.assertEqual(response.status_code, 204)
        response = self.client.post(
            reverse("app-service-category-new"), {"name": "hair", "color": "#3b82f6"}, headers=HTMX
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("already exists", response.content.decode())

        response = self.client.post(
            reverse("app-service-category-delete", args=[self.body.pk]), headers=HTMX
        )
        self.assertEqual(response.status_code, 204)
        self.assertFalse(ServiceCategory.objects.filter(pk=self.body.pk).exists())
        self.massage.refresh_from_db()
        self.assertIsNone(self.massage.category)
