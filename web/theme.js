// Day/night theme: applied before first paint (this file is loaded in <head>), saved per browser.
(function () {
  "use strict";
  var KEY = "timelineTheme";
  var root = document.documentElement;
  function saved() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function apply(theme) {
    if (theme === "light") root.setAttribute("data-theme", "light"); else root.removeAttribute("data-theme");
    var sw = document.getElementById("theme-toggle");
    if (sw) {
      var label = theme === "light" ? "Switch to night mode" : "Switch to day mode";
      sw.setAttribute("aria-label", label);
      sw.title = label;
    }
  }
  apply(saved() === "dark" ? "dark" : "light");
  document.addEventListener("DOMContentLoaded", function () {
    var sw = document.getElementById("theme-toggle");
    if (!sw) return;
    apply(root.getAttribute("data-theme") === "light" ? "light" : "dark");
    sw.addEventListener("click", function () {
      var next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
      apply(next);
      try { localStorage.setItem(KEY, next); } catch (e) { /* storage blocked */ }
    });
  });
})();
