/* Unit tests for the ported grader: tags.js, domain.js, classify.js, explain.js
 * and morph.js. tests/test_parity.py holds them to the Python case by case;
 * these pin the Python string rules the port had to reproduce by hand.
 *
 *   node --test tests/js/
 */

import assert from "node:assert/strict";
import test from "node:test";

import { classify, classifySentence, normalise, tokenise } from "../../pl/static/course/classify.js";
import { ErrorClass, ExpectedSlot, Form, casefold } from "../../pl/static/course/domain.js";
import { explain } from "../../pl/static/course/explain.js";
import * as morph from "../../pl/static/course/morph.js";
import { UnknownTagError, parse } from "../../pl/static/course/tags.js";

const KAWA = [
  ["kawa", "subst:sg:nom:f"],
  ["kawy", "subst:sg:gen:f"],
  ["kawie", "subst:sg:dat.loc:f"],
  ["kawę", "subst:sg:acc:f"],
  ["kawą", "subst:sg:inst:f"],
  ["kawo", "subst:sg:voc:f"],
  ["kawy", "subst:pl:nom.acc.voc:f"],
].map(([surface, tag]) => new Form(surface, "kawa", parse(tag)));

const ACCUSATIVE = new ExpectedSlot(KAWA[3], KAWA);

morph.useLexicon({ kot: [["kot:Sm2", "subst:sg:nom:m2"]] });

test("each position of a tag is a set, so syncretic tags match by intersection", () => {
  const tag = parse("subst:sg:gen.acc:m2");
  assert.deepEqual([...tag.case], ["gen", "acc"]);
  assert.ok(tag.matches(parse("subst:sg:acc:m2")));
  assert.ok(!tag.matches(parse("subst:sg:nom:m2")));
  assert.deepEqual(tag.differingAttrs(parse("subst:pl:nom:m2")), ["number", "case"]);
});

test("an undeclared part of speech, or a wrong number of positions, raises", () => {
  assert.throws(() => parse("foo:sg"), UnknownTagError);
  assert.throws(() => parse("subst:sg"), UnknownTagError);
  assert.throws(() => parse("constructor"), UnknownTagError);
});

test("casefold is Python's str.casefold, not toLowerCase", () => {
  assert.equal(casefold("Straße"), "strasse");
  assert.equal(casefold("ΟΔΟΣ"), "οδοσ"); // toLowerCase would end in ς
  assert.equal(casefold("ŁÓDŹ"), "łódź");
  assert.equal(casefold("ﬁ"), "fi");
  assert.equal(casefold("Ꭰꭰ"), "ᎠᎠ"); // Cherokee folds to capitals
});

test("whitespace is what Python's str.split() splits on", () => {
  assert.equal(normalise(" Widzę\u00a0\x1cKOTA\u3000\x85"), "widzę kota");
  assert.equal(normalise("a\ufeffb"), "a\ufeffb"); // not whitespace to Python
});

test("tokens lose punctuation at their edges only", () => {
  assert.deepEqual(tokenise("„Widzę kota”, powiedział."), ["widzę", "kota", "powiedział"]);
  assert.deepEqual(tokenise("... — ..."), []);
  assert.deepEqual(tokenise("e-mail"), ["e-mail"]);
});

test("the six steps, on one paradigm", () => {
  const cases = [
    ["Kawę", ErrorClass.CORRECT],
    ["kawa", ErrorClass.CASE_WRONG],
    ["kawy", ErrorClass.NUMBER_WRONG], // the plural accusative beats the singular genitive
    ["kawe", ErrorClass.ORTHOGRAPHY],
    ["kawu", ErrorClass.CASE_RIGHT_FORM_WRONG],
    ["kot", ErrorClass.LEXICAL],
    ["xyzzyq", ErrorClass.UNANALYSABLE],
  ];
  for (const [submitted, expected] of cases) {
    assert.equal(classify(ACCUSATIVE, submitted).errorClass, expected, submitted);
  }
});

test("messages name the grammatical decision", () => {
  assert.equal(explain(classify(ACCUSATIVE, "kawa")), "You used the nominative. This slot needs the accusative: kawę.");
  assert.equal(explain(classify(ACCUSATIVE, "kot")), "That is a form of kot. This slot needs kawa: kawę.");
  assert.equal(explain(classify(ACCUSATIVE, "kawe")), "The grammar is right — check the spelling: kawę.");
});

test("a sentence is checked for gaps, then order, then word by word", () => {
  const sentence = (text) => classifySentence(["piję", "kawę"], 1, ACCUSATIVE, text).errorClass;
  assert.equal(sentence("Piję kawę."), ErrorClass.CORRECT);
  assert.equal(sentence("Piję."), ErrorClass.MISSING_CONSTITUENT);
  assert.equal(sentence("Kawę piję."), ErrorClass.WORD_ORDER);
  assert.equal(sentence("Pije kawę."), ErrorClass.ORTHOGRAPHY);
  assert.equal(sentence("Piję kawa."), ErrorClass.CASE_WRONG);
  assert.equal(sentence("Piję kawę dziś."), ErrorClass.LEXICAL);
});

test("the word list answers only for the words it holds, whatever their case", () => {
  assert.deepEqual(morph.analyses("KOT").map((f) => f.lemma), ["kot:Sm2"]);
  assert.deepEqual(morph.analyses("constructor"), []);
  assert.ok(!morph.isKnown("pies"));
});
