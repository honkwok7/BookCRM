from rest_framework import serializers

from core.api import TenantPrimaryKeyRelatedField, TenantScopedModelSerializer
from customer_forms.models import FormQuestion, FormTemplate, FormVersion
from customer_forms.selectors import form_state
from customer_forms.services import (
    add_question,
    create_template,
    update_question,
    update_template,
)
from organizations.selectors import get_request_organization
from services.models import Service


class QuestionReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = FormQuestion
        fields = ("id", "key", "position", "type", "label", "help_text", "required", "options")


class VersionSerializer(serializers.ModelSerializer):
    questions = QuestionReadSerializer(many=True, read_only=True)

    class Meta:
        model = FormVersion
        fields = ("id", "number", "published_at", "questions")


class FormTemplateSerializer(TenantScopedModelSerializer):
    services = TenantPrimaryKeyRelatedField(
        queryset=Service.objects.all(),
        many=True,
        required=False,
        help_text="Booking one of these services gives the customer this form.",
    )
    published_version = serializers.SerializerMethodField()
    draft_version = serializers.SerializerMethodField()

    class Meta:
        model = FormTemplate
        fields = (
            "id",
            "organization",
            "name",
            "kind",
            "description",
            "is_active",
            "services",
            "published_version",
            "draft_version",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "organization", "created_at", "updated_at")

    def _state(self, template):
        cache = self.context.setdefault("_form_states", {})
        if template.pk not in cache:
            cache[template.pk] = form_state(template)
        return cache[template.pk]

    def get_published_version(self, template) -> dict | None:
        version = self._state(template).published
        return VersionSerializer(version).data if version else None

    def get_draft_version(self, template) -> dict | None:
        """Unpublished changes (or the first version, not yet published); null when none."""
        state = self._state(template)
        return VersionSerializer(state.draft).data if state.has_changes else None

    def create(self, validated_data):
        request = self.context["request"]
        services = validated_data.pop("services", [])
        return create_template(
            organization=get_request_organization(request),
            actor=request.user,
            services=services,
            **validated_data,
        )

    def update(self, instance, validated_data):
        services = validated_data.pop("services", None)
        return update_template(
            template=instance,
            actor=self.context["request"].user,
            services=services,
            **validated_data,
        )


class FormQuestionSerializer(serializers.ModelSerializer):
    """A question. Changes always go to the form's draft: editing a published question returns
    its copy in the new draft, with a new ``id`` and the same ``key``."""

    form = serializers.PrimaryKeyRelatedField(
        queryset=FormTemplate.objects.all(),
        source="version.template",
        help_text="The form to add the question to (create only).",
    )
    version = serializers.IntegerField(source="version.number", read_only=True)
    options = serializers.ListField(
        child=serializers.CharField(allow_blank=True, trim_whitespace=False),
        required=False,
        help_text="Choice questions only: the options, in order.",
    )

    class Meta:
        model = FormQuestion
        fields = (
            "id",
            "form",
            "version",
            "key",
            "position",
            "type",
            "label",
            "help_text",
            "required",
            "options",
        )
        read_only_fields = ("id", "version", "key", "position")

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        organization = get_request_organization(request) if request else None
        fields["form"].queryset = FormTemplate.objects.filter(organization=organization)
        if self.instance is not None:
            fields["form"].read_only = True
        return fields

    def create(self, validated_data):
        template = validated_data.pop("version")["template"]
        return add_question(template=template, actor=self.context["request"].user, **validated_data)

    def update(self, instance, validated_data):
        validated_data.pop("version", None)
        return update_question(
            question=instance, actor=self.context["request"].user, **validated_data
        )
