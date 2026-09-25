/* pl.tags, ported: Morfeusz tag strings to structured, comparable feature records.
 *
 * A Morfeusz tag is colon-separated, and every position is a dot-separated set
 * of values: `subst:sg:gen.acc:m1` is one interpretation covering genitive and
 * accusative. So an expected accusative matches `kota` only under set
 * intersection, never under equality. The Python module's docstring has the
 * full reasoning; this port changes none of it.
 */

/* pl.tags._SCHEMA: part of speech -> [required attribute names, optional
 * trailing attribute names]. A Map, so a part of speech can never collide with
 * an Object.prototype name. */
const SCHEMA = new Map([
  // nominal
  ["subst", [["number", "case", "gender"], ["collectivity"]]],
  ["depr", [["number", "case", "gender"], []]],
  ["adj", [["number", "case", "gender", "degree"], []]],
  ["adja", [[], []]],
  ["adjp", [["case"], []]],
  ["adv", [[], ["degree"]]],
  ["num", [["number", "case", "gender", "accommodability"], ["collectivity"]]],
  ["numcol", [["number", "case", "gender", "accommodability"], []]],
  // pronominal
  ["ppron12", [["number", "case", "gender", "person"], ["accentability"]]],
  ["ppron3", [["number", "case", "gender", "person"], ["accentability", "post_prepositionality"]]],
  ["siebie", [["case"], []]],
  // verbal
  ["fin", [["number", "person", "aspect"], []]],
  ["bedzie", [["number", "person", "aspect"], []]],
  ["impt", [["number", "person", "aspect"], []]],
  ["aglt", [["number", "person", "aspect", "vocalicity"], []]],
  ["praet", [["number", "gender", "aspect"], ["agglutination"]]],
  ["winien", [["number", "gender", "aspect"], []]],
  ["inf", [["aspect"], []]],
  ["imps", [["aspect"], []]],
  ["pcon", [["aspect"], []]],
  ["pant", [["aspect"], []]],
  ["ger", [["number", "case", "gender", "aspect", "negation"], []]],
  ["pact", [["number", "case", "gender", "aspect", "negation"], []]],
  ["ppas", [["number", "case", "gender", "aspect", "negation"], []]],
  ["pacta", [[], []]],
  ["pred", [[], []]],
  // closed class and non-words
  ["prep", [["case"], ["vocalicity"]]],
  ["conj", [[], []]],
  ["comp", [[], []]],
  ["qub", [[], []]],
  ["part", [[], ["vocalicity"]]],
  ["interj", [[], []]],
  ["burk", [[], []]],
  ["brev", [["punctuation"], []]],
  ["interp", [[], []]],
  ["xxx", [[], []]],
  ["romandig", [[], []]],
  ["frag", [[], []]],
  ["ign", [[], []]],
]);

/* pl.tags.IGN: the tag Morfeusz gives a string it cannot analyse. */
export const IGN = "ign";

/* pl.tags.ANIMATE_MASCULINE and INANIMATE_MASCULINE. */
export const ANIMATE_MASCULINE = new Set(["m1", "m2"]);
export const INANIMATE_MASCULINE = "m3";

/* pl.tags.UnknownTagError */
export class UnknownTagError extends Error {
  constructor(message) {
    super(message);
    this.name = "UnknownTagError";
  }
}

/* What `values` returns for an attribute the tag omits. Never mutated. */
const NONE = new Set();

/* pl.tags.MorphTag: one interpretation, each attribute held as a value set.
 * `features` is a list of [attribute, Set of values] pairs in tag order. */
export class MorphTag {
  constructor(pos, features = [], raw = "") {
    this.pos = pos;
    this.features = features;
    this.raw = raw;
  }

  /* MorphTag.values */
  values(attr) {
    for (const [name, vals] of this.features) {
      if (name === attr) return vals;
    }
    return NONE;
  }

  /* MorphTag.has */
  has(attr, value) {
    return this.values(attr).has(value);
  }

  /* MorphTag.agrees_on: true when both declare `attr` and the sets intersect.
   * A tag that omits the attribute agrees vacuously. */
  agreesOn(attr, other) {
    const mine = this.values(attr);
    const theirs = other.values(attr);
    if (!mine.size || !theirs.size) return true;
    for (const value of mine) {
      if (theirs.has(value)) return true;
    }
    return false;
  }

  /* MorphTag.matches: same part of speech, and every shared attribute intersects. */
  matches(other) {
    if (this.pos !== other.pos) return false;
    const theirs = new Set(other.features.map(([name]) => name));
    return this.features.every(([name]) => !theirs.has(name) || this.agreesOn(name, other));
  }

  /* MorphTag.differing_attrs: shared attributes that do not intersect, in tag order. */
  differingAttrs(other) {
    const theirs = new Set(other.features.map(([name]) => name));
    return this.features
      .map(([name]) => name)
      .filter((name) => theirs.has(name) && !this.agreesOn(name, other));
  }

  /* MorphTag.case, .number and .gender */
  get case() {
    return this.values("case");
  }

  get number() {
    return this.values("number");
  }

  get gender() {
    return this.values("gender");
  }

  /* MorphTag.is_unknown */
  get isUnknown() {
    return this.pos === IGN;
  }

  /* MorphTag.__str__ */
  toString() {
    return this.raw || this.pos;
  }
}

/* pl.tags.parse: raises when the part of speech is undeclared, or the tag has
 * more or fewer positions than its schema allows. */
export function parse(tag) {
  const parts = tag.split(":");
  const pos = parts[0];
  const schema = SCHEMA.get(pos);
  if (schema === undefined) {
    throw new UnknownTagError(
      `unknown part of speech '${pos}' in tag '${tag}'; ` +
        "add it to pl.tags._SCHEMA once its positions are verified"
    );
  }
  const [required, optional] = schema;
  const attrs = parts.slice(1);
  if (!(required.length <= attrs.length && attrs.length <= required.length + optional.length)) {
    const expected = optional.length
      ? `${required.length}-${required.length + optional.length}`
      : `${required.length}`;
    throw new UnknownTagError(
      `tag '${tag}' has ${attrs.length} attribute position(s); schema for '${pos}' expects ${expected}`
    );
  }
  const names = [...required, ...optional];
  const features = attrs.map((value, i) => [names[i], new Set(value.split("."))]);
  return new MorphTag(pos, features, tag);
}
