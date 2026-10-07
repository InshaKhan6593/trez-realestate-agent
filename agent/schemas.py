"""What the extractor model must return. It only reports what the buyer said;
every decision (tools, questions, scores, handoff) is made in code."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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
    zameen_id: int | None = Field(
        None, description="The Zameen number in a link or listing number in the message; otherwise the "
                          "zameen_id of the listing they mean from OUR_LAST_LIST / LISTINGS_DISCUSSED, "
                          "when you can tell which one it is")
    from_history: str | None = Field(
        None, description="Only when you cannot tell which listing they mean: how they referred to it")


class Intent(BaseModel):
    type: IntentType
    listing: ListingRef | None = None
    topic: str | None = Field(None, description="For listing_question: what about it (parking, floors, gas, ...)")
    question: str | None = Field(None, description="The buyer's question, in their words")


class SlotUpdate(BaseModel):
    # `said` is required for the model (the schema lists it), optional in code.
    model_config = ConfigDict(json_schema_extra=lambda s: s.setdefault("required", []).append("said"))

    slot: Slot
    value: str | int | float | list[str] | None
    said: str | None = Field(None, description="Their own words this value comes from, copied exactly "
                                               "('askari 5', 'emaar', '4 cr tak')")
    source: Literal["stated", "inferred"] = Field(description="stated: they said it; inferred: implied")
    confidence: float = Field(ge=0, le=1)


class Extraction(BaseModel):
    # Signals come before intents, and are required, so the model decides them first:
    # written last, they were dropped (a token offer was caught 1 time in 10 after a long chat).
    model_config = ConfigDict(json_schema_extra=lambda s: s.setdefault("required", []).append("signals"))

    language: Literal["roman_urdu", "urdu", "english", "mixed"]
    signals: list[Literal["visit_request", "token_or_bayana", "cash_ready", "urgent",
                          "dealer", "frustrated", "repeat_question"]] = Field(
        default_factory=list, description="Buying signals in THESE messages; [] if none")
    intents: list[Intent]
    slot_updates: list[SlotUpdate] = Field(default_factory=list)
    rejected: list[ListingRef] = Field(default_factory=list, description="Listings they said no to")
    rejected_reason: str | None = None
