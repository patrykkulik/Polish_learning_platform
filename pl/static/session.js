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

function advanceRail() {
  rail.style.width = queue.length ? `${(index / queue.length) * 100}%` : "0%";
}

async function load() {
  const res = await fetch("/api/session?limit=20");
  const data = await res.json();
  queue = data.items;
  setProgress(data.progress);
  index = 0;
  completed = 0;
  render();
}

function render() {
  advanceRail();
  if (index >= queue.length) return finish();

  const item = queue[index];
  const isChoice = item.exercise_type === "mcq";
  const sentence = item.prompt.includes("___")
    ? item.prompt.replace(
        "___",
        isChoice ? "<u>&nbsp;&nbsp;&nbsp;&nbsp;</u>" : '<input id="answer" autocomplete="off" autocapitalize="off" spellcheck="false">'
      )
    : escapeHtml(item.prompt);

  stage.innerHTML = `
    <div class="node">${escapeHtml(item.node.title)}</div>
    <div class="card">
      ${item.gloss ? `<div class="gloss">${escapeHtml(item.gloss)}</div>` : ""}
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
  if (isChoice) {
    stage.querySelectorAll(".choices button").forEach((b) => {
      b.onclick = () => { b.classList.add("picked"); submit(b.dataset.value); };
    });
  }
}

function renderChoices(item) {
  const opts = (item.options || [])
    .map((o) => `<button data-value="${escapeAttr(o)}">${escapeHtml(o)}</button>`)
    .join("");
  return `<div class="choices">${opts}</div>`;
}

async function submit(answer) {
  const item = queue[index];
  const res = await fetch("/api/submit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      item_id: item.id,
      answer: answer || "",
      latency_ms: Math.round(performance.now() - shown),
    }),
  });
  const data = await res.json();
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
  const res = await fetch(`/api/session/complete?items_completed=${completed}`, {
    method: "POST",
  });
  const data = await res.json();
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
