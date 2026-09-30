"""M2.3: customer notes and the activity timeline."""

import importlib
from datetime import timedelta
from unittest import mock

from django.apps import apps
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from bookings.models import Booking, Customer
from bookings.services import (
    cancel_booking,
    change_booking_status,
    create_booking,
    reschedule_booking,
)
from core.exceptions import ConflictError, DomainError
from core.models import AuditLog
from crm.models import CustomerActivity, CustomerNote
from crm.selectors import customer_timeline, notes_for_customer, team_notes
from crm.services import (
    add_customer_tag,
    anonymize_customer,
    create_note,
    merge_customers,
    remove_customer_tag,
    update_customer,
    update_note,
)
from notifications.tasks import send_templated_email
from organizations.models import OrganizationRole
from tests import factories as f

Kind = CustomerActivity.Kind
INTERNAL = CustomerNote.Visibility.INTERNAL
VISIBLE = CustomerNote.Visibility.CUSTOMER_VISIBLE


def kinds(customer):
    return list(
        CustomerActivity.objects.filter(customer=customer)
        .order_by("created_at")
        .values_list("kind", flat=True)
    )


class NoteServiceTests(TestCase):
    def setUp(self):
        self.organization = f.OrganizationFactory()
        self.author = f.MembershipFactory(organization=self.organization).user
        self.customer = f.CustomerFactory(organization=self.organization)

    def test_create_validates_and_audits_without_content(self):
        with self.assertRaises(DomainError):
            create_note(customer=self.customer, author=self.author, content="   ")
        note = create_note(customer=self.customer, author=self.author, content="Knee injury")
        self.assertEqual(note.visibility, INTERNAL)  # safe default
        entry = AuditLog.objects.get(action="customer_note.created")
        self.assertNotIn("Knee", str(entry.metadata))

    def test_edit_stamps_edited_at_and_redacts_content_in_audit(self):
        note = create_note(customer=self.customer, author=self.author, content="Old text")
        update_note(note=note, actor=self.author, pinned=True)
        note.refresh_from_db()
        self.assertIsNone(note.edited_at)  # pinning is not an edit of the text
        update_note(note=note, actor=self.author, content="New secret text")
        note.refresh_from_db()
        self.assertIsNotNone(note.edited_at)
        changes = [e.metadata.get("changes", {}) for e in AuditLog.objects.all()]
        self.assertIn({"content": "changed"}, changes)
        self.assertNotIn("secret", str(changes))

    def test_selectors_enforce_visibility(self):
        internal = create_note(customer=self.customer, author=self.author, content="internal")
        visible = create_note(
            customer=self.customer, author=self.author, content="visible", visibility=VISIBLE
        )
        self.assertEqual(list(notes_for_customer(self.customer)), [visible])
        self.assertEqual(list(team_notes(self.organization, include_internal=False)), [visible])
        self.assertEqual(
            set(team_notes(self.organization, include_internal=True)), {internal, visible}
        )
        timeline = customer_timeline(self.customer, include_internal=False)
        self.assertEqual([a.subject_id for a in timeline], [str(visible.pk)])

    def test_anonymized_customer_notes_are_deleted_and_frozen(self):
        create_note(customer=self.customer, author=self.author, content="Allergic to latex")
        anonymize_customer(customer=self.customer)
        self.assertFalse(CustomerNote.objects.exists())
        with self.assertRaises(ConflictError):
            create_note(customer=self.customer, author=self.author, content="x")
        self.assertNotIn(
            "latex",
            str(list(CustomerActivity.objects.values())) + str(list(AuditLog.objects.values())),
        )

    def test_merge_moves_notes_and_history(self):
        duplicate = f.CustomerFactory(organization=self.organization)
        note = create_note(customer=duplicate, author=self.author, content="From duplicate")
        merged = merge_customers(target=self.customer, duplicate=duplicate)
        note.refresh_from_db()
        self.assertEqual(note.customer, merged)
        self.assertIn(Kind.NOTE_CREATED, kinds(merged))
        self.assertEqual(kinds(merged)[-1], Kind.CUSTOMER_MERGED)


class ActivityEmissionTests(TestCase):
    """Every service that changes a customer's story writes a timeline entry."""

    def setUp(self):
        self.organization = f.OrganizationFactory()
        self.actor = f.MembershipFactory(organization=self.organization).user
        self.staff = f.StaffProfileFactory(organization=self.organization)
        self.service = f.ServiceFactory(organization=self.organization)
        f.make_bookable(self.staff, self.service)
        self.start = f.future(3)

    def book(self, **kwargs):
        return create_booking(
            organization=self.organization,
            service=self.service,
            staff_profile=self.staff,
            customer_name="Ada Lovelace",
            customer_email="ada@example.test",
            customer_phone="",
            start_datetime=self.start,
            actor=self.actor,
            notify=False,
            **kwargs,
        )

    def test_booking_lifecycle(self):
        booking = self.book()
        customer = booking.customer
        moved = reschedule_booking(booking=booking, new_start=self.start + timedelta(hours=2))
        f.make_current(moved)
        change_booking_status(booking=moved, new_status="checked_in")  # not timeline-worthy
        change_booking_status(booking=moved, new_status="completed")
        second = create_booking(
            organization=self.organization,
            service=self.service,
            staff_profile=self.staff,
            customer_name="Ada Lovelace",
            customer_email="ada@example.test",
            customer_phone="",
            start_datetime=self.start + timedelta(days=1),
            notify=False,
        )
        cancel_booking(booking=second, reason="sick")
        self.assertEqual(
            kinds(customer),
            [
                Kind.CUSTOMER_CREATED,
                Kind.APPOINTMENT_BOOKED,
                Kind.APPOINTMENT_RESCHEDULED,  # not also "booked"
                Kind.APPOINTMENT_COMPLETED,
                Kind.APPOINTMENT_BOOKED,
                Kind.APPOINTMENT_CANCELLED,
            ],
        )
        cancelled = CustomerActivity.objects.get(kind=Kind.APPOINTMENT_CANCELLED)
        self.assertNotIn("sick", str(cancelled.metadata))  # free text never stored
        self.assertEqual(cancelled.subject_id, str(second.pk))

    def test_reschedule_keeps_the_customer_after_an_email_change(self):
        booking = self.book()
        customer = booking.customer
        update_customer(customer=customer, email="ada.new@example.test")
        moved = reschedule_booking(booking=booking, new_start=self.start + timedelta(hours=2))
        self.assertEqual(moved.customer, customer)
        self.assertEqual(Customer.objects.count(), 1)

    def test_booking_refuses_a_customer_from_another_organization(self):
        with self.assertRaises(DomainError):
            self.book(customer=f.CustomerFactory())

    def test_crm_changes(self):
        customer = self.book().customer
        update_customer(customer=customer, city="Toronto", sms_consent=True)
        tag = f.TagFactory(organization=self.organization)
        add_customer_tag(customer=customer, tag=tag)
        remove_customer_tag(customer=customer, tag=tag)
        create_note(customer=customer, author=self.actor, content="hi")
        self.assertEqual(
            kinds(customer)[2:],
            [
                Kind.PROFILE_UPDATED,
                Kind.CONSENT_CHANGED,
                Kind.TAG_ADDED,
                Kind.TAG_REMOVED,
                Kind.NOTE_CREATED,
            ],
        )
        profile = CustomerActivity.objects.get(kind=Kind.PROFILE_UPDATED)
        self.assertEqual(profile.metadata, {"fields": ["city"]})  # names, never values
        self.assertEqual(profile.actor, None)

    @mock.patch("notifications.services.send_templated_email.delay")
    def test_sent_email(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            booking = create_booking(
                organization=self.organization,
                service=self.service,
                staff_profile=self.staff,
                customer_name="Ada",
                customer_email="ada@example.test",
                customer_phone="",
                start_datetime=self.start,
            )
        send_templated_email.apply(kwargs=delay.call_args.kwargs).get()
        entry = CustomerActivity.objects.get(kind=Kind.EMAIL_SENT)
        self.assertEqual(entry.customer, booking.customer)
        self.assertEqual(entry.metadata["notification_type"], "booking_confirmation")


class BackfillTests(TestCase):
    def test_existing_bookings_get_history(self):
        organization = f.OrganizationFactory()
        customer = f.CustomerFactory(organization=organization)
        done = f.BookingFactory(organization=organization, customer=customer, status="completed")
        old = f.BookingFactory(organization=organization, customer=customer, status="cancelled")
        new = f.BookingFactory(organization=organization, customer=customer, rescheduled_from=old)
        f.BookingFactory(
            organization=organization, customer=None, customer_name="X", customer_email="x@x.test"
        )

        migration = importlib.import_module("crm.migrations.0004_backfill_activity")
        migration.backfill(apps, None)
        migration.backfill(apps, None)  # idempotent

        by_subject = {}
        for activity in CustomerActivity.objects.filter(customer=customer):
            by_subject.setdefault(activity.subject_id, set()).add(activity.kind)
        self.assertEqual(by_subject[""], {Kind.CUSTOMER_CREATED})
        self.assertEqual(
            by_subject[str(done.pk)], {Kind.APPOINTMENT_BOOKED, Kind.APPOINTMENT_COMPLETED}
        )
        self.assertEqual(by_subject[str(old.pk)], {Kind.APPOINTMENT_BOOKED})  # moved, not cancelled
        self.assertEqual(by_subject[str(new.pk)], {Kind.APPOINTMENT_RESCHEDULED})


class NoteApiTests(TestCase):
    def setUp(self):
        self.organization = f.OrganizationFactory()
        self.customer = f.CustomerFactory(organization=self.organization)
        self.manager = self.member(OrganizationRole.MANAGER)
        self.receptionist = self.member(OrganizationRole.RECEPTIONIST)
        self.client = APIClient()

    def member(self, role):
        return f.MembershipFactory(organization=self.organization, role=role).user

    def as_user(self, user):
        self.client.force_authenticate(user)
        return self.client

    def post(self, user, **data):
        payload = {"customer": str(self.customer.pk), "content": "Note", **data}
        return self.as_user(user).post("/api/v1/customer-notes/", payload, format="json")

    def test_receptionist_never_sees_internal_notes(self):
        internal = create_note(customer=self.customer, author=self.manager, content="internal")
        visible = create_note(
            customer=self.customer, author=self.manager, content="visible", visibility=VISIBLE
        )
        client = self.as_user(self.receptionist)
        rows = client.get("/api/v1/customer-notes/").json()["results"]
        self.assertEqual([row["id"] for row in rows], [str(visible.pk)])
        self.assertEqual(client.get(f"/api/v1/customer-notes/{internal.pk}/").status_code, 404)
        timeline = client.get(f"/api/v1/customers/{self.customer.pk}/timeline/").json()
        self.assertNotIn(str(internal.pk), str(timeline))

        client = self.as_user(self.manager)
        rows = client.get("/api/v1/customer-notes/").json()["results"]
        self.assertEqual(len(rows), 2)

    def test_internal_notes_need_the_private_capability(self):
        self.assertEqual(self.post(self.receptionist).status_code, 403)  # default is internal
        self.assertEqual(self.post(self.receptionist, visibility="internal").status_code, 403)
        response = self.post(self.receptionist, visibility="customer_visible")
        self.assertEqual(response.status_code, 201, response.data)
        note_id = response.data["id"]
        response = self.as_user(self.receptionist).patch(
            f"/api/v1/customer-notes/{note_id}/", {"visibility": "internal"}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.post(self.manager).status_code, 201)

    def test_only_the_author_or_a_private_notes_holder_can_change_a_note(self):
        note_id = self.post(self.receptionist, visibility="customer_visible").data["id"]
        other = self.member(OrganizationRole.RECEPTIONIST)
        url = f"/api/v1/customer-notes/{note_id}/"
        self.assertEqual(
            self.as_user(other).patch(url, {"pinned": True}, format="json").status_code, 403
        )
        self.assertEqual(self.as_user(other).delete(url).status_code, 403)
        self.assertEqual(
            self.as_user(self.receptionist).patch(url, {"pinned": True}, format="json").status_code,
            200,
        )
        self.assertEqual(self.as_user(self.manager).delete(url).status_code, 204)
        self.assertTrue(AuditLog.objects.filter(action="customer_note.deleted").exists())

    def test_note_cannot_move_to_another_customer(self):
        note_id = self.post(self.manager).data["id"]
        other = f.CustomerFactory(organization=self.organization)
        response = self.as_user(self.manager).patch(
            f"/api/v1/customer-notes/{note_id}/", {"customer": str(other.pk)}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_staff_only_see_notes_on_their_own_customers(self):
        # Full rules in crm/test_crm_api.py (M2.4); here: none of this customer's notes.
        create_note(customer=self.customer, author=self.manager, content="x", visibility=VISIBLE)
        staff = self.member(OrganizationRole.STAFF)
        response = self.as_user(staff).get("/api/v1/customer-notes/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 0)

    def test_timeline_is_paginated_newest_first(self):
        for day in range(3):
            f.CustomerActivityFactory(
                organization=self.organization,
                customer=self.customer,
                occurred_at=timezone.now() - timedelta(days=day),
            )
        body = (
            self.as_user(self.receptionist)
            .get(f"/api/v1/customers/{self.customer.pk}/timeline/")
            .json()
        )
        self.assertEqual(body["count"], 3)
        times = [row["occurred_at"] for row in body["results"]]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertEqual(body["results"][0]["kind_display"], "Profile updated")


class SeededTimelineTests(TestCase):
    def test_seeded_customers_have_a_booking_lifecycle_timeline(self):
        from io import StringIO

        from django.core.management import call_command

        call_command("seed_demo", stdout=StringIO())
        seeded = set(CustomerActivity.objects.values_list("kind", flat=True))
        for kind in (
            Kind.CUSTOMER_CREATED,
            Kind.APPOINTMENT_BOOKED,
            Kind.APPOINTMENT_COMPLETED,
            Kind.APPOINTMENT_NO_SHOW,
            Kind.APPOINTMENT_CANCELLED,
            Kind.TAG_ADDED,
            Kind.NOTE_CREATED,
        ):
            self.assertIn(kind, seeded)
        self.assertTrue(Booking.objects.exists())
