/* The progress view: what the learner has actually retained.
 *
 * Every figure here comes from `/api/graph`. Nothing is computed in the browser
 * beyond formatting — the mastery split that criterion 14 asks for (a latching
 * gate, a decaying display) is a property of the scheduler, and recomputing
 * either one here would let the page and the composer disagree about how far
 * along the learner is.
 */

const main = document.getElementById("main");

function fail(message) {
  main.innerHTML = `
    <div class="empty">
      <p>${escapeHtml(message)}</p>
      <p><a class="link" href="progress">Try again</a></p>
    </div>`;
}

function pct(x) {
  return `${Math.round((x || 0) * 100)}%`;
}

async function load() {
  let data;
  try {
    data = await request("/api/graph");
  } catch (e) {
    return fail(e.message);
  }

  const p = data.progress || {};
  document.getElementById("s-streak").textContent = p.streak ?? "–";
  document.getElementById("s-freezes").textContent = p.freezes ?? "–";
  document.getElementById("s-debt").textContent = p.debt ?? "–";

  main.innerHTML = `
    ${renderTiles(data, p)}
    ${renderMilestones(data.milestones)}
    ${renderCurve(data.retention_curve || [])}
    ${renderNodes(data.nodes || [])}
  `;
}

/* All three standings belong here, unlike the session-end screen which shows
 * only the nearest. This page is where someone comes to look; that one
 * interrupts them on the way out.
 *
 * Each is "where you stand", never "what you just crossed" — the server cannot
 * honestly claim a crossing without recording which milestones it has already
 * announced, and a milestone announced twice teaches the learner the number is
 * decorative. */
function renderMilestones(m) {
  if (!m) return "";
  const rows = [
    ["Remembered a week", m.retained, "cards held at seven days or more"],
    ["Words met", m.vocabulary, "distinct Polish lexemes seen"],
    ["Days in a row", m.streak, "consecutive days both goals were met"],
  ]
    .filter(([, s]) => s)
    .map(([label, s, note]) => {
      const done = s.next === null;
      const pct = done ? 100 : Math.min(100, Math.round((s.value / s.next) * 100));
      const target = done
        ? `${s.reached} — all of them`
        : `${s.value} of ${s.next}`;
      return `
        <div class="ms">
          <div class="ms-head">
            <span class="ms-label">${escapeHtml(label)}</span>
            <span class="ms-target">${escapeHtml(target)}</span>
          </div>
          <div class="meter"><i class="${done ? "done" : ""}" style="width:${pct}%"></i></div>
          <div class="ms-note">${escapeHtml(note)}${
            s.reached ? ` · last milestone ${s.reached}` : ""
          }</div>
        </div>`;
    })
    .join("");
  if (!rows) return "";
  return `
    <section>
      <div>
        <h2>Milestones</h2>
        <p class="sub">Round numbers worth passing. Nothing here expires.</p>
      </div>
      <div class="ms-grid">${rows}</div>
    </section>`;
}

/* Four numbers that answer four different questions. Folding them into a single
 * score would hide the one that is currently bad, which is the only one worth
 * looking at. */
function renderTiles(data, p) {
  const v = data.vocabulary || { met: 0, total: 0 };
  const nodes = data.nodes || [];
  const open = nodes.filter((n) => n.unlocked).length;
  const held = nodes.filter((n) => n.mastered).length;
  return `
    <section>
      <div>
        <h2>Where you are</h2>
        <p class="sub">Retention is a live figure and falls between sessions; unlocks latch and do not.</p>
      </div>
      <div class="tiles">
        <div class="tile">
          <div class="k">Vocabulary met</div>
          <div class="v">${v.met}</div>
          <div class="n">of ${v.total} in the course</div>
        </div>
        <div class="tile">
          <div class="k">Skills open</div>
          <div class="v">${open}</div>
          <div class="n">${held} mastered · ${nodes.length} total</div>
        </div>
        <div class="tile">
          <div class="k">Cards due</div>
          <div class="v">${p.debt ?? 0}</div>
          <div class="n">before midnight</div>
        </div>
        <div class="tile">
          <div class="k">Retained a week</div>
          <div class="v">${p.retained ?? 0}</div>
          <div class="n">of ${p.tracked ?? 0} cards tracked</div>
        </div>
      </div>
    </section>`;
}

/* Recall rate per day. Bars are drawn from the share recalled, not from volume:
 * a day of twenty answers and a day of two are both a percentage, and scaling by
 * volume would make an enthusiastic day look like a good one. */
function renderCurve(curve) {
  if (!curve.length) {
    return `
      <section>
        <div><h2>Retention</h2><p class="sub">Last 30 days.</p></div>
        <div class="empty">No reviews yet. Answer a session and this fills in.</div>
      </section>`;
  }
  const bars = curve
    .map((d) => {
      const rate = d.reviews ? d.recalled / d.reviews : 0;
      const cls = rate >= 0.9 ? "strong" : "";
      return `<div class="day" title="${escapeAttr(d.day)} — ${d.recalled}/${d.reviews} recalled">
                <i class="${cls}" style="height:${Math.max(2, rate * 100)}%"></i>
              </div>`;
    })
    .join("");
  const total = curve.reduce((a, d) => a + d.reviews, 0);
  const good = curve.reduce((a, d) => a + d.recalled, 0);
  return `
    <section>
      <div>
        <h2>Retention</h2>
        <p class="sub">${good} of ${total} reviews recalled over ${curve.length} active day${curve.length === 1 ? "" : "s"} — ${pct(total ? good / total : 0)}.</p>
      </div>
      <div class="curve">${bars}</div>
      <div class="axis"><span>${escapeHtml(curve[0].day)}</span><span>${escapeHtml(curve[curve.length - 1].day)}</span></div>
    </section>`;
}

function renderNodes(nodes) {
  const rows = nodes
    .map((n) => {
      const state = n.mastered
        ? '<span class="pill held">mastered</span>'
        : n.unlocked
        ? '<span class="pill open">open</span>'
        : '<span class="pill shut">locked</span>';
      return `
        <tr class="${n.unlocked ? "" : "locked"}">
          <td>
            <div class="title">${escapeHtml(n.title)}</div>
            <div class="key">${escapeHtml(n.key)} · ${escapeHtml(n.type)}</div>
          </td>
          <td>${state}</td>
          <td style="width:35%">
            <div class="meter"><i class="${n.mastered ? "done" : ""}" style="width:${Math.round((n.mastery || 0) * 100)}%"></i></div>
          </td>
          <td class="num">${pct(n.mastery)}</td>
          <td class="num">${n.mastered_strata}/${n.strata}</td>
        </tr>`;
    })
    .join("");
  return `
    <section>
      <div>
        <h2>Skills</h2>
        <p class="sub">Retention is today's predicted recall across the skill. Held is how many of its strata clear the unlock gate — that figure latches and never falls.</p>
      </div>
      <table>
        <thead>
          <tr>
            <th>Skill</th><th></th><th>Retention</th>
            <th class="num"></th><th class="num">Held</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </section>`;
}


load();
