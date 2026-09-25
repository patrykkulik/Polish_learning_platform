/* The JavaScript half of the parity tests in tests/test_parity.py.
 *
 * Reads one job as JSON on stdin, runs it through the ported modules in
 * pl/static/course/, and writes what they produced as JSON on stdout. pytest
 * runs the same job through the Python and compares the two.
 *
 *   node tests/js/replay.mjs < job.json
 */

import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

import { classify, classifySentence } from "../../pl/static/course/classify.js";
import { fromisoformat, toDb, useClock } from "../../pl/static/course/clock.js";
import * as composer from "../../pl/static/course/composer.js";
import { ExpectedSlot, Form } from "../../pl/static/course/domain.js";
import { explain } from "../../pl/static/course/explain.js";
import { Card, Scheduler } from "../../pl/static/course/fsrs.js";
import * as morph from "../../pl/static/course/morph.js";
import * as phone from "../../pl/static/course/phone.js";
import { SCHEDULER } from "../../pl/static/course/schedule.js";
import { parse } from "../../pl/static/course/tags.js";

const VENDOR = new URL("../../pl/static/vendor/", import.meta.url).pathname;

/* A form as the job carries it: [surface, lemma, tag]. */
const form = ([surface, lemma, tag]) => new Form(surface, lemma, parse(tag));

/* Grading with the phone's word list: each case's diagnosis and message, and
 * the analyses each of `words` gets. */
function grade({ lexicon, cases, words }) {
  morph.useLexicon(JSON.parse(readFileSync(lexicon, "utf8")));
  return {
    cases: cases.map(({ id, slot, submitted, sentence }) => {
      const expected = new ExpectedSlot(form(slot.expected), slot.paradigm.map(form), slot.aspect_partner);
      const diagnosis = sentence
        ? classifySentence(sentence.expected, sentence.target_index, expected, submitted)
        : classify(expected, submitted);
      const observed = diagnosis.observed;
      return {
        id,
        error_class: diagnosis.errorClass,
        message: explain(diagnosis),
        observed: observed && [observed.surface, observed.lemma, observed.tag.raw],
        differing_attr: diagnosis.differingAttr,
      };
    }),
    words: Object.fromEntries(words.map((word) => [word, morph.analyses(word).map((f) => [f.lemma, f.tag.raw])])),
  };
}

/* Scripted review sequences: the card after every review, and its
 * retrievability at each probe offset (microseconds) from that review. Fuzz
 * draws come from the job, in order. */
function fsrs({ sequences }) {
  return {
    sequences: sequences.map(({ id, scheduler, draws = [], card, reviews, probes }) => {
      const pending = [...draws];
      const options = {
        enableFuzzing: scheduler.enable_fuzzing,
        random: () => {
          if (!pending.length) throw new Error(`sequence ${id} ran out of scripted draws`);
          return pending.shift();
        },
      };
      if (scheduler.parameters) options.parameters = scheduler.parameters;
      const s = new Scheduler(options);
      let current = Card.fromDict(card);
      const steps = reviews.map(({ rating, at }) => {
        const reviewed = fromisoformat(at);
        current = s.reviewCard(current, rating, reviewed);
        return {
          card: current.toDict(),
          retrievability: probes.map((offset) => s.getCardRetrievability(current, reviewed + offset)),
        };
      });
      return { id, steps, draws_left: pending.length };
    }),
  };
}

/* Each learner table, and the queued answers, in primary-key order: what a
 * journey leaves behind. */
const ROWS = {
  app_user: "id",
  card: "id",
  node_unlock: "user_id, node_id",
  concept_read: "user_id, concept_key",
  attempt: "id",
  review: "id",
  error_event: "id",
  streak: "user_id",
  item_variant: "id",
};

/* A recorded journey, replayed on a fresh phone: each request at the instant
 * the Python answered it, FSRS fuzz off and the session unshuffled, as the
 * Python ran. Events either start the phone with a ledger, run SQL against its
 * database outside any request, or make a request. */
async function journey({ ledgers, lexicon, concepts, voice, events }) {
  const SQL = await createRequire(import.meta.url)(`${VENDOR}sql-wasm.js`)({ locateFile: (f) => VENDOR + f });
  const sql = new SQL.Database();
  let at = 0;
  useClock(() => at);
  SCHEDULER.enableFuzzing = false;
  phone.useOrder(() => {});
  const words = JSON.parse(readFileSync(lexicon, "utf8"));
  const authored = JSON.parse(readFileSync(concepts, "utf8"));

  let installed = null;
  const responses = [];
  for (const event of events) {
    at = fromisoformat(event.at);
    if (event.start) {
      installed = phone.start({
        SQL,
        database: sql,
        ledger: readFileSync(ledgers[event.start.ledger]),
        contentHash: event.start.hash,
        installedHash: installed,
        lexicon: words,
        concepts: authored,
        zone: event.start.zone,
        voice,
      });
    } else if (event.sql) {
      sql.exec(event.sql);
    } else {
      const [method, url, body] = event.request;
      const answer = phone.handle(method, url, body);
      responses.push({ status: answer.status, body: JSON.parse(answer.body) });
    }
  }

  const rows = {};
  for (const [table, key] of Object.entries(ROWS)) {
    const [result] = sql.exec(`SELECT * FROM "${table}" ORDER BY ${key}`);
    rows[table] = result ? result.values : [];
  }
  return { responses, rows };
}

/* The learner's day, around each instant, in each zone: today's date, and the
 * midnights that start and end it, as DateTime column text. */
function days({ cases }) {
  return {
    days: cases.map(({ zone, at }) => {
      useClock(() => fromisoformat(at));
      const settings = { tz: zone };
      return {
        zone,
        at,
        today: composer.userToday(settings),
        start: toDb(composer.startOfUserDay(settings)),
        end: toDb(composer.endOfUserDay(settings)),
      };
    }),
  };
}

const JOBS = { grade, fsrs, journey, days };

const job = JSON.parse(readFileSync(0, "utf8"));
const run = JOBS[job.kind];
if (run === undefined) throw new Error(`unknown job kind: ${job.kind}`);
process.stdout.write(JSON.stringify(await run(job)));
