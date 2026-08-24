# Polish Learning Platform — Design v1

A morphology-first Polish course for English-speaking adults: a DAG of skill nodes, FSRS scheduling
over three card populations, and a grader that parses what the learner typed and names the
grammatical decision they got wrong.

**Design revision 2**, against `polish-learning-platform-spec.md` draft v0.1 (2026-08-24).
Formulation-complete for **M0 and M1**; roadmap only for M2–M4, because M0 is a kill-gate and
detailed design past it is speculative until it passes.

Revision 2 resolves four blocking issues found by interrogating revision 1. Two shared a root:
revision 1 **conflated Polish orthography with Polish morphophonology** — it put `o/ó` in the
orthographic confusion set, which reclassifies the `stół → stołu` / `Kraków → Krakowie` alternation as
a typo that does not fail the grammar card, and it applied every diacritic pair bidirectionally. The
fix is a **directional** rule: an ASCII-fold in the learner's typing direction is orthographic, its
reverse never is. A third was a contradiction inside the moat's own contract — step 2 was specified as
a database lookup in a module declared pure and a phase declared to have no database. The fourth is
`node_unlock`: revision 1 removed the table to save an entity and latched on a monotone column
instead, but **the ratio's denominator moves**, so the fraction was never monotone. **One table is
added; nothing else grew.**

Resolved before designing: **path A with B's data model** (§2); **M0 is the foundation M1 builds on**,
not a throwaway spike; **M1 content is template instantiation, no LLM**; **M1 exercise types are cloze,
MCQ and preposition drill**.

**Read §"What the sources actually say" first.** The licence question the spec marks *Fatal* is
answered — favourably — and seven further facts, recovered from the packaging, from FSRS's own
semantics and from Polish phonology, change the architecture, the unlock rule, the card model and the
classifier materially. Three contradict the spec directly; one corrects revision 1 of this document.

**The single most important structural claim in this document:** the error taxonomy of §4.6 is not a
list of messages. It is the **routing table that decides which cards a submission fails**. Everything
else follows from that.

---

## Scope

### In

- **M0 — the grader.** Morfeusz ingest, tag parsing, the six-step classifier, learner-facing
  explanations, and a hand-labelled golden corpus that is the kill-gate. No DB, no API, no UI.
- **M1 — the learning loop.** Single user, no auth. ~150 lemmas, nominative and accusative, three
  exercise types, FSRS scheduling over two card populations, session composition, streak, review UI.
- The full **grading and diagnosis subsystem**, specified to implementation depth, per spec §4.6's
  instruction to specify it before anything else.
- The **data model** in B's shape, so commercialising later needs no migration — including the two
  tables spec §8 is missing.
- **Deterministic content generation** from authored frames × the `form` table. No LLM anywhere in M1.
- Node unlock, mastery gating, and the daily session's composition rules.

### Out

- **M2–M4 in detail.** Each gets a roadmap paragraph and a named seam, nothing more.
- Auth, signup, billing, GDPR/DSAR, support (path A; spec §2's 30-day rule governs when this reopens).
- Leagues, leaderboards, cosmetics, push notifications (spec §6.6, deferred).
- The LLM content pipeline, the human review queue, and TTS (spec §5.2–5.3 — M2).
- Offline review sessions (spec §7 — see §"Offline contradicts server-side grading").
- Redis, the morphology sidecar container, blob storage, CDN, Azure OpenAI — none are M1 components.
- Multi-slot exercise types (free typing, word bank, dictation, reordering) and the two error classes
  that only they can produce.
- FSRS parameter optimisation. Stock parameters until a review corpus exists.
- Any claim about learning efficacy. This design ships mechanics, not evidence.

---

## Acceptance Criteria

### M0 — the kill-gate

1. `morfeusz2` installs and loads on arm64 macOS and on linux/amd64, from the same lockfile.
2. Every Morfeusz tag the M1 curriculum needs parses into a structured feature record, and an
   unrecognised tag raises rather than silently yielding a null feature.
3. Given a lemma, the generator returns the full paradigm, and every surface it returns re-analyses
   back to that lemma. Round-trip asserted over the whole M1 lexeme set.
4. **The golden corpus: ≥ 60 hand-labelled `(expected, submitted, error_class)` triples covering all
   eight classes M1 can produce, classified correctly at ≥ 95% overall — and at ≥ 80% within every
   individual class.** This is the gate: if it cannot be met, the thesis is false and the project
   stops here. The per-class floor exists because each class routes to a *different* card outcome
   (§"The error taxonomy is the card routing table") — an aggregate alone lets one systematically
   broken class hide behind seven working ones, and a broken class mis-schedules every card it
   touches.
4b. **The corpus includes vowel-alternation cases even though they are M2 content** — at minimum
   `Krakówie`/`Krakowie` and `stołowi`/`stołu`. They must classify as morphology, never as
   `ORTHOGRAPHY`. These are the regression test for the directional diacritic rule, and without them
   the M0 gate cannot see the error revision 1 shipped.
5. Every classification carries a learner-facing message naming the grammatical decision, not the
   string difference. `"kot is animate, so its accusative borrows the genitive: kota"` — never
   `"expected kota, got kot"`.
6. `Widzę kot` for `Widzę kota` classifies as `ANIMACY`, not `CASE_WRONG`. The masculine accusative
   trap is the first hard concept in the course (spec §4.2) and it must be diagnosed as itself.
7. `matka` for expected `matkę` classifies as `CASE_WRONG`, **not** `ORTHOGRAPHY`. A dropped ogonek
   that lands on another real form of the same lexeme is a case error.
8. `robie` for `robię` classifies as `ORTHOGRAPHY`, and does not fail the grammar card.

### M1 — the learning loop

9. A daily session composes in the order **debt → remediation → new**, and introduces no new cards
   while any card is overdue.
10. One submission produces exactly one `attempt` row, N `review` rows for the cards it scored, and M
    `error_event` rows — all sharing the attempt, with cards it did not score left untouched.
11. `sklepie` for `sklepu` fails the pattern card and leaves the morph card's schedule unchanged;
    `sklepa` fails the morph card and **passes** the pattern card. Asserted directly as a test.
12. The streak advances only when the day's due cards are cleared **and** the daily goal is met, and
    never on a day where neither happened. Evaluated in the user's own timezone.
13. A node, once unlocked, never re-locks — after a lapse, after a *new pattern card is created* for a
    stratum the learner had not yet met, or after a curriculum edit adds a pattern to a prerequisite.
    All three move the ratio; none may move the gate.
14. Mastery *display* decays with retrievability while the unlock gate does not move.
15. New-card introduction is capped per day, and the cap is honoured even when the learner keeps
    asking for more.
16. Content: 8 authored frames × the M1 lexeme set yields ≥ 300 items with no manual review, and every
    generated item's expected surface is a real form of its lexeme, asserted at build time.
17. The API returns JSON; no grading logic, no expected answer and no accepted-variant set is ever
    sent to the client before submission.
18. A failed submission whose every token analyses cleanly is written to the promotion queue rather
    than discarded. **Exercised by a synthetic fixture, not by natural traffic** — M1's single-slot
    cloze admits a valid alternative only under syncretism, so the queue is expected to stay empty.
    The write path ships (spec §10 requires it from day one); the criterion is proven by construction.
19. Traversing `V01 → N01` — a vocabulary node gating a grammar node, the first edge in M1's own graph
    — evaluates the gate over **lexical** cards and never divides by zero.

---

## What the sources actually say

Established before designing, because the spec marks one of these *Fatal* and the rest change the
architecture.

### 1. The analyser licence is 2-clause BSD, and it covers the dictionary data

Resolves spec §11 Q2 and retires §10's only **Fatal** row.

`Verified:` [morfeusz.sgjp.pl/doc/license/en](https://morfeusz.sgjp.pl/doc/license/en) places Morfeusz 2
**and its contained linguistic data** under the 2-clause BSD licence. Both dictionary variants are
covered — SGJP (© Saloni, Gruszczyński, Woliński, Wołosz, Skowrońska) and Polimorf (© IPI PAN), with
different copyright holders but identical terms. Commercial use, redistribution and derivative works
are all permitted; the sole obligation is retaining the copyright notices.

The consequence that matters is not "we may call the analyser". It is that the `form` table — which
**ships derived inflectional data** — is lawful to distribute, commercially included. Under a
non-commercial or share-alike licence the entire data model would have needed redesigning around
runtime-only analysis.

**Path B is not licence-blocked.** The older non-commercial restriction some sources still cite
applies to Morfeusz 1, not to Morfeusz 2.

`Assumption:` we adopt the **SGJP** variant, matching spec §5.1. Polimorf is the SGJP–Morfologik merge
and may have wider coverage of colloquial lemmas. Coverage against the M1 lexeme set is a
Validation Required item; the licence is identical either way, so this is a data decision with no
legal component.

### 2. The packaging deletes the sidecar container

`Verified:` from [PyPI](https://pypi.org/pypi/morfeusz2/json), `morfeusz2` 1.99.15 (1 June 2026) ships
**prebuilt `abi3` wheels** for `macosx_11_0_universal2`, `manylinux_2_28_x86_64`, `win_amd64` and
`win32`, with no source distribution. `cp310-abi3` means one wheel serves 3.10 and every later
CPython.

Three consequences:

- **Local development needs no Docker for the analyser.** The universal2 wheel runs natively on the
  arm64 Mac this will be built on.
- **There is no linux/arm64 wheel.** Azure compute must be pinned to x86-64 or the image will not
  build. Cheap constraint, expensive to discover during a deploy.
- **Spec §7's sidecar solves a problem that does not exist.** "The morphological analyser is a native
  library. Wrap it as an internal HTTP service in its own container" is correct reasoning for a
  library you must compile from source with a fragile runtime. This is a `pip install` into the same
  Python process as the API. See §"The analyser is a library, not a service".

### 3. FSRS is a maintained MIT dependency; porting it is strictly worse

`Verified:` [`fsrs`](https://pypi.org/pypi/fsrs/json) 6.3.2 (9 August 2026), MIT, Python ≥ 3.10, whose
only runtime dependency is `typing-extensions`. It exposes JSON serialisation, which maps directly
onto `card.fsrs_state_json` in spec §8. The parameter optimiser is an **optional extra**
(`fsrs[optimizer]`) pulling torch, numpy, pandas and tqdm.

`Verified:` from `fsrs/card.py`, the `Card` dataclass declares **`stability: float | None`** and
`difficulty: float | None` as public attributes, and `CardDict` — what `to_json()` emits — carries
`card_id`, `state`, `step`, `stability`, `difficulty`, `due`, `last_review`. The unlock gate can
therefore read stability directly off deserialised state, which is the fact §"Mastery gates on
stability" depends on.

**Two consequences that are easy to get wrong:**

- **`stability` is `None` until the first review completes.** It is `None` for every card in the
  `Learning` state, which is every card's first several reviews. A naive `max(stability_max,
  stability)` raises `TypeError` on the first update of every card ever created.
- **`Card` carries no `reps` and no `lapses`.** Those columns in spec §8 are the application's to
  maintain, and `reps − lapses` is *not* the count of successful reviews — `lapses` counts `Again`
  only in the `Review` state. The mastery rule's "≥ 3 successful reviews" must be counted from the
  `review` table, not derived from the card.

Spec §4.4 says FSRS "has reference implementations you can port". Depend on it instead: a port is a
correctness liability on the one subsystem whose bugs are invisible for months. **Install the base
package; do not install the optimizer extra** — with one user there is nothing to fit, and stock
parameters are what every FSRS deployment starts on regardless.

### 4. `retrievability ≥ 0.9` is a currency test, not a mastery test

**Contradicts spec §4.4 directly.**

FSRS schedules a card to fall due at the moment its retrievability decays to the desired retention —
0.9 by default. So `R ≥ 0.9` is true **exactly while a card is not yet due**, and false for every
overdue card. It is a restatement of "you are up to date", not a statement about how well anything is
known.

Under §4.4 as written, a learner who takes four days off drops below `R ≥ 0.9` on most cards, the
node falls under the 80% threshold, and **downstream nodes re-lock**. That is the single most
enraging thing a learning app can do, and it would arrive as a consequence of resting.

The time-invariant quantity is **stability**, `S` — defined as the interval at which `R` falls to
0.9. `S ≥ 7 days` says precisely "this is retained for a week", changes only when the card is
reviewed, and does not move while the learner sleeps. See §"Mastery gates on stability, and latches".

### 5. Pattern cards as specified violate FSRS's central assumption

**Contradicts spec §4.4.**

"They are scheduled against the *rule*, and each review draws a random lexeme the learner already
knows" gives a single card a **different difficulty on every review**. FSRS's difficulty parameter is
fitted per card against Anki review logs, where a card's content is fixed for its lifetime. A card
whose content is resampled each time has no stable difficulty to estimate, and the model's
calibration — the entire reason spec §4.4 chose FSRS over SM-2 — does not transfer to it.

The fix costs nothing and improves the pedagogy: **stratify the draw** (§"Pattern cards are
stratified"). It also repairs a second problem in the same sentence — under one-card-per-rule,
§4.4's "≥ 80% of the node's pattern cards" is 80% of 1.

### 6. Morfeusz returns a lattice, and the commonest learner error returns nothing at all

Two facts about the analyser that spec §4.6's pipeline does not account for.

`Assumption:` **the output is a segmentation DAG, not a token list.** Polish is genuinely ambiguous:
`mamy` is both `mieć:fin:pl:pri:imperf` ("we have") and `mama:subst:sg:gen` / `subst:pl:nom`
("mothers"); clitics split (`zrobiłbym` → `zrobił` + `by` + `m`); segmentation itself is ambiguous.
§4.6 step 4 — "compare against the expected analysis for that slot" — presumes a token alignment that
does not exist until something disambiguates.

The ambiguity is a fact about Polish and is not in doubt. What is unverified is the **shape Morfeusz
returns it in** — and since that shape determines the classifier's interface, it is the first thing
M0 checks (Validation Required). Revision 1 asserted this without a label while labelling the
generation claim correctly; the inconsistency is corrected here rather than defended.

The resolution is that **grading never needs disambiguation** (§"Grading is lattice reachability").

**And the commonest error yields an empty analysis.** `sklepa` for `sklepu` is not a Polish word.
Morfeusz returns no analyses, so there are no tags to compare and steps 3–5 have nothing to operate
on. §4.6 has no branch for this — and it is the *majority* case for a learner producing morphology.
Classification must fall back to the generated paradigm, which is what makes Morfeusz's **generator**
load-bearing and the `form` table the **error-classification search space** rather than display data.

`Verified:` generation is a first-class Morfeusz operation — `generate` is a constructor flag, and
the documentation describes synthesis as the inverse of analysis, from lemma plus desired inflectional
characteristics. `Assumption:` the exact Python signature; Validation Required, M0 week 1.

### 7. Normalising diacritics silently passes case errors

**Contradicts spec §4.6 step 1**, which places diacritic handling in *normalisation*.

Polish diacritics are sometimes orthographic and sometimes the entire grammatical signal:

| Typed | Expected | Is the typed string a real form of the same lexeme? | Correct class |
|---|---|---|---|
| `matka` | `matkę` (acc sg) | **yes** — nominative singular | `CASE_WRONG` |
| `tą` | `tę` (acc sg fem) | **yes** — instrumental singular | `CASE_WRONG` |
| `robie` | `robię` (1sg) | no | `ORTHOGRAPHY` |
| `sie` | `się` | no | `ORTHOGRAPHY` |

Normalise diacritics in step 1 and `matka` compares equal to `matkę`, so a learner who has not learnt
the accusative is told they are correct. The rule that separates the two cases is morphological, so
**the operation belongs in classification, not normalisation**: a diacritic difference is orthographic
*only if* the typed string is not itself a valid form of the same lexeme. Step 0 touches whitespace
and case only.

### 8. `o/ó` is morphology, and treating it as a diacritic pair blinds the grader

The error revision 1 shipped, and the reason revision 2 exists. `ó` is **not** a diacritic variant of
`o`. It is a distinct letter pronounced /u/, and the `o`↔`ó` alternation is morphophonological,
triggered by syllable closure — it is the *content* of the paradigms this product teaches:

| Nominative | Oblique | Alternation |
|---|---|---|
| `stół` | `stołu` (gen) | ó → o |
| `Kraków` | `w Krakowie` (loc) | ó → o |
| `wóz` | `wozu` (gen) | ó → o |
| `bóg` | `boga` (gen) | ó → o |

A learner typing `Krakówie` for `Krakowie` has failed the alternation that spec §4.2 point 5 singles
out as the reason locative is taught with palatalisation. Put `o/ó` in the orthographic confusion set
and that learner is told they made a typo, and — per the routing table — **the grammar card does not
fail**. The single most characteristic Polish morphology error becomes invisible.

It is also invisible to the M0 gate, because these alternations appear in the genitive and locative,
which are M2 content, while criterion 4 covers only the classes M1 can produce. The bug would have
been authored at M0 and detonated a phase later. Hence criterion 4b.

**The distinction that matters is direction.** `ą ć ę ł ń ó ś ź ż` all fold to an ASCII base, and a
learner without a Polish keyboard types the base. That is orthographic. The *reverse* — typing a
Polish letter where the base was required — is never a keyboard artefact, because no keyboard produces
`ó` by accident. See §"The six-step classifier", step 3.

`u/ó`, `ż/rz` and `h/ch` are different animals and stay bidirectional: those are genuine **homophone**
pairs (`u` and `ó` are both /u/; `ż` and `rz` both /ʐ/), and confusing them is the archetypal Polish
spelling error for natives and learners alike.

---

## Design Choices

### The error taxonomy is the card routing table

The spine of the design, and the reason the moat is a moat.

One submission touches several cards, and **the error class decides which of them fail**. Consider a
cloze targeting `sklep` in the genitive after `do`:

| Learner types | Why | Pattern card `do`+gen | Morph card `sklep`:gen.sg |
|---|---|---|---|
| `sklepu` | correct | Good | Good |
| `sklepie` | real form, locative | **Again** — wrong case chosen | *not scored* |
| `sklepa` | not a word; `-a` genitive over-applied to a `-u` noun | **Good** — genitive *was* chosen | **Again** — paradigm confusion |

`CASE_WRONG` versus `CASE_RIGHT_FORM_WRONG` is therefore not a message nicety. It is the difference
between two cards' schedules, and grading them together — which any string comparison does — teaches
the learner nothing and mis-schedules both.

- **Design choice:** the classifier returns a `Diagnosis`, and a single declarative table maps
  `error_class → {population: rating}`. No branching in the scheduler.

| Error class | lexical | morph | pattern | M1? |
|---|---|---|---|---|
| *(correct)* | Good | Good | Good | ✓ |
| `ORTHOGRAPHY` | Hard | Hard | Hard | ✓ |
| `CASE_WRONG` | — | — | **Again** | ✓ |
| `ANIMACY` | — | — | **Again** | ✓ |
| `NUMBER_WRONG` | — | — | **Again** | ✓ |
| `GENDER_AGREEMENT` | — | — | **Again** | ✓ |
| `ASPECT_WRONG` | — | — | **Again** | classifier only |
| `CASE_RIGHT_FORM_WRONG` | — | **Again** | Good | ✓ |
| `LEXICAL` | **Again** | — | — | ✓ |
| `UNANALYSABLE` | — | **Again** | **Again** | ✓ |
| `WORD_ORDER` | — | — | **Again** | M2 |
| `MISSING_CONSTITUENT` | — | — | **Again** | M2 |

- **`—` means no `review` row is written and the card's schedule is untouched.** A learner who
  produced a well-formed locative has demonstrated they can inflect `sklep`; failing the genitive
  morph card would conflate "cannot build this form" with "did not know `do` governs genitive", which
  is the exact conflation this product exists to remove.
- **`UNANALYSABLE` is new.** §4.6's taxonomy has no terminal class, and the classifier must have one
  or it will force a wrong label onto input it does not understand. It fails both grammar cards
  (conservative), shows the answer, and is logged for corpus review.
- **`LEXICAL` in a cloze is degenerate** — the lemma is supplied in the prompt, so a different word is
  not lexical ignorance. It is logged and scores nothing. The class becomes meaningful at M2 when
  exercises stop supplying the lemma.
- **Ratings are `Again` / `Hard` / `Good` only. `Easy` is unused in M1.** There is no calibrated
  signal to justify it; `review.latency_ms` is captured from day one so a four-point mapping can be
  *fitted* at M2 rather than guessed now.
- **`Hard` has exactly one producer: `ORTHOGRAPHY`.** Spec §4.6 requires that a spelling slip "do not
  fail the grammar card". `Hard` is FSRS's own semantics for *recalled, with difficulty*, which is
  the true state — and unlike `Good` it does not pretend the answer was clean. This is a semantic
  choice, not a tuned parameter.

### Grading is lattice reachability, not tagging

- `Verified:` Morfeusz emits a segmentation DAG with genuine ambiguity (§"What the sources actually
  say", 6).
- **Design choice:** the grader never disambiguates. It asks whether **there exists a path** through
  the learner's lattice whose analyses match the expected slot sequence. Existence, not argmax.
- No POS tagger, no disambiguation model, no training data, no per-language tuning. This is the single
  reason M0 is three weeks rather than three months.
- When no matching path exists, the classifier finds the **minimally-differing** path, and the nature
  of that difference is the diagnosis. The error taxonomy is the edit vocabulary of that alignment.
- At M1 every item is single-slot, so the lattice reduces to "does any analysis of this one token
  match". The lattice machinery still goes in at M0 because M2's multi-slot items need it and
  retrofitting alignment into a scalar comparison touches every call site.

### The six-step classifier

Specified to implementation depth, per spec §4.6's instruction. Order is load-bearing: **step 2 must
precede step 3**, or every dropped ogonek that lands on a real form is misreported as a typo (§"What
the sources actually say", 7).

```mermaid
flowchart TD
    S0["<b>0 · Normalise</b><br/>whitespace + case only<br/><b>diacritics untouched</b>"] --> S1
    S1{"<b>1</b> surface ==<br/>expected surface?"} -->|yes| OK["<b>CORRECT</b>"]
    S1 -->|no| S2{"<b>2</b> a form of the<br/><i>expected lexeme</i>?"}

    S2 -->|"yes — tag matches"| OK
    S2 -->|"yes — tag differs"| FEAT["compare feature vectors<br/>in priority order"]
    S2 -->|no| S3

    FEAT --> ANIM{"acc expected<br/>on masc noun,<br/>and nom/gen given?"}
    ANIM -->|yes| E_AN["<b>ANIMACY</b>"]
    ANIM -->|no| E_CASE["<b>CASE_WRONG</b><br/><b>NUMBER_WRONG</b><br/><b>GENDER_AGREEMENT</b>"]

    S3{"<b>3</b> edit distance ≤ 2 and every<br/>edit is an ASCII-fold <i>in the<br/>learner's direction</i>, or a<br/>homophone pair?"} -->|yes| E_ORTH["<b>ORTHOGRAPHY</b>"]
    S3 -->|no| S4

    S4{"<b>4</b> analyses as nothing,<br/>and edit distance ≤ 2?"} -->|yes| E_PARA["<b>CASE_RIGHT_FORM_WRONG</b><br/><i>paradigm-class confusion</i>"]
    S4 -->|no| S5

    S5{"<b>5</b> a form of some<br/><i>other</i> lexeme?"} -->|"yes — aspect partner"| E_ASP["<b>ASPECT_WRONG</b>"]
    S5 -->|"yes — unrelated"| E_LEX["<b>LEXICAL</b>"]
    S5 -->|no| E_UNK["<b>UNANALYSABLE</b>"]
```

- **Step 2 reads the paradigm off `ExpectedSlot`, not out of a database.** Revision 1 specified it as
  `SELECT … WHERE lexeme_id = ? AND surface = ?` inside a module declared pure, in a phase declared to
  have no database — three statements that could not all hold. See §"The moat takes its paradigm as an
  argument".
- **Step 3's confusion set is closed, declared, and *directional*.** Two disjoint mechanisms:
  - **ASCII-folds, one direction only** — `ą→a  ć→c  ę→e  ł→l  ń→n  ó→o  ś→s  ź→z  ż→z`, orthographic
    **only when the learner typed the ASCII base where the Polish letter was required**. This is
    exactly the keyboard case spec §4.6 describes with `sie` for `się`. The reverse direction is never
    orthographic — no keyboard emits `ó` by accident, so `o→ó` is a learner asserting a vowel that
    is not there, which is morphology (§"What the sources actually say", 8).
  - **Homophone pairs, bidirectional** — `ż/rz`, `u/ó`, `h/ch`. Genuine same-sound confusions, and
    unlike the folds they do not encode a grammatical alternation.
  - `o/ó` appears **only** as an ASCII-fold in the `ó→o` direction. It is not a bidirectional pair.
    Revision 1 listed it as one, and that single entry is what made the alternation invisible.

  Worked against every case the design commits to:

  | Expected | Typed | Mechanism | Class |
  |---|---|---|---|
  | `się` | `sie` | fold `ę→e`, learner's direction | `ORTHOGRAPHY` ✓ |
  | `robię` | `robie` | fold `ę→e`, learner's direction | `ORTHOGRAPHY` ✓ |
  | `matkę` | `matka` | — *caught at step 2, a real form* | `CASE_WRONG` ✓ |
  | `Kraków` | `Krakow` | fold `ó→o`, learner's direction | `ORTHOGRAPHY` ✓ |
  | `Krakowie` | `Krakówie` | `o→ó` — **reverse**, no rule fires | falls to step 4 → `CASE_RIGHT_FORM_WRONG` ✓ |
  | `który` | `ktury` | homophone `u/ó` | `ORTHOGRAPHY` ✓ |
- **Step 4 is the branch §4.6 is missing.** `sklepa` is not a word, so there is nothing to compare
  tags against; it is within one edit of `sklepu` and the edit is not orthographic, so the learner
  aimed at the right cell and built the wrong ending. That is the definition of paradigm-class
  confusion.
- `Assumption:` the edit-distance threshold of **2**. Tuned against the golden corpus in M0, not
  before — it trades `CASE_RIGHT_FORM_WRONG` against `UNANALYSABLE` and the right value is an
  empirical question. Validation Required.
- **The `ANIMACY` rule, stated precisely** — because it is the first hard concept in the course and a
  generic `CASE_WRONG` would waste it. Expected case is accusative, lexeme is a masculine noun, and:
  learner gave **nominative** for an *animate* lexeme (`Widzę kot`), or **genitive** for an
  *inanimate* one (`Widzę stołu`). Both are the same misconception with opposite sign, and both must
  say so.
- **Explanations name the decision, never the strings.** `Diagnosis` carries the offending feature and
  the governing rule, and `explain.py` renders from those — so the message is derived from grammar,
  not from a diff. Acceptance criterion 5.

**Two limitations accepted knowingly, rather than engineered around:**

- **Step 3 precedes step 5, so a real word can be called a typo.** `kat` (executioner) typed for
  `kąt` (angle) is an ASCII-fold, so it classifies as `ORTHOGRAPHY` before anything asks whether it is
  a form of some other lexeme. In cloze this is the right answer — the prompt supplies the lemma, so
  the learner was not choosing a word. It stops being right at M2, when exercises stop supplying it,
  and the ordering is revisited then rather than pre-emptively complicated now.
- **An ASCII-fold landing in the *ending* is genuinely ambiguous.** `matke` for `matkę` is classified
  `ORTHOGRAPHY` and does not fail the morph card — but the ogonek *is* the accusative ending. Nothing
  in the input distinguishes "knows `-ę`, has no Polish keyboard" from "believes the ending is `-e`",
  and the design resolves it leniently, matching spec §4.6's instruction not to fail the grammar card
  on a spelling slip. The cost is a real hole in the moat for ending-diacritics specifically. The
  cheap mitigation — an on-screen diacritic strip in the answer input, which removes the excuse and
  makes the lenient reading defensible — is in Optional hardening, not M1.

### The moat takes its paradigm as an argument

Revision 1 made three claims that could not co-exist: step 2 is a `form` table lookup; `pl/grade` is a
pure function of `(ExpectedSlot, str)`; M0 has no database. A pure function of those two arguments has
nothing to look up in, and M0 has no table to query — so the classifier's most important step was
unimplementable as written.

- **Design choice:** `ExpectedSlot` **carries the expected lexeme's full paradigm**, alongside the
  expected `(lemma, morph_tag)` and the lexeme's `gender`, `animacy` and `aspect_partner_id`. The
  signature stays `classify(slot: ExpectedSlot, submitted: str) -> Diagnosis`, and it stays pure.
- Steps 2, 3 and 4 read that paradigm. Only step 5 — *is this a form of some **other** lexeme* — needs
  the open vocabulary, and it calls the in-process analyser, which is not I/O.
- **The two producers of an `ExpectedSlot`:** at M0, `morph.forms(lemma)` directly; at M1, a `form`
  table query. One consumer, two suppliers, no adapter interface and no dependency injection — the
  paradigm of one lexeme is tens of rows, so passing it by value is cheaper than abstracting over
  where it came from.
- This is what makes the golden corpus a **plain data fixture**: a triple plus a paradigm, with no
  database, no fixtures framework and no mocking. It is the reason M0 can gate the project without a
  schema existing.
- The materialisation claim — *paradigms are stored, not generated per request* — survives, but it
  belongs to M1's **caller**, not to the classifier. Stated in the wrong place, it invented a
  dependency the moat does not have.

### The analyser is a library, not a service

**Deletes a component from spec §7.**

- `Verified:` `morfeusz2` is a prebuilt `abi3` wheel installable into the API's own interpreter
  (§"What the sources actually say", 2).
- **Design choice:** `import morfeusz2` in-process. **No sidecar container, no internal HTTP service,
  no cache layer, no `< 50 ms` budget.**
- The `< 50 ms` figure in §7 is a *network* budget. It exists only because of the sidecar, and it
  vanishes with it — an in-process FSA lookup is microseconds. Likewise "cache aggressively" is
  premature optimisation of a call that is already faster than the surrounding ORM query.
- What the sidecar costs: a container image, a deployment unit, an HTTP client, a serialisation
  boundary, a cache with an invalidation story, and a new failure mode on the hot path of the only
  subsystem that matters.
- **The condition under which this reverses:** a Morfeusz instance is not thread-safe or is memory-
  heavy enough that per-worker instances do not fit. Validation Required — measure resident size and
  concurrency behaviour in M0. If it reverses, the wrapper in `pl/morph.py` is the only module that
  changes, because nothing else imports `morfeusz2`.
- **Pin linux/amd64 in both the container image and the CI runner.** `morfeusz2` publishes **no
  sdist**, so on a platform without a wheel `pip` cannot fall back to building — the install fails
  outright. Spec §7 puts CI on GitHub Actions, which now offers arm64 Linux runners; selecting one
  breaks the build in a way that reads as a packaging bug rather than an architecture mismatch. Assert
  the architecture in the same place the container platform is pinned.

### Mastery gates on stability, and latches

**Replaces spec §4.4's threshold.**

- `Verified:` `R ≥ 0.9` holds only while a card is not yet due (§"What the sources actually say", 4).
- **Design choice:** a node is mastered when **≥ 80% of its gating cards have `stability_max ≥ 7
  days`, with ≥ 3 successful reviews spanning ≥ 7 calendar days.** The count and span conditions are
  spec §4.4's, unchanged; only the decaying term is replaced.
- **Design choice — the gating population depends on the node type.** Spec §4.4 and revision 1 both
  said "pattern cards", which is undefined for two of the three node types spec §4.1 declares. M1's
  own graph opens with `V01 (vocabulary) → N01 (grammar)`, so the *first edge the learner traverses*
  evaluated 0/0.

  | Node type | Gating cards | Rationale |
  |---|---|---|
  | **grammar** | its `pattern` cards | the competency is the rule, generalised across a stratum |
  | **vocabulary** | the `lexical` cards of its lexemes | the competency is meaning, and the node has no patterns |
  | **function** | *none* — mastered exactly when its prerequisites are | it composes earlier nodes and introduces no new competency of its own |

  `assert denominator > 0` before dividing, so a mis-authored node fails loudly at session end rather
  than silently unlocking the rest of the course. Acceptance criterion 19.

- **Design choice — the latch is a persisted row.** `node_unlock(user_id, node_id, unlocked_at)`,
  written once when the condition first holds, never deleted.
- **Why a column could not do it, which revision 1 got wrong.** `card.stability_max` *is* monotone.
  The **ratio is not**, because the denominator moves in two ways the design itself creates:
  - Pattern cards are created **lazily** — the draw excludes strata whose known-lexeme intersection is
    empty. A learner reaches 3/3 and unlocks; later they meet a lexeme in the fourth stratum, its card
    is created, and 3/4 = 75% **re-locks the node**. That is an M1 failure, not a hypothetical.
  - Content is **rebuilt, not migrated**, so adding a paradigm class to a node at M2 re-locks every
    node downstream of it.

  Revision 1 traded a table for a column and got an incorrect latch in exchange. One row per unlocked
  node is the only representation that survives both.
- **`card.stability_max` stays**, as the card-level high-water mark the gate reads — `max()` over the
  card's history is the right measure whether or not the ratio is latched, and it saves the gate from
  replaying `review` rows. `Verified:` `Card.stability` is `float | None` and is `None` throughout the
  `Learning` state, so the update is `stability_max = max(stability_max or 0.0, stability or 0.0)`.
  Without the guard, every card raises `TypeError` on its first review.
- **"≥ 3 successful reviews" is counted from `review`**, as `COUNT(*) WHERE card_id = ? AND rating >=
  Good`. `Verified:` `Card` carries neither `reps` nor `lapses`, and `reps − lapses` is not the same
  quantity — `lapses` counts `Again` only in the `Review` state.
- **Unlock is about access; scheduling is about retention.** Separating them is what lets §6.5's
  mastery visualisation fade with retrievability (which is what makes it informative) while nothing
  the learner has earned is taken away. Acceptance criteria 13 and 14 assert both halves.
- **Design choice:** evaluated once at session end, not per review. Nodes never unlock mid-session,
  which would inject unlocked material into a session already composed.

### Pattern cards are stratified

**Replaces spec §4.4's random draw.**

- `Verified:` resampling content per review destroys the per-card difficulty FSRS estimates
  (§"What the sources actually say", 5).
- **Design choice:** a pattern card is **(rule, paradigm class)**, not (rule). Draws within a card are
  drawn from one stratum, so difficulty is homogeneous and FSRS's assumption holds.
- The mastery claim becomes honest and *stronger*: mastering `ACC_AFTER_TRANSITIVE_VERB × m-anim`
  means generalisation across masculine animate nouns, which is the real competency. The unstratified
  version could be satisfied by luck of the draw.
- **This is what makes "≥ 80% of the node's pattern cards" a meaningful fraction.** M1's accusative
  node carries four strata — `f-a`, `m-inanim`, `m-anim`, `n-o` — so 80% means three of four, and the
  learner may carry one weak paradigm class forward while the other three are solid. Under
  one-card-per-rule the threshold was 80% of 1.
- **Design choice:** the draw still excludes lexemes the learner has not met — a pattern card draws
  from `{lexemes in stratum} ∩ {lexemes with a lexical or morph card}`. If the intersection is empty
  the card is not scheduled.

### Cloze does not score lexical cards, and the node type says so

- In cloze-with-lemma-prompt the lemma is **printed in the prompt**. Meaning is not under test, so
  scoring a lexical card on it would credit knowledge the exercise did not probe.
- **Design choice:** `node.type` determines which populations an item scores — a **grammar** node
  scores morph and pattern, a **vocabulary** node scores lexical. No new field; the rule reads off
  spec §8's existing column.
- **The two rules compose by intersection, and this must be stated or it will be miswired.** The
  routing table says what rating a population *would* get; `node.type` says which populations are
  *eligible*. The effective set is the intersection — which means **the routing table's `lexical`
  column is dead under every grammar node**, and cloze never touches a lexical card no matter what the
  table says. Read either rule alone and you get a cloze exercise crediting vocabulary knowledge it
  did not test.
- **Consequence for M1's three types:** MCQ therefore does double duty — form selection under a
  grammar node, meaning recall under a vocabulary node. Same exact-match grading path, same table,
  different parent node. This is how M1 gets a vehicle for lexical cards without a fourth exercise
  type.

### Session composition: debt, then remediation, then new

Spec §4.6 says the weakest node drives the next session; §6.2 says the streak requires clearing review
debt. Both need an explicit ordering to be implementable.

- **Design choice:** the session is built in three segments, in order.
  1. **Debt** — every card with `due_at ≤ end of the learner's local day`, ordered by `due_at`.
  2. **Remediation** — items from the weakest node, by error rate over a trailing 14-day window,
     injected only after debt is exhausted.
  3. **New** — items from unlocked, unstarted nodes, **only if debt is clear** and the daily goal is
     not yet met.
- **Design choice: a daily new-card cap, default 10, configurable.** Without one, an enthusiastic
  first evening creates a debt spike three days later that reads as punishment for engagement, and it
  is the standard way self-hosted SRS deployments fail. Acceptance criterion 15.
- **Design choice:** "weakest node" is a rate, not a count — otherwise the node with the most items
  always wins.
- **Design choice: no cap on total session length at M1. Stated as a decision, not left as an
  omission.** New cards are capped; overdue cards are not. A learner returning after two weeks is
  served the whole backlog in one sitting, and because criterion 12 requires debt cleared *and* the
  goal met, a partial clear earns no streak — the mechanic meant to prevent churn bites hardest at the
  moment churn is most likely. No acceptance criterion requires a bound, and path A has one user who
  can simply stop, so building one now would be a mechanism without a criterion. **The counter-
  mitigation ships instead:** streak freezes (§"Streak") make the absence recoverable, which is the
  cheaper half of spec §6's own caveat. A backlog cap is in Optional hardening, and it is the first
  thing to reconsider if a second learner ever exists.
- **Design choice:** remediation items **do write `review` rows**, even for cards that are not yet
  due. Reviewing early raises stability less than reviewing on time — FSRS models this rather than
  breaking on it — and the alternative, remediation that does not count, means the learner drills
  their weakest node for no scheduling credit. The rating is recorded normally; the routing table does
  not special-case remediation.

### Streak: both conditions, and a real timezone

- **Design choice:** the streak advances when the day's due cards are cleared **and** the daily goal
  is met. Spec §6.2 gives only the first, which advances the streak for free on a day with no due
  cards — including day one, and including a learner who has drifted into having nothing due.
  Requiring both makes §6.1 and §6.2 each load-bearing instead of redundant.
- **Design choice:** the day boundary is the learner's local midnight, from an IANA timezone in
  `user.settings_json`. `streak.last_completed_on` is a local date. Spec §8 has no timezone anywhere,
  and a UTC boundary silently breaks the streak for anyone west of Greenwich in the evening.
- **Design choice:** freezes — cap **2**, one earned per 10 days *of advanced streak*, consumed
  automatically at the first missed day, most recently earned first. Spec §6.3 gives the numbers but
  not the trigger or the accrual basis.
- **Design choice:** the session screen shows retained-lexeme count and node mastery beside the
  streak, per spec §6's own caveat. Costs one query; it is the thing that survives a broken streak.

### M1 content is deterministic instantiation

- **Design choice:** an M1 item is an authored **frame** × a lexeme, resolved against the `form`
  table. `data/frames.yaml` holds ~8 frames; the M1 lexeme set holds ~40 nouns in scope for cases 1–2.

  ```yaml
  - id: ACC_TRANSITIVE_WIDZIEC
    node: N05-accusative-singular
    pattern: ACC_AFTER_TRANSITIVE_VERB
    template: "Widzę ___ ({lemma})"
    gloss:    "I see a {en_gloss}"
    target:   {pos: subst, number: sg, case: acc}
    admits:   [m-anim, m-inanim, f-a, n-o]
  ```

- Eight frames × forty lexemes yields **≥ 300 items with zero review burden**, because the expected
  surface is *looked up*, not written — it cannot be wrong unless SGJP is wrong.
- **No Azure OpenAI, no generation pipeline, no validator, no review queue, no reviewer at M1.** Spec
  §5.2's pipeline exists to produce *communicative context*, which is an M2 need. Drilling morphology
  needs frames, and a frame is more reliable than a reviewed sentence.
- **Design choice — the M2 seam is declared now** so nothing blocks it: `item.source ∈ {template,
  generated}`, and the LLM pipeline writes to the same `item` table through the same build-time
  assertion that every expected surface is a real form (acceptance criterion 16). The frame builder
  and the LLM pipeline are two producers of one validated interface.
- **Design choice:** `data/lexemes.yaml` is hand-curated, per spec §5.1's warning that frequency ≠
  teachability. Frequency rank is carried but does not order the curriculum. This answers spec §11 Q4
  for the first 500: **theme-driven, with frequency as a tiebreak**.
- Content is rebuilt, not migrated — items are a pure function of frames × lexemes, so regenerating is
  always safe. Cards reference `form` and `pattern`, never `item`, so regeneration never orphans a
  schedule. This is why §"Data model" moves the card's referent off `item`.

### Offline contradicts server-side grading

**Two spec §7 constraints that cannot both hold.**

- "The review session must work offline once loaded" and "grading runs server-side" are incompatible:
  offline means no diagnosis until reconnect, and a diagnosis the learner reads on the train home is
  not feedback.
- The asymmetry worth noting: **client-side grading leaks answers, which is fatal for path B and a
  non-issue for path A** — you would be leaking answers to yourself. So offline is *cheap* precisely
  in the path we are building and expensive in the one we are keeping open.
- **Design choice:** keep grading server-side, defer offline past M1. Building the client-side grader
  now would fork the moat into two implementations that must agree, for a benefit that expires the
  moment a second user exists.
- Revisit at M2 with the honest options: a bounded pre-fetched session graded on-device (path A only),
  or accepting deferred feedback for review-only sessions.

### M1 architecture: three components

```mermaid
flowchart TD
    UI["Jinja page + vanilla JS<br/><i>consumes /api, no npm</i>"] -->|HTTPS/JSON| API

    subgraph API["FastAPI — one process"]
        direction TB
        R["/api/session · /api/submit"]
        G["<b>pl/grade/</b> — the moat<br/>lattice · classifier · explain"]
        S["<b>pl/schedule.py</b><br/>fsrs 6.3.2 · routing table"]
        M["<b>pl/morph.py</b><br/><i>import morfeusz2</i> — in-process"]
        R --> G --> S
        G --> M
    end

    API --> PG[("PostgreSQL<br/>all state")]

    X1["✗ Redis"]:::gone
    X2["✗ morphology sidecar"]:::gone
    X3["✗ Blob + CDN"]:::gone
    X4["✗ Azure OpenAI"]:::gone

    classDef gone fill:#00000000,stroke:#888,stroke-dasharray:4 3,color:#888
```

- **Design choice:** the four struck components are all M2+. Redis is a hot-card cache for a user
  count of one; blob/CDN arrives with audio; Azure OpenAI arrives with generated content; the sidecar
  is deleted permanently.
- **Design choice: PostgreSQL from M1, not SQLite.** The whole premise of path A is B's data model
  with no rewrite, and `fsrs_state_json` wants JSONB. A SQLite→Postgres migration means re-testing
  every query, which is exactly the rewrite §2 promises to avoid. Docker Compose in dev; Flexible
  Server later, pinned x86-64.
- **Design choice: no React and no npm at M1** — a deviation from spec §7's `PWA (React + TS)`. The
  M1 review session is one screen. FastAPI serves JSON from `/api/*` and a Jinja page consumes it with
  vanilla JS, matching the stack already proven in `voice-to-quote`. **Because the API is JSON-first,
  M2's React PWA replaces the page and not the backend** — the deviation costs nothing later and saves
  a build pipeline, an API contract and a state library now.
- **Design choice: M1 binds to localhost only, and says so.** There is no auth at M1, so `/api/submit`
  is an unauthenticated write endpoint; that is safe on a loopback interface and a defect anywhere
  else. If it ever needs to reach a phone on the same network, the minimum is a single shared secret
  in a header — five lines, and it stops M2's real auth from being retrofitted onto an open surface.
  Revision 1 drew HTTPS in this diagram and never stated the posture.
- **Design choice:** `pl/grade/` imports nothing from `pl/api.py`, `pl/models.py` or the ORM. The moat
  is a pure function of `(ExpectedSlot, str)` — and `ExpectedSlot` carries its own paradigm
  (§"The moat takes its paradigm as an argument"), so the purity claim holds against every step of the
  classifier rather than only the first. That is what makes the golden corpus a unit test and M0
  possible with no schema at all.

---

## Data model

Spec §8 with three tables added and three columns changed. Additions approved at the simplicity
checkpoint; every one is justified against an acceptance criterion.

```
lexeme            id, lemma, pos, gender, animacy, aspect,
                  aspect_partner_id, paradigm_class, frequency_rank
form              id, lexeme_id, surface, morph_tag, is_irregular
                  ── UNIQUE (lexeme_id, morph_tag); INDEX (lexeme_id, surface) ★
                  ── curriculum lexemes only, not all of SGJP ★
sense             id, lexeme_id, en_gloss, register, notes
node              id, type, title, cefr_level, explanation_md
node_prereq       node_id, prereq_node_id
node_lexeme       node_id, lexeme_id

pattern         ★ id, node_id, rule_key, paradigm_class
                  ── UNIQUE (rule_key, paradigm_class)

item              id, node_id, exercise_type, prompt, expected_answer,
                  target_form_id, pattern_id ★, source ★, audio_url, difficulty
item_variant      item_id, accepted_answer, source(authored|promoted)
user              id, email, created_at, settings_json   ── settings_json.tz : IANA

card              id, user_id, population(lexical|morph|pattern),
                  sense_id ★, form_id ★, pattern_id ★,      ── exactly one non-null
                  fsrs_state_json, due_at, reps, lapses,
                  stability_max ★

node_unlock     ★ user_id, node_id, unlocked_at
                  ── PK (user_id, node_id); written once, never deleted

attempt         ★ id, user_id, item_id, submitted, latency_ms, created_at
review            id, attempt_id ★, card_id, rating
error_event       id, attempt_id ★, node_id, error_class, slot_index ★, expected
streak            user_id, current, longest, freezes, last_completed_on
```

★ = added or changed against spec §8.

- **`pattern`** — spec §8 has no table for what a `population='pattern'` card refers to. `node` is not
  it: a node carries several rules across several paradigm classes (§"Pattern cards are stratified").
  Without this table the innovation of §4.4 has nothing to point at.
- **`attempt`** — one submission fans out to N `review` rows and M `error_event` rows. Without a
  parent, `submitted` and `latency_ms` duplicate across every row and nothing ties the fan-out
  together. It is also the unit both diagnostic queries want: *"every submission we called
  `CASE_WRONG`"* and *"every failed submission whose tokens all analysed"* — the promotion queue of
  §4.6 and acceptance criteria 10 and 18.
- **`card.ref_id` → three nullable typed FKs** with `CHECK (num_nonnulls(sense_id, form_id,
  pattern_id) = 1)`. Spec §8's polymorphic `ref_id` has no referential integrity and cannot express
  which of three tables it points at. Three columns keep one scheduling code path and let the database
  enforce the invariant.
- **`card.stability_max`** — the monotone unlock latch (§"Mastery gates on stability"). One column
  instead of a `node_unlock` table.
- **`item.pattern_id`** — the routing table needs to know which pattern card an item exercises. It is
  not derivable from `node_id`, which is one-to-many over patterns.
- **`item.source`** — the declared M2 seam for LLM-generated content.
- **`error_event.slot_index`** — one submission can carry two errors at different positions. Without
  it, remediation cannot tell one error from two.
- **`node_unlock`** — the latch (§"Mastery gates on stability"). A row per unlocked node per user,
  written once at session end. The only representation that survives a moving denominator, which is
  what acceptance criterion 13 now asserts explicitly.
- **`form` gets `UNIQUE (lexeme_id, morph_tag)` and a composite `INDEX (lexeme_id, surface)`.** The
  hot query builds an `ExpectedSlot` by fetching one lexeme's paradigm, and the classifier's steps 2–4
  then read it in memory; revision 1 declared a bare `surface` index for a query that filters on both
  columns. The uniqueness constraint is what makes ingest idempotent.
- **`form` holds curriculum lexemes only.** Revision 1 said "SGJP ingest → `lexeme` + `form`" without
  a bound, which reads as ingesting the whole dictionary — orders of magnitude more rows than the
  ~40 lexemes M1 teaches, and a long ingest to no purpose. Paradigms are materialised for curriculum
  lexemes via `generate()`. **Step 5 of the classifier — *is this a form of some other lexeme* — is
  therefore an analyser call, not a query**, because the open vocabulary is precisely what the table
  does not hold. That is the one place the two form sources are not interchangeable, and it is why
  the distinction is recorded here rather than left to the implementer.
- **Unchanged and deliberately so:** `item_variant.source(authored|promoted)` already carries the
  promotion mechanism spec §10 insists must exist from day one.

**Cards reference `form`, `sense` and `pattern` — never `item`.** Content is regenerable (§"M1 content
is deterministic instantiation"), and a schedule anchored to a regenerable row would be destroyed by
a rebuild. `attempt.item_id` records what was *asked*; the card records what is being *learnt*.

### The M1 node graph

```mermaid
flowchart LR
    V01["<b>V01</b> · vocab<br/>everyday nouns ×40"] --> N01
    N01["<b>N01</b> · grammar<br/>gender assignment<br/><i>nominative singular</i>"] --> N02["<b>N02</b> · grammar<br/><i>to jest…</i><br/>nominative predicate"]
    N01 --> N03["<b>N03</b> · grammar<br/>accusative sg<br/><i>f-a</i> · kawa → kawę"]
    N01 --> N04["<b>N04</b> · grammar<br/>accusative sg<br/><i>m-inanim, n-o</i> · = nominative"]
    N03 --> N05
    N04 --> N05["<b>N05</b> · grammar<br/>accusative sg <i>m-anim</i><br/><b>the animacy trap</b><br/>kot → kota"]
    N02 --> N06
    N05 --> N06["<b>N06</b> · function<br/>describing what you have<br/><i>composes N02 + N03–N05</i>"]
```

Four pattern strata under the accusative rule — `f-a`, `m-inanim`, `m-anim`, `n-o` — so §4.4's 80%
threshold means three of four, and a learner may carry one weak paradigm class into N06 while the
rest are solid.

---

## Build phases

### M0 — the grader (2–3 weeks). No UI, no DB, no API.

- [ ] `git init`; `uv init`, pin **Python 3.12**; add `morfeusz2`, `pyyaml`, `pytest`. Nothing else.
- [ ] **Spike, day 1 — the DAG.** Print `analyse()` output for `Ala ma kota`, `mamy` and `zrobiłbym`.
      This is the one unverified assumption the classifier's interface is shaped around
      (§"What the sources actually say", 6); everything below assumes its answer.
- [ ] **Spike, day 1 — `fsrs` round-trip.** `uv run --with fsrs` a throwaway script: `Card()` →
      `review_card(Good)` → `to_json` → `from_json`, asserting `stability` survives and is `None`
      before the first review. Not a project dependency yet. It runs at M0 rather than M1 because it
      constrains `card.stability_max` and the whole unlock gate, which are designed *now*.
- [ ] `pl/morph.py` — the *only* module importing `morfeusz2`. `analyse(text) → Lattice`,
      `generate(lemma, tag) → list[str]`, `forms(lemma) → list[Form]`.
- [ ] `pl/tags.py` — Morfeusz tag string → structured features. Unknown tag raises (criterion 2).
- [ ] `pl/domain.py` — frozen dataclasses: `Lexeme`, `Form`, `MorphTag`, `ExpectedSlot`, `Diagnosis`.
      **`ExpectedSlot` carries the expected lexeme's full paradigm**, which is what keeps the
      classifier pure and the corpus a plain fixture.
- [ ] Paradigm round-trip over the M1 lexeme set (criterion 3). Run this *before* authoring frames —
      M1's content generation depends on it.
- [ ] `pl/grade/classify.py` — the six steps, in order. **Step 2 before step 3**, and step 3's folds
      **directional**. Both orderings are load-bearing and both were wrong in revision 1.
- [ ] `pl/grade/explain.py` — `Diagnosis` → message derived from features, never from a string diff.
- [ ] `tests/data/golden.yaml` — **≥ 60 labelled triples, all eight M1 classes, ≥ 5 per class.**
      Write these *before* the classifier; they are the specification, and writing them second means
      writing them to fit the code. **Include the M2 alternation cases** (`Krakówie`/`Krakowie`,
      `stołowi`/`stołu`) per criterion 4b — they are out-of-phase content but they are the only
      regression test for the directional rule.
- [ ] Measure Morfeusz resident size and thread-safety (§"The analyser is a library").
- [ ] Tune the edit-distance threshold against the corpus.
- [ ] **GATE: ≥ 95% aggregate and ≥ 80% in every class.** Below either, stop — this is spec §9's
      instruction and the whole reason M0 exists.

### M1 — the learning loop (6–10 weeks). Single user, no auth.

- [ ] Add `fastapi`, `uvicorn`, `sqlalchemy`, `alembic`, `psycopg[binary]`, `pydantic`, `jinja2`,
      `fsrs`. **Not** `fsrs[optimizer]`.
- [ ] `pl/models.py` + first Alembic migration; Docker Compose for Postgres.
- [ ] SGJP ingest → `lexeme` + `form` for **curriculum lexemes only**, paradigms materialised via
      `generate()`, idempotent on `(lexeme_id, morph_tag)`. Not the whole dictionary.
- [ ] `data/lexemes.yaml` (~40 nouns in scope, hand-curated), `data/nodes.yaml` (the graph above),
      `data/frames.yaml` (~8 frames).
- [ ] `pl/content/frames.py` — instantiate frames × lexemes; build-time assertion that every expected
      surface is a real form (criterion 16).
- [ ] `pl/schedule.py` — `fsrs` wrapper, the declarative routing table, `stability_max` maintenance
      **with the `None` guard**, and the routing × `node.type` intersection.
- [ ] `pl/session.py` — debt → remediation → new, with the daily new-card cap and no total cap.
- [ ] `pl/streak.py` — both conditions, local-midnight boundary, freeze accrual and consumption.
- [ ] Node unlock at session end: **population-appropriate gate**, `assert denominator > 0`, latched
      by writing `node_unlock`.
- [ ] `pl/api.py` — `/api/session`, `/api/submit`, **bound to localhost**. No expected answer leaves
      the server before submission (criterion 17).
- [ ] Promotion queue write path: failed submission, all tokens analysable → `item_variant` candidate
      (criterion 18). **No auto-promotion at M1** — see Optional hardening.
- [ ] `pl/templates/session.html` + `pl/static/session.js`. No npm.
- [ ] Tests for criteria 9–19. Three carry the design's weight and should be written first:
      **11** as a direct assertion on the two-card fan-out (`sklepie` fails pattern only, `sklepa`
      fails morph and *passes* pattern); **13** as unlock monotonicity — master a node at 3/3 strata,
      create the fourth pattern card, assert the node stays unlocked; **19** as the `V01 → N01`
      traversal, asserting a lexical-card gate and no division by zero.
- [ ] **Use it daily for 30 days.** Spec §10's only reliable mitigation for the stall risk, and spec
      §2's trigger for reconsidering path B.

### M2–M4 — roadmap only

- **M2 — A1 complete.** Genitive (split across 3–4 nodes per spec §4.2), instrumental, locative with
  its palatalisation alternations. Aspect pairs enter with the first verb. Multi-slot items, which
  bring `item_slot`, real lattice alignment, and the two error classes M1 cannot produce
  (`WORD_ORDER`, `MISSING_CONSTITUENT`). Spec §5.2's LLM pipeline behind the `item.source` seam, plus
  the reviewer and the cost model that decides whether B1 is viable at all. Audio, blob, CDN. React
  PWA replacing the Jinja page against the unchanged API. Revisit offline (§"Offline contradicts
  server-side grading").
- **M3 — A2.** Dative, vocative. ~1,400 lemmas. Listening and minimal-pair exercises. Auth only if
  path B is live by then.
- **M4 — B1.** Verbs of motion as a curriculum area in its own right, aspect across all tenses,
  conditionals, complex syntax, reading passages.

---

## Estimated Footprint

- **Existing files changed:** 0 — greenfield repository.
- **Files added, M0:** ~13. `pyproject.toml`, `README.md`, `pl/{__init__,domain,tags,morph}.py`,
  `pl/grade/{__init__,classify,explain}.py`, `tests/{test_tags,test_morph,test_classify}.py`,
  `tests/data/golden.yaml`.
- **Files added, M1:** ~17. `pl/{db,models,schedule,session,streak,api}.py`,
  `pl/content/frames.py`, `data/{lexemes,nodes,frames}.yaml`, `pl/templates/session.html`,
  `pl/static/session.js`, one Alembic migration, `docker-compose.yml`, and tests.
- **Files deleted:** 0.
- **New abstractions:** two — `Diagnosis` (the classifier's return type) and the declarative
  `error_class → {population: rating}` routing table. Both are data, not machinery.
- **New tables against spec §8:** three — `pattern`, `attempt`, `node_unlock`. All approved.
  `node_unlock` is revision 2's only addition; the three blocking corrections alongside it added no
  files, no tables and no dependencies.
- **Dependencies, M0:** `morfeusz2`, `pyyaml`, `pytest`.
- **Dependencies added at M1:** `fsrs`, `fastapi`, `uvicorn`, `sqlalchemy`, `alembic`,
  `psycopg[binary]`, `pydantic`, `jinja2`, `httpx` (dev).
- **Components removed from spec §7:** four — morphology sidecar, Redis, blob/CDN, Azure OpenAI.
  Plus the React/npm toolchain, deferred to M2.

---

## Optional hardening

None selected. Recorded for later consideration, each deliberately excluded from M1:

- **Word-order auto-promotion.** Auto-accept a failed answer when the learner's tokens carry the
  expected morphology in a different order, restricted to permutations preserving preposition–
  complement adjacency. Excluded because Morfeusz is an analyser, not a parser: it cannot tell you
  `Idę do sklep` is ungrammatical, only that both words exist. Manual queue at M1 is the honest
  mechanism, and it is the one spec §10 actually asks for.
- **A backlog cap on total session length.** Serve at most *n* overdue cards per day and roll the rest
  forward, with the streak's debt condition reading the capped set. No acceptance criterion requires
  it and path A's single learner can simply stop, so it stays out of M1 — but it is the first thing to
  build if a second learner exists, because the uncapped backlog bites hardest exactly when churn risk
  is highest (§"Session composition").
- **An on-screen diacritic strip in the answer input.** Removes the no-Polish-keyboard excuse and
  makes the lenient `ORTHOGRAPHY` reading of ending-diacritics defensible rather than merely chosen
  (§"The six-step classifier", accepted limitations).
- **Four-point FSRS ratings** fitted from `latency_ms` once a review corpus exists.
- **FSRS parameter optimisation** (`fsrs[optimizer]`) — needs ~1,000 reviews and pulls torch.
- **Client-side grading for offline sessions**, path A only.
- **Polimorf as a second dictionary** for colloquial coverage gaps.

---

## Validation Performed

- **Analyser licence** — 2-clause BSD, covering both the program and *both* dictionaries; commercial
  use, redistribution and derivative works permitted; attribution by retained copyright notice.
  Verified at [morfeusz.sgjp.pl/doc/license/en](https://morfeusz.sgjp.pl/doc/license/en). Spec §10's
  Fatal row and §11 Q2 are closed.
- **Platform coverage** — `morfeusz2` 1.99.15 (2026-06-01) publishes `cp310-abi3` wheels for
  `macosx_11_0_universal2`, `manylinux_2_28_x86_64`, `win_amd64`, `win32`; **no linux/arm64**.
  Verified from the [PyPI JSON API](https://pypi.org/pypi/morfeusz2/json).
- **Build host** — arm64, macOS 26.2, `uv` and `docker` present; system Python is 3.9.6, below the
  wheel's cp310 floor, so 3.12 must be pinned via uv.
- **FSRS packaging** — `fsrs` 6.3.2 (2026-08-09), MIT, Python ≥ 3.10, sole runtime dependency
  `typing-extensions`, JSON serialisation present, optimiser an optional extra. Verified from the
  [PyPI JSON API](https://pypi.org/pypi/fsrs/json).
- **`Card` exposes stability** — `stability: float | None` and `difficulty: float | None` are public
  dataclass attributes, and `CardDict` (what `to_json()` emits) carries `card_id`, `state`, `step`,
  `stability`, `difficulty`, `due`, `last_review`. `stability` is `None` throughout the `Learning`
  state, and `Card` carries neither `reps` nor `lapses`. Read from `fsrs/card.py` at
  `open-spaced-repetition/py-fsrs`. The unlock gate depends on the first fact; the `None` guard and
  the review-count rule depend on the second and third.
- **`o/ó` is a morphophonological alternation, not a diacritic pair** — `stół/stołu`,
  `Kraków/Krakowie`, `wóz/wozu`, `bóg/boga`. Revision 1's confusion set would have classified every
  one of these as a typo that does not fail the grammar card.
- **Generation exists** — Morfeusz exposes synthesis from lemma plus inflectional characteristics;
  `generate` is a constructor flag.
- **House conventions** — `docs/design/<slug>-v1.md`, uv + pytest + `pyproject.toml`, single
  importable package at the repository root, `Verified:`/`Assumption:` labelling. Read from
  `mali-factory-optimisation` and `voice-to-quote`.

## Validation Required

- [ ] Exact `morfeusz2` Python API: `analyse()` return structure, DAG node representation, `generate()`
      signature, and how ambiguity is expressed. M0 week 1 — the classifier's shape depends on it.
- [ ] Morfeusz tag inventory and format for every feature the classifier reads: case, number, gender,
      animacy (`m1/m2/m3`), aspect, degree.
- [ ] SGJP vs Polimorf coverage across the M1 lexeme set. Licence is identical; this is a data call.
- [ ] Morfeusz instance resident size and thread-safety. Reverses §"The analyser is a library" if it
      fails, changing `pl/morph.py` alone.
- [ ] `fsrs` 6.x JSON round-trip preserves `stability` across `to_json`/`from_json`, and the
      `State`/`step` transitions behave as documented. *(That `Card.stability` is public and
      serialised is now Verified; what remains is the round-trip and the state machine.)* M0 day 1.
- [ ] Whether `sie` and `robie` genuinely return empty analyses from SGJP. If either is a real form,
      criterion 8 needs restating — the *rule* holds, but the example changes.
- [ ] Edit-distance threshold of 2, tuned against the golden corpus.
- [ ] Azure PostgreSQL Flexible Server and container compute available on x86-64 in the target region.
- [ ] Spec §11 Q3, Q5, Q6, Q7 remain open and do not block M0 or M1: reviewer cost (M2 gate),
      metalanguage depth, `pan/pani` register, heritage-learner entry point.
