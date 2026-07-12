/* The One Percent — shared helpers (loaded after wctc_data.js) */
window.OP = (function () {
  // apply the saved theme immediately, before first paint, to avoid flashing
  const THEME_KEY = "op-theme";
  try {
    if (localStorage.getItem(THEME_KEY) === "dark")
      document.documentElement.dataset.theme = "dark";
  } catch (e) {}

  const isDark = () => document.documentElement.dataset.theme === "dark";
  const syncThemeButtons = () => {
    document.querySelectorAll("[data-theme-toggle]").forEach((b) => {
      b.innerHTML = isDark()
        ? '<i data-lucide="sun" class="lucide"></i>Light theme'
        : '<i data-lucide="moon" class="lucide"></i>Dark theme';
    });
    if (window.lucide) lucide.createIcons();
  };
  const toggleTheme = () => {
    if (isDark()) delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = "dark";
    try {
      if (isDark()) localStorage.setItem(THEME_KEY, "dark");
      else localStorage.removeItem(THEME_KEY);
    } catch (e) {}
    syncThemeButtons();
  };

  const DATA = window.WCTC_DATA || { people: [], generated_at: "", model: "" };
  const people = DATA.people;

  const DISCIPLINES = [
    ["futures", "Futures"], ["forex", "Forex"], ["stocks", "Stocks"], ["options", "Options"],
  ];

  const disciplineOfEvent = (ev) => {
    const low = ev.toLowerCase();
    for (const [key] of DISCIPLINES) if (low.includes(key.replace("stocks", "stock"))) return key;
    if (low.includes("stock")) return "stocks";
    return null;
  };

  const fmtPct = (n) => (n >= 1000 ? n.toLocaleString("en-US") : String(n));
  const bestPct = (p) => (p.wins.length ? Math.max(...p.wins.map((w) => w.pct)) : 0);
  const linkCount = (t) => (t ? (t.match(/\[[^\]]+\]\(https?:\/\//g) || []).length : 0);
  const totalLinks = (p) => linkCount(p.studies) + linkCount(p.teaches) + linkCount(p.free);

  // all years present in any win, sorted desc; "2024-2025" counts for both
  const yearsOfWin = (w) => w.year.split("-").map((y) => y.trim());
  const allYears = [...new Set(people.flatMap((p) => p.wins.flatMap(yearsOfWin)))]
    .sort((a, b) => b.localeCompare(a));

  const allCountries = [...new Set(people.map((p) => p.country).filter(Boolean))].sort();
  const allPlaces = [...new Set(people.flatMap((p) => p.wins.map((w) => w.place)))]
    .sort((a, b) => parseInt(a) - parseInt(b));

  // best matching win for a person under (discipline, year) filters; null if none
  const bestWinFiltered = (p, disc, year) => {
    let best = null;
    for (const w of p.wins) {
      if (disc && disciplineOfEvent(w.event) !== disc) continue;
      if (year && !yearsOfWin(w).includes(year)) continue;
      if (!best || w.pct > best.pct) best = w;
    }
    return best;
  };

  const placeClass = (place) => {
    const n = parseInt(place);
    return n === 1 ? "place-1" : n === 2 ? "place-2" : n === 3 ? "place-3" : "place-x";
  };

  // navbar: mark current page, wire the profile dropdown, then render icons
  const boot = () => {
    const here = location.pathname.split("/").pop() || "index.html";
    document.querySelectorAll(".nav-links a").forEach((a) => {
      if (a.getAttribute("href") === here) a.classList.add("on");
    });
    const btn = document.querySelector(".profile-btn");
    const menu = document.querySelector(".profile-menu");
    if (btn && menu && !btn.dataset.wired) {
      btn.dataset.wired = "1";
      const close = () => { menu.classList.remove("open"); btn.setAttribute("aria-expanded","false"); };
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const open = menu.classList.toggle("open");
        btn.setAttribute("aria-expanded", open ? "true" : "false");
      });
      document.addEventListener("click", (e) => { if (!menu.contains(e.target)) close(); });
      document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
      const tbtn = menu.querySelector("[data-theme-toggle]");
      if (tbtn) tbtn.addEventListener("click", (e) => { e.stopPropagation(); toggleTheme(); });
    }
    syncThemeButtons();
    if (window.lucide) lucide.createIcons();
  };

  return { DATA, people, DISCIPLINES, disciplineOfEvent, fmtPct, bestPct, linkCount,
           totalLinks, yearsOfWin, allYears, allCountries, allPlaces,
           bestWinFiltered, placeClass, boot, toggleTheme };
})();