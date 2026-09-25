/* pl.concepts, ported: what the learner reads, and the gate that makes them
 * read it.
 *
 * A concept is the teaching unit, authored in data/concepts.yaml, which a phone
 * receives as JSON. Its tables' endings are read out of the stored paradigm,
 * never authored. Only the reading is state. The build-time checks —
 * `validate` and `reconcile` — run in the content build and are not ported.
 */

import { now } from "./clock.js";
import { isUnlocked } from "./composer.js";
import { AssertionError } from "./domain.js";
import { parse } from "./tags.js";

/* Python's `mapping.get(key, default)`: the default only when the key is absent. */
const get = (mapping, key, fallback) => (Object.hasOwn(mapping, key) ? mapping[key] : fallback);

let authored = null;

/* data/concepts.yaml, parsed. */
export function useConcepts(data) {
  authored = data;
}

function load() {
  if (authored === null) throw new Error("no concepts: call concepts.useConcepts first");
  return authored;
}

/* pl.concepts.cases: case metadata, by case. */
export function cases() {
  return load().cases;
}

/* pl.concepts.all_concepts: every concept, in the file's order. */
export function allConcepts() {
  return load().concepts;
}

/* pl.concepts.by_key */
export function byKey(key) {
  return allConcepts().find((c) => c.key === key) ?? null;
}

// ------------------------------------------------------------------- reading

/* pl.concepts.read_keys */
export function readKeys(db, userId) {
  return new Set(
    db.scalars("SELECT concept_read.concept_key FROM concept_read WHERE concept_read.user_id = ?", [userId])
  );
}

/* pl.concepts.mark_read: idempotent, and commits. */
export function markRead(db, userId, key) {
  if (db.get("concept_read", [userId, key]) !== null) return;
  db.insert("concept_read", { user_id: userId, concept_key: key, read_at: now() });
  db.commit();
}

/* pl.concepts.is_open: a concept opens when the node that introduces it does. */
export function isOpen(db, userId, concept) {
  const node = db.first("node", 'WHERE node."key" = ?', [concept.introduced_by]);
  return node !== null && isUnlocked(db, userId, node);
}

/* pl.concepts.gated_node_ids: nodes that may introduce nothing yet, because
 * their concept is unread. */
export function gatedNodeIds(db, userId) {
  const read = readKeys(db, userId);
  const gated = new Set();
  for (const concept of allConcepts()) {
    if (read.has(concept.key)) continue;
    const keys = [concept.introduced_by, ...get(concept, "also_taught_in", [])];
    for (const id of db.scalars(`SELECT node.id FROM node WHERE node."key" IN (${keys.map(() => "?").join(", ")})`, keys)) {
      gated.add(id);
    }
  }
  return gated;
}

/* pl.concepts.lesson_for: the first unread concept whose node is open, in node
 * order, rendered; or null. */
export function lessonFor(db, userId) {
  const read = readKeys(db, userId);
  const order = new Map(db.select("node", "ORDER BY node.id").map((node) => [node.key, node.id]));
  const candidates = allConcepts().filter((c) => !read.has(c.key) && isOpen(db, userId, c));
  candidates.sort((a, b) => (order.get(a.introduced_by) ?? 0) - (order.get(b.introduced_by) ?? 0));
  return candidates.length ? render(db, candidates[0]) : null;
}

// ------------------------------------------------------------------ rendering

/* pl.concepts._surfaces: every stored surface of the lexeme whose tag contains
 * the case, in the order the form index keeps them. */
function surfaces(db, lexeme, grammaticalCase, number) {
  const found = [];
  for (const form of db.select("form", "WHERE form.lexeme_id = ? ORDER BY form.surface, form.id", [lexeme.id])) {
    const tag = parse(form.morph_tag);
    if (tag.pos !== "subst") continue;
    if (tag.number.has(number) && tag.case.has(grammaticalCase) && !found.includes(form.surface)) {
      found.push(form.surface);
    }
  }
  return found;
}

/* pl.concepts.CASE_FIELDS */
const CASE_FIELDS = ["polish", "english", "questions", "gloss"];

/* pl.concepts._case: a case's row metadata, or an error naming what is missing. */
function caseEntry(grammaticalCase) {
  const entry = get(cases(), grammaticalCase, null);
  if (entry === null) throw new AssertionError(`case '${grammaticalCase}' is not in the cases map`);
  const missing = CASE_FIELDS.filter((field) => !entry[field]);
  if (missing.length) {
    throw new AssertionError(`case '${grammaticalCase}' in the cases map has no '${missing[0]}'`);
  }
  return entry;
}

/* pl.concepts.table: one declension table, read out of the paradigm. */
export function table(db, spec) {
  const lexeme = db.first("lexeme", "WHERE lexeme.lemma = ?", [spec.lexeme]);
  if (lexeme === null) throw new AssertionError(`table names lexeme '${spec.lexeme}', which is not in the set`);
  const number = get(spec, "number", "sg");
  const rows = [];
  for (const grammaticalCase of spec.cases) {
    const meta = caseEntry(grammaticalCase);
    const found = surfaces(db, lexeme, grammaticalCase, number);
    if (!found.length) {
      throw new AssertionError(
        `'${spec.lexeme}' has no ${number} '${grammaticalCase}' form, so the table in this concept cannot be built`
      );
    }
    rows.push({
      case: grammaticalCase,
      polish: meta.polish,
      english: meta.english,
      questions: meta.questions,
      gloss: meta.gloss,
      surfaces: found,
    });
  }
  return { lexeme: spec.lexeme, caption: get(spec, "caption", ""), rows };
}

/* pl.concepts.render: a concept with its tables resolved. */
export function render(db, concept) {
  return {
    key: concept.key,
    title: concept.title,
    summary: get(concept, "summary", ""),
    introduced_by: concept.introduced_by,
    sections: get(concept, "sections", []),
    tables: get(concept, "tables", []).map((spec) => table(db, spec)),
  };
}

/* pl.concepts.index: every concept and its state; one not reached carries its
 * title and nothing more. */
export function index(db, userId) {
  const read = readKeys(db, userId);
  return allConcepts().map((concept) => {
    const open = isOpen(db, userId, concept);
    return {
      key: concept.key,
      title: concept.title,
      introduced_by: concept.introduced_by,
      open,
      read: read.has(concept.key),
      summary: open ? concept.summary : "",
    };
  });
}
