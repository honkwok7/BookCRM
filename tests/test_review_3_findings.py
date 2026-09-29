"""Regression tests for the third codebase review (Codex, 2026-09-29): findings F1-F5 on the
M2.5 web shell and the M2.6 CRM screens."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic
from unittest import mock, skipUnless

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts import services as account_services
from crm.models import CustomerNote, Tag
from crm.services import create_note, get_or_create_tag, update_note
from organizations.models import OrganizationMembership, OrganizationRole
from organizations.permissions import Capability
from tests import factories as f

User = get_user_model()
HTMX = {"HX-Request": "true"}


class F1NoteVisibilityTimelineTests(TestCase):
    """Changing a note's visibility moves its timeline entry with it."""

    def setUp(self):
        self.org = f.OrganizationFactory()
        self.owner = f.MembershipFactory(organization=self.org).user
        self.receptionist = f.MembershipFactory(
            organization=self.org, role=OrganizationRole.RECEPTIONIST
        ).user
        self.customer = f.CustomerFactory(organization=self.org)
        self.activity_url = reverse("crm-customer-tab", args=[self.customer.pk, "activity"])

    def receptionist_sees_note_entry(self):
        self.client.force_login(self.receptionist)
        return "Note added" in self.client.get(self.activity_url).content.decode()

    def test_made_internal_hides_the_entry(self):
        note = create_note(
            customer=self.customer,
            author=self.receptionist,
            content="x",
            visibility="customer_visible",
        )
        self.assertTrue(self.receptionist_sees_note_entry())
        update_note(note=note, actor=self.owner, visibility=CustomerNote.Visibility.INTERNAL)
        self.assertFalse(self.receptionist_sees_note_entry())

    def test_made_visible_shows_the_entry(self):
        note = create_note(customer=self.customer, author=self.owner, content="x")
        self.assertFalse(self.receptionist_sees_note_entry())
        update_note(
            note=note, actor=self.owner, visibility=CustomerNote.Visibility.CUSTOMER_VISIBLE
        )
        self.assertTrue(self.receptionist_sees_note_entry())


class F2InternalNoteAuthorTests(TestCase):
    """An author who may no longer write internal notes can no longer change their own."""

    def setUp(self):
        self.org = f.OrganizationFactory()
        profile = f.StaffProfileFactory(organization=self.org)
        self.author = profile.user
        self.membership = f.MembershipFactory(
            organization=self.org, user=self.author, role=OrganizationRole.STAFF
        )
        self.customer = f.CustomerFactory(organization=self.org, assigned_staff=profile)
        self.note = create_note(customer=self.customer, author=self.author, content="Private plan")
        self.client.force_login(self.author)

    def test_provider_may_change_own_internal_note(self):
        response = self.client.post(
            reverse("crm-note-pin", args=[self.customer.pk, self.note.pk]), headers=HTMX
        )
        self.assertEqual(response.status_code, 200)

    def test_after_becoming_a_receptionist_the_author_cannot_change_it(self):
        OrganizationMembership.objects.filter(pk=self.membership.pk).update(
            role=OrganizationRole.RECEPTIONIST
        )
        args = [self.customer.pk, self.note.pk]
        for name, method in (("crm-note-edit", "get"), ("crm-note-pin", "post")):
            with self.subTest(action=name):
                response = getattr(self.client, method)(reverse(name, args=args), headers=HTMX)
                self.assertEqual(response.status_code, 403)
        response = self.client.post(reverse("crm-note-delete", args=args), headers=HTMX)
        self.assertEqual(response.status_code, 403)
        api = APIClient()
        api.force_authenticate(self.author)
        response = api.patch(f"/api/v1/customer-notes/{self.note.pk}/", {"pinned": True})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(CustomerNote.objects.filter(pk=self.note.pk, pinned=False).exists())

    def test_manager_who_lost_the_capability_cannot_change_own_internal_note(self):
        manager_membership = f.MembershipFactory(
            organization=self.org,
            role=OrganizationRole.MANAGER,
            revoked_permissions=[Capability.CUSTOMERS_NOTES_PRIVATE],
        )
        note = create_note(
            customer=self.customer, author=manager_membership.user, content="Written earlier"
        )
        self.client.force_login(manager_membership.user)
        url = reverse("crm-note-delete", args=[self.customer.pk, note.pk])
        self.assertEqual(self.client.post(url, headers=HTMX).status_code, 403)


class F3PasswordResetTokenTests(TestCase):
    def test_a_token_used_meanwhile_is_refused(self):
        user = f.UserFactory()
        token = default_token_generator.make_token(user)
        self.assertTrue(
            account_services.reset_password_with_token(
                user_id=user.pk, token=token, new_password="First-Passphrase-2026"
            )
        )
        self.assertFalse(
            account_services.reset_password_with_token(
                user_id=user.pk, token=token, new_password="Second-Passphrase-2026"
            )
        )
        user.refresh_from_db()
        self.assertTrue(user.check_password("First-Passphrase-2026"))

    def test_web_form_validated_before_a_concurrent_reset_is_still_refused(self):
        user = f.UserFactory()
        token = default_token_generator.make_token(user)
        url = f"{reverse('password-reset-confirm')}?uid={user.pk}&token={token}"
        original = account_services.reset_password_with_token

        def someone_else_first(**kwargs):
            original(user_id=user.pk, token=token, new_password="Someone-Else-2026")
            return original(**kwargs)

        with mock.patch(
            "accounts.web_views.reset_password_with_token", side_effect=someone_else_first
        ):
            response = self.client.post(
                url,
                {"new_password1": "Mine-Passphrase-2026", "new_password2": "Mine-Passphrase-2026"},
            )
        self.assertEqual(response.status_code, 400)
        user.refresh_from_db()
        self.assertTrue(user.check_password("Someone-Else-2026"))


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
class F3PasswordResetRaceTests(TransactionTestCase):
    """Two requests with the same link: the second waits for the first's row lock, then fails
    the token check (the password hash changed), so the first password stands."""

    def test_concurrent_resets_with_one_token(self):
        user = f.UserFactory()
        token = default_token_generator.make_token(user)
        first_inside, release = Event(), Event()
        pids = {}
        original_reset = account_services.reset_password

        def gated_reset(target, new_password):
            if new_password.startswith("First"):
                first_inside.set()
                if not release.wait(10):
                    raise AssertionError("Timed out releasing the first reset")
            return original_reset(target, new_password)

        def worker(label, password):
            connections.close_all()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '10s'")
                    cursor.execute("SELECT pg_backend_pid()")
                    pids[label] = cursor.fetchone()[0]
                return account_services.reset_password_with_token(
                    user_id=user.pk, token=token, new_password=password
                )
            finally:
                connections.close_all()

        with (
            mock.patch.object(account_services, "reset_password", gated_reset),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            first = pool.submit(worker, "first", "First-Passphrase-2026")
            try:
                self.assertTrue(first_inside.wait(10), "First reset never took the lock")
                second = pool.submit(worker, "second", "Second-Passphrase-2026")
                deadline = monotonic() + 10
                while True:
                    if "second" in pids:
                        with connection.cursor() as cursor:
                            cursor.execute("SELECT pg_blocking_pids(%s)", [pids["second"]])
                            if pids["first"] in cursor.fetchone()[0]:
                                break
                    self.assertFalse(second.done(), "Second reset did not wait for the lock")
                    self.assertLess(monotonic(), deadline, "No lock wait observed")
            finally:
                release.set()
            self.assertTrue(first.result(timeout=10))
            self.assertFalse(second.result(timeout=10))
        user.refresh_from_db()
        self.assertTrue(user.check_password("First-Passphrase-2026"))


class F4TagRaceTests(TestCase):
    def test_losing_the_insert_race_reuses_the_winners_tag(self):
        # A rival request created the tag after our lookup: the lookup misses it and our insert
        # collides with it, either at the service's own check or at the unique constraint.
        organization = f.OrganizationFactory()
        winner = f.TagFactory(organization=organization, name="Late", slug="late")
        with mock.patch("crm.services._find_tag", return_value=None):
            self.assertEqual(get_or_create_tag(organization=organization, name="late"), winner)
            with mock.patch("crm.services._check_tag_unique"):
                self.assertEqual(get_or_create_tag(organization=organization, name="Late"), winner)
        self.assertEqual(Tag.objects.filter(organization=organization).count(), 1)

    def test_existing_tag_is_reused_ignoring_case(self):
        organization = f.OrganizationFactory()
        existing = f.TagFactory(organization=organization, name="VIP", slug="vip")
        self.assertEqual(get_or_create_tag(organization=organization, name="vip").pk, existing.pk)


class F5DeleteConfirmationTests(TestCase):
    def setUp(self):
        org = f.OrganizationFactory()
        self.user = f.MembershipFactory(organization=org, role=OrganizationRole.RECEPTIONIST).user
        self.customer = f.CustomerFactory(organization=org)
        self.note = create_note(
            customer=self.customer, author=self.user, content="Keep?", visibility="customer_visible"
        )
        self.url = reverse("crm-note-delete", args=[self.customer.pk, self.note.pk])
        self.client.force_login(self.user)

    def test_without_javascript_a_confirmation_page_comes_first(self):
        response = self.client.post(self.url)
        self.assertContains(response, "Delete this note?")
        self.assertTrue(CustomerNote.objects.filter(pk=self.note.pk).exists())
        self.client.post(self.url, {"confirmed": "yes"})
        self.assertFalse(CustomerNote.objects.filter(pk=self.note.pk).exists())

    def test_htmx_deletes_after_the_browser_confirmation(self):
        self.client.post(self.url, headers=HTMX)
        self.assertFalse(CustomerNote.objects.filter(pk=self.note.pk).exists())
