// Theme initialization. Runs synchronously in <head> to avoid flash-of-unstyled-content.
(function () {
  const savedTheme = localStorage.getItem("al_theme") || "dark";
  if (savedTheme === "light") document.documentElement.dataset.theme = "light";
})();

function toggleTheme() {
  const isLight = document.documentElement.dataset.theme === "light";
  const newTheme = isLight ? "dark" : "light";

  if (newTheme === "light") document.documentElement.dataset.theme = "light";
  else delete document.documentElement.dataset.theme;

  localStorage.setItem("al_theme", newTheme);

  const btn = document.querySelector("[data-theme-toggle]");
  if (btn) btn.setAttribute("aria-pressed", String(newTheme === "light"));
}

document.addEventListener("DOMContentLoaded", () => {
  const themeBtn = document.querySelector("[data-theme-toggle]");
  if (themeBtn) {
    themeBtn.setAttribute(
      "aria-pressed",
      String(document.documentElement.dataset.theme === "light"),
    );
    themeBtn.addEventListener("click", toggleTheme);
  }

  const langSelect = document.querySelector("[data-lang-select]");
  if (langSelect)
    langSelect.addEventListener("change", (event) => {
      const form = event.currentTarget.form;
      if (form) form.submit();
    });
});
