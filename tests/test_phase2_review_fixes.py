"""Fixes from the Phase 2 re-review: account linking, erasure (anonymize and delete), merges,
phone-only matching races, account-email timing, and the case-insensitive email migration."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import time
from importlib import import_module
from unittest import mock, skipUnless

from django.apps import apps
from django.core import mail
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from accounts.models import EmailVerificationToken
from accounts.tasks import send_password_reset_email, send_verification_email
from bookings.models import Booking, BookingStatusHistory, Customer, WaitlistEntry
from bookings.services import cancel_booking, change_booking_status, create_booking
from bookings.waitlist import join_waitlist
from core.audit import AuditLog
from core.exceptions import ConflictError
from crm import services as crm_services
from crm.models import CustomerActivity
from crm.services import (
    anonymize_customer,
    find_or_create_customer,
    merge_customers,
    update_customer,
)
from organizations.models import OrganizationRole
from tests import factories as f

POSTGRES = skipUnless(connection.vendor == "postgresql", "row locks across connections")


class Fixtures:
    def make_fixtures(self):
        self.org = f.OrganizationFactory(slug="glow", timezone="UTC")
        self.service = f.ServiceFactory(organization=self.org, name="Massage", is_public=True)
        self.staff = f.StaffProfileFactory(organization=self.org)
        f.make_bookable(self.staff, self.service, start=time(8), end=time(20))

    def book(self, hour=10, email="ada@x.test", **kwargs):
        return create_booking(
            organization=self.org,
            service=self.service,
            staff_profile=self.staff,
            customer_name="Ada Lovelace",
            customer_email=email,
            start_datetime=f.future(3, hour=hour),
            notify=False,
            **kwargs,
        )


class AccountLinkingTests(Fixtures, TestCase):
    """A booking links the caller's account only to their own verified email."""

    def setUp(self):
        self.make_fixtures()

    def find(self, email, user):
        return find_or_create_customer(
            organization=self.org, name="Someone", email=email, user=user
        )

    def test_someone_elses_email_is_never_linked(self):
        attacker = f.UserFactory(email="attacker@x.test", email_verified=True)
        customer = self.find("victim@x.test", attacker)
        self.assertIsNone(customer.user)
        # The victim's later bookings match that customer: the attacker can't see them.
        booking = self.book(email="victim@x.test")
        self.assertEqual(booking.customer, customer)
        self.assertIsNone(booking.customer.user)

    def test_unverified_account_is_not_linked_even_to_its_own_email(self):
        user = f.UserFactory(email="me@x.test")
        self.assertIsNone(self.find("me@x.test", user).user)

    def test_own_verified_email_is_linked_ignoring_case(self):
        user = f.UserFactory(email="Me@X.test", email_verified=True)
        self.assertEqual(self.find("me@x.test", user).user, user)

    def test_booking_and_waitlist_paths_use_the_same_rule(self):
        attacker = f.UserFactory(email="attacker@x.test", email_verified=True)
        booking = self.book(email="victim@x.test", customer_user=attacker)
        self.assertIsNone(booking.customer.user)
        entry = join_waitlist(
            organization=self.org,
            service=self.service,
            customer_name="Victim",
            customer_email="other-victim@x.test",
            customer_user=attacker,
        )
        self.assertIsNone(entry.customer.user)


class ErasureTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.account = f.UserFactory(email="ada@x.test", email_verified=True)
        self.manager = f.MembershipFactory(
            organization=self.org, role=OrganizationRole.MANAGER
        ).user
        self.booking = self.book(actor=self.account, customer_user=self.account)
        self.customer = self.booking.customer
        self.assertEqual(self.customer.user, self.account)
        cancel_booking(booking=self.booking, actor=self.account, reason="pregnancy complication")
        self.other = self.book(hour=14, actor=self.manager)
        change_booking_status(
            booking=self.other,
            new_status=Booking.Status.CANCELLED,
            actor=self.manager,
            reason="staff reason",
            note="staff note",
        )

    def test_anonymizing_severs_the_customers_own_account_everywhere(self):
        anonymize_customer(customer=self.customer, actor=self.manager)
        bookings = Booking.objects.filter(customer=self.customer)
        self.assertFalse(bookings.filter(created_by=self.account).exists())
        self.assertFalse(bookings.filter(cancelled_by=self.account).exists())
        history = BookingStatusHistory.objects.filter(booking__in=bookings)
        self.assertFalse(history.filter(changed_by=self.account).exists())
        self.assertFalse(history.exclude(reason="").exists())
        self.assertFalse(history.exclude(note="").exists())
        self.assertFalse(
            CustomerActivity.objects.filter(customer=self.customer, actor=self.account).exists()
        )
        # Staff actions keep their staff actor.
        self.assertTrue(history.filter(changed_by=self.manager).exists())

    def test_waitlist_entries_and_their_emails_are_scrubbed_after_an_email_change(self):
        entry = join_waitlist(
            organization=self.org,
            service=self.service,
            customer_name="Ada Lovelace",
            customer_email="ada@x.test",
            customer=self.customer,
        )
        log = f.NotificationLogFactory(
            organization=self.org,
            recipient_email="ada@x.test",
            related_waitlist_entry=entry,
            notification_type="waitlist_slot_available",
            failure_reason="mailbox ada@x.test full",
        )
        update_customer(customer=self.customer, actor=self.manager, email="ada.new@x.test")
        anonymize_customer(customer=self.customer, actor=self.manager)
        entry.refresh_from_db()
        log.refresh_from_db()
        self.assertEqual((entry.customer_email, entry.customer_phone), ("", ""))
        self.assertEqual((log.recipient_email, log.failure_reason), ("", ""))
        self.assertIsNone(log.recipient)

    def test_api_delete_erases_before_deleting(self):
        client = APIClient()
        client.force_authenticate(self.manager)
        response = client.delete(
            f"/api/v1/customers/{self.customer.pk}/", HTTP_X_ORGANIZATION_SLUG="glow"
        )
        self.assertEqual(response.status_code, 204, response.content)
        self.assertFalse(Customer.objects.filter(pk=self.customer.pk).exists())
        self.booking.refresh_from_db()
        self.assertIsNone(self.booking.customer)
        self.assertEqual(
            (
                self.booking.customer_email,
                self.booking.customer_phone,
                self.booking.cancellation_reason,
            ),
            ("", "", ""),
        )
        self.assertNotEqual(self.booking.customer_name, "Ada Lovelace")
        actions = set(AuditLog.objects.values_list("action", flat=True))
        self.assertTrue({"customer.anonymized", "customer.deleted"} <= actions)


class MergeTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.target = f.CustomerFactory(organization=self.org, email="t@x.test")
        self.duplicate = f.CustomerFactory(organization=self.org, email="d@x.test")

    def test_a_customer_removed_meanwhile_is_a_conflict(self):
        stale = Customer.objects.get(pk=self.duplicate.pk)
        Customer.objects.filter(pk=self.duplicate.pk).delete()
        with self.assertRaises(ConflictError) as raised:
            merge_customers(target=self.target, duplicate=stale)
        self.assertEqual(raised.exception.code, "customer_gone")

    def test_waitlist_entries_move_to_the_target(self):
        other_service = f.ServiceFactory(organization=self.org)
        f.make_bookable(self.staff, other_service)

        def join(customer, service):
            return join_waitlist(
                organization=self.org,
                service=service,
                customer_name="X",
                customer_email=customer.email,
                customer=customer,
            )

        kept = join(self.target, self.service)
        both = join(self.duplicate, self.service)  # both wait for the same service
        only = join(self.duplicate, other_service)
        merge_customers(target=self.target, duplicate=self.duplicate)
        for entry in (kept, both, only):
            entry.refresh_from_db()
            self.assertEqual(entry.customer, self.target)
        self.assertEqual(kept.status, WaitlistEntry.Status.WAITING)
        self.assertEqual(both.status, WaitlistEntry.Status.CLOSED)
        self.assertEqual(only.status, WaitlistEntry.Status.WAITING)


@POSTGRES
class PhoneOnlyRaceTests(Fixtures, TransactionTestCase):
    def setUp(self):
        self.make_fixtures()

    def test_concurrent_phone_only_lookups_make_one_customer(self):
        barrier = threading.Barrier(2, timeout=10)
        real_lock = crm_services._lock_phone

        def together(*args):
            barrier.wait()  # both arrive at the lookup at the same moment
            return real_lock(*args)

        def attempt(name):
            connections.close_all()
            try:
                return find_or_create_customer(
                    organization=self.org, name=name, phone="+1 416 555 0100"
                ).pk
            finally:
                connections.close_all()

        with (
            mock.patch.object(crm_services, "_lock_phone", together),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            results = list(pool.map(attempt, ("First", "Second")))
        self.assertEqual(results[0], results[1])
        self.assertEqual(Customer.objects.filter(phone="+1 416 555 0100").count(), 1)


class AccountEmailTimingTests(TestCase):
    """Known and unknown addresses do the same request-side work: one task, keyed by email."""

    @mock.patch("accounts.services.send_password_reset_email.delay")
    def test_reset_queues_the_same_task_for_known_and_unknown_addresses(self, delay):
        user = f.UserFactory(email="ada@x.test")
        with self.captureOnCommitCallbacks(execute=True):
            APIClient().post("/api/forgot-password/", {"email": "ada@x.test"}, format="json")
            APIClient().post("/api/forgot-password/", {"email": "nobody@x.test"}, format="json")
        self.assertEqual(
            [call.kwargs for call in delay.call_args_list],
            [{"email": "ada@x.test"}, {"email": "nobody@x.test"}],
        )
        send_password_reset_email.apply(kwargs={"email": "nobody@x.test"}).get()
        self.assertEqual(len(mail.outbox), 0)
        send_password_reset_email.apply(kwargs={"email": "ADA@x.test"}).get()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(f"uid={user.pk}", mail.outbox[0].body)

    @mock.patch("accounts.services.send_verification_email.delay")
    def test_resend_verification_queues_the_same_task_either_way(self, delay):
        unverified = f.UserFactory(email="new@x.test")
        f.UserFactory(email="done@x.test", email_verified=True)
        with self.captureOnCommitCallbacks(execute=True):
            for email in ("new@x.test", "done@x.test", "nobody@x.test"):
                APIClient().post("/api/resend-verification/", {"email": email}, format="json")
        self.assertEqual(delay.call_count, 3)
        self.assertFalse(EmailVerificationToken.objects.exists())  # nothing made in the request
        for call in delay.call_args_list:
            send_verification_email.apply(kwargs=call.kwargs).get()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [unverified.email])
        self.assertEqual(EmailVerificationToken.objects.get().user, unverified)


class CaseDuplicateMigrationTests(Fixtures, TestCase):
    def test_case_only_duplicates_are_merged_into_the_oldest(self):
        self.make_fixtures()
        constraint = next(
            item
            for item in Customer._meta.constraints
            if item.name == "customer_unique_email_per_org"
        )
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        with connection.schema_editor() as editor:
            editor.remove_constraint(Customer, constraint)
        keeper = f.CustomerFactory(organization=self.org, email="Sam@x.test", phone="")
        later = f.CustomerFactory(organization=self.org, email="sam@x.test", phone="555-0100")
        booking = self.book(email="unrelated@x.test")
        Booking.objects.filter(pk=booking.pk).update(customer=later)
        migration = import_module("bookings.migrations.0004_customer_constraints")
        with connection.schema_editor() as editor:
            migration.merge_case_duplicates(apps, editor)
            editor.add_constraint(Customer, constraint)
        self.assertFalse(Customer.objects.filter(pk=later.pk).exists())
        keeper.refresh_from_db()
        booking.refresh_from_db()
        self.assertEqual(booking.customer, keeper)
        self.assertEqual(keeper.phone, "555-0100")
