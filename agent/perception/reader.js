// One pass over the visible DOM. Returns a plain-object snapshot that the
// Python side parses into a typed Observation.
//
// The reader mutates the document lightly: each enumerated control receives a
// `data-agent-ref` attribute so Playwright can locate it later without needing
// XPath or a brittle CSS path. Attributes are re-stamped on every read so the
// page never accumulates state, and only interactive elements are stamped so
// mutation observers on layout roots stay quiet.

(({ include_text }) => {
  const INTERACTIVE_ROLES = new Set([
    "button", "link", "checkbox", "radio", "switch", "tab",
    "menuitem", "menuitemradio", "option", "gridcell",
    "combobox", "textbox", "searchbox", "spinbutton",
  ]);

  const LANDMARK_SELECTOR = [
    '[role="dialog"]', '[role="search"]', '[role="banner"]',
    '[role="navigation"]', '[role="main"]', '[role="contentinfo"]',
    '[role="complementary"]', 'dialog', 'header', 'nav', 'main',
    'aside', 'footer', 'form',
  ].join(",");

  const LANDMARK_NAMES = {
    banner: "header",
    navigation: "nav",
    contentinfo: "footer",
    complementary: "aside",
  };

  const isVisible = (el) => {
    if (!el || !el.isConnected) return false;
    if (el.closest('[aria-hidden="true"],[inert]')) return false;
    return el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
  };

  const isDisabled = (el) => {
    if (el.matches(":disabled")) return true;
    if (el.closest('[aria-disabled="true"]')) return true;
    return false;
  };

  const centerInViewport = (rect) => {
    // Only enumerate elements the user can actually see. Off-screen controls
    // (below the fold, or in a scrolled panel out of view) cannot help the
    // current decision and drown the action space — a 250-element page
    // with newsletter signups and side-nav links has the model picking
    // "Email address" and "Subscribe" instead of the product it wanted.
    // Old jevis had exactly this check for exactly this reason.
    const cx = rect.x + rect.w / 2;
    const cy = rect.y + rect.h / 2;
    return cx >= 0 && cx < innerWidth && cy >= 0 && cy < innerHeight;
  };

  const roleOf = (el) => {
    const explicit = el.getAttribute && el.getAttribute("role");
    if (explicit && INTERACTIVE_ROLES.has(explicit)) return explicit;
    const tag = el.tagName;
    if (tag === "BUTTON" || tag === "SUMMARY") return "button";
    if (tag === "A" && el.hasAttribute("href")) return "link";
    if (tag === "SELECT") return "select";
    if (tag === "TEXTAREA" || el.isContentEditable) return "textbox";
    if (tag === "INPUT") {
      const type = (el.type || "text").toLowerCase();
      if (type === "checkbox" || type === "radio") return type;
      if (type === "button" || type === "submit" || type === "reset" || type === "image") return "button";
      if (type === "search") return "searchbox";
      if (type === "number") return "spinbutton";
      if (["text", "email", "url", "tel", "password"].includes(type)) return "textbox";
    }
    return null;
  };

  const nameOf = (el, seen = new Set()) => {
    if (!el || seen.has(el)) return "";
    seen.add(el);
    const labelledBy = (el.getAttribute && el.getAttribute("aria-labelledby") || "")
      .split(/\s+/).filter(Boolean).map((id) => nameOf(document.getElementById(id), seen))
      .filter(Boolean).join(" ");
    if (labelledBy) return labelledBy.trim();
    const ariaLabel = el.getAttribute && el.getAttribute("aria-label");
    if (ariaLabel) return ariaLabel.trim();
    if (el.labels && el.labels.length) {
      const joined = [...el.labels].map((l) => nameOf(l, seen)).filter(Boolean).join(" ").trim();
      if (joined) return joined;
    }
    if (el.type === "button" || el.type === "submit" || el.type === "reset") {
      if (el.value) return el.value.trim();
    }
    const alt = el.getAttribute && el.getAttribute("alt");
    if (alt) return alt.trim();
    if (el.tagName !== "INPUT") {
      const text = [...el.childNodes].map((n) => {
        if (n.nodeType === 3) return n.textContent;
        if (n.nodeType === 1 && n.getAttribute("aria-hidden") !== "true") return nameOf(n, seen);
        return "";
      }).join(" ").replace(/\s+/g, " ").trim();
      if (text) return text;
    }
    return (el.getAttribute && (el.getAttribute("title") || el.getAttribute("placeholder"))) || "";
  };

  const sectionOf = (el) => {
    const landmark = el.closest(LANDMARK_SELECTOR);
    if (!landmark) return null;
    const role = landmark.getAttribute("role");
    if (role) return LANDMARK_NAMES[role] || role;
    return landmark.tagName.toLowerCase();
  };

  const contextOf = (el, name) => {
    // Disambiguating text from an ancestor card, stripped of the control's
    // own label so we return only the surrounding info (product name, price,
    // rating). Walks up until the ancestor holds more than a few interactive
    // siblings — past that it is a toolbar, whose text describes nothing
    // specific to this control.
    for (let node = el.parentElement, hops = 0; node && hops < 4; node = node.parentElement, hops++) {
      const siblings = node.querySelectorAll("a,button,input,select,textarea,[role='button']").length;
      if (siblings > 3) break;
      const whole = (node.innerText || "").replace(/\s+/g, " ").trim();
      if (!whole || whole.length > 300) continue;
      if (whole.toLowerCase() === (name || "").toLowerCase()) continue;
      const rest = name
        ? whole.split(name).join(" ").replace(/\s+/g, " ").trim()
        : whole;
      if (rest.length > 2) return rest.slice(0, 200);
    }
    return null;
  };

  const boundsOf = (el) => {
    const r = el.getBoundingClientRect();
    return { x: r.x, y: r.y, w: r.width, h: r.height };
  };

  const visibleTextOf = (el) => {
    // What a sighted user actually sees on this control, ignoring the ARIA
    // rule that hides `aria-hidden="true"` subtrees. Sites use aria-hidden
    // as a visual-styling toggle (e.g. an autocomplete row's completion
    // delta appears in a highlight span marked aria-hidden), so the
    // accessible name computed for a screen reader loses information the
    // agent needs. Returned separately from `name` so the two views stay
    // distinct — model reconciles.
    if (!el || el.tagName === "INPUT") return "";
    const raw = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
    return raw.slice(0, 200);
  };

  const stateOf = (el, role) => {
    const state = {};
    if (role === "checkbox" || role === "radio" || role === "switch") {
      state.checked = el.checked === true || el.getAttribute("aria-checked") === "true";
    }
    if (role === "option" || role === "tab" || role === "menuitem") {
      state.selected = el.getAttribute("aria-selected") === "true";
    }
    if (el.hasAttribute && el.hasAttribute("aria-expanded")) {
      state.expanded = el.getAttribute("aria-expanded") === "true";
    }
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
      state.value = el.value || null;
      state.editable = !el.readOnly && !el.disabled;
    } else if (el.tagName === "SELECT") {
      state.value = el.value || null;
      state.editable = !el.disabled;
    } else if (el.isContentEditable) {
      state.value = (el.textContent || "").trim() || null;
      state.editable = true;
    }
    return state;
  };

  const optionsOf = (el) => {
    if (el.tagName !== "SELECT") return [];
    return [...el.options].map((option) => ({
      label: (option.label || option.textContent || "").trim(),
      value: option.value,
      disabled: option.disabled || (option.parentElement &&
        option.parentElement.tagName === "OPTGROUP" && option.parentElement.disabled),
    }));
  };

  const selectorFor = (index) => `[data-agent-ref="e${index}"]`;

  const hashString = (input) => {
    // Small djb2. Enough to detect state changes without a crypto import.
    let hash = 5381;
    for (let i = 0; i < input.length; i++) hash = ((hash * 33) ^ input.charCodeAt(i)) & 0xffffffff;
    return (hash >>> 0).toString(36);
  };

  const guardOf = (el, snapshot) => hashString(JSON.stringify([
    snapshot.role, snapshot.name, snapshot.value ?? null,
    snapshot.checked ?? null, snapshot.selected ?? null,
    snapshot.expanded ?? null, snapshot.editable ?? false,
    el.getAttribute("aria-disabled") || "", el.matches(":disabled"),
    (el.getAttribute("href") || "").slice(0, 200),
  ]));

  // ---- Scroll area ------------------------------------------------------
  // What a scroll action should move. An open modal dialog blocks the page
  // behind it (sites lock body scroll while it is open), so the dialog's own
  // scroller wins. Without a modal the page scrolls, unless the page cannot
  // and an app-style panel does. Scroll state is measured on that area, not
  // on document.body: a DoorDash item dialog scrolls a 585px box over a
  // locked 9943px body, and its required options sit below that box's fold.
  const SCROLLABLE_Y = new Set(["auto", "scroll", "overlay"]);
  const canScrollY = (el) => el.scrollHeight > el.clientHeight + 4
    && SCROLLABLE_Y.has(getComputedStyle(el).overflowY);
  const largestScroller = (root) => {
    let best = null;
    let bestArea = 0;
    for (const el of [root, ...root.querySelectorAll("*")]) {
      if (!canScrollY(el) || !isVisible(el)) continue;
      const area = el.clientWidth * el.clientHeight;
      if (area > bestArea) {
        best = el;
        bestArea = area;
      }
    }
    return best;
  };
  const centerIn = (rect, area) => {
    const cx = rect.x + rect.w / 2;
    const cy = rect.y + rect.h / 2;
    return cx >= area.x && cx < area.x + area.w && cy >= area.y && cy < area.y + area.h;
  };
  // The topmost open modal. `aria-modal` or a native modal <dialog> only: a
  // non-modal role="dialog" (a chat widget, a toast) leaves the page usable.
  const modal = [...document.querySelectorAll('[aria-modal="true"],dialog')]
    .filter((el) => (el.getAttribute("aria-modal") === "true" || el.matches(":modal")) && isVisible(el))
    .pop() || null;
  const doc = document.scrollingElement || document.documentElement;
  const pageScrolls = doc.scrollHeight > innerHeight + 4
    && getComputedStyle(doc).overflowY !== "hidden"
    && !(document.body && getComputedStyle(document.body).overflowY === "hidden");
  let scrollArea = "page";
  let scroller = null;
  if (modal) {
    scrollArea = "dialog";
    scroller = largestScroller(modal);
  } else if (!pageScrolls && document.body) {
    scroller = largestScroller(document.body);
    if (scroller) scrollArea = "panel";
  }
  // The part of the scroller inside the viewport: where a wheel must land,
  // and the box a control's centre must sit in to be seen.
  let scrollBox = { x: 0, y: 0, w: innerWidth, h: innerHeight };
  if (scroller) {
    const r = scroller.getBoundingClientRect();
    const x = Math.max(r.x, 0);
    const y = Math.max(r.y, 0);
    scrollBox = { x, y, w: Math.min(r.right, innerWidth) - x, h: Math.min(r.bottom, innerHeight) - y };
  }
  const scrollTop = scroller ? scroller.scrollTop : (modal ? 0 : scrollY);

  // Clear previous stamps so the page never accumulates them.
  for (const stamped of document.querySelectorAll("[data-agent-ref]")) {
    stamped.removeAttribute("data-agent-ref");
  }

  const roots = [
    'a[href]', 'button', 'input:not([type="hidden"])', 'textarea', 'select', 'summary',
    '[contenteditable="true"]',
    ...[...INTERACTIVE_ROLES].map((role) => `[role="${role}"]`),
  ].join(",");

  const seen = new Set();
  // Enumerate viewport controls in DOM order up to MAX_ELEMENTS. The cap
  // protects Jev from oversized payloads on big pages (Amazon results,
  // YouTube). Old jevis used the same cap for the same reason.
  const MAX_ELEMENTS = 250;
  const elements = [];
  const guards = {};
  let index = 0;
  for (const el of document.querySelectorAll(roots)) {
    if (elements.length >= MAX_ELEMENTS) break;
    if (seen.has(el)) continue;
    // An open modal makes the page behind it inert: a click there lands on
    // the backdrop. Offering those controls let a run "add" a background item.
    if (modal && !modal.contains(el)) continue;
    if (!isVisible(el)) continue;
    if (isDisabled(el)) continue;
    if (el.type === "password" || el.type === "file") continue;
    const role = roleOf(el);
    if (!role) continue;
    if (role === "gridcell" && el.querySelector('button,[role="button"]')) continue;
    const bounds = boundsOf(el);
    if (bounds.w <= 0 || bounds.h <= 0) continue;
    if (!centerInViewport(bounds)) continue;
    // Inside the viewport but scrolled out of its own box: hidden, not offered.
    if (scroller && scroller.contains(el) && !centerIn(bounds, scrollBox)) continue;
    seen.add(el);
    // Never drop an element for lacking a computed accessible name: on a
    // mid-hydration page (Walmart search results, Amazon), product buttons
    // render before ARIA names are wired up, and dropping them shrinks the
    // observation to a nav-and-header shell. Old jevis has the same rule
    // (label = name || role) — the fallback keeps the element in the space
    // so the model at least sees "how many buttons live here", and a
    // subsequent read after names settle upgrades the label.
    const name = ((nameOf(el) || "").replace(/\s+/g, " ").trim().slice(0, 200)) || role;
    const state = stateOf(el, role);
    const ref = selectorFor(index++);
    el.setAttribute("data-agent-ref", `e${index - 1}`);
    const opens = el.getAttribute && el.getAttribute("aria-haspopup");
    const visible = visibleTextOf(el);
    const snapshot = {
      ref, role, name, bounds,
      section: sectionOf(el),
      context: contextOf(el, name),
      ...state,
      options: optionsOf(el),
    };
    if (opens) snapshot.opens = opens;
    if (visible && visible !== name && visible.toLowerCase() !== name.toLowerCase()) {
      snapshot.visible = visible;
    }
    guards[ref] = guardOf(el, snapshot);
    elements.push(snapshot);
  }

  const markerParts = [
    location.href,
    scrollX, scrollY, innerWidth, innerHeight,
    // A scroll inside a dialog or panel moves this and nothing above it.
    scrollArea, Math.round(scrollTop),
    document.title,
    elements.length,
    ...elements.map((e) => e.role + "|" + e.name).slice(0, 32),
  ];
  const marker = hashString(markerParts.join("~"));

  const text = include_text
    ? (document.body ? document.body.innerText.replace(/\s+/g, " ").trim().slice(0, 6000) : "")
    : "";

  // `document.body` can be null on interstitial redirect pages that ship
  // only a <head> before the JS reroute fires. Guard every access.
  const bodyHeight = (document.body && document.body.scrollHeight) || 0;
  const canScrollDown = scroller
    ? scroller.scrollTop + scroller.clientHeight < scroller.scrollHeight - 4
    : !modal && bodyHeight > 0 && (scrollY + innerHeight) < (bodyHeight - 4);

  // `loading` is advisory (not part of the marker): lets the model choose
  // Wait deliberately instead of picking a stale action that will not fire.
  const loading = document.readyState !== "complete"
    || !!document.querySelector('[aria-busy="true"],[role="progressbar"]');

  return {
    url: location.href,
    title: document.title,
    text,
    elements,
    guards,
    marker,
    loading,
    scroll_y: Math.round(scrollY),
    can_go_back: history.length > 1,
    can_scroll_up: scrollTop > 4,
    can_scroll_down: canScrollDown,
    scroll_area: scrollArea,
    // The open modal's own text, for judging what the dialog is. The page
    // text above also holds everything behind it.
    dialog_text: modal ? (modal.innerText || "").replace(/\s+/g, " ").trim().slice(0, 1500) : null,
    // Only for a dialog or panel. The page keeps the executor's fixed wheel
    // point, which Walmart's hover-sensitive tiles were tuned against.
    scroll_point: scroller && scrollBox.w > 0 && scrollBox.h > 0
      ? [Math.round(scrollBox.x + scrollBox.w / 2), Math.round(scrollBox.y + scrollBox.h / 2)]
      : null,
    // A dialog box can be shorter than the page step (700px), which would
    // jump past whole option rows. Scroll 80% of the box, keeping overlap.
    scroll_step: scroller && scrollBox.h > 0 ? Math.max(100, Math.round(scrollBox.h * 0.8)) : null,
    viewport: [innerWidth, innerHeight],
  };
})
