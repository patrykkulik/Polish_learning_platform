/* The course on a phone: `request()` answered in-process, not over the network.
 *
 * Loaded only on the static site, after `common.js`, by a tag carrying the names
 * of the files this deploy was built with. It loads the course's runtime — the
 * Python's modules ported to JavaScript, in the deploy's `app.<hash>/` folder —
 * and sql.js, restores the learner's database from IndexedDB, and replaces
 * `request()` with a call into `phone.handle`. The page scripts do not change:
 * they already send every JSON call through `request()`, and a failure here
 * reaches them as the error `request()` throws.
 *
 * A call that changed the database is saved to IndexedDB before the page sees
 * its answer, so an answer the learner has seen graded is stored — and a save
 * that fails is an error, not a silence. A call that changed nothing is not
 * saved: saving copies the whole database.
 *
 * Sound comes from the phone's own Polish voice, through `speechSynthesis`: it
 * replaces `playAudio()`. A phone without one is not offered dictation, and
 * its options carry no play button.
 */

(function () {
  const RELOADED = "pl-stale-reload";
  const STORE = "phone";
  // The slow rate. Speech at 0.8 of normal speed, as the local server's slow pass
  // is, takes a quarter longer. Browsers map this rate onto the platform's own
  // non-linearly: measured on the owner's iPhone, 0.8 took no longer than 1.0,
  // and 0.5 took 1.28 times as long (1.27 in Chrome on the Mac).
  const SLOW = 0.5;

  const script = document.currentScript;
  const stamped = {
    app: script.dataset.app,
    concepts: script.dataset.concepts,
    content: script.dataset.content,
    lexicon: script.dataset.lexicon,
  };
  const sameNames = (a, b) => Object.keys(stamped).every((key) => a[key] === b[key]);

  let phone = null;
  let voice = null;
  let database = null;
  let installed = null;
  let store = null;
  let saving = Promise.resolve();
  // A change a failed save left unsaved, which the next call saves.
  let unsaved = false;

  /* One reload per set of names, so a page the cache keeps serving stale cannot
   * reload forever. Storage that refuses is treated as "already reloaded". */
  function reloadOnce(reason) {
    try {
      if (sessionStorage.getItem(RELOADED) === reason) return false;
      sessionStorage.setItem(RELOADED, reason);
    } catch (e) {
      return false;
    }
    location.reload();
    return true;
  }

  /* `site.json` names what is live. Asked with a query no cache has seen and
   * with no-store, because for ten minutes after a deploy the browser may serve
   * an old page and its old files without asking the server. */
  async function currentNames() {
    try {
      const res = await fetch(`site.json?t=${Date.now()}`, { cache: "no-store" });
      if (!res.ok) return stamped;
      const live = await res.json();
      if (sameNames(live, stamped)) return stamped;
      if (reloadOnce(Object.keys(stamped).map((key) => live[key]).join("|"))) {
        return new Promise(() => {});
      }
      // Still stale after the one reload: take what is live, so this phone never
      // installs content older than what it may already hold.
      return live;
    } catch (e) {
      return stamped;
    }
  }

  /* A file of this deploy. One that has gone from the server means the page is
   * stale, and earns the one reload. */
  async function download(name) {
    let res;
    try {
      res = await fetch(name);
    } catch (e) {
      throw new Error("The course could not be downloaded. Check the connection and reload.");
    }
    if (res.status === 404 && reloadOnce(`missing ${name}`)) {
      return new Promise(() => {});
    }
    if (!res.ok) throw new Error(`The course could not be downloaded (${res.status}).`);
    return res;
  }

  /* sql.js is a classic script that defines `initSqlJs`. */
  function classicScript(src) {
    return new Promise((resolve, reject) => {
      const tag = document.createElement("script");
      tag.src = src;
      tag.onload = resolve;
      tag.onerror = () => reject(new Error(`${src} could not be loaded`));
      document.head.appendChild(tag);
    });
  }

  async function runtime(app) {
    try {
      await classicScript(`${app}/vendor/sql-wasm.js`);
      const SQL = await initSqlJs({ locateFile: (file) => `${app}/vendor/${file}` });
      // Resolved against the page's base, not this script's folder.
      const module = await import(new URL(`${app}/course/phone.js`, document.baseURI).href);
      return [SQL, module];
    } catch (error) {
      // A stale page whose app folder is gone reloads here, once.
      await download(`${app}/course/phone.js`);
      throw error;
    }
  }

  /* The phone's Polish voice, or null. Some browsers list their voices a
   * moment after the page first asks. */
  function polishVoice() {
    if (!("speechSynthesis" in window)) return Promise.resolve(null);
    const pick = () => speechSynthesis.getVoices().find((v) => /^pl([-_]|$)/i.test(v.lang)) ?? null;
    if (speechSynthesis.getVoices().length) return Promise.resolve(pick());
    return new Promise((resolve) => {
      const done = () => resolve(pick());
      speechSynthesis.addEventListener("voiceschanged", done, { once: true });
      setTimeout(done, 1000);
    });
  }

  function openStore() {
    return new Promise((resolve, reject) => {
      const open = indexedDB.open("polish", 1);
      open.onupgradeneeded = () => open.result.createObjectStore(STORE);
      open.onsuccess = () => resolve(open.result);
      open.onerror = () => reject(open.error);
    });
  }

  /* The saved database's bytes and the content hash installed into it. */
  function restore() {
    return new Promise((resolve, reject) => {
      const transaction = store.transaction(STORE);
      const saved = { database: null, content: null };
      for (const key of Object.keys(saved)) {
        transaction.objectStore(STORE).get(key).onsuccess = (event) => {
          saved[key] = event.target.result ?? null;
        };
      }
      transaction.oncomplete = () => resolve(saved);
      transaction.onerror = () => reject(transaction.error);
    });
  }

  /* Both in one transaction, so a phone never holds a database from one content
   * file recorded against another. */
  function persist(bytes, content) {
    return new Promise((resolve, reject) => {
      const transaction = store.transaction(STORE, "readwrite");
      transaction.objectStore(STORE).put(bytes, "database");
      transaction.objectStore(STORE).put(content, "content");
      transaction.oncomplete = resolve;
      transaction.onerror = () => reject(transaction.error);
      transaction.onabort = () => reject(transaction.error);
    });
  }

  /* Saves never overlap. Each exports the database as it is when it runs, so a
   * save also stores whatever calls came before it. */
  function save() {
    saving = saving.then(() => persist(database.export(), installed));
    return saving.then(
      () => {
        unsaved = false;
      },
      () => {
        saving = Promise.resolve();
        unsaved = true;
        throw new Error(
          "This phone could not save your progress. Your last answer may not be kept — try again, or reload the page."
        );
      }
    );
  }

  async function boot() {
    if (navigator.storage && navigator.storage.persist) {
      navigator.storage.persist().catch(() => {});
    }
    const names = await currentNames();

    const [[SQL, module], saved, lexicon, concepts, found] = await Promise.all([
      runtime(names.app),
      openStore().then((opened) => {
        store = opened;
        return restore();
      }),
      download(names.lexicon).then((res) => res.json()),
      download(names.concepts).then((res) => res.json()),
      polishVoice(),
    ]);

    // The phone only ever installs the content file `site.json` names, and
    // fetches it only when that is not the one it already has.
    const ledger =
      saved.content === names.content ? null : new Uint8Array(await (await download(names.content)).arrayBuffer());
    database = saved.database ? new SQL.Database(saved.database) : new SQL.Database();
    installed = module.start({
      SQL,
      database,
      ledger,
      contentHash: names.content,
      installedHash: saved.content,
      lexicon,
      concepts,
      zone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
      voice: found !== null,
    });
    // Only now is there a runtime to answer the page and its play buttons.
    phone = module;
    voice = found;
    if (!saved.database || installed !== saved.content) await save();
  }

  /* Start-up, and start-up again after a failure: a download that failed once,
   * on a first visit or after a content update, need not strand the page, whose
   * "Try again" then asks again. */
  function start() {
    const attempt = boot();
    // A failure is reported by the first request that waits on it.
    attempt.catch(() => {});
    return attempt;
  }

  let ready = start();

  /* A page the browser brings back from its back/forward cache still holds the
   * database as it was when the page was left, and its next save would
   * overwrite whatever pages opened since have saved. Reloaded, it restores
   * what was saved. */
  addEventListener("pageshow", (event) => {
    if (event.persisted) location.reload();
  });

  /* Speaks what the play button at `url` says. Synchronous from the tap to
   * `speak()`, because iOS starts speech only from a user's gesture: the text
   * comes from `phone.handle`, which answers in-process and at once.
   *
   * Nothing is cancelled first. On iOS, cancelling an utterance mid-sentence
   * silenced the next ones for seconds (measured), so a tap while one is
   * speaking is queued after it instead. */
  playAudio = function (url) {
    if (phone === null || voice === null) return;
    const answer = phone.handle("GET", url);
    if (answer.status !== 200) return;
    const utterance = new SpeechSynthesisUtterance(JSON.parse(answer.body).text);
    utterance.voice = voice;
    utterance.lang = voice.lang;
    utterance.rate = new URL(url, document.baseURI).searchParams.get("speed") === "slow" ? SLOW : 1.0;
    speechSynthesis.speak(utterance);
  };

  request = async function (url, options) {
    if (ready === null) ready = start();
    try {
      await ready;
    } catch (e) {
      ready = null;
      throw new Error(
        e && e.message ? `The app could not start on this phone: ${e.message}` : "The app could not start on this phone."
      );
    }
    const method = ((options && options.method) || "GET").toUpperCase();
    const body = options && options.body !== undefined ? options.body : null;
    const answer = phone.handle(method, url, body);
    if (answer.changed || unsaved) await save();
    const data = JSON.parse(answer.body);
    if (answer.status < 200 || answer.status >= 300) {
      throw new Error(data.detail || `The app returned ${answer.status}.`);
    }
    return data;
  };
})();
