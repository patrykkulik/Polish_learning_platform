/* pl.device, ported: the course on a phone — the page's API calls, answered
 * in-process, as the local server answers them.
 *
 * Everything a phone keeps is one SQLite database: the course's content and one
 * learner's progress. Content arrives as the published ledger and replaces the
 * content tables wholesale; the learner's tables are never read from it. The
 * page's device.js restores and saves the database's bytes; nothing here
 * touches the network or the browser's storage, so Node runs it all.
 */

import { knownZone } from "./clock.js";
import { useVoice } from "./composer.js";
import * as teaching from "./concepts.js";
import * as course from "./course.js";
import * as database from "./db.js";
import * as morph from "./morph.js";

/* pl.device.CONTENT_TABLES: replaced wholesale by each published ledger.
 * Queued answers live in `item_variant`, so a content update drops them. */
export const CONTENT_TABLES = [
  "lexeme",
  "form",
  "sense",
  "node",
  "node_prereq",
  "node_lexeme",
  "pattern",
  "item",
  "item_slot",
  "item_variant",
];

/* pl.device.LEARNER_TABLES: the learner's own rows, never read from a ledger. */
export const LEARNER_TABLES = [
  "app_user",
  "card",
  "node_unlock",
  "concept_read",
  "attempt",
  "review",
  "error_event",
  "streak",
];

/* The order a session is served in: shuffled, unless a test fixes it. */
function shuffle(items) {
  for (let i = items.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    [items[i], items[j]] = [items[j], items[i]];
  }
}

let order = shuffle;

export function useOrder(shuffler) {
  order = shuffler ?? shuffle;
}

let phoneDatabase = null;

function column(sql, text, params = []) {
  const [result] = sql.exec(text, params);
  return result ? result.values.map((row) => row[0]) : [];
}

function indexesOf(shipped, table) {
  return column(shipped, "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL", [
    table,
  ]);
}

function definitionOf(shipped, table) {
  const [definition] = column(shipped, "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", [table]);
  if (definition === undefined) throw new Error(`the published ledger has no '${table}' table`);
  return definition;
}

/* pl.device._replace: one content table and its indexes, dropped and recreated
 * from the ledger's own definitions, its rows copied with their ids. */
function replace(target, shipped, table) {
  target.exec(`DROP TABLE IF EXISTS main."${table}"`);
  target.exec(definitionOf(shipped, table));
  const [rows] = shipped.exec(`SELECT * FROM "${table}"`);
  if (rows) {
    const insert = target.prepare(`INSERT INTO main."${table}" VALUES (${rows.columns.map(() => "?").join(", ")})`);
    try {
      for (const values of rows.values) insert.run(values);
    } finally {
      insert.free();
    }
  }
  for (const sql of indexesOf(shipped, table)) target.exec(sql);
}

/* pl.device.install: replace the content tables with the ledger's, in one
 * transaction. Learner rows point at content by id, and the deploy's
 * `check-ids` guarantees a published id never names a different row. */
export function install(target, shipped) {
  target.exec("BEGIN");
  try {
    for (const table of CONTENT_TABLES) replace(target, shipped, table);
    target.exec("COMMIT");
  } catch (error) {
    target.exec("ROLLBACK");
    throw error;
  }
}

/* pl.db.create_all, on a phone: each learner table created from the ledger's
 * definitions if missing, or given the nullable columns it lacks. The ledger is
 * built by the Python models, so they stay the one schema. */
export function createLearnerTables(target, shipped) {
  target.exec("BEGIN");
  try {
    for (const table of LEARNER_TABLES) {
      if (!column(target, "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", [table]).length) {
        target.exec(definitionOf(shipped, table));
        for (const sql of indexesOf(shipped, table)) target.exec(sql);
        continue;
      }
      const [present] = target.exec(`PRAGMA table_info("${table}")`);
      const have = new Set(present.values.map((row) => row[1]));
      const [shippedColumns] = shipped.exec(`PRAGMA table_info("${table}")`);
      for (const [, name, type, notNull] of shippedColumns.values) {
        if (have.has(name)) continue;
        if (notNull) {
          throw new Error(
            `${table}.${name} is missing from this phone's database and is NOT NULL, so it cannot be added ` +
              "without deciding what existing rows should hold. This needs a migration."
          );
        }
        target.exec(`ALTER TABLE "${table}" ADD COLUMN "${name}" ${type}`);
      }
    }
    target.exec("COMMIT");
  } catch (error) {
    target.exec("ROLLBACK");
    throw error;
  }
}

/* pl.device._ensure_learner: this phone's one learner, in the phone's own
 * timezone, or UTC for a zone the browser cannot name. */
function ensureLearner(zone) {
  const known = knownZone(zone) ? zone : "UTC";
  const db = database.session();
  try {
    if (db.first("app_user", "LIMIT 1") === null) {
      const user = course.ensureUser(db);
      user.settings_json = { ...user.settings_json, tz: known };
      db.update("app_user", user.id, { settings_json: user.settings_json });
      db.commit();
    }
  } finally {
    db.close();
  }
}

/* pl.device.start: make the phone's database ready before any request.
 *
 * `ledger` is the published content file's bytes when the page fetched it —
 * it does not when `installedHash` already names `contentHash`. Install and
 * update are the same step. Returns the hash now installed, which device.js
 * saves with the database. */
export function start({ SQL, database: sql, ledger, contentHash, installedHash, lexicon, concepts, zone, voice = false }) {
  morph.useLexicon(lexicon);
  teaching.useConcepts(concepts);
  useVoice(voice);
  database.useDatabase(sql);
  phoneDatabase = sql;

  const content = ledger !== null && installedHash !== contentHash ? ledger : null;
  if (content !== null) {
    // A copy: sql.js keeps the array it is given as the database file.
    const shipped = new SQL.Database(new Uint8Array(content));
    try {
      install(sql, shipped);
      createLearnerTables(sql, shipped);
    } finally {
      shipped.close();
    }
  }
  ensureLearner(zone);
  return content !== null ? contentHash : installedHash;
}

/* A request the page could not have meant: FastAPI answers these 422. */
class Invalid extends Error {}

const REASONS = { 404: "Not Found", 405: "Method Not Allowed" };

/* pl.device._TRUE and _FALSE: what FastAPI reads as a boolean. */
const TRUE = new Set(["1", "on", "t", "true", "y", "yes"]);
const FALSE = new Set(["0", "off", "f", "false", "n", "no"]);

/* Python's `int(text)`, for the digits a query or a body carries. */
function toInteger(text) {
  const match = /^\s*([+-]?)(\d+(?:_\d+)*)\s*$/.exec(text);
  return match ? Number(match[1] + match[2].replaceAll("_", "")) : null;
}

/* pl.device._int */
function intParam(query, name, fallback) {
  if (!Object.hasOwn(query, name)) return fallback;
  const value = toInteger(query[name]);
  if (value === null) throw new Invalid(`${name} must be an integer`);
  return value;
}

/* pl.device._path_int */
function pathInt(text, name) {
  const value = toInteger(text);
  if (value === null) throw new Invalid(`${name} must be an integer`);
  return value;
}

/* pl.device._bool */
function boolParam(query, name, fallback) {
  if (!Object.hasOwn(query, name)) return fallback;
  const value = query[name].toLowerCase();
  if (TRUE.has(value)) return true;
  if (FALSE.has(value)) return false;
  throw new Invalid(`${name} must be a boolean`);
}

/* pl.device._as_int: an integer as pydantic's lax mode reads one — never a boolean. */
function asInt(value) {
  if (typeof value === "boolean") return null;
  if (typeof value === "number") return Number.isInteger(value) ? value : null;
  if (typeof value === "string") return toInteger(value.trim());
  return null;
}

/* pl.device._submission: what pl.api.Submission accepts. */
function submission(body) {
  let payload;
  try {
    payload = JSON.parse(body ?? "");
  } catch {
    throw new Invalid("the body is not JSON");
  }
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Invalid("the body must be an object");
  }
  const itemId = asInt(payload.item_id);
  const answer = payload.answer;
  let latency = Object.hasOwn(payload, "latency_ms") ? payload.latency_ms : null;
  if (itemId === null) throw new Invalid("item_id must be an integer");
  if (typeof answer !== "string") throw new Invalid("answer must be a string");
  if (latency !== null) {
    latency = asInt(latency);
    if (latency === null) throw new Invalid("latency_ms must be an integer");
  }
  return [itemId, answer, latency];
}

const same = (segments, pattern) =>
  segments.length === pattern.length && pattern.every((part, i) => part === null || part === segments[i]);

/* pl.device._route: a call to make, or the status when nothing here answers —
 * 404 for no such path, 405 for a known path asked with the wrong method. */
function route(method, segments, query, body) {
  let wanted;
  let call;
  if (same(segments, ["api", "session"])) {
    wanted = "GET";
    call = () => course.getSession(intParam(query, "limit", 20), boolParam(query, "extra", false), { order });
  } else if (same(segments, ["api", "submit"])) {
    wanted = "POST";
    call = () => course.submit(...submission(body));
  } else if (same(segments, ["api", "session", "complete"])) {
    wanted = "POST";
    call = course.complete;
  } else if (same(segments, ["api", "graph"])) {
    wanted = "GET";
    call = course.graph;
  } else if (same(segments, ["api", "concepts"])) {
    wanted = "GET";
    call = course.listConcepts;
  } else if (same(segments, ["api", "concepts", null])) {
    wanted = "GET";
    call = () => course.getConcept(segments[2]);
  } else if (same(segments, ["api", "concepts", null, "read"])) {
    wanted = "POST";
    call = () => course.readConcept(segments[2]);
  } else if (same(segments, ["api", "audio", null])) {
    wanted = "GET";
    call = () => course.speechText(pathInt(segments[2], "item_id"));
  } else if (same(segments, ["api", "audio", null, "option", null])) {
    wanted = "GET";
    call = () => course.speechText(pathInt(segments[2], "item_id"), pathInt(segments[4], "index"));
  } else {
    return 404;
  }
  return method === wanted ? call : 405;
}

/* Python's `unquote`: percent-decoding that leaves a malformed escape as it is. */
function unquote(segment) {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

function totalChanges() {
  return column(phoneDatabase, "SELECT total_changes()")[0];
}

/* pl.device.handle: answer one of the page's API calls. `changed` says whether
 * the request wrote anything, rolled back or not, so device.js saves only then. */
export function handle(method, url, body = null) {
  const before = totalChanges();
  let status;
  let answer;
  try {
    const parsed = new URL(url, "http://phone.invalid/");
    const segments = parsed.pathname.replace(/^\/+|\/+$/g, "").split("/").map(unquote);
    // parse_qs drops blank values, and the last of a repeated name wins.
    const query = {};
    for (const [name, value] of parsed.searchParams) if (value !== "") query[name] = value;
    const answering = route(method.toUpperCase(), segments, query, body);
    if (typeof answering === "number") {
      status = answering;
      answer = { detail: REASONS[answering] };
    } else {
      status = 200;
      answer = answering();
    }
  } catch (error) {
    if (error instanceof course.Refused) {
      status = error.status;
      answer = { detail: error.detail };
    } else if (error instanceof Invalid) {
      status = 422;
      answer = { detail: error.message };
    } else {
      // Logged so Safari's Web Inspector shows where; the page shows a message.
      console.error(error);
      status = 500;
      answer = { detail: `The app hit an error on this phone (${error.name}).` };
    }
  }
  return { status, body: JSON.stringify(answer), changed: totalChanges() !== before };
}
