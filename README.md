# Polish Learning Platform

A morphology-first Polish course for English speakers. **Morphology is the
curriculum; vocabulary is the material it operates on.**

**M0 — the grader.** No UI, no database, no API. The one thing M0 exists to prove
is that a machine can take a wrong answer and name *which grammatical decision*
the learner got wrong, well enough to schedule remediation against it. If that
cannot be done, the product's central claim is false and the right response is to
stop here.

Design: [`docs/design/polish-learning-platform-v1.md`](docs/design/polish-learning-platform-v1.md)

## Status

**The M0 gate passes.**

| | Result | Threshold |
|---|---|---|
| Golden corpus | **98.5%** (64/65) | ≥ 95% |
| Weakest class | `CASE_WRONG` **85.7%** | ≥ 80% per class |
| Paradigm round-trip | all 23 M1 lexemes | no exceptions |
| Test suite | 165 passing | — |

Measured while proving it:

- Dictionary footprint **28 MB**, analyse latency **22 µs**. The design budgeted
  `< 50 ms` for a network hop to a sidecar container; in-process is three orders
  of magnitude inside that, which is why there is no sidecar and no cache.
- 8 threads × 400 mixed analyse/generate calls: no errors.

**M1 — the learning loop.** Single user, no auth. 44 lexemes, nominative and
accusative, three exercise types, FSRS over three card populations, session
composition, streak, and a review screen. 352 items, generated with no LLM and
no human review.

## Quick start

```bash
uv sync && uv run pytest
```

Build the curriculum and run the app:

```bash
uv run python -m pl.content.ingest && uv run uvicorn pl.api:app --port 8117
```

Then open http://localhost:8117. SQLite is the default so this needs no daemon;
Postgres is the deployment target and is selected with `DATABASE_URL`:

```bash
DATABASE_URL=postgresql+psycopg://polish:polish@localhost:5432/polish uv run python -m pl.content.ingest
```

Python 3.12 is pinned. `morfeusz2` ships prebuilt `abi3` wheels for macOS
(universal2), Linux **x86-64** and Windows, and publishes **no sdist** — on
linux/arm64 there is no artefact and `pip` cannot fall back to a build, so pin CI
runners and container images accordingly.

## How grading works

Six steps, in an order where two of the transitions are load-bearing.

1. **Exact** — the expected surface.
2. **Another form of the same lexeme** → compare feature *sets*; the first
   disagreement in precedence order names the error.
3. **Spelling** — an ASCII fold in the learner's typing direction, or a homophone.
4. **Not a word, but close** → the right case built with the wrong paradigm's ending.
5. **A form of some other lexeme** → aspect partner, or a vocabulary error.
6. **Neither** → unanalysable.

**Step 2 precedes step 3** so a dropped diacritic that lands on another real form
is diagnosed as the case error it is — `matka` for `matkę` is the nominative, not
a typo.

**Step 3 precedes step 5** because `sie` and `robie` are both real Polish forms
(`si` plural adjective; dative of `roba`). Reverse the order and the commonest
keyboard artefact in the language gets reported as a vocabulary error.

**Step 3 is directional.** `ę` typed as `e` is a missing keystroke. `o` typed as
`ó` is not — no keyboard produces `ó` by accident — so it falls through to step 4
and fails the grammar card. That asymmetry is what stops the `stół → stołu` and
`Kraków → Krakowie` vowel alternations being written off as typos.

### Why tags are compared as sets, never for equality

Morfeusz collapses syncretism into the tag rather than emitting one
interpretation per reading. `kota` carries a single nominal analysis of the
animal lexeme:

```
subst:sg:gen.acc:m2
```

An expected accusative matches that only under **set intersection**. Under
equality, the correct answer to the masculine-animate accusative — the first hard
concept in the course — would be reported as a case error.

### The distinction the whole design rests on

| Expected `sklepu` | Learner types | Diagnosis | Because |
|---|---|---|---|
| genitive of `sklep` | `sklepie` | `CASE_WRONG` | a well-formed locative: they can inflect this noun, and chose the wrong case |
| genitive of `sklep` | `sklepa` | `CASE_RIGHT_FORM_WRONG` | not a word: they chose the genitive correctly and built it with the wrong paradigm's ending |

At M1 these route to different cards — the first fails the pattern card and
leaves the morphological card's schedule untouched; the second fails the
morphological card while the pattern card *passes*. The review screen shows
which cards an answer moved, and which it deliberately did not:

```
ANIMACY
kot is animate, so its accusative borrows the genitive: kota.
  the rule · Again    this word's form · untouched    vocabulary · untouched
```

### Pattern cards are stratified, and the key is derived

A pattern card is scheduled against **(rule, paradigm class)**, not against the
rule alone, so every draw within a card is homogeneous in difficulty — which is
what FSRS assumes and what a random draw across the whole vocabulary would break.

`paradigm_class` is derived from the lexeme's own generated endings, never from
gender. Gender predicts the accusative and nothing past it:

| | gender | genitive |
|---|---|---|
| `sklep`, `dom`, `rower` | m3 | `-u` |
| `chleb`, `ser` | m3 | `-a` |

A gender-keyed stratum would look correct for the whole of M1 and silently mix
difficulties from M2 onward. Deriving it costs one function and a re-ingest when
the curriculum grows; getting it wrong would be invisible until the genitive.

## Layout

```
pl/tags.py            tag strings -> comparable feature records
pl/domain.py          frozen value types; ExpectedSlot carries its own paradigm
pl/morph.py           the only module importing morfeusz2
pl/grade/classify.py  the six steps
pl/grade/explain.py   diagnosis -> a sentence naming the decision

pl/models.py          schema, in path B's shape
pl/content/ingest.py  data files + Morfeusz -> lexemes, forms, nodes, strata
pl/content/frames.py  frames x lexemes -> items
pl/schedule.py        FSRS, and the error-class -> card routing table
pl/session.py         debt -> remediation -> new, and the unlock gate
pl/streak.py          both conditions, in the learner's own timezone
pl/api.py             JSON API; grading never runs in the browser

data/lexemes.yaml     44 nouns, hand-curated, theme-driven
data/nodes.yaml       the skill DAG
data/frames.yaml      9 authored frames
tests/data/golden.yaml  the kill-gate corpus
```

`pl/grade` imports no I/O of any kind. `ExpectedSlot` carries the expected
lexeme's whole paradigm, so `classify` is a pure function and the corpus is a
plain YAML fixture — which is what makes the gate runnable with no schema in
existence.

## Licensing

Morfeusz 2 and both its dictionaries (SGJP, Polimorf) are under the 2-clause BSD
licence. Commercial use, redistribution and derivative works are permitted; the
obligation is retaining the copyright notices. That covers **the dictionary data**,
not just the program, which is what makes shipping a derived table of inflected
forms lawful.
