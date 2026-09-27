/* Unit tests for phone.js and db.js, on the committed ledger. The journey in
 * tests/test_parity.py holds every response and learner row to the Python;
 * these pin what the Python has no counterpart for: the `changed` flag that
 * device.js saves on, and the phone's own start-up.
 *
 *   node --test tests/js/
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";

import * as phone from "../../pl/static/course/phone.js";

const ROOT = new URL("../../", import.meta.url).pathname;
const VENDOR = `${ROOT}pl/static/vendor/`;
const SQL = await createRequire(import.meta.url)(`${VENDOR}sql-wasm.js`)({ locateFile: (f) => VENDOR + f });
const LEDGER = readFileSync(`${ROOT}publish/content.db`);
const LEXICON = JSON.parse(readFileSync(`${ROOT}publish/lexicon.json`, "utf8"));
const NO_LESSONS = { cases: {}, concepts: [] };

function newPhone(zone = "Europe/Warsaw", voice = false) {
  const sql = new SQL.Database();
  const installed = phone.start({
    SQL,
    database: sql,
    ledger: LEDGER,
    contentHash: "first",
    installedHash: null,
    lexicon: LEXICON,
    concepts: NO_LESSONS,
    zone,
    voice,
  });
  return { sql, installed };
}

const one = (sql, text) => sql.exec(text)[0].values[0][0];

test("a new phone installs the content and creates one learner in its zone", () => {
  const { sql, installed } = newPhone();
  assert.equal(installed, "first");
  assert.ok(one(sql, "SELECT count(*) FROM item") > 1000);
  assert.equal(one(sql, "SELECT count(*) FROM app_user"), 1);
  assert.equal(JSON.parse(one(sql, "SELECT settings_json FROM app_user")).tz, "Europe/Warsaw");
  assert.equal(one(sql, "SELECT freezes FROM streak"), 2);
});

test("a zone the browser cannot name falls back to UTC", () => {
  const { sql } = newPhone("Mars/Olympus_Mons");
  assert.equal(JSON.parse(one(sql, "SELECT settings_json FROM app_user")).tz, "UTC");
});

test("a read leaves the database unchanged, so nothing is saved", () => {
  newPhone();
  for (const url of ["api/session?limit=20", "api/graph", "api/concepts"]) {
    const answer = phone.handle("GET", url);
    assert.equal(answer.status, 200, url);
    assert.equal(answer.changed, false, url);
  }
});

test("an answer changes the database, so it is saved", () => {
  const { sql } = newPhone();
  const session = JSON.parse(phone.handle("GET", "api/session?limit=20").body);
  const item = session.items[0];
  const expected = sql.exec("SELECT expected_answer FROM item WHERE id = ?", [item.id])[0].values[0][0];
  const answer = phone.handle("POST", "api/submit", JSON.stringify({ item_id: item.id, answer: expected }));
  assert.equal(answer.status, 200);
  assert.equal(answer.changed, true);
  assert.equal(JSON.parse(answer.body).correct, true);
  assert.equal(one(sql, "SELECT count(*) FROM attempt"), 1);
});

test("a refused answer writes nothing the phone keeps", () => {
  const { sql } = newPhone();
  const answer = phone.handle("POST", "api/submit", JSON.stringify({ item_id: 999999, answer: "kot" }));
  assert.equal(answer.status, 404);
  assert.equal(JSON.parse(answer.body).detail, "no such item");
  assert.equal(one(sql, "SELECT count(*) FROM attempt"), 0);
});

test("the same content again installs nothing, and a new one keeps the learner's rows", () => {
  const { sql } = newPhone();
  const session = JSON.parse(phone.handle("GET", "api/session?limit=20").body);
  phone.handle("POST", "api/submit", JSON.stringify({ item_id: session.items[0].id, answer: "x" }));
  sql.exec("DELETE FROM item WHERE id = (SELECT max(id) FROM item)");
  const items = one(sql, "SELECT count(*) FROM item");

  const again = { SQL, database: sql, ledger: LEDGER, lexicon: LEXICON, concepts: NO_LESSONS, zone: "UTC" };
  assert.equal(phone.start({ ...again, contentHash: "first", installedHash: "first" }), "first");
  assert.equal(one(sql, "SELECT count(*) FROM item"), items);

  assert.equal(phone.start({ ...again, contentHash: "second", installedHash: "first" }), "second");
  assert.equal(one(sql, "SELECT count(*) FROM item"), items + 1);
  assert.equal(one(sql, "SELECT count(*) FROM attempt"), 1);
  assert.equal(one(sql, "SELECT count(*) FROM app_user"), 1);
});

/* The committed ledger as newer models would build it: one statement more. */
function ledgerWith(statement) {
  const shipped = new SQL.Database(new Uint8Array(LEDGER));
  try {
    shipped.exec(statement);
    return shipped.export();
  } finally {
    shipped.close();
  }
}

const columnsOf = (sql, table) => sql.exec(`PRAGMA table_info("${table}")`)[0].values.map((row) => row[1]);

/* A phone with one answer behind it: at least one card and one attempt. */
function phoneWithProgress() {
  const { sql } = newPhone();
  const session = JSON.parse(phone.handle("GET", "api/session?limit=20").body);
  phone.handle("POST", "api/submit", JSON.stringify({ item_id: session.items[0].id, answer: "x" }));
  const cards = sql.exec("SELECT * FROM card ORDER BY id")[0].values;
  assert.ok(cards.length);
  return { sql, cards };
}

test("a content update gives a learner table its new nullable column, and keeps its rows", () => {
  const { sql, cards } = phoneWithProgress();
  const newer = ledgerWith("ALTER TABLE card ADD COLUMN note TEXT");

  const update = { SQL, database: sql, ledger: newer, lexicon: LEXICON, concepts: NO_LESSONS, zone: "UTC" };
  assert.equal(phone.start({ ...update, contentHash: "second", installedHash: "first" }), "second");

  assert.equal(columnsOf(sql, "card").at(-1), "note");
  const kept = sql.exec("SELECT * FROM card ORDER BY id")[0].values;
  assert.deepEqual(kept.map((row) => row.slice(0, -1)), cards);
  assert.ok(kept.every((row) => row.at(-1) === null));
  assert.equal(one(sql, "SELECT count(*) FROM attempt"), 1);
  assert.equal(phone.handle("GET", "api/session?limit=20").status, 200);
});

test("a content update refuses a NOT NULL learner column, and changes no learner table", () => {
  const { sql, cards } = phoneWithProgress();
  const columns = columnsOf(sql, "card");
  const newer = ledgerWith("ALTER TABLE card ADD COLUMN level INTEGER NOT NULL DEFAULT 0");

  const update = { SQL, database: sql, ledger: newer, lexicon: LEXICON, concepts: NO_LESSONS, zone: "UTC" };
  assert.throws(() => phone.start({ ...update, contentHash: "second", installedHash: "first" }), /needs a migration/);

  assert.deepEqual(columnsOf(sql, "card"), columns);
  assert.deepEqual(sql.exec("SELECT * FROM card ORDER BY id")[0].values, cards);
  assert.equal(one(sql, "SELECT count(*) FROM attempt"), 1);
});

test("a phone with a voice says a listening item's sentence and an option by position, nothing else", () => {
  const { sql } = newPhone("UTC", true);
  const [listening, sentence] = sql.exec(
    "SELECT id, expected_answer FROM item WHERE exercise_type = 'listening_dictation' ORDER BY id LIMIT 1"
  )[0].values[0];
  const [choice, options] = sql.exec("SELECT id, options_json FROM item WHERE exercise_type = 'mcq' ORDER BY id LIMIT 1")[0]
    .values[0];
  const say = (url) => {
    const answer = phone.handle("GET", url);
    return [answer.status, JSON.parse(answer.body)];
  };
  assert.deepEqual(say(`api/audio/${listening}?speed=slow`), [200, { text: sentence }]);
  assert.deepEqual(say(`api/audio/${choice}/option/1`), [200, { text: JSON.parse(options)[1] }]);
  assert.deepEqual(say(`api/audio/${choice}`), [404, { detail: "no audio for this item" }]);
  assert.deepEqual(say(`api/audio/${listening}/option/0`), [404, { detail: "no such option" }]);
  assert.equal(say("api/audio/one")[0], 422);
  assert.equal(phone.handle("GET", `api/audio/${listening}`).changed, false);
});

test("a phone without a voice offers no sound and no dictation", () => {
  newPhone("UTC", false);
  const items = JSON.parse(phone.handle("GET", "api/session?limit=20").body).items;
  assert.ok(items.length);
  assert.ok(items.every((item) => !item.has_audio && !item.options_audio));
  assert.deepEqual(JSON.parse(phone.handle("GET", "api/audio/1").body), {
    detail: "no speech synthesiser on this machine",
  });
  newPhone("UTC", true);
  const voiced = JSON.parse(phone.handle("GET", "api/session?limit=20").body).items;
  assert.ok(voiced.some((item) => item.options_audio));
});

test("what the phone does not know it answers as the server does", () => {
  newPhone();
  const cases = [
    ["GET", "api/nothing", null, 404, "Not Found"],
    ["DELETE", "api/graph", null, 405, "Method Not Allowed"],
    ["GET", "api/session?limit=many", null, 422, "limit must be an integer"],
    ["POST", "api/submit", "{", 422, "the body is not JSON"],
    ["POST", "api/submit", '{"item_id": true, "answer": "x"}', 422, "item_id must be an integer"],
  ];
  for (const [method, url, body, status, detail] of cases) {
    const answer = phone.handle(method, url, body);
    assert.equal(answer.status, status, url);
    assert.equal(JSON.parse(answer.body).detail, detail, url);
    assert.equal(answer.changed, false, url);
  }
});
