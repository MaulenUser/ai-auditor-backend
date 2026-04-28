from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class BusinessProfile:
    company_name: str = ""
    website_url: str = ""
    instagram_url: str = ""
    price_list: str = ""
    average_ticket_kzt: float | None = None
    advantages: str = ""
    promotions: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> BusinessProfile:
        return cls(
            company_name=d.get("company_name") or "",
            website_url=d.get("website_url") or "",
            instagram_url=d.get("instagram_url") or "",
            price_list=d.get("price_list") or "",
            average_ticket_kzt=d.get("average_ticket_kzt"),
            advantages=d.get("advantages") or "",
            promotions=d.get("promotions") or "",
        )
