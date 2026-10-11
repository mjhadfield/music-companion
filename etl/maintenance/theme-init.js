// Theme before first paint -- loaded from every page's <head>, ahead of shared.css's first use.
//
// Opened in its own tab: the choice saved by the nav toggle, localStorage["theme"] (no attribute = dark).
// Inside Citadel (Companion's gear button opens these pages in Citadel's Companion frame): Citadel's look
// instead -- light/dark plus a custom theme's colours, the same CSS variable names as here. It arrives by
// postMessage on every page load and every change in Citadel (see shared.js); the last one is kept in
// sessionStorage so moving between pages here doesn't flash. ?theme= (added by Companion's gear link and
// by our own link back) covers the first page of a visit, before Citadel has said anything.

const CITADEL_ORIGIN = "http://192.168.0.66:8080";
const IN_CITADEL = window.self !== window.top;

let citadelVars = [];
function applyCitadelLook(msg) {
  const root = document.documentElement;
  if (msg.theme === "light") root.dataset.theme = "light"; else delete root.dataset.theme;
  root.style.colorScheme = msg.theme;
  citadelVars.forEach((name) => root.style.removeProperty(name));
  citadelVars = [];
  if (msg.vars && typeof msg.vars === "object") {
    for (const [name, value] of Object.entries(msg.vars)) {
      if (!/^--[a-z0-9-]+$/.test(name) || typeof value !== "string" || value.length > 200) continue;
      root.style.setProperty(name, value);
      citadelVars.push(name);
    }
  }
  root.classList.toggle("citadel-bg", !!msg.bg);
}

(() => {
  try {
    if (!IN_CITADEL) {
      if (localStorage.getItem("theme") === "light") document.documentElement.dataset.theme = "light";
      return;
    }
    let look = JSON.parse(sessionStorage.getItem("citadel-look") || "null");
    const asked = new URLSearchParams(location.search).get("theme");
    if ((asked === "light" || asked === "dark") && look?.theme !== asked) look = { theme: asked };
    if (look?.theme === "light" || look?.theme === "dark") applyCitadelLook(look);
  } catch { /* storage blocked or junk stored: dark until Citadel says otherwise */ }
})();
