// Live progress updater for the quiz form. Loaded and templated by
// ui.progress_script(): __TOTAL__ is replaced with the attempt size before the
// string is handed to st.components.v1.html(..., height=0).
//
// The component iframe is same-origin, so window.parent.document reaches the
// app page. On every change event (capturing delegation on the parent document
// — survives Streamlit node churn) it counts answered radiogroups in the form,
// rewrites #quiz-progress-label, resizes #quiz-progress-fill, and unhides the
// progress block. Bails silently if any target is absent. Cosmetic only —
// server-side validation stays the enforcement.
(function () {
  const TOTAL = __TOTAL__;
  const P = window.parent.document;
  function update() {
    try {
      const label = P.getElementById("quiz-progress-label");
      const fill = P.getElementById("quiz-progress-fill");
      if (!label || !fill) return;
      let answered = 0;
      P.querySelectorAll('[data-testid="stForm"] [role="radiogroup"]').forEach(function (g) {
        if (g.querySelector("input:checked")) answered += 1;
      });
      label.textContent = answered + " / " + TOTAL + " answered";
      fill.style.width = (100 * answered / TOTAL) + "%";
      const block = fill.closest(".progress-block");
      if (block) block.removeAttribute("hidden");
    } catch (e) { /* cosmetic only */ }
  }
  const PIN_TOP = 60; // px: pin threshold = where the panel content sits once pinned
  function sticky() {
    // Keep the whole progress panel (title, caption, counter, bar) visible
    // while the questions scroll. position:sticky loses to Streamlit's
    // overflow wrappers, so pin manually: when the panel's keyed anchor
    // scrolls past the header, switch the panel to position:fixed sized to
    // the anchor, and hold the anchor's height so layout doesn't jump.
    // Re-applied on every scroll (capturing, so inner scrollers fire it too),
    // which also self-heals after Streamlit re-renders. Cosmetic only.
    try {
      const anchor = P.querySelector('[class*="st-key-progress-sticky"]');
      const block = anchor && anchor.querySelector(".progress-panel");
      if (!anchor || !block) return;
      const r = anchor.getBoundingClientRect();
      if (r.top < PIN_TOP) {
        if (!block.classList.contains("progress-pinned")) {
          // Measure BEFORE pinning: the pinned class adds top padding that
          // would otherwise inflate minHeight and jump the layout below.
          anchor.style.minHeight = block.offsetHeight + "px";
          block.classList.add("progress-pinned");
        }
        // top 0: the pinned panel's own padding-top clears the header, and its
        // CSS backdrop covers the full strip above so nothing scrolls past it.
        block.style.top = "0px";
        block.style.left = r.left + "px";
        block.style.width = r.width + "px";
      } else {
        block.classList.remove("progress-pinned");
        block.style.top = block.style.left = block.style.width = "";
        anchor.style.minHeight = "";
      }
    } catch (e) { /* cosmetic only */ }
  }
  P.addEventListener("change", update, true);
  P.addEventListener("change", sticky, true);
  P.addEventListener("scroll", sticky, true);
  window.parent.addEventListener("resize", sticky);
  update();
  sticky();
})();
