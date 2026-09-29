"""M2.6: the CRM screens. The customer list, the profile tabs, and creating, tagging and
annotating customers with htmx, under the same access rules as the API."""

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from bookings.models import Booking, Customer
from core.models import AuditLog
from crm.models import CustomerNote, CustomerTag, Tag
from crm.services import anonymize_customer, create_note
from organizations.models import OrganizationRole
from organizations.permissions import Capability
from tests import factories as f

HTMX = {"HX-Request": "true"}


def member(role, organization, **kwargs):
    return f.MembershipFactory(organization=organization, role=role, **kwargs).user


class CrmWebTestCase(TestCase):
    def setUp(self):
        self.org = f.OrganizationFactory()
        self.other_org = f.OrganizationFactory()
        self.owner = member(OrganizationRole.OWNER, self.org)
        self.receptionist = member(OrganizationRole.RECEPTIONIST, self.org)
        self.provider_profile = f.StaffProfileFactory(organization=self.org)
        self.provider = self.provider_profile.user
        f.MembershipFactory(organization=self.org, user=self.provider, role=OrganizationRole.STAFF)
        self.ada = f.CustomerFactory(
            organization=self.org, first_name="Ada", last_name="Lovelace", phone="+14165550101"
        )
        self.grace = f.CustomerFactory(
            organization=self.org, first_name="Grace", last_name="Hopper"
        )
        self.outsider = f.CustomerFactory(
            organization=self.other_org, first_name="Alan", last_name="Turing"
        )

    def detail(self, customer, tab=None):
        if tab:
            return reverse("crm-customer-tab", args=[customer.pk, tab])
        return reverse("crm-customer-detail", args=[customer.pk])


class CustomerListTests(CrmWebTestCase):
    def test_lists_only_own_organization(self):
        self.client.force_login(self.receptionist)
        response = self.client.get(reverse("crm-customer-list"))
        self.assertContains(response, "Ada Lovelace")
        self.assertContains(response, "Grace Hopper")
        self.assertNotContains(response, "Alan Turing")

    def test_search_by_phone_in_any_format_and_htmx_partial(self):
        self.client.force_login(self.receptionist)
        response = self.client.get(
            reverse("crm-customer-list"), {"q": "(416) 555-0101"}, headers=HTMX
        )
        body = response.content.decode()
        self.assertNotIn("<html", body)
        self.assertIn("Ada Lovelace", body)
        self.assertNotIn("Grace Hopper", body)

    def test_tag_and_status_filters(self):
        tag = f.TagFactory(organization=self.org, name="VIP")
        f.CustomerTagFactory(organization=self.org, customer=self.grace, tag=tag)
        anonymize_customer(customer=f.CustomerFactory(organization=self.org, first_name="Zed"))
        self.client.force_login(self.receptionist)
        tagged = self.client.get(reverse("crm-customer-list"), {"tag": str(tag.pk)})
        self.assertContains(tagged, "Grace Hopper")
        self.assertNotContains(tagged, "Ada Lovelace")
        self.assertNotContains(self.client.get(reverse("crm-customer-list")), ">Anonymized</a>")
        anonymized = self.client.get(reverse("crm-customer-list"), {"status": "anonymized"})
        self.assertContains(anonymized, ">Anonymized</a>")

    def test_a_foreign_tag_filter_is_ignored(self):
        foreign = f.TagFactory(organization=self.other_org)
        self.client.force_login(self.receptionist)
        response = self.client.get(reverse("crm-customer-list"), {"tag": str(foreign.pk)})
        self.assertContains(response, "Ada Lovelace")

    def test_provider_sees_only_their_customers_and_cannot_add(self):
        f.BookingFactory(organization=self.org, customer=self.ada, staff=self.provider_profile)
        self.client.force_login(self.provider)
        response = self.client.get(reverse("crm-customer-list"))
        self.assertContains(response, "Ada Lovelace")
        self.assertNotContains(response, "Grace Hopper")
        self.assertNotContains(response, reverse("crm-customer-new"))
        self.assertEqual(self.client.get(reverse("crm-customer-new")).status_code, 403)

    def test_members_without_customer_access_are_refused(self):
        self.client.force_login(member(OrganizationRole.CUSTOMER, self.org))
        self.assertEqual(self.client.get(reverse("crm-customer-list")).status_code, 403)
        revoked = member(
            OrganizationRole.RECEPTIONIST,
            self.org,
            revoked_permissions=[Capability.CUSTOMERS_VIEW],
        )
        self.client.force_login(revoked)
        self.assertEqual(self.client.get(reverse("crm-customer-list")).status_code, 403)
        self.assertNotContains(
            self.client.get(reverse("app-dashboard")), reverse("crm-customer-list")
        )


class CustomerCreateTests(CrmWebTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.receptionist)
        self.url = reverse("crm-customer-new")

    def test_form_opens_in_the_dialog(self):
        response = self.client.get(self.url, headers=HTMX)
        body = response.content.decode()
        self.assertNotIn("<html", body)
        self.assertIn('id="modal-title"', body)

    def test_create_with_tags_redirects_to_the_profile(self):
        tag = f.TagFactory(organization=self.org, name="New client")
        response = self.client.post(
            self.url,
            {"first_name": "Rowan", "last_name": "Hale", "phone": "416 555 0199", "tags": [tag.pk]},
            headers=HTMX,
        )
        customer = Customer.objects.get(organization=self.org, first_name="Rowan")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["HX-Redirect"], self.detail(customer))
        self.assertEqual(customer.source, Customer.Source.RECEPTION)
        self.assertEqual(customer.created_by, self.receptionist)
        self.assertTrue(CustomerTag.objects.filter(customer=customer, tag=tag).exists())
        self.assertTrue(AuditLog.objects.filter(action="customer.created").exists())

    def test_service_rules_show_as_form_errors(self):
        no_contact = self.client.post(self.url, {"first_name": "Rowan"}, headers=HTMX)
        self.assertEqual(no_contact.status_code, 422)
        self.assertContains(no_contact, "email address or a phone number", status_code=422)
        duplicate = self.client.post(
            self.url, {"first_name": "Rowan", "email": self.ada.email.upper()}, headers=HTMX
        )
        self.assertContains(duplicate, "already exists", status_code=422)
        self.assertFalse(Customer.objects.filter(first_name="Rowan").exists())

    def test_foreign_tags_and_staff_are_refused(self):
        foreign_tag = f.TagFactory(organization=self.other_org)
        foreign_staff = f.StaffProfileFactory(organization=self.other_org)
        response = self.client.post(
            self.url,
            {
                "first_name": "Rowan",
                "phone": "4165550199",
                "tags": [foreign_tag.pk],
                "assigned_staff": foreign_staff.pk,
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertFalse(Customer.objects.filter(first_name="Rowan").exists())

    def test_works_without_javascript(self):
        response = self.client.post(self.url, {"first_name": "Noah", "email": "noah@example.test"})
        customer = Customer.objects.get(first_name="Noah")
        self.assertRedirects(response, self.detail(customer))


class CustomerProfileTests(CrmWebTestCase):
    TABS = ("appointments", "notes", "communications", "forms", "transactions", "activity")

    def test_every_tab_renders(self):
        f.BookingFactory(organization=self.org, customer=self.ada)
        self.client.force_login(self.receptionist)
        self.assertContains(self.client.get(self.detail(self.ada)), "Visits")
        for tab in self.TABS:
            with self.subTest(tab=tab):
                self.assertEqual(self.client.get(self.detail(self.ada, tab)).status_code, 200)
        self.assertEqual(self.client.get(self.detail(self.ada, "secrets")).status_code, 404)

    def test_tab_switch_with_htmx_returns_only_the_tabs(self):
        self.client.force_login(self.receptionist)
        body = self.client.get(self.detail(self.ada, "notes"), headers=HTMX).content.decode()
        self.assertNotIn("<html", body)
        self.assertIn('aria-current="page"', body)

    def test_another_organizations_customer_is_not_found(self):
        self.client.force_login(self.owner)
        for tab in (None, *self.TABS):
            with self.subTest(tab=tab):
                self.assertEqual(self.client.get(self.detail(self.outsider, tab)).status_code, 404)
        self.assertEqual(
            self.client.get(reverse("crm-customer-edit", args=[self.outsider.pk])).status_code,
            404,
        )

    def test_provider_sees_own_customers_only_and_no_spend(self):
        f.BookingFactory(organization=self.org, customer=self.ada, staff=self.provider_profile)
        self.client.force_login(self.provider)
        response = self.client.get(self.detail(self.ada))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Spend to date")
        self.assertNotContains(response, reverse("crm-customer-edit", args=[self.ada.pk]))
        self.assertEqual(self.client.get(self.detail(self.grace)).status_code, 404)

    def test_provider_sees_only_their_appointments_with_the_customer(self):
        mine = f.BookingFactory(
            organization=self.org, customer=self.ada, staff=self.provider_profile
        )
        theirs = f.BookingFactory(organization=self.org, customer=self.ada)
        self.client.force_login(self.provider)
        response = self.client.get(self.detail(self.ada, "appointments"))
        self.assertContains(response, mine.reference)
        self.assertNotContains(response, theirs.reference)

    def test_overview_shows_alerts_visits_and_spend(self):
        self.ada.alerts = "Uses a wheelchair"
        self.ada.save()
        f.BookingFactory(
            organization=self.org,
            customer=self.ada,
            status=Booking.Status.COMPLETED,
            start_datetime=timezone.now() - timezone.timedelta(days=3),
            price_snapshot=120,
        )
        self.client.force_login(self.receptionist)
        response = self.client.get(self.detail(self.ada))
        self.assertContains(response, "Uses a wheelchair")
        self.assertContains(response, "120.00")
        self.assertContains(response, "predicted customer lifetime value is not calculated")

    def test_edit_updates_through_the_service(self):
        self.client.force_login(self.receptionist)
        url = reverse("crm-customer-edit", args=[self.ada.pk])
        self.assertContains(self.client.get(url, headers=HTMX), 'value="Ada"')
        response = self.client.post(
            url,
            {"first_name": "Ada", "last_name": "King", "phone": "+14165550101", "status": "active"},
            headers=HTMX,
        )
        self.assertEqual(response.status_code, 204)
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.name, "Ada King")

    def test_anonymized_customer_is_read_only(self):
        anonymize_customer(customer=self.grace)
        self.client.force_login(self.owner)
        response = self.client.get(self.detail(self.grace))
        self.assertContains(response, "was anonymized")
        self.assertNotContains(response, reverse("crm-customer-edit", args=[self.grace.pk]))
        self.assertNotContains(self.client.get(self.detail(self.grace, "notes")), "Add a note")


class TaggingTests(CrmWebTestCase):
    def setUp(self):
        super().setUp()
        self.vip = f.TagFactory(organization=self.org, name="VIP")
        self.url = reverse("crm-customer-tags", args=[self.ada.pk])

    def test_ticking_tags_saves_them_and_returns_the_section(self):
        self.client.force_login(self.receptionist)
        response = self.client.post(self.url, {"tags": [self.vip.pk]}, headers=HTMX)
        self.assertContains(response, 'id="customer-tags"')
        self.assertNotContains(response, "<html")
        self.assertIn("toast", response["HX-Trigger"])
        self.assertEqual(list(self.ada.tag_set.all()), [self.vip])
        self.client.post(self.url, {}, headers=HTMX)  # untick everything
        self.assertFalse(self.ada.tag_set.exists())

    def test_a_new_tag_is_created_once_and_added(self):
        self.client.force_login(self.receptionist)
        self.client.post(self.url, {"new_tag": "Prefers mornings"}, headers=HTMX)
        self.client.post(
            reverse("crm-customer-tags", args=[self.grace.pk]),
            {"new_tag": "prefers MORNINGS"},
            headers=HTMX,
        )
        tag = Tag.objects.get(organization=self.org, slug="prefers-mornings")
        self.assertEqual(Tag.objects.filter(slug="prefers-mornings").count(), 1)
        self.assertIn(tag, self.ada.tag_set.all())
        self.assertIn(tag, self.grace.tag_set.all())

    def test_foreign_tag_is_refused_and_nothing_changes(self):
        f.CustomerTagFactory(organization=self.org, customer=self.ada, tag=self.vip)
        foreign = f.TagFactory(organization=self.other_org)
        self.client.force_login(self.receptionist)
        response = self.client.post(self.url, {"tags": [foreign.pk]}, headers=HTMX)
        self.assertEqual(response.status_code, 204)
        self.assertIn("error", response["HX-Trigger"])
        self.assertEqual(list(self.ada.tag_set.all()), [self.vip])

    def test_provider_cannot_tag(self):
        f.BookingFactory(organization=self.org, customer=self.ada, staff=self.provider_profile)
        self.client.force_login(self.provider)
        self.assertEqual(self.client.post(self.url, {"tags": [self.vip.pk]}).status_code, 403)


class NotesTests(CrmWebTestCase):
    def add(self, customer, **data):
        payload = {"note_type": "general", "visibility": "customer_visible", "content": "Hi"}
        return self.client.post(
            reverse("crm-note-create", args=[customer.pk]), payload | data, headers=HTMX
        )

    def test_receptionist_adds_a_note_without_a_reload(self):
        self.client.force_login(self.receptionist)
        response = self.add(self.ada, content="Prefers the window room")
        self.assertContains(response, "Prefers the window room")
        self.assertContains(response, 'id="notes"')
        self.assertNotContains(response, "<html")
        note = CustomerNote.objects.get(customer=self.ada)
        self.assertEqual(note.author, self.receptionist)

    def test_receptionist_cannot_write_or_read_internal_notes(self):
        self.client.force_login(self.receptionist)
        response = self.add(self.ada, visibility="internal", content="Secret")
        self.assertEqual(response.status_code, 422)
        self.assertFalse(CustomerNote.objects.filter(content="Secret").exists())
        create_note(
            customer=self.ada, author=self.owner, content="Owner only", visibility="internal"
        )
        self.assertNotContains(self.client.get(self.detail(self.ada, "notes")), "Owner only")
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(self.detail(self.ada, "notes")), "Owner only")

    def test_internal_note_activity_is_hidden_without_the_capability(self):
        create_note(customer=self.ada, author=self.owner, content="x", visibility="internal")
        self.client.force_login(self.receptionist)
        self.assertNotContains(self.client.get(self.detail(self.ada, "activity")), "Note added")
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(self.detail(self.ada, "activity")), "Note added")

    def test_only_the_author_or_private_notes_holders_change_a_note(self):
        note = create_note(
            customer=self.ada, author=self.owner, content="Owner's", visibility="customer_visible"
        )
        self.client.force_login(self.receptionist)
        for name in ("crm-note-pin", "crm-note-delete"):
            with self.subTest(action=name):
                url = reverse(name, args=[self.ada.pk, note.pk])
                self.assertEqual(self.client.post(url, headers=HTMX).status_code, 403)
        self.assertTrue(CustomerNote.objects.filter(pk=note.pk).exists())

    def test_author_edits_pins_and_deletes(self):
        self.client.force_login(self.receptionist)
        self.add(self.ada, content="Draft")
        note = CustomerNote.objects.get(customer=self.ada)
        edit_url = reverse("crm-note-edit", args=[self.ada.pk, note.pk])
        self.assertContains(self.client.get(edit_url, headers=HTMX), 'id="modal-title"')
        response = self.client.post(
            edit_url,
            {"content": "Final", "note_type": "call", "visibility": "customer_visible"},
            headers=HTMX,
        )
        self.assertEqual(response["HX-Retarget"], "#notes")
        self.assertIn("modal:close", response["HX-Trigger-After-Swap"])
        note.refresh_from_db()
        self.assertEqual((note.content, note.note_type), ("Final", "call"))
        self.assertIsNotNone(note.edited_at)
        self.client.post(reverse("crm-note-pin", args=[self.ada.pk, note.pk]), headers=HTMX)
        note.refresh_from_db()
        self.assertTrue(note.pinned)
        self.client.post(reverse("crm-note-delete", args=[self.ada.pk, note.pk]), headers=HTMX)
        self.assertFalse(CustomerNote.objects.filter(pk=note.pk).exists())

    def test_note_of_another_customer_or_organization_is_not_found(self):
        note = create_note(customer=self.grace, author=self.receptionist, content="x")
        self.client.force_login(self.receptionist)
        wrong_customer = reverse("crm-note-edit", args=[self.ada.pk, note.pk])
        self.assertEqual(self.client.get(wrong_customer).status_code, 404)
        self.assertEqual(self.add(self.outsider).status_code, 404)

    def test_provider_writes_notes_on_own_customers_only(self):
        f.BookingFactory(organization=self.org, customer=self.ada, staff=self.provider_profile)
        self.client.force_login(self.provider)
        response = self.add(self.ada, visibility="internal", content="Treatment plan")
        self.assertContains(response, "Treatment plan")
        self.assertEqual(self.add(self.grace).status_code, 404)


class HeaderSearchTests(CrmWebTestCase):
    def test_search_dropdown_uses_the_browsing_rules(self):
        self.client.force_login(self.receptionist)
        response = self.client.get(reverse("app-search"), {"q": "Ada"}, headers=HTMX)
        body = response.content.decode()
        self.assertNotIn("<html", body)
        self.assertIn("Ada Lovelace", body)
        self.assertNotIn(
            "Alan Turing", self.client.get(reverse("app-search"), {"q": "Alan"}).content.decode()
        )

    def test_provider_search_finds_only_their_customers(self):
        f.BookingFactory(organization=self.org, customer=self.ada, staff=self.provider_profile)
        self.client.force_login(self.provider)
        self.assertContains(self.client.get(reverse("app-search"), {"q": "Ada"}), "Ada Lovelace")
        self.assertNotContains(
            self.client.get(reverse("app-search"), {"q": "Grace"}), "Grace Hopper"
        )

    def test_short_queries_ask_for_more(self):
        self.client.force_login(self.receptionist)
        self.assertContains(self.client.get(reverse("app-search"), {"q": "a"}), "at least 2")


class ReceptionistAcceptanceTests(CrmWebTestCase):
    """M2.6 acceptance, server side: every step of find, create, tag and annotate is an htmx
    request answered with a page fragment (or an HX-Redirect), never a full page.

    That the browser really swaps these fragments without reloading was checked with a
    headless Chrome walkthrough (docs/IMPLEMENTATION_PLAN.md, M2.6); this test cannot see the
    browser side (targets, swaps, history)."""

    def test_each_step_answers_with_a_fragment(self):
        self.client.force_login(self.receptionist)
        found = self.client.get(reverse("crm-customer-list"), {"q": "Lovelace"}, headers=HTMX)
        self.assertContains(found, "Ada Lovelace")

        created = self.client.post(
            reverse("crm-customer-new"),
            {"first_name": "Mia", "last_name": "Wong", "email": "mia@example.test"},
            headers=HTMX,
        )
        self.assertEqual(created.status_code, 204)
        mia = Customer.objects.get(email="mia@example.test")

        tagged = self.client.post(
            reverse("crm-customer-tags", args=[mia.pk]), {"new_tag": "New client"}, headers=HTMX
        )
        self.assertContains(tagged, "New client")

        noted = self.client.post(
            reverse("crm-note-create", args=[mia.pk]),
            {
                "content": "Referred by Ada",
                "note_type": "general",
                "visibility": "customer_visible",
            },
            headers=HTMX,
        )
        self.assertContains(noted, "Referred by Ada")
        for response in (found, tagged, noted):
            self.assertNotIn("<html", response.content.decode())
