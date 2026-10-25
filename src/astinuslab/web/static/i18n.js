// src/astinuslab/web/static/i18n.js
// Reads the server-serialized translations from #al-i18n and exposes a tiny
// lookup. Loaded in <head> AFTER #al-i18n and BEFORE any page script, so both
// classic-deferred and module scripts can call window.t(...).
(function () {
  let dict = {};
  try {
    dict = JSON.parse(document.getElementById("al-i18n")?.textContent || "{}");
  } catch (e) {
    dict = {};
  }
  window.AL_T = dict;
  window.t = function (key, fallback) {
    if (Object.prototype.hasOwnProperty.call(dict, key)) return dict[key];
    return fallback != null ? fallback : key;
  };
})();
