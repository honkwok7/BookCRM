"""Forms API (``/api/v1/forms/`` and ``/api/v1/form-questions/``). Everything needs
``forms.manage``. Writes go through customer_forms.services, which audits them."""

import uuid

from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import mixins, permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from core.api import AuditedModelViewSetMixin
from core.permissions import HasCapability
from customer_forms.models import FormQuestion, FormTemplate
from customer_forms.serializers import (
    FormQuestionSerializer,
    FormTemplateSerializer,
    VersionSerializer,
)
from customer_forms.services import (
    delete_question,
    delete_template,
    discard_draft,
    move_question,
    publish,
)
from organizations.selectors import get_request_organization, scope_queryset_by_organization

FORMS_MANAGE = HasCapability(read="forms.manage", write="forms.manage")


class FormTemplateViewSet(AuditedModelViewSetMixin, viewsets.ModelViewSet):
    """Forms with their latest published version and their draft (if it has changes).

    Deleting a form customers have been given gives 409 ``in_use``: deactivate it instead.
    """

    audited_by_service = ("create", "update", "delete")
    serializer_class = FormTemplateSerializer
    permission_classes = [permissions.IsAuthenticated, FORMS_MANAGE]
    search_fields = ("name", "description")
    ordering_fields = ("name", "created_at")

    def get_queryset(self):
        queryset = FormTemplate.objects.select_related("organization").prefetch_related("services")
        return scope_queryset_by_organization(queryset, self.request)

    def perform_destroy(self, instance):
        delete_template(template=instance, actor=self.request.user)

    @extend_schema(request=None, responses=VersionSerializer)
    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        """Publish the draft: customers are given this version from now on."""
        version = publish(template=self.get_object(), actor=request.user)
        return Response(VersionSerializer(version).data)

    @extend_schema(request=None, responses={204: None})
    @action(detail=True, methods=["post"], url_path="discard-draft")
    def discard_draft(self, request, pk=None):
        """Throw away unpublished changes."""
        discard_draft(template=self.get_object(), actor=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class FormQuestionViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """Add, change, reorder or remove a form's questions (always in its draft).

    The list holds the questions of every version; ``?form=<id>`` narrows it to one form and
    ``?version=<number>`` to one version. ``/forms/{id}/`` shows the current ones."""

    serializer_class = FormQuestionSerializer
    permission_classes = [permissions.IsAuthenticated, FORMS_MANAGE]

    def get_queryset(self):
        organization = get_request_organization(self.request)
        if organization is None:
            return FormQuestion.objects.none()
        queryset = FormQuestion.objects.select_related("version__template").filter(
            version__template__organization=organization
        )
        if self.action == "list":
            form = self.request.query_params.get("form")
            version = self.request.query_params.get("version")
            if form:
                try:
                    queryset = queryset.filter(version__template_id=uuid.UUID(form))
                except ValueError:
                    queryset = queryset.none()
            if version:
                queryset = (
                    queryset.filter(version__number=int(version))
                    if version.isdigit()
                    else queryset.none()
                )
            queryset = queryset.order_by("version__template__name", "-version__number", "position")
        return queryset

    def perform_destroy(self, instance):
        delete_question(question=instance, actor=self.request.user)

    @extend_schema(
        request=inline_serializer(
            "FormQuestionMove", {"direction": serializers.ChoiceField(choices=["up", "down"])}
        ),
        responses=FormQuestionSerializer,
    )
    @action(detail=True, methods=["post"])
    def move(self, request, pk=None):
        question = move_question(
            question=self.get_object(),
            actor=request.user,
            direction=request.data.get("direction", ""),
        )
        return Response(self.get_serializer(question).data)
