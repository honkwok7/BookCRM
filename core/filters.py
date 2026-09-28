"""Tenant-safe django-filter helpers.

Relation filters (``?service=<id>``) must validate ids against the caller's organization only.
Otherwise the "invalid choice" error would reveal whether another organization's id exists.
"""

import django_filters

from organizations.selectors import get_request_organization


def tenant_queryset(model):
    """Callable queryset for ``ModelChoiceFilter``: rows of the request's organization only."""

    def queryset(request):
        organization = get_request_organization(request) if request is not None else None
        if organization is None:
            return model.objects.none()
        return model.objects.filter(organization=organization)

    return queryset


class TenantModelChoiceFilter(django_filters.ModelChoiceFilter):
    def __init__(self, model, **kwargs):
        super().__init__(queryset=tenant_queryset(model), **kwargs)
