/* pl.morph's word-list backend, ported. On a phone this is all of `pl.morph`.
 *
 * Morfeusz is a native library with no WebAssembly build, so a phone answers
 * `analyse` from the word list the content build made: every word the course
 * contains, mapped to Morfeusz's own analyses in Morfeusz's order. A word the
 * list holds gets exactly those readings; any other word gets none, and reads as
 * not a Polish word. `forms` and the rest of `pl.morph` run only in the build.
 */

import { Form, casefold } from "./domain.js";
import { parse } from "./tags.js";

/* surface -> [[lemma, tag], ...]. A Map, so a learner who types "constructor"
 * reaches no Object.prototype property. */
let lexicon = null;

/* pl.morph.use_lexicon: `table` is the parsed `lexicon.json`; null forgets it. */
export function useLexicon(table) {
  lexicon = table === null ? null : new Map(Object.entries(table));
}

/* pl.morph.analyse, with a word list in use: one edge per reading, each spanning
 * the whole text. */
export function analyse(text) {
  if (lexicon === null) throw new Error("no word list: call morph.useLexicon first");
  return (lexicon.get(casefold(text)) ?? []).map(([lemma, tag]) => ({
    start: 0,
    end: 1,
    form: new Form(text, lemma, parse(tag)),
  }));
}

/* pl.morph.analyses: every reading of one token, `ign` excluded. Empty means the
 * token is not a Polish word. */
export function analyses(token) {
  return analyse(token)
    .map((edge) => edge.form)
    .filter((form) => !form.tag.isUnknown);
}

/* pl.morph.is_known */
export function isKnown(token) {
  return analyses(token).length > 0;
}
