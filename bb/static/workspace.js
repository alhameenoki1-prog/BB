(() => {
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  const supported = ["light", "dark", "system"];
  let theme = "system";
  try {
    const stored = localStorage.getItem("bb-appearance");
    if (supported.includes(stored)) theme = stored;
  } catch {
    // Local storage can be unavailable in private or embedded browsers.
  }

  function applyTheme() {
    document.documentElement.dataset.theme = theme === "system"
      ? (system.matches ? "dark" : "light")
      : theme;
  }

  applyTheme();
  if (system.addEventListener) system.addEventListener("change", applyTheme);
  else system.addListener(applyTheme);
  document.addEventListener("DOMContentLoaded", () => {
    const picker = document.getElementById("workspace-theme");
    if (!picker) return;
    picker.value = theme;
    picker.addEventListener("change", () => {
      theme = picker.value;
      applyTheme();
      try {
        localStorage.setItem("bb-appearance", theme);
      } catch {
        // The choice is still applied on this page.
      }
    });
    document.querySelector(".mobile-nav")?.addEventListener("keydown", event => {
      if (event.key !== "Escape") return;
      const menu = event.currentTarget;
      menu.open = false;
      menu.querySelector("summary").focus();
    });
  });

  document.addEventListener("invalid", event => {
    for (let details = event.target.closest("details"); details; details = details.parentElement.closest("details")) {
      details.open = true;
    }
  }, true);
})();
