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
| Test suite | 273 passing | — |

Measured while proving it:

- Dictionary footprint **28 MB**, analyse latency **22 µs**. The design budgeted
  `< 50 ms` for a network hop to a sidecar container; in-process is three orders
  of magnitude inside that, which is why there is no sidecar and no cache.
- 8 threads × 400 mixed analyse/generate calls: no errors.

**M1 — the learning loop.** Single user, no auth. FSRS over three card
populations, session composition, streak, and a review screen.

**Progress and reward.** `GET /progress` shows per-node mastery that decays with
retrievability while the unlock gate does not (criterion 14), a thirty-day
retention curve, vocabulary met and cards due. Finishing a session names the
skill just unlocked and the sentence describing it, rather than the node's
primary key, and shows the nearest of three milestones — cards remembered a
week, words met, days in a row. Milestones are a *standing*, never "you just
crossed one": claiming a crossing needs a record of what has already been
announced, and one announced twice teaches the learner the number is decorative.

**M2 — cases, aspect and whole sentences.** *(partly built)* Instrumental,
genitive split three ways, locative with its palatalisation alternations, and
aspect taught from the first verb as pairs. 82 lexemes, 25 frames, 32 authored
sentences, **983 items** across six exercise types — cloze (673),
free translation (123), multiple choice (113, covering both form selection and
meaning recall), preposition drill (30), listening dictation (26) and aspect
choice (18) — all generated with no LLM and no human review. Multi-slot items
bring the two error classes a single blank cannot produce: `WORD_ORDER` and
`MISSING_CONSTITUENT`.

**Frames are gated by theme, so nonsense does not scale with vocabulary.** An
ungated frame makes one sentence per lexeme, so an unsuitable pairing is one bad
sentence *per unsuitable noun* — `Kupuję szkołę` ("I am buying the school") is a
curiosity at 76 lexemes and a systematic defect at 2,000. Gating the core frames
removed 139 such sentences and added none; letting a lexeme carry several themes
(`dom` is a venue you go to *and* a home you own) recovered 25 good ones the
gates had cost. That is why the item count above went down: it is the same
curriculum with the absurd sentences taken out.

**Content pipeline and audio.** The design's pipeline is generate → validate →
review → speak. `pl/content/validate.py` implements the validation stage and
needs no model — every check is a morphological or arithmetic fact. The
generation stage is authored offline and committed as `data/sentences.yaml`,
which is what `item.source` was declared for: the runtime holds no API key and
makes no network call. Speech comes from the local Polish voice at two speeds,
cached to disk.

Not built: blob storage and a CDN (deployment concerns, not development ones);
the React PWA (deferred — no capability gain over the current page).

**What a learner actually meets is measured, not assumed.** The counts above say
what was built; `scripts/journey_sim.py` says what a learner reaches. The two
once differed by a factor of fifty. A diligent learner answered 1,190 questions
over sixty days and met twenty distinct items — no grammar, nothing unlocked,
every headline number above still true. The cause was the middle segment of the
session: remediation had no budget, so it filled every session from the weakest
node and the introduction segment never ran again after day one. Fixed, and
pinned by `test_remediation_does_not_starve_new_material`.

*An earlier revision of this file blamed that on criterion 9 and "day four".
That explanation was written before anyone simulated it and was wrong: debt was
clear on most of those days.*

**Criterion 9 has a bound, and the bound is a measured number.** "Introduce
nothing while anything is overdue" is the stricter-sounding rule and it was the
next thing stopping the course opening: a learner at 85% accuracy is rarely at
zero due cards and almost never at zero twice running, so introduction fired
about one day in four. `pl.session.DEBT_TOLERANCE` is what the measurement
bought — over ninety simulated days, averaged across four seeds:

| bound | distinct items | nodes unlocked | exercise types | peak backlog |
|---|---:|---:|---:|---:|
| overdue = 0 (as written) | 123 | 1 | 2 of 6 | 22 |
| **overdue ≤ 5** | **224** | **4** | **4 of 6** | 20 |
| overdue ≤ 10 | 236 | 4 | 4 of 6 | 27 |

Five rather than ten because ten lets the backlog reach 27 against a twenty-item
session — more than one sitting can clear, and criterion 12 makes clearing it the
condition for the streak.

```bash
uv run python scripts/journey_sim.py 90 20 0.85
```

The simulation is deterministic: same arguments, same numbers, whatever else the
machine is doing. That took two fixes and is worth knowing about, because without
them it disagreed with itself by 80% and briefly argued for the wrong bound —
FSRS fuzzes every interval from the *global* RNG, and it reads the real clock
unless you hand it one.

At this point the learner met 58 of 76 lexemes and stopped — the rest sat behind
nodes ninety days did not open. What was still holding those nodes shut is below.

**Build order was choosing the curriculum.** 26 listening-dictation items, 131
free translations and 58 prep drills were built, graded correctly, and never
served — a card is shared by every exercise built on the same form or stratum,
the draw offered whichever had the lowest id, and ids follow build order, where
every sentence's cloze is generated before its dictation. Fixed by ordering the
draw least-practised-first for every population and rotating among equals, and by
starting the introduction round-robin at a different node each session (a ten-card
budget is spent by the sixth node, so `N12` was never reached). A ninety-day
learner goes from 262 distinct items and two exercise types to 474 and five.

**The busiest cards were being crushed by their own popularity.** A pattern card
is shared by every item in its stratum and was scored once per item, so a session
holding ten items of one rule reviewed that card ten times minutes apart — and
FSRS grows stability from the interval actually elapsed. One card took **447
reviews and stalled at 6.11 stability**, under the seven-day mastery bar, while a
low-traffic card in the same node reached 112 on eight reviews. The cards the
learner practised most were the least able to master. `ONE_REVIEW_PER_DAY`
advances a card at most once daily; later encounters still record their attempt
and error events, so remediation still sees everything that went wrong.

**And it made the streak unearnable, which review caught and no test did.**
Suppressing a card's schedule advance left `due_at` in the past, and FSRS puts a
new card's first steps minutes apart — so on any day the learner met new
material the debt never reached zero, and criterion 12 makes clearing it the
condition for the streak. The learner answered, and the due counter did not
move. A card that has already had its turn today is no longer counted as debt
nor re-offered, by one definition (`session.settled_today`) that the composer and
the streak both read. Measured over thirty simulated days at 85% accuracy: the
streak is earned on 13 days, against 4 before.

Every positive streak test answered items without creating a single card, so
`debt_remaining` counted nothing and the condition passed for the wrong reason —
and `journey_sim.py` never called `record_activity` at all, so the ninety-day
instrument could not see criterion 12 either. Both are fixed: the simulation now
finishes each day the way the review page does and reports the streak, and
`test_a_learner_who_finishes_a_real_session_earns_the_streak` drives the whole
loop without hand-writing a single `due_at`.

`MASTERY_ALLOWED_SHORTFALL = 1` implements the design's own worked example, which
the code never delivered: it says a four-stratum node means "three of four, and
the learner may carry one weak paradigm class forward", but 3/4 = 0.75, so the
fraction always demanded four of four.

Across six seeds, ninety days each: median distinct items 456 → **526**, median
nodes opened 4 → **8**, busiest card 196–578 reviews → **34–58**.

**The last chokepoint was one node with one stratum.** `N03` carried a single
paradigm class, so its one pattern card gated six nodes behind it, and one run in
six stalled at four nodes with `V01` 58/58, `N01` 5/5, `N02` 5/5, `N04` 3/3 — and
`N03` 0/1, one card at 5.8 stability against a 7.0 bar.

All twenty feminine nouns shared the class `f:acc-ę|nom-a`. Six consonant-final
feminines (`noc`, `sól`, `wieś`, `mysz`, `rzecz`, `twarz`) are `f:acc-0|nom-0` —
accusative equals nominative, a real learner trap the course could not teach.
That second stratum takes `N03` from 1-of-1 to 1-of-2, because
`MASTERY_ALLOWED_SHORTFALL = 1`. **Under the old gate the same edit would have
made it harder** (2-of-2): the gate change and the content change are only useful
together. Eight nodes now open on **all six seeds**, median 605 of 983 items.

**A content pipeline that validates morphology validates one word in three.**
Adding those nouns exposed two defects the "looked up, not written" guarantee
does not cover, both found by reading sentences aloud rather than by any test.
`Jestem w wsi` was built where Polish requires `we wsi` — phonology, now handled
by `frames.euphonic`. And `Jestem w uniwersytecie` was built where it must be
**`na uniwersytecie`** — lexical government, which nothing derives from gender,
paradigm or theme, so `lexemes.yaml` carries `locative_preposition` and
`frames.place_preposition` applies it.

The two corrections disagree on the same word, and their order matters: `we wsi`
is the correct way to say the *wrong* preposition, and the right answer is
`na wsi`. Expected surfaces come from SGJP and cannot be wrong; every word
around them is authored, and nothing was checking that.

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
pl/content/validate.py  the pipeline's automated stage
pl/audio.py           the only module that knows how speech is made

data/lexemes.yaml     82 lexemes — 64 nouns and 18 verbs in 9 aspect pairs
data/nodes.yaml       the skill DAG — 13 nodes
data/frames.yaml      25 authored frames
data/sentences.yaml   32 authored sentences, validated at build time
data/function_words.yaml  40 lemmas the whitelist has to admit
tests/data/golden.yaml  the kill-gate corpus — 65 cases
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
