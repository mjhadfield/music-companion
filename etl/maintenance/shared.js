/*
 * Shared helpers for the maintenance tool's pages (index.html, artists.html,
 * duplicates.html, albums.html, album-duplicates.html).
 * Plain <script src> include, no module system -- matches the rest of the
 * project's no-build-step convention.
 */

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Each entry in `lines` is already one complete line (server reads the
// subprocess's stdout line-by-line) -- styled here by a light heuristic
// on its own text, no need to change what the underlying script prints.
function appendLines(logEl, lines) {
  for (const rawLine of lines) {
    const line = rawLine.replace(/\n$/, "");
    const div = document.createElement("div");
    div.className = "log-line";
    if (/^(={5,}|What's new|Scrobbles:|Setlists:|Vinyl holdings:|New artists)/.test(line.trim())) {
      div.className += " header";
    } else if (line.includes("⚠")) {
      div.className += " warn";
    } else if (line.trim() === "") {
      div.className += " dim";
    }
    div.textContent = line;
    logEl.appendChild(div);
  }
  logEl.scrollTop = logEl.scrollHeight;
}

// Polls /status/<jobId> until the job reports done, appending new lines to
// logEl as they arrive and calling onDone(data) once with the final payload.
async function pollJob(jobId, logEl, onDone) {
  let since = 0;
  while (true) {
    const res = await fetch(`/status/${jobId}?since=${since}`);
    const data = await res.json();
    if (data.lines && data.lines.length) {
      appendLines(logEl, data.lines);
      since = data.total;
    }
    if (data.done) {
      onDone(data);
      return;
    }
    await new Promise((r) => setTimeout(r, 500));
  }
}
