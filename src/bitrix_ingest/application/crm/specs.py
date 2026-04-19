"""Declarative export specs for CRM list entities.

Each entity is described by a spec rather than hand-written code so adding a
new entity is one dataclass entry. This is Open/Closed in practice: extending
the set of exported entities doesn't require changing the export service.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EntityExportSpec:
    """How to export one CRM list entity.

    Attributes:
        name: Log-friendly display name, e.g. ``"Deals"``.
        method: Bitrix API method, e.g. ``"crm.deal.list"``.
        output_file: Destination filename under the output directory.
        select: Fields to pull from Bitrix.
        modified_field: Field used for the ``>=`` date filter when the caller
            supplies ``--modified-from``. If ``None``, the filter is not applied.
    """

    name: str
    method: str
    output_file: str
    select: tuple[str, ...]
    modified_field: str | None = "DATE_MODIFY"


CRM_ENTITY_SPECS: tuple[EntityExportSpec, ...] = (
    EntityExportSpec(
        name="Deals",
        method="crm.deal.list",
        output_file="deals.json",
        select=(
            "ID", "TITLE", "STAGE_ID", "CATEGORY_ID", "ASSIGNED_BY_ID",
            "OPPORTUNITY", "CURRENCY_ID", "CONTACT_ID", "COMPANY_ID",
            "DATE_CREATE", "DATE_MODIFY", "CLOSEDATE", "CLOSED",
        ),
    ),
    EntityExportSpec(
        name="Contacts",
        method="crm.contact.list",
        output_file="contacts.json",
        select=(
            "ID", "NAME", "SECOND_NAME", "LAST_NAME", "TYPE_ID", "SOURCE_ID",
            "PHONE", "EMAIL", "ASSIGNED_BY_ID", "DATE_CREATE", "DATE_MODIFY",
        ),
    ),
    EntityExportSpec(
        name="Companies",
        method="crm.company.list",
        output_file="companies.json",
        select=(
            "ID", "TITLE", "COMPANY_TYPE", "SOURCE_ID", "PHONE", "EMAIL",
            "ASSIGNED_BY_ID", "DATE_CREATE", "DATE_MODIFY",
        ),
    ),
    EntityExportSpec(
        name="Leads",
        method="crm.lead.list",
        output_file="leads.json",
        select=(
            "ID", "TITLE", "STATUS_ID", "SOURCE_ID", "ASSIGNED_BY_ID",
            "OPPORTUNITY", "CURRENCY_ID", "DATE_CREATE", "DATE_MODIFY",
        ),
    ),
)


ACTIVITY_SPEC = EntityExportSpec(
    name="Activities",
    method="crm.activity.list",
    output_file="activities.json",
    select=(
        "ID", "TYPE_ID", "PROVIDER_ID", "PROVIDER_TYPE_ID", "SUBJECT",
        "OWNER_ID", "OWNER_TYPE_ID", "RESPONSIBLE_ID", "START_TIME",
        "END_TIME", "COMPLETED", "DIRECTION", "DESCRIPTION", "LAST_UPDATED",
    ),
    modified_field="LAST_UPDATED",
)
