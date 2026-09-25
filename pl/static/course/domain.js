/* pl.domain, ported: the value types shared by the grader.
 *
 * `ExpectedSlot` carries the expected lexeme's whole paradigm beside the cell
 * under test, which keeps `classify` a pure function of (slot, text).
 *
 * `casefold` lives here too. It is Python's `str.casefold`, which the grader
 * relies on in three modules, and this is the one module all of them import.
 */

import { ANIMATE_MASCULINE, INANIMATE_MASCULINE } from "./tags.js";

/* pl.domain.ErrorClass */
export const ErrorClass = Object.freeze({
  CORRECT: "CORRECT",
  CASE_WRONG: "CASE_WRONG",
  CASE_RIGHT_FORM_WRONG: "CASE_RIGHT_FORM_WRONG",
  GENDER_AGREEMENT: "GENDER_AGREEMENT",
  NUMBER_WRONG: "NUMBER_WRONG",
  ASPECT_WRONG: "ASPECT_WRONG",
  ANIMACY: "ANIMACY",
  ORTHOGRAPHY: "ORTHOGRAPHY",
  LEXICAL: "LEXICAL",
  UNANALYSABLE: "UNANALYSABLE",
  WORD_ORDER: "WORD_ORDER",
  MISSING_CONSTITUENT: "MISSING_CONSTITUENT",
});

/* pl.domain.ExerciseType */
export const ExerciseType = Object.freeze({
  CLOZE: "cloze",
  MCQ: "mcq",
  PREP_DRILL: "prep_drill",
  ASPECT_CHOICE: "aspect_choice",
  FREE_TRANSLATION: "free_translation",
  LISTENING_DICTATION: "listening_dictation",
});

/* pl.domain.MULTI_SLOT, AUDIBLE and STRICT_ORTHOGRAPHY */
export const MULTI_SLOT = new Set([ExerciseType.FREE_TRANSLATION, ExerciseType.LISTENING_DICTATION]);
export const AUDIBLE = new Set([ExerciseType.LISTENING_DICTATION]);
export const STRICT_ORTHOGRAPHY = new Set([ExerciseType.LISTENING_DICTATION]);

/* Python's `str.casefold`, one code point at a time, since case folding has no
 * context rules. `toLowerCase` alone is not it: `ß` folds to `ss` and a final
 * `ς` to `σ`.
 *
 * Upper- then lower-casing a character gives its full case folding, with four
 * exceptions handled first. Measured against Python 3.12's `casefold` over every
 * code point: this agrees on all of them that Python's Unicode (15.0) assigns. */
function foldCharacter(ch) {
  const cp = ch.codePointAt(0);
  if (cp < 0x80) return ch.toLowerCase();
  // Cherokee folds to its capitals, the reverse of every other script.
  if (cp >= 0x13a0 && cp <= 0x13f5) return ch;
  if ((cp >= 0x13f8 && cp <= 0x13fd) || (cp >= 0xab70 && cp <= 0xabbf)) return ch.toUpperCase();
  if (cp === 0x0131) return ch; // dotless ı folds to itself
  if (cp === 0x1e9e) return "ss"; // capital ẞ
  return ch.toUpperCase().toLowerCase();
}

export function casefold(text) {
  let out = "";
  for (const ch of text) out += foldCharacter(ch);
  return out;
}

/* pl.domain.Form: one inflected surface with its analysis. */
export class Form {
  constructor(surface, lemma, tag, labels = []) {
    this.surface = surface;
    this.lemma = lemma;
    this.tag = tag;
    this.labels = labels;
  }

  /* Form.base_lemma */
  get baseLemma() {
    return this.lemma.split(":", 1)[0];
  }
}

/* pl.domain.ExpectedSlot: a single blank, the form required, and its paradigm. */
export class ExpectedSlot {
  constructor(expected, paradigm, aspectPartner = null) {
    this.expected = expected;
    this.paradigm = paradigm;
    this.aspectPartner = aspectPartner;
  }

  /* ExpectedSlot.lemma */
  get lemma() {
    return this.expected.lemma;
  }

  /* ExpectedSlot.tag */
  get tag() {
    return this.expected.tag;
  }

  /* ExpectedSlot.with_surface: forms of this lexeme with that surface,
   * case-insensitively. */
  withSurface(surface) {
    const folded = casefold(surface);
    return this.paradigm.filter((f) => casefold(f.surface) === folded);
  }

  /* ExpectedSlot.masculine_animacy: "animate", "inanimate", or null when the
   * expected form is not a masculine noun. */
  get masculineAnimacy() {
    if (this.expected.tag.pos !== "subst") return null;
    const genders = this.expected.tag.gender;
    for (const gender of genders) {
      if (ANIMATE_MASCULINE.has(gender)) return "animate";
    }
    if (genders.has(INANIMATE_MASCULINE)) return "inanimate";
    return null;
  }
}

/* Python's AssertionError, which the course raises for content it cannot
 * serve and the progress page catches from `is_mastered`. */
export class AssertionError extends Error {
  constructor(message) {
    super(message);
    this.name = "AssertionError";
  }
}

/* pl.domain.Diagnosis: what the grader concluded. */
export class Diagnosis {
  constructor({ errorClass, submitted, slot, observed = null, differingAttr = null }) {
    this.errorClass = errorClass;
    this.submitted = submitted;
    this.slot = slot;
    this.observed = observed;
    this.differingAttr = differingAttr;
  }

  /* Diagnosis.is_correct */
  get isCorrect() {
    return this.errorClass === ErrorClass.CORRECT;
  }
}
