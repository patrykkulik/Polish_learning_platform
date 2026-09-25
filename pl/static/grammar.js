/* The grammar: an index of concepts, and a page per concept.
 *
 * One file serves both, because they are the same fetch with a different shape —
 * `/grammar` lists what `/api/concepts` returns, `/grammar/KEY` renders what
 * `/api/concepts/KEY` returns. The server decides what a locked concept carries;
 * this file never hides anything it was sent, because a page that withholds what
 * its own API hands out is not a gate.
 */

const main = document.getElementById("main");

function fail(message) {
  main.innerHTML = `
    <div class="empty">
      <p>${escapeHtml(message)}</p>
      <p><a class="link" href="grammar">Back to the grammar</a></p>
    </div>`;
}

function renderConcept(c) {
  if (!c.open) {
    return `
      <section>
        <h2>${escapeHtml(c.title)}</h2>
        <p class="sub">This opens when you reach ${escapeHtml(c.key)}. The course has not taught it yet.</p>
        <p><a class="link" href="grammar">All concepts</a></p>
      </section>`;
  }
  const sections = (c.sections || [])
    .map(
      (s) => `
      <div class="sec">
        <h3>${escapeHtml(s.heading)}</h3>
        <div class="body">${markdown(s.body)}</div>
      </div>`
    )
    .join("");
  const tables = (c.tables || []).map(declension).join("");
  return `
    <section class="lesson">
      <h2>${escapeHtml(c.title)}</h2>
      <p class="sub">${escapeHtml(c.summary || "")}</p>
      ${sections}
      ${tables}
      <p style="margin-top:1.5rem"><a class="link" href="grammar">All concepts</a></p>
    </section>`;
}

function renderIndex(concepts) {
  const cards = concepts
    .map((c) => {
      const state = c.read
        ? '<span class="pill held">read</span>'
        : c.open
        ? '<span class="pill open">open</span>'
        : '<span class="pill shut">locked</span>';
      const inner = `
        <div class="row"><h3>${escapeHtml(c.title)}</h3>${state}</div>
        ${c.summary ? `<p>${escapeHtml(c.summary)}</p>` : ""}
        <p class="key">${escapeHtml(c.key)} · introduced at ${escapeHtml(c.introduced_by)}</p>`;
      return `<div class="concept${c.open ? "" : " shut"}">${
        c.open ? `<a href="grammar/${encodeURIComponent(c.key)}">${inner}</a>` : inner
      }</div>`;
    })
    .join("");
  return `
    <section>
      <h2>Grammar</h2>
      <p class="sub">Every concept the course teaches. A page opens when its skill does, and stays here afterwards.</p>
      ${cards}
    </section>`;
}

async function load() {
  // Read relative to the page's <base>: "/" on the local server, the site's path
  // on GitHub Pages, which also serves a concept page as a folder, "KEY/".
  const base = new URL(document.baseURI).pathname;
  const path = location.pathname.startsWith(base) ? location.pathname.slice(base.length) : location.pathname;
  const key = decodeURIComponent(path.replace(/^grammar\/?/, "").replace(/\/$/, ""));
  try {
    if (key) {
      main.innerHTML = renderConcept(await request(`/api/concepts/${encodeURIComponent(key)}`));
    } else {
      const data = await request("/api/concepts");
      main.innerHTML = renderIndex(data.concepts || []);
    }
  } catch (e) {
    fail(e.message);
  }
}

load();
