// Small progressive enhancements. Every page works without this file.
(function () {
  "use strict";

  // Times are printed in the event's zone. Add the reader's own local time
  // as a tooltip so nobody has to do time zone sums at 3am.
  var local = new Intl.DateTimeFormat(undefined, {
    weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZoneName: "short"
  });
  function localTimes(root) {
    root.querySelectorAll("time[data-local]").forEach(function (el) {
      var when = new Date(el.getAttribute("datetime"));
      if (!isNaN(when)) el.title = "Your time: " + local.format(when);
    });
  }
  localTimes(document);

  // Live regions (the judging dashboard): re-read a server-rendered fragment
  // every few seconds while the tab is visible. The server does the counting;
  // this only swaps the HTML in.
  document.querySelectorAll("[data-live]").forEach(function (box) {
    var every = (parseInt(box.getAttribute("data-live-every"), 10) || 10) * 1000;
    function refresh() {
      if (document.visibilityState !== "visible") return;
      fetch(box.getAttribute("data-live"), { credentials: "same-origin", headers: { "Accept": "text/html" } })
        .then(function (response) { return response.ok ? response.text() : Promise.reject(response.status); })
        .then(function (html) { box.innerHTML = html; localTimes(box); })
        .catch(function () { /* keep showing the last good numbers */ });
    }
    setInterval(refresh, every);
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") refresh();
    });
  });

  // Scorecard: show the weighted total as marks are picked. The server
  // recomputes it on save; this is only a preview.
  document.querySelectorAll("form[data-scorecard]").forEach(function (form) {
    var out = form.querySelector("[data-total]");
    if (!out) return;
    function recompute() {
      var sum = 0, weight = 0, missing = false;
      form.querySelectorAll("[data-criterion]").forEach(function (group) {
        var w = parseFloat(group.getAttribute("data-weight")) || 0;
        var picked = group.querySelector("input:checked");
        if (picked) { sum += w * parseFloat(picked.value); weight += w; } else { missing = true; }
      });
      out.textContent = missing || !weight ? "…" : (sum / weight).toFixed(2);
    }
    form.addEventListener("change", recompute);
    recompute();
  });

  // Ballot: keep the credit meter honest as votes are picked, and grey out
  // choices that no longer fit in what's left. The server checks the budget
  // again on save; this is only a guide.
  document.querySelectorAll("form[data-ballot][data-quadratic='1']").forEach(function (form) {
    var credits = parseInt(form.getAttribute("data-credits"), 10) || 0;
    var rows = form.querySelectorAll("[data-ballot-row]");
    var left = form.querySelector("[data-left]"), meter = form.querySelector("[data-meter]");
    var over = form.querySelector("[data-over]");
    function votesIn(row) {
      var field = row.querySelector("input[type=radio][data-votes]:checked") ||
        row.querySelector("input[type=number][data-votes]");
      var n = field ? parseInt(field.value, 10) : 0;
      return isNaN(n) || n < 0 ? 0 : n;
    }
    function recompute() {
      var spent = 0;
      rows.forEach(function (row) {
        var n = votesIn(row), cost = row.querySelector("[data-cost]");
        spent += n * n;
        row.classList.toggle("has-votes", n > 0);
        if (cost) cost.textContent = n ? (n * n) + (n * n === 1 ? " credit" : " credits") : "";
      });
      if (left) left.textContent = credits - spent;
      if (meter) meter.value = Math.min(spent, credits);
      if (over) over.hidden = spent <= credits;
      form.classList.toggle("is-over", spent > credits);
      rows.forEach(function (row) {
        var room = credits - spent + votesIn(row) * votesIn(row);
        row.querySelectorAll("input[type=radio][data-votes]").forEach(function (input) {
          var v = parseInt(input.value, 10);
          input.parentNode.classList.toggle("too-dear", v * v > room);
        });
      });
    }
    form.addEventListener("change", recompute);
    form.addEventListener("input", recompute);
    recompute();
  });

  // Show a part of a form only when a radio choice calls for it, e.g. the
  // track picker when "one track" is chosen. data-show-when="name:value".
  document.querySelectorAll("[data-show-when]").forEach(function (el) {
    var rule = el.getAttribute("data-show-when").split(":");
    var form = el.closest("form");
    if (!form) return;
    function sync() {
      var picked = form.querySelector('input[name="' + rule[0] + '"]:checked');
      el.hidden = !(picked && picked.value === rule[1]);
    }
    form.addEventListener("change", sync);
    sync();
  });

  // Countdowns. The server decides what is open; this only keeps the
  // number on screen honest between page loads.
  function human(ms) {
    var s = Math.max(0, Math.floor(ms / 1000));
    var d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d >= 2) return d + " days";
    if (d === 1) return "1 day, " + h + " hour" + (h === 1 ? "" : "s");
    if (h > 0) return h + " h " + m + " min";
    if (m > 0) return m + " min " + (s % 60) + " s";
    return (s % 60) + " seconds";
  }
  var counters = document.querySelectorAll("[data-countdown]");
  function tick() {
    var now = Date.now();
    counters.forEach(function (el) {
      var target = Date.parse(el.getAttribute("data-countdown"));
      if (isNaN(target)) return;
      if (target <= now) {
        el.textContent = el.getAttribute("data-done") || "closed";
        el.classList.add("is-done");
        var banner = document.getElementById("deadline-passed");
        if (banner) banner.hidden = false;
      } else {
        el.textContent = human(target - now);
      }
    });
  }
  if (counters.length) { tick(); setInterval(tick, 1000); }

  // Copy buttons (invite links).
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var field = document.querySelector(btn.getAttribute("data-copy"));
      if (!field) return;
      var original = btn.textContent;
      function done(label) {
        btn.textContent = label;
        setTimeout(function () { btn.textContent = original; }, 1600);
      }
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(field.value).then(function () { done("Copied"); }, function () {
          field.select(); done("Press Ctrl+C");
        });
      } else {
        field.select(); done("Press Ctrl+C");
      }
    });
  });

  // Ask before destructive actions.
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.getAttribute("data-confirm"))) event.preventDefault();
    });
  });

  // Filters apply as soon as a select changes.
  document.querySelectorAll("select[data-autosubmit]").forEach(function (select) {
    select.addEventListener("change", function () {
      if (select.form.requestSubmit) select.form.requestSubmit(); else select.form.submit();
    });
  });

  // Tab bars scroll sideways when they don't fit. Open them at the page
  // you're on, so a tab near the end (Activity, CSV exports) isn't hidden
  // past the edge.
  document.querySelectorAll("nav.tabs, nav.subtabs").forEach(function (nav) {
    var here = nav.querySelector("[aria-current='page']");
    if (!here) return;
    var left = here.getBoundingClientRect().left - nav.getBoundingClientRect().left + nav.scrollLeft;
    if (left + here.offsetWidth <= nav.clientWidth) return;
    nav.scrollLeft = Math.max(0, left - (nav.clientWidth - here.offsetWidth) / 2);
  });

  // Custom question editor: the options box only matters for "pick one".
  var kind = document.getElementById("id_kind");
  var options = document.getElementById("id_options");
  if (kind && options) {
    var box = options.closest(".field");
    var sync = function () { if (box) box.hidden = kind.value !== "choice"; };
    kind.addEventListener("change", sync);
    sync();
  }

  // Warn before leaving the submission form with unsaved edits.
  var form = document.querySelector("form[data-warn-unsaved]");
  if (form) {
    var dirty = false;
    form.addEventListener("input", function () { dirty = true; });
    form.addEventListener("change", function () { dirty = true; });
    form.addEventListener("submit", function () { dirty = false; });
    window.addEventListener("beforeunload", function (event) {
      if (dirty) { event.preventDefault(); event.returnValue = ""; }
    });
  }
})();
