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
  setProgress(data.progress);
  // A concept is taught before it is drilled, so a lesson takes the session
  // before any question does. Its node introduces nothing until it is read.
  if (data.lesson) return renderLesson(data.lesson);
  queue = data.items || [];
  index = 0;
  completed = 0;
  render();
}

/* The lesson step. Reading is not answering: nothing here is graded, nothing is
 * scheduled, and it does not count toward the day's goal.
 *
 * Acknowledging re-loads the session rather than continuing with the queue this
 * response carried — that queue was composed while the concept was still unread,
 * so it holds none of the material the lesson just explained. Without the
 * re-load the lesson and its exercises fall on different days. */
function renderLesson(lesson) {
  const sections = (lesson.sections || [])
    .map(
      (s) => `
      <div class="sec">
        <h3>${escapeHtml(s.heading)}</h3>
        <div class="body">${markdown(s.body)}</div>
      </div>`
    )
    .join("");
  // The tables are the half a learner can check for themselves: `kot` shows one
  // form in two rows, which is the rule rather than a claim about it.
  const tables = (lesson.tables || []).map(declension).join("");

  stage.innerHTML = `
    <div class="node">New skill — read this first</div>
    <div class="card lesson">
      <h2>${escapeHtml(lesson.title)}</h2>
      <p class="what">${escapeHtml(lesson.summary || "")}</p>
      ${sections}
      ${tables}
      <p>
        <a class="link" href="/grammar/${encodeURIComponent(lesson.key)}">Keep this page — it stays in the grammar</a>
      </p>
    </div>
    <p>
      <button class="primary" id="got-it">Got it — start the questions</button>
    </p>`;

  document.getElementById("got-it").onclick = async () => {
    try {
      await request(`/api/concepts/${encodeURIComponent(lesson.key)}/read`, { method: "POST" });
    } catch (e) {
      return fail(e.message, load);
    }
    load();
  };
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
    <div class="routed">${routedChips(data.scored, data.counted_earlier || [], data.cooled_down || [])}</div>
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
function routedChips(scored, countedEarlier, cooledDown) {
  return ["pattern", "morph", "lexical"]
    .map((pop) => {
      const label = POPULATION_LABEL[pop];
      if (pop in scored) {
        const [cls, text] = RATING[scored[pop]] || ["", "?"];
        return `<span class="chip ${cls}"><b>${label}</b> · ${text}</span>`;
      }
      // "This counted, and the schedule moves once a day" is a different fact
      // from "this exercise does not test that", and the learner should not
      // have to guess which one they are looking at.
      // Not due, and it moved within the last few days: the schedule is resting,
      // which is a different fact from "counted earlier today".
      if (cooledDown.includes(pop)) {
        return `<span class="chip earlier">${label} · not due yet, resting</span>`;
      }
      if (countedEarlier.includes(pop)) {
        return `<span class="chip earlier">${label} · counted earlier today</span>`;
      }
      return `<span class="chip untouched">${label} · untouched</span>`;
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

  stage.innerHTML = `
    <div class="done">
      <h2>Session finished</h2>
      <p>${completed} answered · streak ${data.streak} · ${data.progress.debt} still due</p>
      ${renderUnlocks(data.unlocked || [])}
      ${renderMilestones(data.milestones)}
      <p>
        <button class="primary" onclick="location.reload()">Another round</button>
        <a class="link" href="/progress">See your progress</a>
      </p>
    </div>
  `;
}

/* Unlocking is the one moment in the loop where the course visibly opens up.
 * It used to arrive as the string "N01" — the database's name for the thing,
 * which tells the learner nothing about what they just earned. */
function renderUnlocks(unlocked) {
  if (!unlocked.length) return "";
  return unlocked
    .map(
      (n) => `
      <div class="unlock">
        <div class="label">New skill unlocked</div>
        <h3>${escapeHtml(n.title)}</h3>
        ${n.explanation ? `<p class="what">${escapeHtml(firstLine(n.explanation))}</p>` : ""}
      </div>`
    )
    .join("");
}

/* The explanation is Markdown written for the lesson screen. The unlock moment
 * wants one sentence, not the whole thing. */
function firstLine(md) {
  const text = String(md).replace(/[*_`#]/g, "").trim();
  const stop = text.indexOf(". ");
  return stop === -1 ? text.split("\n")[0] : text.slice(0, stop + 1);
}

/* Where the learner stands against the next round number — never "you just
 * crossed", which the server cannot honestly claim without recording what it
 * has already announced. Only the nearest of the three is shown: three progress
 * bars at once is a dashboard, and a dashboard is not encouragement. */
function renderMilestones(m) {
  if (!m) return "";
  const named = {
    retained: ["cards remembered a week", "card remembered a week"],
    vocabulary: ["Polish words met", "Polish word met"],
    streak: ["days in a row", "day in a row"],
  };
  const open = Object.keys(named)
    .map((k) => ({ k, ...m[k] }))
    .filter((s) => s && s.next);
  if (!open.length) return "";
  // Nearest to its next threshold, proportionally.
  open.sort((a, b) => b.value / b.next - a.value / a.next);
  const best = open[0];
  const [plural, singular] = named[best.k];
  const pct = Math.min(100, Math.round((best.value / best.next) * 100));
  return `
    <div class="milestone">
      <div class="rail"><i style="width:${pct}%"></i></div>
      <p class="what">
        <strong>${best.value}</strong> ${best.value === 1 ? singular : plural}
        · ${best.next - best.value} to go
      </p>
    </div>`;
}


load();
