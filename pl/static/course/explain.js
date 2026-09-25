/* pl.grade.explain, ported: a Diagnosis as a sentence naming the grammatical
 * decision. Every message is built from the features the classifier compared,
 * never from a string difference, and every one must read exactly as the
 * Python's does.
 */

import { ErrorClass } from "./domain.js";

/* pl.grade.explain._CASES, _NUMBERS, _GENDERS and _ASPECTS, in their order. */
const CASES = new Map([
  ["nom", "nominative"],
  ["gen", "genitive"],
  ["dat", "dative"],
  ["acc", "accusative"],
  ["inst", "instrumental"],
  ["loc", "locative"],
  ["voc", "vocative"],
]);

const NUMBERS = new Map([
  ["sg", "singular"],
  ["pl", "plural"],
]);

const GENDERS = new Map([
  ["m1", "masculine personal"],
  ["m2", "masculine animate"],
  ["m3", "masculine inanimate"],
  ["f", "feminine"],
  ["n", "neuter"],
]);

const ASPECTS = new Map([
  ["imperf", "imperfective"],
  ["perf", "perfective"],
]);

/* pl.grade.explain._CASE_ORDER: `gen.acc` always reads "genitive or accusative". */
const CASE_ORDER = ["nom", "gen", "dat", "acc", "inst", "loc", "voc"];

/* pl.grade.explain.GENERIC_FALLBACK */
export const GENERIC_FALLBACK = "The form needed here is {want}.";

/* pl.grade.explain._render */
function render(values, names, order = null) {
  const keys = order ?? [...names.keys()];
  const present = keys.filter((key) => values.has(key)).map((key) => names.get(key));
  if (!present.length) return "unspecified";
  if (present.length === 1) return present[0];
  return [present.slice(0, -1).join(", "), present[present.length - 1]].join(" or ");
}

/* pl.grade.explain._case, _number, _gender and _aspect */
const caseOf = (tag) => render(tag.case, CASES, CASE_ORDER);
const numberOf = (tag) => render(tag.number, NUMBERS);
const genderOf = (tag) => render(tag.gender, GENDERS);
const aspectOf = (tag) => render(tag.values("aspect"), ASPECTS);

/* pl.grade.explain.explain: a learner-facing sentence for `diagnosis`. */
export function explain(diagnosis) {
  const slot = diagnosis.slot;
  const want = slot.expected.surface;
  const lemma = slot.expected.baseLemma;
  const expectedTag = slot.tag;
  const observed = diagnosis.observed;

  switch (diagnosis.errorClass) {
    case ErrorClass.CORRECT:
      return "Correct.";

    case ErrorClass.ANIMACY:
      if (slot.masculineAnimacy === "animate") {
        return `${lemma} is animate, so its accusative borrows the genitive: ${want}.`;
      }
      return `${lemma} is inanimate, so its accusative is the same as the nominative: ${want}.`;

    case ErrorClass.CASE_WRONG: {
      const got = observed ? caseOf(observed.tag) : "another case";
      return `You used the ${got}. This slot needs the ${caseOf(expectedTag)}: ${want}.`;
    }

    case ErrorClass.CASE_RIGHT_FORM_WRONG:
      return `The ${caseOf(expectedTag)} was the right choice — but ${lemma} does not build it that way: ${want}.`;

    case ErrorClass.NUMBER_WRONG: {
      const got = observed ? numberOf(observed.tag) : "the wrong number";
      return `You used the ${got}. This slot needs the ${numberOf(expectedTag)}: ${want}.`;
    }

    case ErrorClass.GENDER_AGREEMENT: {
      const got = observed ? genderOf(observed.tag) : "the wrong gender";
      return `${lemma} has to agree with a ${genderOf(expectedTag)} noun here, not a ${got} one: ${want}.`;
    }

    case ErrorClass.ASPECT_WRONG: {
      const partner = (slot.aspectPartner || "").split(":", 1)[0];
      const got = partner ? `${partner} is ` : "That is ";
      const aspect = observed ? aspectOf(observed.tag) : "the wrong aspect";
      return `${got}${aspect}. This needs the ${aspectOf(expectedTag)} ${lemma}: ${want}.`;
    }

    case ErrorClass.ORTHOGRAPHY:
      return `The grammar is right — check the spelling: ${want}.`;

    case ErrorClass.WORD_ORDER:
      return "Every word is right — the order is not. Polish word order is freer than English, but not free.";

    case ErrorClass.MISSING_CONSTITUENT:
      return "Something is missing. Every word you heard has to be there.";

    case ErrorClass.LEXICAL: {
      const other = observed ? observed.baseLemma : "another word";
      return `That is a form of ${other}. This slot needs ${lemma}: ${want}.`;
    }

    case ErrorClass.UNANALYSABLE:
      return `That is not a Polish word. The ${caseOf(expectedTag)} of ${lemma} is ${want}.`;
  }
  return GENERIC_FALLBACK.replace("{want}", () => want);
}
