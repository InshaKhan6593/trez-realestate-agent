"""What the extractor model must return. It only reports what the buyer said;
every decision (tools, questions, scores, handoff) is made in code."""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import (BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, create_model,
                      field_validator, model_validator)

log = logging.getLogger(__name__)

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


# Each detail the buyer gives is reported with its evidence: the value, their own words it
# comes from (`said`, checked in code against their message) and how sure the reading is.
# Every kind of detail has its own value type and meaning, so the schema the model fills in
# says exactly what each field holds (one loose "any value" type for all of them left the
# meanings only in the prompt text).
PropertyKind = Literal["house", "flat", "plot", "commercial", "portion"]
_SAID = (str, Field(description="The buyer's own words this comes from, copied exactly"))
_SOURCE = (Literal["stated", "inferred"], Field(description="stated: they said it; inferred: implied by what they said"))
_CONFIDENCE = (float, Field(ge=0, le=1))


def _said(name: str, doc: str, value_type, value_doc: str, **validators) -> type[BaseModel]:
    return create_model(name, __doc__=doc, __validators__=validators or None,
                        value=(value_type, Field(description=value_doc)),
                        said=_SAID, source=_SOURCE, confidence=_CONFIDENCE)


def _as_list(cls, v):
    return [v] if isinstance(v, str) else v


PurposeSaid = _said("PurposeSaid", "Buy or rent, as they said it.", Literal["sale", "rent"],
                    "sale: they want to buy; rent: they want to rent")
KindsSaid = _said("KindsSaid", "Kinds of property, as they named them.", list[PropertyKind],
                  "Every kind they named (a flat may be called an apartment; a home, bungalow or villa "
                  "is a house)", one_kind=field_validator("value", mode="before")(_as_list))
PlaceSaid = _said("PlaceSaid", "A place from PLACES.", int, "The id of the place in PLACES")
PlaceTextSaid = _said("PlaceTextSaid", "A place that is not in PLACES.", str, "Their name for the place")
AmountSaid = _said("AmountSaid", "An amount of money.", int,
                   "Whole PKR: 1 crore = 10,000,000; 1 lakh = 100,000")
SizeSaid = _said("SizeSaid", "A size.", float,
                 "Square yards: 1 marla = 25, 1 kanal = 500, sq ft / 9; gaz = sq yd")
CountSaid = _said("CountSaid", "A number of rooms.", int, "Bedrooms (or rooms) asked for")
TimelineSaid = _said("TimelineSaid", "When they want it.",
                     Literal["under_1_month", "1_3_months", "3_6_months", "browsing"], "How soon")
PaymentSaid = _said("PaymentSaid", "How they will pay.",
                    Literal["cash", "installments", "bank_loan", "selling_first"], "Payment mode")
DeciderSaid = _said("DeciderSaid", "Who decides.", Literal["self", "with_family", "for_someone_else"],
                    "Who makes the decision")
UseSaid = _said("UseSaid", "What it is for.", Literal["live", "invest"], "To live in, or as an investment")
NameSaid = _said("NameSaid", "Their name.", str, "The name they gave")


def _all_required(schema: dict) -> None:
    schema["required"] = list(schema["properties"])


class Wants(BaseModel):
    """What the buyer wants to buy or rent, from these messages. Every field is null unless
    these messages say it."""
    # One field per kind of detail, each answered, rather than a free list: a second detail
    # in one sentence was dropped as a list ("3 rooms + house" kept both 20/20 this way, 3/12).
    model_config = ConfigDict(json_schema_extra=_all_required)

    @model_validator(mode="before")
    @classmethod
    def _readable_fields(cls, data):
        """A field the model filled wrongly is dropped on its own; the rest of the reading
        stays (one bad field used to fail the whole extraction). A bare value ("location_id":
        6655) is kept but quotes no evidence, so the evidence check drops it as not said."""
        if not isinstance(data, dict):
            return data
        out = {}
        for k, v in data.items():
            if k in cls.model_fields and v is not None and not isinstance(v, dict):
                v = {"value": v, "said": "", "source": "inferred", "confidence": 0.0}
            if k in cls.model_fields and v is not None:
                try:
                    _ADAPTERS[k].validate_python(v)
                except ValidationError as err:
                    log.warning("extractor field %s dropped: %s", k, err.errors()[0]["msg"])
                    v = None
            out[k] = v
        return out

    purpose: PurposeSaid | None = Field(None, description="Whether they want to buy or rent; null unless they say which")
    property_types: KindsSaid | None = Field(None, description="The kinds of property they are looking for")
    location_id: PlaceSaid | None = Field(None, description="The place they want, from PLACES (the most specific that fits)")
    location_text: PlaceTextSaid | None = Field(None, description="A place they want that is not in PLACES")
    budget_min: AmountSaid | None = Field(None, description="The least they would pay")
    budget_max: AmountSaid | None = Field(None, description="The most they would pay")
    size_min_sqyd: SizeSaid | None = Field(None, description="The smallest size they want")
    size_max_sqyd: SizeSaid | None = Field(None, description="The largest size they want")
    bedrooms_min: CountSaid | None = Field(None, description="The bedrooms they need")
    timeline: TimelineSaid | None = Field(None, description="When they want to buy or move")
    payment_mode: PaymentSaid | None = Field(None, description="How they will pay")
    decision_maker: DeciderSaid | None = Field(None, description="Who decides with them")
    use: UseSaid | None = Field(None, description="To live in or to invest")
    name: NameSaid | None = Field(None, description="Their name, if they give it")


_ADAPTERS = {k: TypeAdapter(f.annotation) for k, f in Wants.model_fields.items()}


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
