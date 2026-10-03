"""M6.1: form templates, versioning and the builder (service layer, web pages and the API)."""

from unittest import mock

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from core.exceptions import ConflictError, DomainError
from core.models import AuditLog
from customer_forms.models import FormQuestion, FormTemplate, FormVersion
from customer_forms.selectors import form_state
from customer_forms.services import (
    add_question,
    create_template,
    delete_question,
    delete_template,
    discard_draft,
    move_question,
    publish,
    update_question,
    update_template,
)
from organizations.models import OrganizationRole
from tests import factories as f

TEN_QUESTIONS = [
    {"label": "Full name", "type": "text", "required": "on"},
    {"label": "Date of birth", "type": "date", "required": "on"},
    {"label": "Phone number", "type": "text"},
    {"label": "Reason for your visit", "type": "textarea", "required": "on"},
    {"label": "Pain level today (0 to 10)", "type": "number"},
    {"label": "Have you had massage therapy before?", "type": "yes_no"},
    {
        "label": "Preferred pressure",
        "type": "select",
        "options": "Light\nMedium\nFirm",
        "required": "on",
    },
    {"label": "Areas to avoid", "type": "multi_select", "options": "Neck\nBack\nFeet\nFace"},
    {"label": "Medications", "type": "textarea", "help_text": "Leave empty if none."},
    {"label": "Signature", "type": "signature_placeholder", "required": "on"},
]


class FormTestCase(TestCase):
    def setUp(self):
        self.org = f.OrganizationFactory()
        self.owner = f.MembershipFactory(organization=self.org, role=OrganizationRole.OWNER).user
        self.service = f.ServiceFactory(organization=self.org, name="Initial assessment")

    def form(self, name="New client intake", **fields):
        return create_template(organization=self.org, actor=self.owner, name=name, **fields)

    def question(self, template, label="Full name", type="text", **fields):
        return add_question(template=template, label=label, type=type, **fields)

    def labels(self, version):
        return list(version.questions.order_by("position").values_list("label", flat=True))


class TemplateTests(FormTestCase):
    def test_create_starts_an_empty_draft(self):
        template = self.form(kind="consent", services=[self.service], description=" Hi ")
        version = template.versions.get()
        self.assertEqual((version.number, version.is_draft), (1, True))
        self.assertEqual(template.description, "Hi")
        self.assertEqual(list(template.services.all()), [self.service])
        self.assertEqual(AuditLog.objects.get(action="form.created").user, self.owner)

    def test_names_are_unique_per_organization_ignoring_case(self):
        self.form()
        with self.assertRaises(ConflictError):
            self.form(name="new CLIENT intake")
        other = f.OrganizationFactory()
        create_template(organization=other, name="New client intake")

    def test_services_of_another_organization_are_refused(self):
        stranger = f.ServiceFactory(organization=f.OrganizationFactory())
        with self.assertRaises(DomainError) as caught:
            self.form(services=[stranger])
        self.assertEqual(caught.exception.code, "invalid_service")
        template = self.form()
        with self.assertRaises(DomainError):
            update_template(template=template, services=[stranger])

    def test_update_records_the_change_including_services(self):
        template = self.form()
        update_template(
            template=template, actor=self.owner, is_active=False, services=[self.service]
        )
        changes = AuditLog.objects.get(action="form.updated").metadata["changes"]
        self.assertEqual(changes["is_active"], [True, False])
        self.assertEqual(changes["services"][1], [str(self.service.pk)])

    def test_delete(self):
        template = self.form()
        delete_template(template=template, actor=self.owner)
        self.assertFalse(FormTemplate.objects.exists())
        self.assertTrue(AuditLog.objects.filter(action="form.deleted").exists())


class QuestionTests(FormTestCase):
    def test_choice_questions_need_two_different_options(self):
        template = self.form()
        for options, code in (
            (["Only one"], "invalid_options"),
            (["Yes", " yes "], "invalid_options"),
            ([""], "invalid_options"),
        ):
            with self.subTest(options=options), self.assertRaises(DomainError) as caught:
                self.question(template, type="select", options=options)
            self.assertEqual(caught.exception.code, code)
        question = self.question(template, type="multi_select", options=[" A ", "", "B"])
        self.assertEqual(question.options, ["A", "B"])

    def test_only_choice_questions_have_options(self):
        with self.assertRaises(DomainError) as caught:
            self.question(self.form(), type="text", options=["A", "B"])
        self.assertEqual(caught.exception.code, "invalid_options")

    def test_label_and_type_are_checked(self):
        template = self.form()
        with self.assertRaises(DomainError):
            self.question(template, label="  ")
        with self.assertRaises(DomainError):
            self.question(template, type="file_upload")

    def test_a_form_has_a_question_limit(self):
        template = self.form()
        with mock.patch("customer_forms.services.MAX_QUESTIONS", 2):
            self.question(template, label="One")
            self.question(template, label="Two")
            with self.assertRaises(DomainError) as caught:
                self.question(template, label="Three")
        self.assertEqual(caught.exception.code, "too_many")

    def test_move_and_delete_keep_positions_in_order(self):
        template = self.form()
        first = self.question(template, label="A")
        self.question(template, label="B")
        third = self.question(template, label="C")
        move_question(question=third, direction="up")
        draft = template.versions.get()
        self.assertEqual(self.labels(draft), ["A", "C", "B"])
        move_question(question=first, direction="up")  # already first: stays
        delete_question(question=first)
        self.assertEqual(self.labels(draft), ["C", "B"])
        self.assertEqual(list(draft.questions.values_list("position", flat=True)), [1, 2])
        with self.assertRaises(DomainError):
            move_question(question=third, direction="sideways")

    def test_question_edits_are_not_audited_one_by_one(self):
        template = self.form()
        question = self.question(template)
        update_question(question=question, label="Name")
        self.assertEqual(list(AuditLog.objects.values_list("action", flat=True)), ["form.created"])


class VersioningTests(FormTestCase):
    def setUp(self):
        super().setUp()
        self.template = self.form()
        self.name = self.question(self.template, label="Full name", required=True)
        self.question(self.template, label="Pressure", type="select", options=["Light", "Firm"])

    def test_publishing_needs_questions_and_changes(self):
        empty = self.form(name="Empty")
        with self.assertRaises(DomainError) as caught:
            publish(template=empty)
        self.assertEqual(caught.exception.code, "no_questions")
        publish(template=self.template)
        with self.assertRaises(DomainError) as caught:
            publish(template=self.template)
        self.assertEqual(caught.exception.code, "nothing_to_publish")

    def test_published_versions_never_change(self):
        v1 = publish(template=self.template, actor=self.owner)
        self.assertEqual((v1.number, v1.published_by), (1, self.owner))
        log = AuditLog.objects.get(action="form.published")
        self.assertEqual(log.metadata, {"version": 1, "questions": 2})

        # Editing a published question starts version 2, a copy with the same keys.
        copy = update_question(question=self.name, label="Your full name")
        v2 = copy.version
        self.assertEqual((v2.number, v2.is_draft), (2, True))
        self.assertNotEqual(copy.pk, self.name.pk)
        self.assertEqual(copy.key, self.name.key)
        self.assertEqual(self.labels(v1), ["Full name", "Pressure"])
        self.assertEqual(self.labels(v2), ["Your full name", "Pressure"])

        # Further edits go to the same draft, whichever version's question is addressed.
        self.question(self.template, label="Allergies")
        update_question(question=self.name, required=False)
        self.assertEqual(FormVersion.objects.filter(template=self.template).count(), 2)
        self.assertFalse(v2.questions.get(key=self.name.key).required)

        publish(template=self.template)
        state = form_state(self.template)
        self.assertEqual((state.published.number, state.draft, state.has_changes), (2, None, False))

    def test_discard_goes_back_to_the_published_version(self):
        publish(template=self.template)
        self.question(self.template, label="Extra")
        self.assertTrue(form_state(self.template).has_changes)
        discard_draft(template=self.template, actor=self.owner)
        state = form_state(self.template)
        self.assertEqual((state.draft, state.published.number), (None, 1))
        self.assertEqual([q.label for q in state.questions], ["Full name", "Pressure"])
        self.assertTrue(AuditLog.objects.filter(action="form.draft_discarded").exists())

    def test_discard_before_the_first_publish_empties_the_draft(self):
        discard_draft(template=self.template)
        version = self.template.versions.get()
        self.assertEqual((version.number, version.questions.count()), (1, 0))

    def test_a_draft_identical_to_the_published_version_has_no_changes(self):
        publish(template=self.template)
        move_question(question=self.name, direction="up")  # a no-op still opens a draft
        self.assertFalse(form_state(self.template).has_changes)

    def test_a_question_removed_from_the_draft_is_stale(self):
        publish(template=self.template)
        delete_question(question=self.name)
        with self.assertRaises(DomainError) as caught:
            update_question(question=self.name, label="Again")
        self.assertEqual(caught.exception.code, "stale")


class BuilderPageTests(FormTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_an_owner_builds_and_publishes_a_ten_question_intake_form(self):
        response = self.client.post(
            reverse("app-form-new"),
            {"name": "New client intake", "kind": "intake", "services": [self.service.pk]},
        )
        template = FormTemplate.objects.get()
        builder = reverse("app-form-builder", args=[template.pk])
        self.assertRedirects(response, builder)
        for data in TEN_QUESTIONS:
            response = self.client.post(reverse("app-form-question-new", args=[template.pk]), data)
            self.assertRedirects(response, builder)
        response = self.client.post(reverse("app-form-publish", args=[template.pk]))
        self.assertRedirects(response, builder)

        version = template.versions.get()
        self.assertEqual((version.number, version.is_draft), (1, False))
        questions = list(version.questions.all())
        self.assertEqual(len(questions), 10)
        self.assertEqual(questions[6].options, ["Light", "Medium", "Firm"])
        self.assertEqual(sum(q.required for q in questions), 5)

        page = self.client.get(builder)
        self.assertContains(page, "Version 1 is published")
        self.assertContains(page, "Preferred pressure")
        listing = self.client.get(reverse("app-form-list"))
        self.assertContains(listing, "New client intake")
        self.assertContains(listing, "Initial assessment")

    def test_invalid_options_are_shown_on_the_field(self):
        template = self.form()
        response = self.client.post(
            reverse("app-form-question-new", args=[template.pk]),
            {"label": "Pressure", "type": "select", "options": "Light"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("Give at least two options", response.context["form"].errors["options"][0])

    def test_htmx_moves_rerender_the_builder(self):
        template = self.form()
        self.question(template, label="First")
        second = self.question(template, label="Second")
        response = self.client.post(
            reverse("app-form-question-move", args=[second.pk]),
            {"to": "up"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertTrue(content.lstrip().startswith('<div id="builder"'))
        self.assertLess(content.index("Second"), content.index("First"))

    def test_publish_errors_show_in_the_builder(self):
        template = self.form()
        response = self.client.post(
            reverse("app-form-publish", args=[template.pk]), HTTP_HX_REQUEST="true"
        )
        self.assertContains(response, "Add at least one question before publishing")

    def test_editing_a_published_question_says_customers_keep_the_old_version(self):
        template = self.form()
        question = self.question(template)
        publish(template=template)
        self.client.post(
            reverse("app-form-question-edit", args=[question.pk]),
            {"label": "Your name", "type": "text"},
        )
        page = self.client.get(reverse("app-form-builder", args=[template.pk]))
        self.assertContains(page, "Unpublished changes")
        self.assertContains(page, "Customers still get version 1 until you publish.")

    def test_settings_and_delete(self):
        template = self.form()
        response = self.client.post(
            reverse("app-form-settings", args=[template.pk]),
            {"name": "Intake", "kind": "questionnaire", "description": "", "is_active": "on"},
        )
        self.assertRedirects(response, reverse("app-form-builder", args=[template.pk]))
        template.refresh_from_db()
        self.assertEqual((template.name, template.kind), ("Intake", "questionnaire"))
        response = self.client.post(reverse("app-form-delete", args=[template.pk]))
        self.assertRedirects(response, reverse("app-form-list"))
        self.assertFalse(FormTemplate.objects.exists())


class BuilderAccessTests(FormTestCase):
    def test_only_roles_with_forms_manage(self):
        for role, status in (
            (OrganizationRole.MANAGER, 200),
            (OrganizationRole.RECEPTIONIST, 403),
            (OrganizationRole.STAFF, 403),
        ):
            with self.subTest(role=role):
                self.client.force_login(f.MembershipFactory(organization=self.org, role=role).user)
                self.assertEqual(self.client.get(reverse("app-form-list")).status_code, status)

    def test_another_organizations_forms_are_not_found(self):
        other = f.OrganizationFactory()
        template = create_template(organization=other, name="Theirs")
        question = add_question(template=template, label="Q", type="text")
        self.client.force_login(self.owner)
        for url in (
            reverse("app-form-builder", args=[template.pk]),
            reverse("app-form-settings", args=[template.pk]),
            reverse("app-form-question-edit", args=[question.pk]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
        for url in (
            reverse("app-form-publish", args=[template.pk]),
            reverse("app-form-delete", args=[template.pk]),
            reverse("app-form-question-move", args=[question.pk]),
            reverse("app-form-question-delete", args=[question.pk]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url, {"to": "up"}).status_code, 404)
        self.assertTrue(FormQuestion.objects.filter(pk=question.pk).exists())
        self.assertNotContains(self.client.get(reverse("app-form-list")), "Theirs")


class FormApiTests(FormTestCase):
    def setUp(self):
        super().setUp()
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def test_build_publish_and_change_through_the_api(self):
        response = self.api.post(
            "/api/v1/forms/",
            {"name": "Intake", "kind": "intake", "services": [str(self.service.pk)]},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        form_id = response.data["id"]
        for label, kind, options in (
            ("Full name", "text", []),
            ("Pressure", "select", ["Light", "Firm"]),
        ):
            response = self.api.post(
                "/api/v1/form-questions/",
                {"form": form_id, "label": label, "type": kind, "options": options},
                format="json",
            )
            self.assertEqual(response.status_code, 201, response.data)
        response = self.api.post(f"/api/v1/forms/{form_id}/publish/")
        self.assertEqual((response.status_code, response.data["number"]), (200, 1))

        detail = self.api.get(f"/api/v1/forms/{form_id}/").data
        self.assertIsNone(detail["draft_version"])
        published = detail["published_version"]["questions"]
        self.assertEqual([q["label"] for q in published], ["Full name", "Pressure"])

        response = self.api.patch(
            f"/api/v1/form-questions/{published[0]['id']}/", {"label": "Name"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotEqual(response.data["id"], published[0]["id"])
        self.assertEqual((response.data["key"], response.data["version"]), (published[0]["key"], 2))
        detail = self.api.get(f"/api/v1/forms/{form_id}/").data
        self.assertEqual(detail["draft_version"]["number"], 2)

        response = self.api.post(
            f"/api/v1/form-questions/{response.data['id']}/move/", {"direction": "down"}
        )
        self.assertEqual(response.data["position"], 2)
        response = self.api.post(f"/api/v1/forms/{form_id}/discard-draft/")
        self.assertEqual(response.status_code, 204)

    def test_validation_errors_have_codes(self):
        template = self.form()
        response = self.api.post(
            "/api/v1/form-questions/",
            {"form": str(template.pk), "label": "Pick", "type": "select", "options": ["One"]},
            format="json",
        )
        self.assertEqual((response.status_code, response.data["code"]), (400, "invalid_options"))

    def test_isolation_and_permissions(self):
        other = f.OrganizationFactory()
        theirs = create_template(organization=other, name="Theirs")
        question = add_question(template=theirs, label="Q", type="text")
        self.assertEqual(self.api.get(f"/api/v1/forms/{theirs.pk}/").status_code, 404)
        self.assertEqual(self.api.get(f"/api/v1/form-questions/{question.pk}/").status_code, 404)
        response = self.api.post(
            "/api/v1/form-questions/",
            {"form": str(theirs.pk), "label": "Injected", "type": "text"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(FormQuestion.objects.filter(label="Injected").exists())
        self.assertEqual(self.api.get("/api/v1/forms/").data["count"], 0)

        receptionist = f.MembershipFactory(
            organization=self.org, role=OrganizationRole.RECEPTIONIST
        ).user
        self.api.force_authenticate(receptionist)
        self.assertEqual(self.api.get("/api/v1/forms/").status_code, 403)

    def test_question_list_filters_by_form_and_version(self):
        first = self.form()
        add_question(template=first, label="A", type="text")
        publish(template=first)
        add_question(template=first, label="B", type="text")  # version 2 draft: A, B
        second = self.form(name="Other")
        add_question(template=second, label="C", type="text")

        def labels(**params):
            response = self.api.get("/api/v1/form-questions/", params)
            self.assertEqual(response.status_code, 200)
            return [row["label"] for row in response.data["results"]]

        self.assertEqual(labels(form=str(first.pk), version=1), ["A"])
        self.assertEqual(labels(form=str(first.pk), version=2), ["A", "B"])
        self.assertEqual(labels(form=str(second.pk)), ["C"])
        self.assertEqual(labels(form="not-a-uuid"), [])
