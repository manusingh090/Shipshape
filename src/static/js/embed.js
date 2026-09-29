/* Shipshape embeddable gallery loader.
 *
 *   <div data-shipshape-gallery="event-slug" data-track="3" data-limit="12" data-theme="auto"></div>
 *   <script src="https://your-portal/embed.js" async></script>
 *
 * Every such div gets an iframe showing that event's public gallery. The
 * iframe tells this script its height, so it grows and shrinks with its
 * content instead of scrolling inside the host page. Nothing else passes
 * between the two pages.
 */
(function () {
  "use strict";
  var script = document.currentScript;
  var base = script ? script.src.replace(/\/embed\.js(\?.*)?$/, "") : "";
  var frames = {};
  var options = ["track", "tag", "q", "sort", "limit", "theme"];

  function mount(box, n) {
    if (box.getAttribute("data-shipshape-mounted")) return;
    box.setAttribute("data-shipshape-mounted", "1");
    var id = "shipshape-" + n + "-" + Math.random().toString(36).slice(2, 8);
    var params = ["id=" + encodeURIComponent(id)];
    options.forEach(function (name) {
      var value = box.getAttribute("data-" + name);
      if (value) params.push(name + "=" + encodeURIComponent(value));
    });
    var frame = document.createElement("iframe");
    frame.src = base + "/embed/events/" + encodeURIComponent(box.getAttribute("data-shipshape-gallery")) +
      "/gallery?" + params.join("&");
    frame.title = box.getAttribute("data-title") || "Hackathon projects";
    frame.loading = "lazy";
    frame.setAttribute("sandbox", "allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox");
    frame.style.width = "100%";
    frame.style.border = "0";
    frame.style.display = "block";
    frame.style.height = (box.getAttribute("data-height") || "640") + "px";
    box.appendChild(frame);
    frames[id] = frame;
  }

  window.addEventListener("message", function (event) {
    var data = event.data;
    if (!data || data.type !== "shipshape:height" || base.indexOf(event.origin) !== 0) return;
    var frame = frames[data.id];
    if (frame && event.source === frame.contentWindow && data.height > 0 && data.height < 20000) {
      frame.style.height = Math.ceil(data.height) + "px";
    }
  });

  var boxes = document.querySelectorAll("[data-shipshape-gallery]");
  for (var i = 0; i < boxes.length; i++) mount(boxes[i], i);
})();
