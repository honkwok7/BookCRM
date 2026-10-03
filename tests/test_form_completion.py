"""M6.2: giving forms to customers, filling them in (emailed link, portal), the CRM tab."""

import re
from datetime import time
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from bookings.models import Customer
from bookings.services import create_booking, reschedule_booking
from core.exceptions import ConflictError, DomainError
from core.models import AuditLog
from crm.models import CustomerActivity
from crm.services import anonymize_customer, merge_customers
from customer_forms.assignments import (
    InvalidAnswers,
    assign_for_booking,
    assign_form,
    cancel_assignment,
    submit_answers,
)
from customer_forms.links import make_token, read_token
from customer_forms.models import FormAnswer, FormAssignment, FormSubmission
from customer_forms.services import (
    add_question,
    create_template,
    delete_template,
    publish,
    update_question,
)
from notifications.models import NotificationLog
from notifications.tasks import send_templated_email
from organizations.models import OrganizationRole
from tests import factories as f

EMAIL = "sam@example.test"


class FormFixtures:
    def make_fixtures(self):
        self.org = f.OrganizationFactory(name="Glow Spa", slug="glow", timezone="UTC")
        self.owner = f.MembershipFactory(organization=self.org, role=OrganizationRole.OWNER).user
        self.service = f.ServiceFactory(organization=self.org, name="Assessment")
        self.other_service = f.ServiceFactory(organization=self.org, name="Facial")
        self.provider = f.StaffProfileFactory(organization=self.org)
        f.make_bookable(
            self.provider, self.service, self.other_service, start=time(8), end=time(18)
        )
        self.intake = self.published_form("Intake", "intake", services=[self.service])

    def published_form(self, name, kind, services=()):
        template = create_template(
            organization=self.org, name=name, kind=kind, services=list(services)
        )
        self.name_q = add_question(template=template, label="Full name", type="text", required=True)
        self.pressure_q = add_question(
            template=template, label="Pressure", type="select", options=["Light", "Firm"]
        )
        self.areas_q = add_question(
            template=template, label="Areas", type="multi_select", options=["Neck", "Back"]
        )
        self.agree_q = add_question(template=template, label="Agree?", type="yes_no")
        self.born_q = add_question(template=template, label="Born", type="date")
        self.pain_q = add_question(template=template, label="Pain", type="number")
        publish(template=template)
        return template

    def book(self, service=None, *, days=3, hour=10, email=EMAIL, notify=False, user=None):
        return create_booking(
            organization=self.org,
            service=service or self.service,
            staff_profile=self.provider,
            customer_name="Sam Lee",
            customer_email=email,
            start_datetime=f.future(days, hour=hour),
            customer_user=user,
            notify=notify,
        )

    def customer(self, email=EMAIL):
        return Customer.objects.get(organization=self.org, email=email)

    def answers(self, assignment, **overrides):
        version = assignment.version
        q = {question.label: question for question in version.questions.all()}
        data = {
            f"q_{q['Full name'].pk}": "Sam Lee",
            f"q_{q['Pressure'].pk}": "Firm",
            f"q_{q['Areas'].pk}": ["Neck", "Back"],
            f"q_{q['Agree?'].pk}": "yes",
            f"q_{q['Born'].pk}": "1990-04-02",
            f"q_{q['Pain'].pk}": "3.50",
        }
        data.update(overrides)
        return data


class AssignTests(FormFixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.book(self.other_service)  # makes the customer, no linked form
        self.sam = self.customer()

    def test_by_hand_gets_the_latest_published_version_and_emails_a_link(self):
        update_question(question=self.name_q, label="Your name")  # an unpublished draft
        with (
            mock.patch("notifications.services.send_templated_email.delay") as send,
            self.captureOnCommitCallbacks(execute=True),
        ):
            assignment = assign_form(template=self.intake, customer=self.sam, actor=self.owner)
        self.assertEqual(
            (assignment.version.number, assignment.status, assignment.source),
            (1, "pending", "manual"),
        )
        log = NotificationLog.objects.get(notification_type="form_request")
        self.assertEqual((log.related_form_assignment, log.recipient_email), (assignment, EMAIL))
        with override_settings(SITE_URL="https://book.example"):
            send_templated_email.apply(kwargs=send.call_args.kwargs).get()
        (message,) = mail.outbox
        self.assertEqual(message.subject, "Please fill in: Intake")
        token = re.search(r"https://book\.example/forms/([^/\s]+)/", message.body).group(1)
        self.assertEqual(read_token(token), (str(assignment.pk), False))
        entry = CustomerActivity.objects.get(kind="email_sent", customer=self.sam)
        self.assertEqual(entry.metadata["form"], str(self.intake.pk))
        self.assertEqual(AuditLog.objects.get(action="form.assigned").user, self.owner)

    def test_what_cannot_be_given(self):
        unpublished = create_template(organization=self.org, name="Draft")
        inactive = self.published_form("Old", "consent")
        inactive.is_active = False
        inactive.save()
        stranger = create_template(organization=f.OrganizationFactory(), name="Theirs")
        for template, code in (
            (unpublished, "not_published"),
            (inactive, "inactive"),
            (stranger, "invalid_form"),
        ):
            with self.subTest(code=code), self.assertRaises(DomainError) as caught:
                assign_form(template=template, customer=self.sam)
            self.assertEqual(caught.exception.code, code)
        assign_form(template=self.intake, customer=self.sam, notify=False)
        with self.assertRaises(ConflictError) as caught:
            assign_form(template=self.intake, customer=self.sam)
        self.assertEqual(caught.exception.code, "already_waiting")

    def test_no_email_without_an_address_or_when_not_asked(self):
        assign_form(template=self.intake, customer=self.sam, notify=False)
        walk_in = Customer.objects.create(organization=self.org, first_name="Walk", phone="+1555")
        assign_form(template=self.intake, customer=walk_in)
        self.assertFalse(NotificationLog.objects.filter(notification_type="form_request").exists())

    def test_a_form_customers_were_given_cannot_be_deleted(self):
        assign_form(template=self.intake, customer=self.sam, notify=False)
        with self.assertRaises(ConflictError) as caught:
            delete_template(template=self.intake)
        self.assertEqual(caught.exception.code, "in_use")


class BookingTests(FormFixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def test_booking_a_linked_service_gives_its_forms(self):
        booking = self.book()
        assignment = FormAssignment.objects.get()
        self.assertEqual(
            (assignment.template, assignment.booking, assignment.source, assignment.customer),
            (self.intake, booking, "booking", booking.customer),
        )
        self.book(self.other_service, days=4)
        self.assertEqual(FormAssignment.objects.count(), 1)

    def test_with_the_confirmation_email_comes_the_form_email(self):
        with (
            mock.patch("notifications.services.send_templated_email.delay"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.book(notify=True)
        types = set(NotificationLog.objects.values_list("notification_type", flat=True))
        self.assertEqual(types, {"booking_confirmation", "form_request"})

    def test_an_intake_form_is_asked_for_once(self):
        self.book()
        self.book(days=4)  # still waiting: not again
        assignment = FormAssignment.objects.get()
        submit_answers(assignment=assignment, data=self.answers(assignment))
        self.book(days=5)  # completed: not again
        self.assertEqual(FormAssignment.objects.count(), 1)

    def test_a_consent_form_is_asked_for_each_appointment(self):
        consent = self.published_form("Consent", "consent", services=[self.other_service])
        self.book(self.other_service)
        first = FormAssignment.objects.get(template=consent)
        submit_answers(assignment=first, data=self.answers(first))
        self.book(self.other_service, days=4)
        self.assertEqual(FormAssignment.objects.filter(template=consent).count(), 2)

    def test_rescheduling_asks_for_nothing_new(self):
        consent = self.published_form("Consent", "consent", services=[self.other_service])
        booking = self.book(self.other_service)
        first = FormAssignment.objects.get(template=consent)
        submit_answers(assignment=first, data=self.answers(first))
        reschedule_booking(booking=booking, new_start=f.future(5, hour=11))
        self.assertEqual(FormAssignment.objects.filter(template=consent).count(), 1)

    def test_inactive_or_unpublished_forms_are_not_given(self):
        self.intake.is_active = False
        self.intake.save()
        draft_only = create_template(organization=self.org, name="Later", services=[self.service])
        add_question(template=draft_only, label="Q", type="text")
        booking = self.book()
        self.assertEqual(assign_for_booking(booking), [])
        self.assertFalse(FormAssignment.objects.exists())


class SubmitTests(FormFixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.book()
        self.assignment = FormAssignment.objects.get()

    def test_answers_are_stored_by_type(self):
        submission = submit_answers(
            assignment=self.assignment, data=self.answers(self.assignment), ip_address="10.0.0.1"
        )
        values = {
            answer.question.label: answer.value
            for answer in FormAnswer.objects.filter(submission=submission)
        }
        self.assertEqual(
            values,
            {
                "Full name": "Sam Lee",
                "Pressure": "Firm",
                "Areas": ["Neck", "Back"],
                "Agree?": True,
                "Born": "1990-04-02",
                "Pain": "3.5",
            },
        )
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, "completed")
        self.assertIsNotNone(self.assignment.completed_at)
        self.assertEqual(submission.ip_address, "10.0.0.1")

    def test_optional_questions_can_be_left_empty(self):
        data = self.answers(self.assignment)
        for question in (self.pressure_q, self.areas_q, self.agree_q, self.born_q, self.pain_q):
            data.pop(f"q_{question.pk}")
        submission = submit_answers(assignment=self.assignment, data=data)
        self.assertEqual(
            FormAnswer.objects.filter(submission=submission, value__isnull=True).count(), 5
        )

    def test_answers_that_do_not_fit_are_refused(self):
        for overrides in (
            {f"q_{self.name_q.pk}": ""},  # required
            {f"q_{self.pressure_q.pk}": "Medium"},  # not an option
            {f"q_{self.areas_q.pk}": ["Neck", "Feet"]},
            {f"q_{self.pain_q.pk}": "lots"},
            {f"q_{self.born_q.pk}": "yesterday"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(InvalidAnswers):
                submit_answers(
                    assignment=self.assignment, data=self.answers(self.assignment, **overrides)
                )
        self.assertFalse(FormSubmission.objects.exists())

    def test_completed_once_and_never_logged_with_its_answers(self):
        submit_answers(assignment=self.assignment, data=self.answers(self.assignment))
        with self.assertRaises(ConflictError):
            submit_answers(assignment=self.assignment, data=self.answers(self.assignment))
        entry = CustomerActivity.objects.get(kind="form_completed")
        self.assertEqual(entry.metadata, {"form": str(self.intake.pk), "version": 1})
        audit = AuditLog.objects.get(action="form.completed")
        self.assertNotIn("Sam Lee", str(audit.metadata))
        self.assertNotIn("Firm", str(audit.metadata))

    def test_a_cancelled_form_cannot_be_filled_in(self):
        cancel_assignment(assignment=self.assignment, actor=self.owner)
        with self.assertRaises(ConflictError):
            submit_answers(assignment=self.assignment, data=self.answers(self.assignment))
        with self.assertRaises(ConflictError):
            cancel_assignment(assignment=self.assignment)


class LinkTests(FormFixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.book()
        self.assignment = FormAssignment.objects.get()
        self.url = reverse("form-fill", args=[make_token(self.assignment)])

    def test_fill_in_through_the_emailed_link(self):
        page = self.client.get(self.url)
        self.assertContains(page, "Intake")
        self.assertContains(page, "Pressure")
        response = self.client.post(self.url, self.answers(self.assignment))
        self.assertContains(response, "Thank you")
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, "completed")
        submission = self.assignment.submission
        self.assertIsNone(submission.submitted_by)
        again = self.client.get(self.url)
        self.assertContains(again, "Already filled in")
        self.assertNotContains(again, "Sam Lee")

    def test_errors_are_shown_on_the_questions(self):
        response = self.client.post(
            self.url, self.answers(self.assignment, **{f"q_{self.name_q.pk}": ""})
        )
        self.assertEqual(response.status_code, 422)
        self.assertContains(response, "This field is required", status_code=422)

    def test_forged_expired_and_suspended(self):
        self.assertEqual(self.client.get(self.url[:-3] + "xx/").status_code, 404)
        with mock.patch("customer_forms.links.max_age_seconds", return_value=-1):
            response = self.client.get(self.url)
        self.assertContains(response, "This link has expired", status_code=410)
        self.org.is_suspended = True
        self.org.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_a_cancelled_form_link_is_closed(self):
        cancel_assignment(assignment=self.assignment)
        self.assertContains(self.client.get(self.url), "No longer needed")


class PortalTests(FormFixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.user = f.UserFactory(email=EMAIL, email_verified=True)
        self.book(user=self.user)
        self.assignment = FormAssignment.objects.get()
        self.client.force_login(self.user)

    def test_the_customer_fills_in_their_form_in_the_portal(self):
        overview = self.client.get(reverse("portal-organization", args=["glow"]))
        self.assertContains(overview, "Forms (1)")
        listing = self.client.get(reverse("portal-forms", args=["glow"]))
        url = reverse("portal-form", args=["glow", self.assignment.pk])
        self.assertContains(listing, url)
        response = self.client.post(url, self.answers(self.assignment))
        self.assertContains(response, "Thank you")
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.submission.submitted_by, self.user)
        self.assertContains(self.client.get(reverse("portal-forms", args=["glow"])), "Completed")

    def test_other_customers_forms_are_not_found(self):
        self.book(email="other@example.test", days=4, hour=12)
        theirs = FormAssignment.objects.exclude(pk=self.assignment.pk).get()
        url = reverse("portal-form", args=["glow", theirs.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, self.answers(theirs)).status_code, 404)


class CrmTests(FormFixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.book()
        self.assignment = FormAssignment.objects.get()
        self.sam = self.assignment.customer
        submit_answers(assignment=self.assignment, data=self.answers(self.assignment))

    def tab(self):
        return self.client.get(reverse("crm-customer-tab", args=[self.sam.pk, "forms"]))

    def test_the_forms_tab_and_answers(self):
        self.client.force_login(self.owner)
        answers_url = reverse("crm-customer-form", args=[self.sam.pk, self.assignment.pk])
        self.assertContains(self.tab(), answers_url)
        page = self.client.get(answers_url)
        self.assertContains(page, "Sam Lee")
        self.assertContains(page, "Neck, Back")
        self.assertContains(page, "2 Apr 1990")
        self.assertContains(page, "Yes")
        timeline = self.client.get(reverse("crm-customer-tab", args=[self.sam.pk, "activity"]))
        self.assertContains(timeline, "Form completed")
        self.assertContains(timeline, "Intake")

    def test_a_receptionist_sends_a_form(self):
        consent = self.published_form("Consent", "consent")
        receptionist = f.MembershipFactory(
            organization=self.org, role=OrganizationRole.RECEPTIONIST
        ).user
        self.client.force_login(receptionist)
        response = self.client.post(
            reverse("crm-customer-form-send", args=[self.sam.pk]),
            {"template": consent.pk, "notify": ""},
        )
        self.assertRedirects(response, reverse("crm-customer-tab", args=[self.sam.pk, "forms"]))
        sent = FormAssignment.objects.get(template=consent)
        self.assertEqual((sent.source, sent.assigned_by), ("manual", receptionist))
        response = self.client.post(
            reverse("crm-customer-form-cancel", args=[self.sam.pk, sent.pk])
        )
        sent.refresh_from_db()
        self.assertEqual(sent.status, "cancelled")

    def test_providers_see_forms_but_not_answers(self):
        provider_user = self.provider.user
        f.MembershipFactory(organization=self.org, user=provider_user, role=OrganizationRole.STAFF)
        self.client.force_login(provider_user)
        tab = self.tab()
        self.assertContains(tab, "Intake")
        answers_url = reverse("crm-customer-form", args=[self.sam.pk, self.assignment.pk])
        self.assertNotContains(tab, answers_url)
        self.assertEqual(self.client.get(answers_url).status_code, 403)
        self.assertEqual(
            self.client.get(reverse("crm-customer-form-send", args=[self.sam.pk])).status_code,
            403,
        )

    def test_another_organization_cannot_reach_them(self):
        outsider = f.MembershipFactory(role=OrganizationRole.OWNER).user
        self.client.force_login(outsider)
        url = reverse("crm-customer-form", args=[self.sam.pk, self.assignment.pk])
        self.assertEqual(self.client.get(url).status_code, 404)


class PrivacyTests(FormFixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.book()
        self.assignment = FormAssignment.objects.get()
        self.sam = self.assignment.customer

    def test_anonymizing_deletes_answers_and_cancels_waiting_forms(self):
        submit_answers(assignment=self.assignment, data=self.answers(self.assignment))
        consent = self.published_form("Consent", "consent")
        waiting = assign_form(template=consent, customer=self.sam, notify=False)
        anonymize_customer(customer=self.sam)
        self.assertFalse(FormSubmission.objects.exists())
        self.assertFalse(FormAnswer.objects.exists())
        waiting.refresh_from_db()
        self.assertEqual(waiting.status, "cancelled")
        with self.assertRaises(DomainError):
            assign_form(template=consent, customer=Customer.objects.get(pk=self.sam.pk))

    def test_merging_moves_forms_and_keeps_one_waiting_copy(self):
        duplicate = Customer.objects.create(
            organization=self.org, first_name="Sam", email="sam2@example.test"
        )
        theirs = assign_form(template=self.intake, customer=duplicate, notify=False)
        merge_customers(target=self.sam, duplicate=duplicate)
        theirs.refresh_from_db()
        self.assertEqual((theirs.customer, theirs.status), (self.sam, "cancelled"))
        self.assertEqual(
            FormAssignment.objects.filter(customer=self.sam, status="pending").count(), 1
        )
