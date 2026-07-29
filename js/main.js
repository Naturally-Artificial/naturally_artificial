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
  // no cookie is used to single out a visitor across sites
  gtag("config", ANALYTICS_ID, { anonymize_ip: true });
}

function showConsentBanner() {
  if (document.querySelector(".consent")) return;

  const bar = document.createElement("div");
  bar.className = "consent";
  bar.setAttribute("role", "dialog");
  bar.setAttribute("aria-label", "Cookies");
  bar.innerHTML = `
    <p class="consent__text">We'd like to count visits to see which pages people actually read. That's all analytics cookies are used for here — nothing personal, nothing sold. <a href="privacy.html">How we handle data</a></p>
    <div class="consent__actions">
      <button class="consent__btn consent__btn--ghost" type="button" data-consent="denied">Decline</button>
      <button class="consent__btn" type="button" data-consent="granted">Accept</button>
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

// Contact forms: validate here, then hand the values to Netlify Forms.
// Netlify accepts a url-encoded POST to the site root as long as the body
// carries the form-name field; submitting over fetch keeps the visitor on the
// page so we can show our own success state instead of Netlify's.
document.querySelectorAll(".contact-form form").forEach((form) => {
  form.addEventListener("submit", async (e) => {
    e.preventDefault();

    const generalError = form.querySelector(".contact-form__error--general");
    const success = form.querySelector(".contact-form__success");
    let valid = true;

    form.querySelectorAll("input[required], textarea[required]").forEach((field) => {
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

    if (generalError) generalError.classList.toggle("visible", !valid);
    if (!valid) return;

    // Netlify only handles forms on a deployed site, so opening the files
    // locally skips the request and just shows the success state.
    const isLocal = ["localhost", "127.0.0.1", ""].includes(location.hostname);
    if (!isLocal) {
      try {
        const res = await fetch("/", {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams(new FormData(form)).toString(),
        });
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

// Telegram archive: reveal the feed in batches so the first paint stays light
const tgFeed = document.getElementById("tg-feed");
const tgMore = document.getElementById("tg-more");
if (tgFeed && tgMore) {
  const BATCH = 30;
  const posts = Array.from(tgFeed.querySelectorAll(".tg-post"));
  let shown = 0;

  const reveal = () => {
    posts.slice(shown, shown + BATCH).forEach((p) => p.classList.remove("tg-post--hidden"));
    shown = Math.min(shown + BATCH, posts.length);
    tgMore.hidden = shown >= posts.length;
    tgMore.textContent = `Show more posts (${posts.length - shown} left)`;
  };

  posts.forEach((p) => p.classList.add("tg-post--hidden"));
  reveal();
  tgMore.addEventListener("click", reveal);
}

// Mobile nav toggle
const navToggle = document.querySelector(".nav-toggle");
if (navToggle) {
  navToggle.addEventListener("click", () => {
    const open = document.body.classList.toggle("nav-open");
    navToggle.setAttribute("aria-expanded", String(open));
  });
  // close the menu when a nav link is chosen
  document.querySelectorAll(".site-header__nav a, .site-header__contact").forEach((link) => {
    link.addEventListener("click", () => {
      document.body.classList.remove("nav-open");
      navToggle.setAttribute("aria-expanded", "false");
    });
  });
}
