"""The organization app's sidebar, driven by capabilities.

An item is shown only when the member holds every capability its page requires. The page's
own view enforces the same capabilities (``TenantPageMixin.required_capabilities``); the test
suite checks that the two agree, so a link is never shown for a page that would refuse access.
New screens register here as they are built.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from django.urls import reverse

from bookings.calendar import can_use_calendar
from crm.permissions import can_browse_customers
from organizations.permissions import Capability
from organizations.tenancy import TenantContext


@dataclass(frozen=True)
class NavItem:
    label: str
    url_name: str
    icon: str
    capabilities: tuple[Capability, ...] = ()
    # For pages whose rule is more than "holds these capabilities" (the same function the
    # page itself checks), e.g. crm.permissions.can_browse_customers.
    allow: Callable[[TenantContext], bool] | None = None

    def allowed(self, tenant: TenantContext) -> bool:
        if not tenant.has(*self.capabilities):
            return False
        return self.allow is None or self.allow(tenant)


@dataclass(frozen=True)
class NavSection:
    label: str
    items: tuple[NavItem, ...]


APP_NAVIGATION: tuple[NavSection, ...] = (
    NavSection(
        "",
        (
            NavItem("Dashboard", "app-dashboard", "home"),
            NavItem(
                "Reception",
                "app-reception",
                "clipboard",
                (Capability.APPOINTMENTS_MANAGE, Capability.APPOINTMENTS_VIEW_ALL),
            ),
            NavItem("My schedule", "staff-dashboard", "calendar"),
        ),
    ),
    NavSection(
        "Appointments",
        (
            NavItem("Calendar", "app-calendar", "calendar", allow=can_use_calendar),
            NavItem("Appointments", "app-appointment-list", "clipboard", allow=can_use_calendar),
            NavItem("Waitlist", "app-waitlist", "inbox", (Capability.WAITLIST_MANAGE,)),
        ),
    ),
    NavSection(
        "CRM",
        (NavItem("Customers", "crm-customer-list", "users", allow=can_browse_customers),),
    ),
    NavSection(
        "Organization",
        (
            NavItem("Services", "app-service-list", "clipboard", (Capability.SERVICES_VIEW,)),
            NavItem("Staff", "app-staff-list", "user", (Capability.STAFF_VIEW,)),
            NavItem("Locations", "app-location-list", "map-pin", (Capability.LOCATIONS_VIEW,)),
            NavItem("Team", "app-team", "building", (Capability.MEMBERS_VIEW,)),
            NavItem("Audit log", "app-audit-log", "clipboard", (Capability.AUDIT_VIEW,)),
        ),
    ),
)


def navigation_for(tenant: TenantContext, current_url_name: str | None) -> list[dict]:
    sections = []
    for section in APP_NAVIGATION:
        items = [
            {
                "label": item.label,
                "url": reverse(item.url_name),
                "icon": item.icon,
                "active": item.url_name == current_url_name,
            }
            for item in section.items
            if item.allowed(tenant)
        ]
        if items:
            sections.append({"label": section.label, "items": items})
    return sections
