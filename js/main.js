// ---------------------------------------------------------------------------
// Cookie consent. Analytics loads only after the visitor accepts — nothing is
// requested from Google before that, and declining leaves the site with no
// cookies at all. The choice itself lives in localStorage rather than a cookie,
// so storing it doesn't require the very consent it records.
// ---------------------------------------------------------------------------

// Put the Google Analytics Measurement ID here (looks like "G-XXXXXXXXXX").
// While it is empty the banner still works and nothing is ever loaded.
const ANALYTICS_ID = "";

const CONSENT_KEY = "na-cookie-consent";

const readConsent = () => {
  try {
    return localStorage.getItem(CONSENT_KEY);
  } catch {
    return null; // private mode, or storage blocked
  }
};

const saveConsent = (value) => {
  try {
    localStorage.setItem(CONSENT_KEY, value);
  } catch {
    /* nothing we can do — the visitor simply gets asked again */
  }
};

function loadAnalytics() {
  if (!ANALYTICS_ID || window.__analyticsLoaded) return;
  window.__analyticsLoaded = true;

  const tag = document.createElement("script");
  tag.async = true;
  tag.src = `https://www.googletagmanager.com/gtag/js?id=${ANALYTICS_ID}`;
  document.head.appendChild(tag);

  window.dataLayer = window.dataLayer || [];
  function gtag() { window.dataLayer.push(arguments); }
  window.gtag = gtag;
  gtag("js", new Date());
  gtag("config", ANALYTICS_ID, { anonymize_ip: true });
}

function showConsentBanner() {
  if (document.querySelector(".consent")) return;

  const bar = document.createElement("div");
  bar.className = "consent";
  bar.setAttribute("role", "dialog");
  bar.setAttribute("aria-label", "Cookies");
  bar.innerHTML = `
    <p class="consent__text">We'd like to count visits to see which pages people actually read. That's all analytics cookies are used for here — nothing personal, nothing sold. <a href="/privacy.html">How we handle data</a></p>
    <div class="consent__actions">
      <button class="btn btn--ghost btn--sm" type="button" data-consent="denied">Decline</button>
      <button class="btn btn--primary btn--sm" type="button" data-consent="granted">Accept</button>
    </div>`;

  bar.querySelectorAll("[data-consent]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const choice = btn.dataset.consent;
      saveConsent(choice);
      bar.remove();
      if (choice === "granted") loadAnalytics();
    });
  });

  document.body.appendChild(bar);
}

const consent = readConsent();
if (consent === "granted") {
  loadAnalytics();
} else if (consent !== "denied") {
  showConsentBanner();
}

// lets the privacy page offer a way to change the answer
const consentReset = document.getElementById("consent-reset");
if (consentReset) {
  consentReset.addEventListener("click", () => {
    try {
      localStorage.removeItem(CONSENT_KEY);
    } catch { /* ignore */ }
    showConsentBanner();
  });
}

// ---------------------------------------------------------------------------
// Header: transparent over the top of the page, a blurred bar once the visitor
// has scrolled past it.
// ---------------------------------------------------------------------------

const header = document.querySelector("[data-header]");
if (header) {
  const sync = () => header.classList.toggle("is-scrolled", window.scrollY > 12);
  sync();
  window.addEventListener("scroll", sync, { passive: true });
}

// Mobile menu: a full-screen sheet, closed by its button, by Escape, or by
// choosing somewhere to go.
const navToggle = document.querySelector(".nav-toggle");
if (navToggle) {
  const setOpen = (open) => {
    document.body.classList.toggle("nav-open", open);
    navToggle.setAttribute("aria-expanded", String(open));
    navToggle.setAttribute("aria-label", open ? "Close menu" : "Open menu");
  };
  navToggle.addEventListener("click", () => {
    setOpen(!document.body.classList.contains("nav-open"));
  });
  document.querySelectorAll(".site-nav a").forEach((link) => {
    link.addEventListener("click", () => setOpen(false));
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") setOpen(false);
  });
}

// ---------------------------------------------------------------------------
// Sections ease in as they are scrolled to. If the browser cannot observe
// that, everything is simply shown — the content matters more than the effect.
// ---------------------------------------------------------------------------

const revealables = document.querySelectorAll("[data-reveal]");
if (revealables.length) {
  if ("IntersectionObserver" in window) {
    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          entry.target.classList.add("is-in");
          io.unobserve(entry.target);
        });
      },
      { rootMargin: "0px 0px -8% 0px", threshold: 0.08 }
    );
    revealables.forEach((el) => io.observe(el));
  } else {
    revealables.forEach((el) => el.classList.add("is-in"));
  }
}

// Swap the poster for the real YouTube player once the visitor asks for it
document.querySelectorAll(".video-embed").forEach((wrap) => {
  const trigger = wrap.querySelector(".video-embed__trigger");
  if (!trigger) return;
  trigger.addEventListener("click", () => {
    const frame = document.createElement("iframe");
    frame.src =
      `https://www.youtube-nocookie.com/embed/${wrap.dataset.video}` +
      "?autoplay=1&rel=0&modestbranding=1";
    frame.title = wrap.querySelector(".video-embed__poster")?.alt || "Video";
    frame.allow =
      "accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share";
    frame.allowFullscreen = true;
    wrap.replaceChildren(frame);
  });
});

// ---------------------------------------------------------------------------
// Contact form: validate here, then hand the values to Netlify Forms.
// Netlify accepts a POST to the site root as long as the body carries the
// form-name field; submitting over fetch keeps the visitor on the page so we
// can show our own success state instead of Netlify's.
// ---------------------------------------------------------------------------

// The sponsor and guest pages link here with ?topic=…, so the form opens with
// the right subject already chosen and the visitor is not asked twice.
const TOPICS = {
  guest: "Suggest a guest",
  startup: "Pitch a startup",
  sponsors: "Advertising and collaborations",
  press: "Press",
};

const topicSelect = document.getElementById("contact-topic");
if (topicSelect) {
  const topicButtons = document.querySelectorAll("[data-topic]");
  const choose = (key) => {
    if (!TOPICS[key]) return;
    topicSelect.value = TOPICS[key];
    topicSelect.classList.remove("invalid");
    topicButtons.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.topic === key)));
  };

  choose(new URLSearchParams(location.search).get("topic"));

  topicButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      choose(btn.dataset.topic);
      document.getElementById("contact-name")?.focus({ preventScroll: true });
      document.querySelector(".contact-form")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
  });

  topicSelect.addEventListener("change", () => {
    const key = Object.keys(TOPICS).find((k) => TOPICS[k] === topicSelect.value);
    topicButtons.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.topic === key)));
  });
}

document.querySelectorAll(".contact-form form").forEach((form) => {
  form.addEventListener("submit", async (e) => {
    e.preventDefault();

    const generalError = form.querySelector(".contact-form__error--general");
    const success = form.querySelector(".contact-form__success");
    let valid = true;

    form
      .querySelectorAll("input[required], select[required], textarea[required]")
      .forEach((field) => {
        const isEmail = field.type === "email";
        const filled = field.value.trim().length > 0;
        const emailOk = !isEmail || /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(field.value.trim());
        const fieldOk = filled && emailOk;
        field.classList.toggle("invalid", !fieldOk);
        if (isEmail) {
          const emailError = field.parentElement.querySelector(".contact-form__error");
          if (emailError) emailError.classList.toggle("visible", filled && !emailOk);
        }
        if (!fieldOk) valid = false;
      });

    // Netlify caps the whole form request at 8 MB, so the file is held to 7 —
    // the remaining megabyte is for the other fields. Caught here rather than
    // at the server, where the visitor would only see a failure.
    const MAX_FILE = 7 * 1024 * 1024;
    const fileField = form.querySelector('input[type="file"]');
    const fileError = form.querySelector(".contact-form__error--file");
    const tooBig = !!(fileField && fileField.files[0] && fileField.files[0].size > MAX_FILE);
    if (fileField) fileField.classList.toggle("invalid", tooBig);
    if (fileError) fileError.classList.toggle("visible", tooBig);
    if (tooBig) valid = false;

    if (generalError) generalError.classList.toggle("visible", !valid);
    if (!valid) return;

    // Netlify only handles forms on a deployed site, so opening the files
    // locally skips the request and just shows the success state.
    const isLocal = ["localhost", "127.0.0.1", ""].includes(location.hostname);
    if (!isLocal) {
      try {
        // Sent as multipart, not url-encoded: an attachment cannot survive
        // url-encoding. The Content-Type is deliberately left unset so the
        // browser can add the boundary it generates.
        const res = await fetch("/", { method: "POST", body: new FormData(form) });
        if (!res.ok) throw new Error("send failed");
      } catch {
        if (generalError) {
          generalError.textContent = "Something went wrong — please try again.";
          generalError.classList.add("visible");
        }
        return;
      }
    }

    form.reset();
    if (success) success.classList.add("visible");
    const btn = form.querySelector('button[type="submit"]');
    if (btn) btn.disabled = true;
  });
});

// ---------------------------------------------------------------------------
// Blog index. Every entry is already a link in the markup, so nothing here
// costs a crawler anything: the list is shortened for readers, and the search
// box filters what is already on the page.
// ---------------------------------------------------------------------------

const blogItems = document.getElementById("blog-items");
if (blogItems) {
  const BATCH = 30;
  const entries = Array.from(blogItems.querySelectorAll(".blog-item"));
  const more = document.getElementById("blog-more");
  const search = document.getElementById("blog-search");
  const count = document.getElementById("blog-count");
  const empty = document.getElementById("blog-empty");
  // the text each entry is matched against, read once
  const haystack = entries.map((e) => e.textContent.toLowerCase());
  let shown = BATCH;

  const render = () => {
    const q = (search?.value || "").trim().toLowerCase();
    const matches = entries.filter((_, i) => !q || haystack[i].includes(q));
    entries.forEach((e) => { e.hidden = true; });
    // a search shows everything it finds; browsing shows a batch at a time
    const visible = q ? matches : matches.slice(0, shown);
    visible.forEach((e) => { e.hidden = false; });

    const left = matches.length - visible.length;
    if (more) {
      more.hidden = left <= 0;
      more.textContent = `Show more posts (${left} left)`;
    }
    if (count) {
      count.textContent = q
        ? `${matches.length} of ${entries.length} posts`
        : `${entries.length} posts`;
    }
    if (empty) empty.hidden = matches.length > 0;
  };

  more?.addEventListener("click", () => {
    shown += BATCH;
    render();
  });
  search?.addEventListener("input", render);
  render();
}
