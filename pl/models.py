"""The schema, in path B's shape.

Nothing here is narrowed for the single-user path A build: `card.user_id` and
`app_user` exist from the first row written, so commercialising later needs no
migration. What path A omits is *behaviour* — signup, billing, leagues — not
structure.

Three tables are additions to the specification's data model, each justified
against an acceptance criterion:

  pattern      what a population='pattern' card refers to. The spec has no table
               for it, and `node` is not it — a node carries several rules across
               several paradigm classes.
  attempt      one submission fans out to N reviews and M error events. Without a
               parent, the submitted text and latency duplicate across every row
               and nothing ties the fan-out together (criteria 10, 18).
  node_unlock  the unlock latch. A monotone column cannot latch a *ratio*,
               because the denominator moves when a pattern card is created
               lazily or a curriculum edit adds a stratum (criterion 13).

`form.morph_tag` is a plain string and needs no parsed representation: feature
comparison happens in Python over one lexeme's paradigm — tens of rows, loaded by
`(lexeme_id, surface)` — and never as a SQL predicate.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------- lexicon


class Lexeme(Base):
    __tablename__ = "lexeme"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Full Morfeusz identifier — `kot:Sm2`, not `kot`. The citation form alone
    #: is ambiguous: `kot` is both the animal (m2) and a colloquial personal
    #: noun (m1), and they take different accusatives.
    lemma: Mapped[str] = mapped_column(String(64), unique=True)
    pos: Mapped[str] = mapped_column(String(16))
    gender: Mapped[str | None] = mapped_column(String(8))
    #: `animate` / `inanimate` / None. Derived from the three-valued SGJP gender:
    #: accusative borrows the genitive for m1 and m2, the nominative for m3.
    animacy: Mapped[str | None] = mapped_column(String(16))
    aspect: Mapped[str | None] = mapped_column(String(8))
    aspect_partner_id: Mapped[int | None] = mapped_column(ForeignKey("lexeme.id"))
    #: Stratification key for pattern cards. Derived from the lexeme's own
    #: endings, never from gender — see pl.morph.paradigm_class.
    paradigm_class: Mapped[str | None] = mapped_column(String(64), index=True)
    frequency_rank: Mapped[int | None] = mapped_column(Integer)

    forms: Mapped[list[Form]] = relationship(back_populates="lexeme")
    senses: Mapped[list[Sense]] = relationship(back_populates="lexeme")


class Form(Base):
    __tablename__ = "form"
    __table_args__ = (
        UniqueConstraint("lexeme_id", "morph_tag", name="form_cell_unique"),
        # The hot read builds an ExpectedSlot from one lexeme's paradigm.
        Index("form_lexeme_surface", "lexeme_id", "surface"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    lexeme_id: Mapped[int] = mapped_column(ForeignKey("lexeme.id"))
    surface: Mapped[str] = mapped_column(String(64))
    morph_tag: Mapped[str] = mapped_column(String(96))
    is_irregular: Mapped[bool] = mapped_column(default=False)

    lexeme: Mapped[Lexeme] = relationship(back_populates="forms")


class Sense(Base):
    __tablename__ = "sense"

    id: Mapped[int] = mapped_column(primary_key=True)
    lexeme_id: Mapped[int] = mapped_column(ForeignKey("lexeme.id"))
    en_gloss: Mapped[str] = mapped_column(String(128))
    register: Mapped[str | None] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(String(512))

    lexeme: Mapped[Lexeme] = relationship(back_populates="senses")


# --------------------------------------------------------------- curriculum


class Node(Base):
    __tablename__ = "node"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(48), unique=True)
    #: grammar | vocabulary | function. Decides which card populations an item
    #: under this node scores, and which population gates the node's unlock.
    type: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(128))
    cefr_level: Mapped[str | None] = mapped_column(String(8))
    explanation_md: Mapped[str | None] = mapped_column(String(4096))


class NodePrereq(Base):
    __tablename__ = "node_prereq"

    node_id: Mapped[int] = mapped_column(ForeignKey("node.id"), primary_key=True)
    prereq_node_id: Mapped[int] = mapped_column(
        ForeignKey("node.id"), primary_key=True
    )


class NodeLexeme(Base):
    __tablename__ = "node_lexeme"

    node_id: Mapped[int] = mapped_column(ForeignKey("node.id"), primary_key=True)
    lexeme_id: Mapped[int] = mapped_column(ForeignKey("lexeme.id"), primary_key=True)


class Pattern(Base):
    """One grammatical rule, restricted to one paradigm class.

    Stratified so that every draw within a card is homogeneous in difficulty.
    FSRS estimates difficulty per card against a fixed content; a card that
    resampled across `sklep` and `chleb` would have no stable difficulty to
    estimate.
    """

    __tablename__ = "pattern"
    __table_args__ = (
        UniqueConstraint("rule_key", "paradigm_class", name="pattern_stratum_unique"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("node.id"))
    rule_key: Mapped[str] = mapped_column(String(64))
    paradigm_class: Mapped[str] = mapped_column(String(64))


class Item(Base):
    __tablename__ = "item"

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("node.id"))
    pattern_id: Mapped[int | None] = mapped_column(ForeignKey("pattern.id"))
    exercise_type: Mapped[str] = mapped_column(String(32))
    prompt: Mapped[str] = mapped_column(String(256))
    gloss: Mapped[str | None] = mapped_column(String(256))
    expected_answer: Mapped[str] = mapped_column(String(96))
    target_form_id: Mapped[int | None] = mapped_column(ForeignKey("form.id"))
    #: Distractors for multiple choice, as a JSON list. Never sent with the
    #: correct answer marked.
    options_json: Mapped[list | None] = mapped_column(JSON)
    #: template | generated. The seam the M2 LLM pipeline writes through.
    source: Mapped[str] = mapped_column(String(16), default="template")
    audio_url: Mapped[str | None] = mapped_column(String(256))
    difficulty: Mapped[float | None] = mapped_column(Float)


class ItemVariant(Base):
    __tablename__ = "item_variant"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id"))
    accepted_answer: Mapped[str] = mapped_column(String(96))
    #: authored | promoted. `promoted` is how a learner's valid answer enters the
    #: accepted set; nothing auto-promotes at M1.
    source: Mapped[str] = mapped_column(String(16), default="authored")


# --------------------------------------------------------------- learner state


class AppUser(Base):
    #: Not `user` — that is a reserved word in Postgres and quoting it everywhere
    #: is a standing tax for no benefit.
    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str | None] = mapped_column(String(256), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    #: Carries the IANA timezone the streak's day boundary is evaluated in, and
    #: the daily goal. A UTC boundary silently breaks the streak for anyone west
    #: of Greenwich studying in the evening.
    settings_json: Mapped[dict] = mapped_column(JSON, default=dict)


class Card(Base):
    __tablename__ = "card"
    __table_args__ = (
        CheckConstraint(
            "(CASE WHEN sense_id IS NULL THEN 0 ELSE 1 END"
            " + CASE WHEN form_id IS NULL THEN 0 ELSE 1 END"
            " + CASE WHEN pattern_id IS NULL THEN 0 ELSE 1 END) = 1",
            name="card_exactly_one_referent",
        ),
        Index("card_due", "user_id", "due_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    #: lexical | morph | pattern
    population: Mapped[str] = mapped_column(String(16))
    # Three typed referents rather than one polymorphic `ref_id`: the database
    # can then enforce which table the card points at.
    sense_id: Mapped[int | None] = mapped_column(ForeignKey("sense.id"))
    form_id: Mapped[int | None] = mapped_column(ForeignKey("form.id"))
    pattern_id: Mapped[int | None] = mapped_column(ForeignKey("pattern.id"))

    fsrs_state_json: Mapped[dict] = mapped_column(JSON)
    due_at: Mapped[datetime] = mapped_column(DateTime)
    reps: Mapped[int] = mapped_column(default=0)
    lapses: Mapped[int] = mapped_column(default=0)
    #: Monotone high-water mark of FSRS stability, in days. The unlock gate reads
    #: this rather than current stability, so resting cannot lower it.
    stability_max: Mapped[float] = mapped_column(default=0.0)


class NodeUnlock(Base):
    """Written once, never deleted. The unlock latch."""

    __tablename__ = "node_unlock"

    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("node.id"), primary_key=True)
    unlocked_at: Mapped[datetime] = mapped_column(DateTime)


class Attempt(Base):
    """One submission. The parent of everything it caused."""

    __tablename__ = "attempt"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id"))
    submitted: Mapped[str] = mapped_column(String(256))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class Review(Base):
    __tablename__ = "review"

    id: Mapped[int] = mapped_column(primary_key=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey("attempt.id"))
    card_id: Mapped[int] = mapped_column(ForeignKey("card.id"))
    #: FSRS rating, 1 Again / 2 Hard / 3 Good. Easy is unused: there is no
    #: calibrated signal to justify it until a latency corpus exists.
    rating: Mapped[int] = mapped_column(Integer)


class ErrorEvent(Base):
    __tablename__ = "error_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey("attempt.id"))
    node_id: Mapped[int] = mapped_column(ForeignKey("node.id"))
    error_class: Mapped[str] = mapped_column(String(32), index=True)
    slot_index: Mapped[int] = mapped_column(default=0)
    expected: Mapped[str] = mapped_column(String(96))


class Streak(Base):
    __tablename__ = "streak"

    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), primary_key=True)
    current: Mapped[int] = mapped_column(default=0)
    longest: Mapped[int] = mapped_column(default=0)
    freezes: Mapped[int] = mapped_column(default=0)
    last_completed_on: Mapped[date | None] = mapped_column(Date)
