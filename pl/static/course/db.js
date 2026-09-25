/* The database, standing in for pl.db and SQLAlchemy's session: one sql.js
 * database, and a session per request that commits exactly where the Python
 * commits and rolls back everything else when it closes.
 *
 * Writes reach the database as they are made. SQLAlchemy holds them until a
 * flush, but it flushes before every query, and nothing in the Python turns
 * that off, so each query here sees what the Python's would, and rows get the
 * ids they would.
 *
 * Rows come back as plain objects keyed by column name, typed as the models
 * declare: DateTime as clock.js instants, Date as "YYYY-MM-DD", JSON parsed and
 * Boolean as true or false. A row object is a copy: code that changes a row
 * both writes it (`update`) and sets the field, as the Python sets an
 * attribute the session later flushes.
 */

import { fromDb, toDb } from "./clock.js";

/* Columns that are not stored as they are used. */
const TYPES = {
  app_user: { created_at: "datetime", settings_json: "json" },
  attempt: { created_at: "datetime" },
  card: { fsrs_state_json: "json", due_at: "datetime", created_at: "datetime" },
  concept_read: { read_at: "datetime" },
  node_unlock: { unlocked_at: "datetime" },
  streak: { last_completed_on: "date", absence_settled_on: "date" },
  form: { is_irregular: "bool" },
  item: { options_json: "json" },
};

/* Primary keys other than `id`. */
const KEYS = {
  concept_read: ["user_id", "concept_key"],
  node_lexeme: ["node_id", "lexeme_id"],
  node_prereq: ["node_id", "prereq_node_id"],
  node_unlock: ["user_id", "node_id"],
  streak: ["user_id"],
};

function fromColumn(type, value) {
  if (value === null || value === undefined) return null;
  if (type === "datetime") return fromDb(value);
  if (type === "json") return JSON.parse(value);
  if (type === "bool") return Boolean(value);
  return value;
}

function toColumn(type, value) {
  if (value === null || value === undefined) return null;
  if (type === "datetime") return toDb(value);
  if (type === "json") return JSON.stringify(value);
  if (type === "bool") return value ? 1 : 0;
  return value;
}

let database = null;

/* The sql.js database every session opens on. */
export function useDatabase(sqlDatabase) {
  database = sqlDatabase;
}

/* pl.db.session */
export function session() {
  if (database === null) throw new Error("no database: call db.useDatabase first");
  return new Session(database);
}

class Session {
  constructor(sql) {
    this.sql = sql;
    // Prepared once per request and freed when it closes: sql.js's export(),
    // which saving runs, frees every statement.
    this.statements = new Map();
    this.sql.exec("BEGIN");
    this.open = true;
  }

  _each(text, params, visit) {
    let statement = this.statements.get(text);
    if (statement === undefined) {
      statement = this.sql.prepare(text);
      this.statements.set(text, statement);
    }
    try {
      statement.bind(params);
      while (statement.step()) visit(statement);
    } finally {
      statement.reset();
    }
  }

  /* Every row, as an array of raw values. */
  values(text, params = []) {
    const out = [];
    this._each(text, params, (s) => out.push(s.get()));
    return out;
  }

  /* The first column of every row. */
  scalars(text, params = []) {
    return this.values(text, params).map((row) => row[0]);
  }

  /* The first column of the first row, or null: SQLAlchemy's `scalar`. */
  scalar(text, params = []) {
    let out = null;
    let found = false;
    this._each(text, params, (s) => {
      if (!found) out = s.get()[0];
      found = true;
    });
    return out;
  }

  /* Rows of `table` as typed objects, from a query selecting its columns. */
  rows(table, text, params = []) {
    const types = TYPES[table] ?? {};
    const out = [];
    this._each(text, params, (s) => {
      const row = s.getAsObject();
      for (const column of Object.keys(types)) {
        if (column in row) row[column] = fromColumn(types[column], row[column]);
      }
      out.push(row);
    });
    return out;
  }

  /* `SELECT * FROM table WHERE ...`, typed. */
  select(table, where = "", params = []) {
    return this.rows(table, `SELECT * FROM "${table}" ${where}`, params);
  }

  /* The first row of `select`, or null. */
  first(table, where = "", params = []) {
    return this.select(table, where, params)[0] ?? null;
  }

  /* `db.get(Model, key)`: one row by primary key, or null. */
  get(table, key) {
    const columns = KEYS[table] ?? ["id"];
    const values = Array.isArray(key) ? key : [key];
    const where = columns.map((c) => `"${c}" = ?`).join(" AND ");
    return this.first(table, `WHERE ${where}`, values);
  }

  /* `db.add(row); db.flush()`: returns the new row's id. */
  insert(table, row) {
    const types = TYPES[table] ?? {};
    const columns = Object.keys(row);
    const text =
      `INSERT INTO "${table}" (${columns.map((c) => `"${c}"`).join(", ")}) ` +
      `VALUES (${columns.map(() => "?").join(", ")})`;
    this.run(text, columns.map((c) => toColumn(types[c], row[c])));
    return this.scalar("SELECT last_insert_rowid()");
  }

  /* Changed attributes of one row, flushed. */
  update(table, key, changes) {
    const types = TYPES[table] ?? {};
    const keyColumns = KEYS[table] ?? ["id"];
    const keyValues = Array.isArray(key) ? key : [key];
    const columns = Object.keys(changes);
    const text =
      `UPDATE "${table}" SET ${columns.map((c) => `"${c}" = ?`).join(", ")} ` +
      `WHERE ${keyColumns.map((c) => `"${c}" = ?`).join(" AND ")}`;
    this.run(text, [...columns.map((c) => toColumn(types[c], changes[c])), ...keyValues]);
  }

  /* A statement that returns no rows. */
  run(text, params = []) {
    this._each(text, params, () => {});
  }

  /* `db.commit()`, and the next transaction begins. */
  commit() {
    this.sql.exec("COMMIT");
    this.sql.exec("BEGIN");
  }

  /* `db.close()`: whatever was not committed is rolled back. */
  close() {
    if (!this.open) return;
    this.open = false;
    for (const statement of this.statements.values()) statement.free();
    this.statements.clear();
    this.sql.exec("ROLLBACK");
  }
}
