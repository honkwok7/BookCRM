"""Capability registry: the single source of truth for tenant authorization.

Views check capabilities, never role names. Roles only supply a default capability set;
individual memberships can be granted extra capabilities or have defaults revoked.
docs/PERMISSIONS.md is generated from this module (see test_permissions_doc_matches_registry).
"""

from __future__ import annotations

from enum import StrEnum

from organizations.models import OrganizationRole


class Capability(StrEnum):
    ORGANIZATION_VIEW = "organization.view"
    ORGANIZATION_MANAGE = "organization.manage"
    BILLING_VIEW = "billing.view"
    BILLING_MANAGE = "billing.manage"
    MEMBERS_VIEW = "members.view"
    MEMBERS_INVITE = "members.invite"
    STAFF_VIEW = "staff.view"
    STAFF_MANAGE = "staff.manage"
    SERVICES_VIEW = "services.view"
    SERVICES_MANAGE = "services.manage"
    LOCATIONS_MANAGE = "locations.manage"
    APPOINTMENTS_VIEW_ALL = "appointments.view_all"
    APPOINTMENTS_MANAGE = "appointments.manage"
    CUSTOMERS_VIEW = "customers.view"
    CUSTOMERS_MANAGE = "customers.manage"
    CUSTOMERS_NOTES_PRIVATE = "customers.notes.private"
    CUSTOMERS_ERASE = "customers.erase"
    WAITLIST_MANAGE = "waitlist.manage"
    REPORTS_VIEW = "reports.view"
    COMMUNICATIONS_MANAGE = "communications.manage"
    FORMS_MANAGE = "forms.manage"
    SETTINGS_MANAGE = "settings.manage"
    AUDIT_VIEW = "audit.view"


CAPABILITY_DESCRIPTIONS: dict[Capability, str] = {
    Capability.ORGANIZATION_VIEW: "View the organization profile",
    Capability.ORGANIZATION_MANAGE: "Edit the organization profile, branding and booking policies",
    Capability.BILLING_VIEW: "View the subscription and invoices",
    Capability.BILLING_MANAGE: "Change the subscription plan and billing details",
    Capability.MEMBERS_VIEW: "View team members and their roles",
    Capability.MEMBERS_INVITE: "Invite team members (up to the inviter's own role)",
    Capability.STAFF_VIEW: "View staff profiles and schedules",
    Capability.STAFF_MANAGE: "Create and edit staff profiles, availability and time off",
    Capability.SERVICES_VIEW: "View services and categories",
    Capability.SERVICES_MANAGE: "Create and edit services and categories",
    Capability.LOCATIONS_MANAGE: "Create and edit locations",
    Capability.APPOINTMENTS_VIEW_ALL: "View every appointment (otherwise only one's own)",
    Capability.APPOINTMENTS_MANAGE: "Book, reschedule, cancel and change appointment status",
    Capability.CUSTOMERS_VIEW: "View customer records",
    Capability.CUSTOMERS_MANAGE: "Create and edit customer records",
    Capability.CUSTOMERS_NOTES_PRIVATE: "Read and write internal customer notes",
    Capability.CUSTOMERS_ERASE: "Delete or anonymize customer records (irreversible)",
    Capability.WAITLIST_MANAGE: "View and manage the waitlist",
    Capability.REPORTS_VIEW: "View dashboards and reports",
    Capability.COMMUNICATIONS_MANAGE: "Edit notification templates and reminders",
    Capability.FORMS_MANAGE: "Create and edit customer forms",
    Capability.SETTINGS_MANAGE: "Change organization settings",
    Capability.AUDIT_VIEW: "View the organization audit log",
}

_ALL = frozenset(Capability)

ROLE_CAPABILITIES: dict[str, frozenset[Capability]] = {
    OrganizationRole.OWNER: _ALL,
    OrganizationRole.MANAGER: _ALL
    - {
        Capability.ORGANIZATION_MANAGE,
        Capability.BILLING_VIEW,
        Capability.BILLING_MANAGE,
        Capability.COMMUNICATIONS_MANAGE,
        Capability.SETTINGS_MANAGE,
        Capability.AUDIT_VIEW,
    },
    OrganizationRole.RECEPTIONIST: frozenset(
        {
            Capability.ORGANIZATION_VIEW,
            Capability.STAFF_VIEW,
            Capability.SERVICES_VIEW,
            Capability.APPOINTMENTS_VIEW_ALL,
            Capability.APPOINTMENTS_MANAGE,
            Capability.CUSTOMERS_VIEW,
            Capability.CUSTOMERS_MANAGE,
            Capability.WAITLIST_MANAGE,
        }
    ),
    # Providers work with their own appointments; object-level scoping lives in the selectors.
    OrganizationRole.STAFF: frozenset(
        {
            Capability.ORGANIZATION_VIEW,
            Capability.STAFF_VIEW,
            Capability.SERVICES_VIEW,
            Capability.APPOINTMENTS_MANAGE,
        }
    ),
    OrganizationRole.CUSTOMER: frozenset({Capability.ORGANIZATION_VIEW}),
}

# Higher rank may grant any role at or below its own; used for invitations.
ROLE_RANK: dict[str, int] = {
    OrganizationRole.CUSTOMER: 0,
    OrganizationRole.STAFF: 1,
    OrganizationRole.RECEPTIONIST: 2,
    OrganizationRole.MANAGER: 3,
    OrganizationRole.OWNER: 4,
}


def validate_capability_codes(codes) -> list[str]:
    """Return the unknown codes in ``codes`` (empty list means all valid)."""
    known = {c.value for c in Capability}
    return [code for code in codes or [] if code not in known]


def capabilities_for(role: str, granted=(), revoked=()) -> frozenset[Capability]:
    base = ROLE_CAPABILITIES.get(role, frozenset())
    extra = {Capability(c) for c in granted or [] if c in Capability._value2member_map_}
    removed = {Capability(c) for c in revoked or [] if c in Capability._value2member_map_}
    return frozenset((base | extra) - removed)


def can_grant_role(granter_role: str, target_role: str) -> bool:
    return ROLE_RANK.get(target_role, 99) <= ROLE_RANK.get(granter_role, -1)


PERMISSIONS_DOC_PATH = "docs/PERMISSIONS.md"


def render_permissions_markdown() -> str:
    """Render docs/PERMISSIONS.md from this registry (``manage.py generate_permissions_doc``)."""
    roles = list(ROLE_RANK)[::-1]  # owner first
    labels = dict(OrganizationRole.choices)
    lines = [
        "# Permissions",
        "",
        "<!-- Generated by `python manage.py generate_permissions_doc`. Do not edit by hand. -->",
        "",
        "Authorization inside an organization is **capability-based**. Each membership has a role",
        "that supplies a default set of capabilities; an owner can additionally grant or revoke",
        "individual capabilities per membership (`granted_permissions` / `revoked_permissions`).",
        "Views check capabilities, never role names. Source of truth:",
        "`organizations/permissions.py`.",
        "",
        "Rules that sit outside the matrix:",
        "",
        "- A tenant context exists only through an active membership of an active, non-suspended",
        "  organization. A client-supplied organization slug only selects among the user's own",
        "  memberships (see `organizations/tenancy.py`).",
        "- Platform superusers have **no** implicit access to tenant data.",
        "- Invitations: a member can only invite roles at or below their own role.",
        "- Staff without `appointments.view_all` see only appointments assigned to them;",
        "  customers see only their own appointments (linked account or verified email).",
        "",
        "## Default capabilities by role",
        "",
        "| Capability | " + " | ".join(labels[r] for r in roles) + " | Description |",
        "|---|" + "---|" * len(roles) + "---|",
    ]
    for capability in Capability:
        marks = ["✅" if capability in ROLE_CAPABILITIES[r] else "–" for r in roles]
        lines.append(
            f"| `{capability.value}` | "
            + " | ".join(marks)
            + f" | {CAPABILITY_DESCRIPTIONS[capability]} |"
        )
    lines += [
        "",
        "## Role ranks (invitation ceiling)",
        "",
        "| Role | Rank |",
        "|---|---|",
    ]
    lines += [f"| {labels[r]} | {ROLE_RANK[r]} |" for r in roles]
    return "\n".join(lines) + "\n"
