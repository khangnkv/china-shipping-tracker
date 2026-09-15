// Small, dependency-free interactivity layer shared across templates.
// Every piece here is progressive enhancement: if this file fails to load,
// the page still works exactly as a normal form-driven Flask app (theme/
// language links still navigate, <details> still toggles, flash messages
// still show, quote totals still compute server-side on save).
(function () {
  "use strict";

  // ---- Instant theme switching (no page reload) -----------------------
  // theme_switch() (templates/_macros.html) renders a [data-theme-switch]
  // wrapper around three <a data-mode="light|auto|dark"> pills. The server
  // already rendered the correct active pill for this request; clicking
  // one updates the DOM immediately and persists the cookie in the
  // background via /set-theme/<mode> (app.py) without navigating.
  document.querySelectorAll("[data-theme-switch]").forEach(function (group) {
    group.querySelectorAll("a[data-mode]").forEach(function (a) {
      a.addEventListener("click", function (e) {
        e.preventDefault();
        var mode = a.dataset.mode;
        var root = document.documentElement;
        if (mode === "auto") root.removeAttribute("data-theme");
        else root.setAttribute("data-theme", mode);
        group.querySelectorAll("a[data-mode]").forEach(function (sib) {
          var active = sib.dataset.mode === mode;
          sib.style.background = active ? "var(--brand)" : "";
          sib.style.color = active ? "#fff" : "var(--text-muted)";
        });
        fetch(a.getAttribute("href"), { credentials: "same-origin", keepalive: true }).catch(function () {});
      });
    });
  });

  // ---- Toast-style flash messages --------------------------------------
  // Same get_flashed_messages() data, just auto-dismissing with a fade
  // instead of sitting as a static banner.
  document.querySelectorAll(".js-toast").forEach(function (el, i) {
    el.style.transition = "opacity 300ms ease, transform 300ms ease";
    setTimeout(function () {
      el.style.opacity = "0";
      el.style.transform = "translateY(-6px)";
      setTimeout(function () { el.remove(); }, 320);
    }, 4200 + i * 300);
  });

  // ---- Smooth <details> accordions (Inbox threads) ----------------------
  // Keeps <details>/<summary> as the source of truth (no-JS fallback: the
  // server-rendered `open` attribute still controls initial state and the
  // browser's native toggle still works if this never runs); with JS, the
  // summary click is intercepted so the body animates via max-height.
  document.querySelectorAll("details.js-accordion").forEach(function (details) {
    var summary = details.querySelector(":scope > summary");
    var body = details.querySelector(":scope > .js-accordion-body");
    if (!summary || !body) return;
    body.style.overflow = "hidden";
    body.style.transition = "max-height 220ms ease";
    body.style.maxHeight = details.open ? body.scrollHeight + "px" : "0px";
    summary.addEventListener("click", function (e) {
      e.preventDefault();
      if (details.open) {
        body.style.maxHeight = body.scrollHeight + "px";
        requestAnimationFrame(function () { body.style.maxHeight = "0px"; });
        setTimeout(function () { details.open = false; }, 220);
      } else {
        details.open = true;
        body.style.maxHeight = body.scrollHeight + "px";
      }
    });
  });

  // ---- Live quote total (admin Requests queue) --------------------------
  // Recomputes the total + Within/Over-budget chip client-side as the cost
  // fields are typed, mirroring quote_status() (app.py) so the admin sees
  // the outcome before saving -- the actual save still goes through the
  // normal form POST to /admin/requests/<id>/quote.
  document.querySelectorAll("[data-quote-form]").forEach(function (form) {
    var itemInput = form.querySelector('[name="item_cost"]');
    var shipInput = form.querySelector('[name="shipping_cost"]');
    var totalEl = form.querySelector("[data-quote-total]");
    var chipEl = form.querySelector("[data-quote-chip]");
    var budgetNum = form.dataset.budgetNumber ? parseFloat(form.dataset.budgetNumber) : null;
    if (!itemInput || !shipInput || !totalEl || !chipEl) return;
    function recompute() {
      var item = parseFloat(itemInput.value);
      var ship = parseFloat(shipInput.value);
      var hasItem = !isNaN(item), hasShip = !isNaN(ship);
      if (!hasItem && !hasShip) {
        totalEl.textContent = "";
        chipEl.textContent = "Awaiting quote";
        chipEl.style.background = "var(--border-soft)";
        chipEl.style.color = "var(--text-muted)";
        return;
      }
      var total = (hasItem ? item : 0) + (hasShip ? ship : 0);
      totalEl.textContent = "Total ฿" + total.toLocaleString(undefined, { maximumFractionDigits: 0 });
      if (budgetNum === null || isNaN(budgetNum)) {
        chipEl.textContent = "Quoted";
        chipEl.style.background = "var(--border-soft)";
        chipEl.style.color = "var(--text-secondary)";
      } else if (total <= budgetNum) {
        chipEl.textContent = "Within budget";
        chipEl.style.background = "#ecfdf5";
        chipEl.style.color = "#059669";
      } else {
        chipEl.textContent = "Over budget";
        chipEl.style.background = "#fef2f2";
        chipEl.style.color = "#dc2626";
      }
    }
    itemInput.addEventListener("input", recompute);
    shipInput.addEventListener("input", recompute);
  });

  // ---- Landing page scroll-reveal ---------------------------------------
  // Progressive enhancement only -- .reveal elements are fully visible by
  // default (see the .reveal CSS rule) so nothing breaks without JS.
  if ("IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("reveal-visible");
          io.unobserve(entry.target);
        }
      });
    }, { threshold: 0.15 });
    document.querySelectorAll(".reveal").forEach(function (el) { io.observe(el); });
  }
})();
