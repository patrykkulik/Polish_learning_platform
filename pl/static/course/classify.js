/* pl.grade.classify, ported: the six-step error classifier.
 *
 * `classify` is a pure function of (ExpectedSlot, text). Steps 1 to 4 read only
 * the paradigm the slot carries; step 5 asks the word list whether the text is a
 * form of some other lexeme. The two orderings the Python docstring defends —
 * step 2 before step 3, and step 3 before step 5 — are kept exactly, as is the
 * one-directional ASCII fold of step 3.
 *
 * Strings follow Python's rules, not JavaScript's: whitespace is what
 * `str.split()` splits on, case is `str.casefold`, and lengths and positions
 * count code points.
 */

import { Diagnosis, ErrorClass, casefold } from "./domain.js";
import * as morph from "./morph.js";

/* pl.grade.classify._ASCII_FOLD: Polish letters and the ASCII a learner without
 * a Polish keyboard types instead. One direction only. */
const ASCII_FOLD = new Map([
  ["ą", "a"],
  ["ć", "c"],
  ["ę", "e"],
  ["ł", "l"],
  ["ń", "n"],
  ["ó", "o"],
  ["ś", "s"],
  ["ź", "z"],
  ["ż", "z"],
]);

/* pl.grade.classify._HOMOPHONES: true homophone confusions, both directions. */
const HOMOPHONES = [
  ["ż", "rz"],
  ["u", "ó"],
  ["h", "ch"],
];

/* pl.grade.classify._PRIORITY: which difference names the error. */
const PRIORITY = ["case", "number", "gender", "aspect", "person", "degree"];

/* pl.grade.classify._ATTR_TO_CLASS */
const ATTR_TO_CLASS = new Map([
  ["case", ErrorClass.CASE_WRONG],
  ["number", ErrorClass.NUMBER_WRONG],
  ["gender", ErrorClass.GENDER_AGREEMENT],
  ["aspect", ErrorClass.ASPECT_WRONG],
]);

/* pl.grade.classify.MAX_ATTEMPT_DISTANCE */
export const MAX_ATTEMPT_DISTANCE = 2;

/* What Python's `str.split()` with no argument splits on: every character for
 * which `str.isspace()` is true. JavaScript's `\s` differs by six characters. */
const WHITESPACE = /[\t-\r\x1c-\x20\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/;

/* Python's `text.split()`. */
function splitWords(text) {
  return text.split(WHITESPACE).filter((word) => word !== "");
}

/* pl.grade.classify.normalise: collapse whitespace and case, and nothing else. */
export function normalise(text) {
  return casefold(splitWords(text).join(" "));
}

/* pl.grade.classify.PUNCTUATION */
export const PUNCTUATION = ".,!?;:—–\"'()„”";

/* Python's `text.strip(chars)`. Every character in PUNCTUATION is one UTF-16
 * unit, so stepping by unit is stepping by character. */
function stripChars(text, chars) {
  let start = 0;
  let end = text.length;
  while (start < end && chars.includes(text[start])) start += 1;
  while (end > start && chars.includes(text[end - 1])) end -= 1;
  return text.slice(start, end);
}

/* pl.grade.classify.tokenise: normalised words, without punctuation. */
export function tokenise(text) {
  return splitWords(normalise(text))
    .map((token) => stripChars(token, PUNCTUATION))
    .filter((token) => token !== "");
}

/* pl.grade.classify._fold */
function fold(text) {
  let out = "";
  for (const ch of text) out += ASCII_FOLD.get(ch) ?? ch;
  return out;
}

/* pl.grade.classify._is_learner_direction_fold: `submitted` is `expected` with
 * Polish letters typed as ASCII, and no difference runs the other way. Equal
 * folds have equal lengths, since the fold maps one character to one. */
function isLearnerDirectionFold(submitted, expected) {
  if (fold(submitted) !== fold(expected)) return false;
  const s = Array.from(submitted);
  const e = Array.from(expected);
  return s.every((ch, i) => ch === e[i] || (ASCII_FOLD.has(e[i]) && ch === ASCII_FOLD.get(e[i])));
}

/* pl.grade.classify._homophone_variants: `text` plus everything reachable by at
 * most `depth` homophone swaps. */
function homophoneVariants(text, depth) {
  const seen = new Set([text]);
  let frontier = new Set([text]);
  for (let round = 0; round < depth; round += 1) {
    const next = new Set();
    for (const word of frontier) {
      for (const [a, b] of HOMOPHONES) {
        for (const [src, dst] of [
          [a, b],
          [b, a],
        ]) {
          let start = 0;
          let i;
          while ((i = word.indexOf(src, start)) !== -1) {
            next.add(word.slice(0, i) + dst + word.slice(i + src.length));
            start = i + 1;
          }
        }
      }
    }
    for (const word of seen) next.delete(word);
    for (const word of next) seen.add(word);
    frontier = next;
  }
  return seen;
}

/* pl.grade.classify._is_orthographic: the difference is spelling, not grammar. */
function isOrthographic(submitted, expected) {
  for (const variant of homophoneVariants(submitted, 2)) {
    if (isLearnerDirectionFold(variant, expected)) return true;
  }
  return false;
}

/* pl.grade.classify._distance: Levenshtein distance, over code points. */
function distance(a, b) {
  if (a === b) return 0;
  const left = Array.from(a);
  const right = Array.from(b);
  let previous = Array.from({ length: right.length + 1 }, (_, j) => j);
  left.forEach((ca, i) => {
    const current = [i + 1];
    right.forEach((cb, j) => {
      current.push(Math.min(previous[j + 1] + 1, current[j] + 1, previous[j] + (ca !== cb ? 1 : 0)));
    });
    previous = current;
  });
  return previous[previous.length - 1];
}

/* pl.grade.classify._first_difference: the highest-precedence attribute on which
 * the two tags fail to intersect. */
function firstDifference(observed, expected) {
  for (const attr of PRIORITY) {
    if (!observed.agreesOn(attr, expected)) return attr;
  }
  return null;
}

/* pl.grade.classify._best_candidate: fewest disagreements, then agreeing on
 * case; the first of equals, as Python's `min` keeps. */
function bestCandidate(candidates, expected) {
  const rank = (form) => [
    PRIORITY.filter((attr) => !form.tag.agreesOn(attr, expected)).length,
    form.tag.agreesOn("case", expected) ? 0 : 1,
  ];
  let best = candidates[0];
  let bestRank = rank(best);
  for (const form of candidates.slice(1)) {
    const r = rank(form);
    if (r[0] < bestRank[0] || (r[0] === bestRank[0] && r[1] < bestRank[1])) {
      best = form;
      bestRank = r;
    }
  }
  return best;
}

/* pl.grade.classify._is_animacy_error: a masculine accusative built from the
 * wrong animacy rule. */
function isAnimacyError(slot, observed) {
  if (!slot.tag.case.has("acc")) return false;
  const animacy = slot.masculineAnimacy;
  if (animacy === "animate") return observed.case.has("nom");
  if (animacy === "inanimate") return observed.case.has("gen");
  return false;
}

/* pl.grade.classify.classify_sentence: a whole typed sentence, position by
 * position — too few tokens, then the right tokens in the wrong order, then each
 * position, with the target's position through the full classifier. */
export function classifySentence(expected, targetIndex, target, submitted) {
  const tokens = tokenise(submitted);
  const wanted = expected.map(normalise);
  const result = (errorClass) => new Diagnosis({ errorClass, submitted, slot: target });

  if (tokens.length < wanted.length) return result(ErrorClass.MISSING_CONSTITUENT);

  // Only whether the sorted lists are equal matters, so any consistent order does.
  const same = (a, b) => a.length === b.length && a.every((token, i) => token === b[i]);
  if (!same(tokens, wanted) && same([...tokens].sort(), [...wanted].sort())) {
    return result(ErrorClass.WORD_ORDER);
  }

  for (let index = 0; index < wanted.length; index += 1) {
    const token = tokens[index];
    const want = wanted[index];
    if (token === want) continue;
    if (index === targetIndex) return classify(target, token);
    if (isOrthographic(token, want)) return result(ErrorClass.ORTHOGRAPHY);
    return result(ErrorClass.LEXICAL);
  }

  // The expected sentence is a strict prefix of what was typed.
  if (tokens.length > wanted.length) return result(ErrorClass.LEXICAL);
  return result(ErrorClass.CORRECT);
}

/* pl.grade.classify.classify: diagnose `submitted` against the cell `slot` needs. */
export function classify(slot, submitted) {
  const text = normalise(submitted);
  const expectedSurface = normalise(slot.expected.surface);
  const result = (errorClass, observed = null, differingAttr = null) =>
    new Diagnosis({ errorClass, submitted, slot, observed, differingAttr });

  // 1 — the expected surface, exactly.
  if (text === expectedSurface) return result(ErrorClass.CORRECT, slot.expected);

  // 2 — another form of the same lexeme, of the expected part of speech.
  const candidates = slot.withSurface(text).filter((f) => f.tag.pos === slot.tag.pos);
  if (candidates.length) {
    if (candidates.some((f) => f.tag.matches(slot.tag))) {
      return result(ErrorClass.CORRECT, candidates[0]);
    }
    const observed = bestCandidate(candidates, slot.tag);
    const attr = firstDifference(observed.tag, slot.tag);
    if (attr === "case" && isAnimacyError(slot, observed.tag)) {
      return result(ErrorClass.ANIMACY, observed, attr);
    }
    if (attr !== null) {
      return result(ATTR_TO_CLASS.get(attr) ?? ErrorClass.CASE_RIGHT_FORM_WRONG, observed, attr);
    }
    return result(ErrorClass.CASE_RIGHT_FORM_WRONG, observed);
  }

  // 3 — spelling, in the learner's typing direction only.
  if (isOrthographic(text, expectedSurface)) return result(ErrorClass.ORTHOGRAPHY);

  const known = morph.analyses(text);

  // 4 — not a word, but close enough to be an attempt at this cell.
  if (!known.length && distance(text, expectedSurface) <= MAX_ATTEMPT_DISTANCE) {
    return result(ErrorClass.CASE_RIGHT_FORM_WRONG);
  }

  // 5 — a form of some other lexeme.
  if (known.length) {
    if (slot.aspectPartner !== null) {
      const partner = slot.aspectPartner.split(":", 1)[0];
      if (known.some((f) => f.baseLemma === partner)) {
        return result(ErrorClass.ASPECT_WRONG, known[0], "aspect");
      }
    }
    return result(ErrorClass.LEXICAL, known[0]);
  }

  // 6 — not a word, and too far away to guess at.
  return result(ErrorClass.UNANALYSABLE);
}
