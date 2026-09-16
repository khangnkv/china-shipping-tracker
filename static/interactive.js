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
      // Mirrors quote_status() (app.py) exactly: no item cost yet -> always
      // "awaiting", regardless of shipping; the budget covers the ITEM
      // only, so shipping shows in the total but never enters the verdict.
      var item = parseFloat(itemInput.value);
      var ship = parseFloat(shipInput.value);
      var hasItem = !isNaN(item), hasShip = !isNaN(ship);
      var total = (hasItem ? item : 0) + (hasShip ? ship : 0);
      totalEl.textContent = (hasItem || hasShip) ? ("Total ฿" + total.toLocaleString(undefined, { maximumFractionDigits: 0 })) : "";
      if (!hasItem) {
        chipEl.textContent = "Awaiting quote";
        chipEl.style.background = "var(--border-soft)";
        chipEl.style.color = "var(--text-muted)";
      } else if (budgetNum === null || isNaN(budgetNum)) {
        chipEl.textContent = "Quoted";
        chipEl.style.background = "var(--border-soft)";
        chipEl.style.color = "var(--text-secondary)";
      } else if (item <= budgetNum) {
        chipEl.textContent = "Item within budget";
        chipEl.style.background = "var(--success-bg)";
        chipEl.style.color = "var(--success-text)";
      } else {
        chipEl.textContent = "Item over budget";
        chipEl.style.background = "var(--danger-bg)";
        chipEl.style.color = "var(--danger-text)";
      }
    }
    itemInput.addEventListener("input", recompute);
    shipInput.addEventListener("input", recompute);
  });

  // ---- Generic image dropzone (Request form's reference photo) ----------
  // Drag-and-drop with a preview, and a plain click-to-browse fallback for
  // free (the whole thing is a <label> wrapping the real file input, so a
  // click always works even if this script never runs). Dispatches
  // "dropzone:filled" so other widgets (the step tracker below) can react.
  document.querySelectorAll("[data-dropzone]").forEach(function (dz) {
    var input = dz.querySelector('input[type="file"]');
    var preview = dz.querySelector("[data-dropzone-preview]");
    var icon = dz.querySelector("[data-dropzone-icon]");
    var hint = dz.querySelector("[data-dropzone-hint]");
    if (!input) return;

    function setFile(file) {
      if (!file || file.type.indexOf("image/") !== 0) return;
      var dt = new DataTransfer();
      dt.items.add(file);
      input.files = dt.files;
      if (preview) {
        preview.src = URL.createObjectURL(file);
        preview.hidden = false;
      }
      if (icon) icon.hidden = true;
      if (hint) hint.textContent = file.name;
      dz.dispatchEvent(new CustomEvent("dropzone:filled", { bubbles: true, detail: { file: file } }));
    }

    input.addEventListener("change", function () {
      if (input.files[0]) setFile(input.files[0]);
    });
    ["dragover", "dragenter"].forEach(function (ev) {
      dz.addEventListener(ev, function (e) {
        e.preventDefault();
        dz.style.borderColor = "var(--brand)";
        dz.style.background = "var(--brand-tint-bg)";
      });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      dz.addEventListener(ev, function (e) {
        e.preventDefault();
        dz.style.borderColor = "";
        dz.style.background = "";
      });
    });
    dz.addEventListener("drop", function (e) {
      var file = e.dataTransfer.files[0];
      if (file) setFile(file);
    });
  });

  // ---- Request-form wizard: a REAL gate, not just a progress cue --------
  // The Jinja markup renders every fieldset visible and un-hidden (so a
  // no-JS visitor still gets one flowing page, exactly as before -- nothing
  // here is required to submit the form). When this runs, it hides steps
  // 2+ and only reveals the next one once the current step's required
  // fields pass native validation, driving the [data-step-tracker] dots
  // (STEP_NAMES below) to match the fieldset actually in view.
  var STEP_NAMES = { 1: "details", 2: "photo", 3: "budget" };
  var TRACKER_STYLE = {
    current: { dotBg: "var(--brand)", dotColor: "#fff", labelColor: "var(--text)" },
    done: { dotBg: "var(--brand-tint-bg)", dotColor: "var(--brand-text)", labelColor: "var(--text-secondary)" },
    upcoming: { dotBg: "var(--border-soft)", dotColor: "var(--text-faint2)", labelColor: "var(--text-faint)" },
  };

  document.querySelectorAll("[data-wizard]").forEach(function (form) {
    var steps = Array.prototype.slice.call(form.querySelectorAll("[data-wizard-step]"));
    if (steps.length < 2) return; // nothing to gate (e.g. the photo-less revisit form)
    var last = steps.length;
    var tracker = document.querySelector("[data-step-tracker]");
    var nextLabel = form.dataset.labelNext || "Next";
    var backLabel = form.dataset.labelBack || "Back";

    function stepEl(n) { return form.querySelector('[data-wizard-step="' + n + '"]'); }

    function setTracker(n) {
      if (!tracker) return;
      steps.forEach(function (el) {
        var stepNum = Number(el.dataset.wizardStep);
        var name = STEP_NAMES[stepNum];
        var row = tracker.querySelector('[data-step="' + name + '"]');
        if (!row) return;
        var state = stepNum === n ? "current" : (stepNum < n ? "done" : "upcoming");
        var style = TRACKER_STYLE[state];
        var dot = row.querySelector("[data-step-dot]");
        var label = row.querySelector("[data-step-label]");
        if (dot) { dot.style.background = style.dotBg; dot.style.color = style.dotColor; }
        if (label) label.style.color = style.labelColor;
      });
    }

    // steer=true moves focus/scroll to the new step -- used for an actual
    // Next/Back/Enter navigation, and for the initial mount ONLY when a
    // server-side validation bounce lands the visitor past step 1 (they need
    // to see the problem field immediately). A plain fresh page load stays
    // at step 1 with steer=false: audit review caught the original version
    // grabbing focus and smooth-scrolling on every load, including a normal
    // first visit, before the visitor had read anything on the page.
    function showStep(n, steer) {
      steps.forEach(function (el) {
        el.hidden = Number(el.dataset.wizardStep) !== n;
      });
      current = n;
      setTracker(n);
      if (!steer) return;
      var target = stepEl(n);
      var focusable = target && target.querySelector("input:not([type=hidden]), textarea, select");
      if (focusable) focusable.focus({ preventScroll: true });
      if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    function validateStep(n) {
      var el = stepEl(n);
      if (!el) return true;
      var fields = el.querySelectorAll("input, textarea, select");
      for (var i = 0; i < fields.length; i++) {
        if (!fields[i].checkValidity()) { fields[i].reportValidity(); return false; }
      }
      return true;
    }

    // Inject Back/Next controls at the end of each step -- keeping them out
    // of the Jinja markup means a no-JS visitor never sees an inert button.
    steps.forEach(function (el) {
      var n = Number(el.dataset.wizardStep);
      var nav = document.createElement("div");
      nav.className = "flex gap-2 pt-1";
      if (n > 1) {
        var back = document.createElement("button");
        back.type = "button";
        back.className = "btn flex-1 justify-center";
        back.textContent = backLabel;
        back.addEventListener("click", function () { showStep(n - 1, true); });
        nav.appendChild(back);
      }
      if (n < last) {
        var next = document.createElement("button");
        next.type = "button";
        next.className = "btn-primary flex-1 justify-center";
        next.textContent = nextLabel;
        next.addEventListener("click", function () { if (validateStep(n)) showStep(n + 1, true); });
        nav.appendChild(next);
      }
      if (nav.childNodes.length) {
        var submitBtn = el.querySelector('button[type="submit"]');
        if (submitBtn) el.insertBefore(nav, submitBtn); else el.appendChild(nav);
      }
    });

    // A field in an earlier step is still a text input, so pressing Enter
    // there would otherwise submit the form early (the browser's implicit
    // submission picks the form's one <button type=submit>, in the last
    // step, regardless of what's currently visible) -- redirect Enter to
    // "advance the current step" instead, same as clicking Next.
    form.addEventListener("keydown", function (e) {
      if (e.key !== "Enter" || e.target.tagName === "TEXTAREA") return;
      if (current >= last) return;
      e.preventDefault();
      if (validateStep(current)) showStep(current + 1, true);
    });

    var current = 1;
    var startAt = Number(form.dataset.wizardStart) || 1;
    showStep(Math.min(Math.max(startAt, 1), last), startAt > 1);
  });

  // ---- Generic "paste customer info, auto-split" helper -----------------
  // Shared by Orders' new-order form and Order detail's customer-edit form
  // (both call window.splitCustomerBlob with their own field ids) so the
  // fetch/fill logic lives in one place instead of being copy-pasted.
  window.splitCustomerBlob = async function (opts) {
    var blob = document.getElementById(opts.blobId).value.trim();
    var msg = document.getElementById(opts.msgId);
    var btn = document.getElementById(opts.btnId);
    if (!blob) { msg.textContent = "Paste something first."; return; }
    btn.disabled = true;
    msg.textContent = "Splitting…";
    try {
      var body = new URLSearchParams({ text: blob, csrf_token: opts.csrfToken });
      var r = await fetch(opts.url, { method: "POST", body: body });
      var d = await r.json();
      if (d.ok) {
        if (d.name) document.getElementById(opts.nameId).value = d.name;
        if (d.phone) document.getElementById(opts.phoneId).value = d.phone;
        if (d.address && opts.addressId) document.getElementById(opts.addressId).value = d.address;
        msg.textContent = "Filled — please review.";
      } else {
        msg.textContent = "Auto-split unavailable (no API key) — fill manually.";
      }
    } catch (e) {
      msg.textContent = "Split failed — fill manually.";
    }
    btn.disabled = false;
  };

  // ---- Copy-all-info button ----------------------------------------------
  // Assembles the text in [data-copy-source] into one block and copies it
  // via the Clipboard API; falls back to selecting a plain <textarea> when
  // that API isn't available (older browser, non-HTTPS context, etc.).
  document.querySelectorAll("[data-copy-all]").forEach(function (btn) {
    btn.addEventListener("click", async function () {
      var source = document.querySelector(btn.dataset.copyAll);
      if (!source) return;
      var text = source.value !== undefined ? source.value : source.textContent;
      var label = btn.textContent;
      try {
        await navigator.clipboard.writeText(text);
        btn.textContent = "Copied!";
      } catch (e) {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        try { document.execCommand("copy"); btn.textContent = "Copied!"; }
        catch (e2) { btn.textContent = "Copy failed — select manually"; }
        document.body.removeChild(ta);
      }
      setTimeout(function () { btn.textContent = label; }, 1800);
    });
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

  // ---- Trust-stat count-up (landing page) --------------------------------
  // [data-count-to] already holds its real final value in the markup (so a
  // no-JS visitor sees the correct number immediately, same as before this
  // existed) -- JS only replaces that with an animated count-up once it's
  // scrolled into view, then leaves the real value in place.
  if ("IntersectionObserver" in window && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    var countEls = document.querySelectorAll("[data-count-to]");
    if (countEls.length) {
      var countIo = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          countIo.unobserve(entry.target);
          var el = entry.target;
          var target = parseInt(el.dataset.countTo, 10);
          if (!target) return;
          var start = performance.now();
          var duration = 900;
          function step(now) {
            var progress = Math.min((now - start) / duration, 1);
            var eased = 1 - Math.pow(1 - progress, 3); // ease-out cubic
            el.textContent = Math.round(target * eased);
            if (progress < 1) requestAnimationFrame(step);
            else el.textContent = target;
          }
          requestAnimationFrame(step);
        });
      }, { threshold: 0.5 });
      countEls.forEach(function (el) { countIo.observe(el); });
    }
  }

  // ---- Submit-button loading feedback -------------------------------------
  // A form opts in with data-loading-text="Saving…" -- on submit, the
  // button that actually triggered it disables and swaps to that label, so
  // a slow connection doesn't read as "did my click even register?". Uses
  // event.submitter (not form.querySelector) because at least one form
  // here (order_detail's main form) nests a second submit button that's
  // logically tied elsewhere via form="otherFormId" -- querySelector would
  // match that one by DOM position regardless of which was really clicked.
  // Every form here full-page navigates on both success and failure, so
  // there's nothing to restore.
  document.querySelectorAll("form[data-loading-text]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      var btn = e.submitter;
      if (!btn || btn.tagName !== "BUTTON" || btn.disabled) return;
      btn.disabled = true;
      btn.textContent = form.dataset.loadingText;
    });
  });
})();
