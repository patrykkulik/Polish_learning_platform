/* Shared by session.js and progress.js.
 *
 * `escapeHtml` is the only XSS control on either page. Held in one place because
 * a correction to a duplicated escaper silently misses its copy, and duplicated
 * escaping is the worst kind to duplicate. `request` sits beside it because the
 * two pages need the same distinction — "the server could not be reached" is a
 * different message from "the server said no" — and a page that renders neither
 * is a blank page the learner cannot act on.
 */

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

/* Attribute values need exactly the same escaping here, because every attribute
 * this app writes is quoted. Named separately so the call sites say which
 * context they are in, and so the two can diverge if one ever must. */
function escapeAttr(s) {
  return escapeHtml(s);
}

/* Every request goes through here, because none of them checked `res.ok`.
 *
 * A failure still parses as JSON — FastAPI returns `{"detail": ...}` — so the
 * caller read `data.items` or `data.error_class` off an error body, got
 * undefined, and threw somewhere further on. The learner saw a page that had
 * simply stopped: no message, and an answer that appeared to have been
 * swallowed rather than rejected. */
async function request(url, options) {
  let res;
  try {
    res = await fetch(url, options);
  } catch (e) {
    throw new Error("The server could not be reached.");
  }
  if (!res.ok) {
    let detail = "";
    try {
      detail = (await res.json()).detail || "";
    } catch (e) {
      /* An error page that is not JSON. The status is the whole message. */
    }
    throw new Error(detail || `The server returned ${res.status}.`);
  }
  return res.json();
}

/* Lessons are rendered in two places — the session step that teaches a
 * concept and the grammar page that keeps it — so their renderer lives here
 * beside `escapeHtml`, for the reason that one does.
 *
 * The authored prose is Markdown, and only the little of it the lessons use is
 * supported: paragraphs, lists, tables, `code`, *emphasis* and **strong**.
 * Everything is escaped first, so an author cannot write HTML into a lesson by
 * accident or otherwise. */
function markdown(md) {
  const inline = (s) =>
    escapeHtml(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>");

  const out = [];
  let list = null;
  let table = null;
  // Authored prose wraps at the column the file is written in, so a paragraph
  // arrives as several lines. One <p> per line would set every sentence as its
  // own paragraph, which is how the first render of this page read.
  let para = null;

  const closePara = () => { if (para) { out.push(`<p>${inline(para.join(" "))}</p>`); para = null; } };
  const closeList = () => { if (list) { out.push(`<ul>${list.join("")}</ul>`); list = null; } };
  const closeTable = () => {
    if (!table) return;
    const [head, ...rows] = table;
    out.push(
      `<table><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead>` +
      `<tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`
    );
    table = null;
  };

  for (const raw of String(md || "").split("\n")) {
    const line = raw.trim();
    if (!line) { closePara(); closeList(); closeTable(); continue; }

    if (line.startsWith("|")) {
      const cells = line.split("|").slice(1, -1).map((c) => c.trim());
      // The |---|---| separator carries no content.
      if (cells.every((c) => /^:?-{2,}:?$/.test(c))) continue;
      closePara();
      closeList();
      (table ||= []).push(cells);
      continue;
    }
    closeTable();

    if (line.startsWith("- ")) { closePara(); (list ||= []).push(`<li>${inline(line.slice(2))}</li>`); continue; }
    closeList();
    (para ||= []).push(line);
  }
  closePara();
  closeList();
  closeTable();
  return out.join("");
}

/* A declension table, rendered from the paradigm the server read out of SGJP.
 * Two cases sharing one surface is the lesson — the masculine animate accusative
 * *is* its genitive — so the repeat is marked rather than collapsed. */
function declension(t) {
  const seen = new Map();
  const rows = t.rows
    .map((r) => {
      const form = r.surfaces.join(" / ");
      const shared = seen.has(form);
      seen.set(form, true);
      return `
        <tr${shared ? ' class="shared"' : ""}>
          <td class="case">${escapeHtml(r.polish)}<div class="key">${escapeHtml(r.english)}</div></td>
          <td class="q">${escapeHtml(r.questions)}<div class="key">${escapeHtml(r.gloss || "")}</div></td>
          <td class="form"><b>${escapeHtml(form)}</b></td>
        </tr>`;
    })
    .join("");

  const repeated = [...new Set(t.rows.map((r) => r.surfaces.join(" / ")))].length < t.rows.length;
  return `
    <table class="decl">
      <caption>${escapeHtml(t.caption || t.lexeme)}</caption>
      <tbody>${rows}</tbody>
    </table>
    ${repeated ? '<div class="shared-note">Two cases, one form — that repeat is the rule, not a coincidence.</div>' : ""}`;
}
