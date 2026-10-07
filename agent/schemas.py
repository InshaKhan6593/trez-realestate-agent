"""What the extractor model must return. It only reports what the buyer said;
every decision (tools, questions, scores, handoff) is made in code."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    """One thing the buyer wants (the form the code works with)."""
    slot: Slot
    value: str | int | float | list[str] | None
    said: str | None = Field(None, description="Their own words this value comes from, copied exactly")
    source: Literal["stated", "inferred"] = Field(description="stated: they said it; inferred: implied")
    confidence: float = Field(ge=0, le=1)


class Said(BaseModel):
    """A value the buyer gave, with the words it came from."""
    value: str | int | float | list[str]
    said: str = Field(description="Their own words this value comes from, copied exactly")
    source: Literal["stated", "inferred"] = Field(description="stated: they said it; inferred: implied")
    confidence: float = Field(ge=0, le=1)


def _all_required(schema: dict) -> None:
    schema["required"] = list(schema["properties"])


class Wants(BaseModel):
    """What the buyer wants to buy or rent, from THESE messages. One field per kind of detail,
    each null unless these messages say it: answering every field, rather than listing what
    comes to mind, is what keeps a second detail in one sentence from being dropped (measured:
    "3 rooms + house" kept both 20/20 this way, 3/12 as a free list)."""
    model_config = ConfigDict(json_schema_extra=_all_required)

    purpose: Said | None = None
    property_types: Said | None = None
    location_id: Said | None = None
    location_text: Said | None = None
    budget_min: Said | None = None
    budget_max: Said | None = None
    size_min_sqyd: Said | None = None
    size_max_sqyd: Said | None = None
    bedrooms_min: Said | None = None
    timeline: Said | None = None
    payment_mode: Said | None = None
    decision_maker: Said | None = None
    use: Said | None = None
    name: Said | None = None


class Extraction(BaseModel):
    # Signals come before intents, and are required, so the model decides them first:
    # written last, they were dropped (a token offer was caught 1 time in 10 after a long chat).
    model_config = ConfigDict(json_schema_extra=lambda s: (
        s.setdefault("required", []).extend(["signals", "wants"]),
        s["properties"].pop("slot_updates", None)))

    language: Literal["roman_urdu", "urdu", "english", "mixed"]
    signals: list[Literal["visit_request", "token_or_bayana", "cash_ready", "urgent",
                          "dealer", "frustrated", "repeat_question"]] = Field(
        default_factory=list, description="Buying signals in THESE messages; [] if none")
    intents: list[Intent]
    wants: Wants = Field(default_factory=Wants)
    rejected: list[ListingRef] = Field(default_factory=list, description="Listings they said no to")
    rejected_reason: str | None = None
    # The code's form of `wants` (not shown to the model). Filled from `wants`; tests may pass it directly.
    slot_updates: list[SlotUpdate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _wants_as_slot_updates(self) -> "Extraction":
        if not self.slot_updates:
            self.slot_updates = [SlotUpdate(slot=name, **said.model_dump())
                                 for name, said in self.wants if said is not None]
        return self
