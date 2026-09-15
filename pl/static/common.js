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
