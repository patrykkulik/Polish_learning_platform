/* Review session: fetch a queue, answer one item at a time, submit for grading.
 *
 * Nothing here grades anything. The server returns the diagnosis and which cards
 * it moved; this file's only job is to render that and keep the keyboard working.
 */

const stage = document.getElementById("stage");
const rail = document.getElementById("rail");

let queue = [];
let index = 0;
let completed = 0;
let shown = 0;

const POPULATION_LABEL = {
  lexical: "vocabulary",
  morph: "this word's form",
  pattern: "the rule",
};
const RATING = { 1: ["again", "Again"], 2: ["hard", "Hard"], 3: ["good", "Good"] };

function setProgress(p) {
  if (!p) return;
  document.getElementById("s-streak").textContent = p.streak;
  document.getElementById("s-debt").textContent = p.debt;
  document.getElementById("s-retained").textContent = `${p.retained}/${p.tracked}`;
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

/* Say so, and leave a way out. A dead page is the one thing this must not be. */
function fail(message, retry) {
  stage.innerHTML = `
    <div class="done">
      <h2>Something went wrong</h2>
      <p>${escapeHtml(message)}</p>
      <p><button class="primary" id="retry">Try again</button></p>
    </div>
  `;
  document.getElementById("retry").onclick = retry || (() => location.reload());
}

function advanceRail() {
  rail.style.width = queue.length ? `${(index / queue.length) * 100}%` : "0%";
}

async function load() {
  let data;
  try {
    data = await request("/api/session?limit=20");
  } catch (e) {
    return fail(e.message, load);
  }
  queue = data.items || [];
  setProgress(data.progress);
  index = 0;
  completed = 0;
  render();
}

function render() {
  advanceRail();
  if (index >= queue.length) return finish();

  const item = queue[index];
  const isChoice = item.exercise_type === "mcq" || item.exercise_type === "aspect_choice";
  // Free translation shows no Polish at all — the learner produces the whole
  // sentence — so its input is the answer field, not a gap inside a template.
  const isFree = item.exercise_type === "free_translation"
    || item.exercise_type === "listening_dictation";
  const sentence = isFree
    ? '<input id="answer" class="wide" autocomplete="off" autocapitalize="off" spellcheck="false">'
    : item.prompt.includes("___")
    ? item.prompt.replace(
        "___",
        isChoice ? "<u>&nbsp;&nbsp;&nbsp;&nbsp;</u>" : '<input id="answer" autocomplete="off" autocapitalize="off" spellcheck="false">'
      )
    : escapeHtml(item.prompt);

  stage.innerHTML = `
    <div class="node">${escapeHtml(item.node.title)}</div>
    <div class="card">
      ${item.gloss ? `<div class="gloss">${escapeHtml(item.gloss)}</div>` : ""}
      ${item.has_audio ? renderAudio(item) : ""}
      <div class="sentence">${sentence}</div>
      ${isChoice ? renderChoices(item) : ""}
    </div>
    <div class="verdict" id="verdict"></div>
    <div class="actions">
      <!-- Not "Skip": this submits an empty answer and moves the cards. A
           control has to say what it actually does. -->
      <button class="primary" id="go">${isChoice ? "I don’t know" : "Check"}</button>
      <span class="hint">Enter to check, Enter again for the next one</span>
    </div>
  `;

  shown = performance.now();
  const input = document.getElementById("answer");
  if (input) {
    input.focus();
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); submit(input.value); }
    });
  }
  document.getElementById("go").onclick = () => submit(input ? input.value : "");
  stage.querySelectorAll(".play").forEach((b) => {
    b.onclick = () => {
      const player = new Audio(`/api/audio/${item.id}?speed=${b.dataset.speed}`);
      player.play().catch(() => {});
    };
  });
  if (isChoice) {
    stage.querySelectorAll(".choices button").forEach((b) => {
      b.onclick = () => { b.classList.add("picked"); submit(b.dataset.value); };
    });
  }
}

/* Two speeds, as the design asks for: a learner who cannot follow the natural
 * pace needs the same sentence slower, not an easier one. */
function renderAudio(item) {
  return `
    <div class="audio">
      <button type="button" class="play" data-speed="normal">▶ Play</button>
      <button type="button" class="play" data-speed="slow">▶ Slower</button>
    </div>`;
}

function renderChoices(item) {
  const opts = (item.options || [])
    .map((o) => `<button data-value="${escapeAttr(o)}">${escapeHtml(o)}</button>`)
    .join("");
  return `<div class="choices">${opts}</div>`;
}

async function submit(answer) {
  const item = queue[index];
  let data;
  try {
    data = await request("/api/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        item_id: item.id,
        answer: answer || "",
        latency_ms: Math.round(performance.now() - shown),
      }),
    });
  } catch (e) {
    // Retrying re-renders the same item with the answer still in hand, rather
    // than counting an attempt the server never recorded.
    return fail(e.message, render);
  }
  // Counted only once the server has actually recorded it.
  completed += 1;
  setProgress(data.progress);
  showVerdict(data);
}

function showVerdict(data) {
  const v = document.getElementById("verdict");
  // Orthography is its own state: the grammar was right, so it is neither a
  // pass nor a failure and must not be dressed as one.
  const tone = data.correct ? "ok" : data.error_class === "ORTHOGRAPHY" ? "warn" : "err";
  const heading = data.correct
    ? "Correct"
    : data.error_class === "ORTHOGRAPHY"
    ? "Spelling"
    : data.error_class.replace(/_/g, " ").toLowerCase();

  v.className = `verdict show ${tone}`;
  v.innerHTML = `
    <div class="label">${escapeHtml(heading)}</div>
    <div class="why">${escapeHtml(data.message)}</div>
    <div class="routed">${routedChips(data.scored)}</div>
  `;

  const go = document.getElementById("go");
  go.textContent = index + 1 >= queue.length ? "Finish" : "Next";
  go.onclick = next;
  go.focus();
  document.querySelectorAll(".choices button").forEach((b) => (b.disabled = true));
  const input = document.getElementById("answer");
  if (input) input.disabled = true;

  document.addEventListener("keydown", onceEnter);
}

function onceEnter(e) {
  if (e.key !== "Enter") return;
  e.preventDefault();
  document.removeEventListener("keydown", onceEnter);
  next();
}

/* The scheduling made legible: a card that was not scored is shown as
 * untouched, because "we deliberately left this alone" is information the
 * learner benefits from seeing. */
function routedChips(scored) {
  return ["pattern", "morph", "lexical"]
    .map((pop) => {
      const label = POPULATION_LABEL[pop];
      if (!(pop in scored)) {
        return `<span class="chip untouched">${label} · untouched</span>`;
      }
      const [cls, text] = RATING[scored[pop]] || ["", "?"];
      return `<span class="chip ${cls}"><b>${label}</b> · ${text}</span>`;
    })
    .join("");
}

function next() {
  document.removeEventListener("keydown", onceEnter);
  index += 1;
  render();
}

async function finish() {
  let data;
  try {
    // No count is sent. The server holds the authoritative one in `attempt`,
    // and a client that reports its own is a client that can award itself a
    // streak.
    data = await request("/api/session/complete", { method: "POST" });
  } catch (e) {
    return fail(e.message, finish);
  }
  setProgress(data.progress);
  rail.style.width = "100%";

  const unlocked = data.unlocked.length
    ? `<p>Unlocked: <strong>${data.unlocked.map(escapeHtml).join(", ")}</strong></p>`
    : "";
  stage.innerHTML = `
    <div class="done">
      <h2>Session finished</h2>
      <p>${completed} answered · streak ${data.streak} · ${data.progress.debt} still due</p>
      ${unlocked}
      <p><button class="primary" onclick="location.reload()">Another round</button></p>
    </div>
  `;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}
function escapeAttr(s) {
  return escapeHtml(s);
}

load();
