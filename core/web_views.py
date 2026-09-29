from django.core.paginator import Paginator
from django.views.generic import TemplateView

from core.audit import AuditAction
from core.models import AuditLog
from core.web import HtmxPartialMixin, TenantPageMixin
from organizations.permissions import Capability


class LandingPageView(TemplateView):
    template_name = "web/landing.html"


class FeaturesPageView(TemplateView):
    template_name = "web/features.html"


class PricingPageView(TemplateView):
    template_name = "web/pricing.html"


ACTION_CODES = sorted(a.value for a in AuditAction if a is not AuditAction.SYSTEM_TEST)


class AuditLogView(TenantPageMixin, HtmxPartialMixin, TemplateView):
    """The organization's audit trail, newest first, filterable by action."""

    template_name = "core/audit_log.html"
    required_capabilities = (Capability.AUDIT_VIEW,)
    page_size = 50

    def get_context_data(self, **kwargs):
        action = self.request.GET.get("action", "")
        entries = AuditLog.objects.filter(organization=self.tenant.organization).select_related(
            "user"
        )
        if action in AuditAction._value2member_map_:
            entries = entries.filter(action=action)
        page = Paginator(entries, self.page_size).get_page(self.request.GET.get("page"))
        return super().get_context_data(**kwargs) | {
            "page_obj": page,
            "action": action,
            "filters": [
                {
                    "name": "action",
                    "label": "Action",
                    "value": action,
                    "options": [(code, code) for code in ACTION_CODES],
                    "all_label": "All actions",
                }
            ],
            "filtered": bool(action),
        }
