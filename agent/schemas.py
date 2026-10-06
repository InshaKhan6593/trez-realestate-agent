"""What the extractor model must return. It only reports what the buyer said;
every decision (tools, questions, scores, handoff) is made in code."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

IntentType = Literal[
    "greeting",            # salam, hi
    "search",              # looking for something: "Askari mein flat chahiye"
    "listing_question",    # a question about a specific listing: parking, floors, installments
    "availability",        # "ye available hai?"
    "photos",              # wants pictures
    "video",               # wants a video
    "negotiation",         # last price, discount, "kam karo"
    "ask_human",           # wants to talk to a person / call
    "legal_or_documents",  # registry, NOC, transfer, ownership
    "more_options",        # "aur options?", "koi aur?"
    "not_interested",      # "nahi chahiye", "bas dekh raha tha"
    "seller",              # wants to sell or rent out their own property
    "thanks",
    "other",
]

Slot = Literal[
    "purpose",             # "sale" (buy) or "rent"
    "property_types",      # list of: house, flat, plot, commercial, portion
    "location_id",         # id chosen from the place list given in the prompt
    "location_text",       # what they called the place, if no id fits
    "budget_min",          # PKR, whole number
    "budget_max",          # PKR, whole number ("4 cr tak" -> 40000000)
    "size_min_sqyd",
    "size_max_sqyd",
    "bedrooms_min",
    "timeline",            # "under_1_month" | "1_3_months" | "3_6_months" | "browsing"
    "payment_mode",        # "cash" | "installments" | "bank_loan" | "selling_first"
    "decision_maker",      # "self" | "with_family" | "for_someone_else"
    "use",                 # "live" | "invest"
    "name",
]


class ListingRef(BaseModel):
    zameen_id: int | None = Field(None, description="Only if a Zameen number or link is in the message")
    from_history: str | None = Field(
        None, description="How they refer to one we discussed: 'the DHA one', 'the first one', 'the cheaper one'")


class Intent(BaseModel):
    type: IntentType
    listing: ListingRef | None = None
    topic: str | None = Field(None, description="For listing_question: what about it (parking, floors, gas, ...)")
    question: str | None = Field(None, description="The buyer's question, in their words")


class SlotUpdate(BaseModel):
    slot: Slot
    value: str | int | float | list[str] | None
    source: Literal["stated", "inferred"] = Field(description="stated: they said it; inferred: implied")
    confidence: float = Field(ge=0, le=1)


class Extraction(BaseModel):
    language: Literal["roman_urdu", "urdu", "english", "mixed"]
    intents: list[Intent]
    slot_updates: list[SlotUpdate] = Field(default_factory=list)
    signals: list[Literal["visit_request", "token_or_bayana", "cash_ready", "urgent",
                          "dealer", "frustrated", "repeat_question"]] = Field(default_factory=list)
    rejected: list[ListingRef] = Field(default_factory=list, description="Listings they said no to")
    rejected_reason: str | None = None
