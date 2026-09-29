/* Inside the embedded gallery: tell the page that embeds us how tall we are,
 * so it can size the iframe to fit. Only the height is sent. */
(function () {
  "use strict";
  var id = document.documentElement.getAttribute("data-embed-id");
  if (!id || window.parent === window) return;
  var last = 0;
  function report() {
    // The content's own height: the document's scrollHeight is never less than
    // the frame, so it could make the widget grow but never shrink to fit.
    var height = Math.ceil(document.body.getBoundingClientRect().height);
    if (Math.abs(height - last) < 2) return;
    last = height;
    window.parent.postMessage({ type: "shipshape:height", id: id, height: height }, "*");
  }
  if (window.ResizeObserver) new ResizeObserver(report).observe(document.body);
  window.addEventListener("load", report);
  report();
})();
