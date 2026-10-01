# Fjord Design System & Requirements

Fjord is the design system for an app where people hand browser tasks to an AI agent and watch it work: they describe a task, follow each step live, and step in when the agent needs a decision. It covers the web app (desktop-first) and the marketing website, in a light and a dark theme.

The look in one line: a cool mist canvas, near-white panels, one pine accent, one typeface, square edges.

---

## 1. Design principles

- **Quiet and uncluttered.** Every element earns its place. Empty areas are layout problems, not reasons for filler.
- **Show the work honestly.** Steps appear as they happen, in plain words. Never show progress you cannot know.
- **Ask before anything irreversible.** Payments, submissions and messages pause the run and wait for the person. That pause is the most important screen in the app, so it is never subtle.
- **Calm, editorial minimalism.** Generous space, restrained colour, clear typographic hierarchy. Considered, not busy.
- **Purposeful additions only.** Add sections, copy or controls intentionally, never to fill space.

---

## 2. Color

Cool mist neutrals, one pine accent and two reserved status colours. Nothing is pure black or pure white. Light is the default theme; dark uses the same roles.

| Token | Light | Dark | Use |
| --- | --- | --- | --- |
| `canvas` | `#edf1f0` | `#0f1719` | The page canvas: behind panels, under the side nav, the website background. |
| `panel` | `#fbfcfc` | `#152023` | Panels, tables, the browser frame, the composer and inputs; one level above canvas. |
| `well` | `#f2f5f4` | `#1a272a` | Recessed level: row hover, the URL field, an empty browser viewport. |
| `line` | `#d3dcdb` | `#28373a` | Panel edges and header rules. Decorative: never the only edge of a control. |
| `line-faint` | `#e3e9e8` | `#1f2d30` | Row dividers inside tables, step logs and lists. |
| `line-strong` | `#7b8b8f` | `#6d8084` | Edges of controls: inputs, secondary buttons, the switch. 3:1 on canvas, panel and well. |
| `ink` | `#17262a` | `#e2eae8` | Primary text and icons, on canvas, panel, well and every tint. |
| `ink-muted` | `#53646a` | `#9aabae` | Secondary text: labels, hints, step details, timestamps. 4.5:1 on canvas, panel, well and pine-tint. |
| `pine` | `#2e5c4f` | `#86c0a9` | The accent, small and meaningful only: primary button fill, the Live dot, the current tab rule. Never a large area. |
| `pine-strong` | `#234a3f` | `#a2d3c0` | Hover and pressed fill of a primary button. |
| `on-pine` | `#fbfcfc` | `#0f1f1a` | Text and icons on a pine or pine-strong fill. |
| `pine-tint` | `#dae7e2` | `#1d3831` | The one selected-state fill: active nav item, selected table row, quiet button hover. |
| `pine-ink` | = `pine` | `#9fd4bf` | Text and icons on pine-tint, quiet buttons, running status, the current step. 4.5:1 on panel, canvas and pine-tint. |
| `attention` | `#7d5719` | `#e2bc74` | Needs you: the status, the flag icon, the paused Live dot and a waiting step. Text-safe on panel, canvas, well, pine-tint and attention-tint. |
| `attention-tint` | `#f3ead6` | `#36291a` | Fill of an attention Notice only. |
| `danger` | `#9b3b3b` | `#ef9a92` | Failures and destructive actions: the failed status, error text, the Stop button label. Text-safe on panel, canvas, well, pine-tint and danger-tint. |
| `danger-tint` | `#f6e3e1` | `#3b2224` | Fill of a danger Notice and the Stop button hover only. |
| `focus` | = `pine` | = `pine` | The focus ring colour: an alias of pine. 3:1 on every ground in both themes. |

- **Two levels, always.** The page is `canvas`. Panels, tables, the browser frame, the composer and inputs sit on `panel` above it. Never paint the canvas and the panels the same colour, or the page reads as one undifferentiated block.
- **`well` is the recessed level:** row hover, the URL field, an empty browser viewport.
- **Pine is small and meaningful.** Primary buttons (`pine` with `on-pine`), the current tab rule, the Live dot and the focus ring. Large areas are never pine: a band, a hero or a page is always `canvas` or `panel`.
- **One selected fill.** The active nav item, a selected table row and a quiet button's hover all use `pine-tint`, with `pine-ink` for their text. Never invent a one-off tint.
- **Status colour only means something.** `attention` means "needs you"; `danger` means failure or a destructive action. Everything else is `ink` and `ink-muted`. `attention-tint` and `danger-tint` are only ever the fill of a Notice (and `danger-tint` the Stop button hover).
- **Two text colours, no third.** `ink` for primary text, `ink-muted` for secondary. There is no fainter tertiary grey, because it cannot stay readable on every ground.
- **Three lines.** `line` for panel edges and header rules, `line-faint` for row dividers, `line-strong` for the edge of anything you can type into or press.
- **Chroma stays low everywhere.** Reach for the contrast between `ink` and `ink-muted` before reaching for colour.
- **Dark theme:** pine gets lighter and text on it flips dark (`on-pine`). Never put white text on the dark-theme pine.

---

## 3. Typography

- **Single typeface: Hanken Grotesk** (300, 400, 500, 600) for everything, including numbers, timers and URLs.
- Tables, step logs and timers use tabular figures: `font-variant-numeric: tabular-nums`. Never a second font for numbers or code.
- Load: `https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@300..700&display=swap`

| Style | Size / line | Weight | Tracking | Use |
| --- | --- | --- | --- | --- |
| `display` | 44px / 50px | 300 | -0.02em | Website headlines only. Light weight, so never below 32px. |
| `title` | 28px / 34px | 400 | -0.01em | Page titles in the app: the run name, Runs, Settings. |
| `heading` | 18px / 24px | 500 | 0 | Panel and section headings. |
| `lead` | 17px / 26px | 400 | 0 | The task composer and website intro paragraphs. |
| `body` | 15px / 23px | 400 | 0 | Default text: notices, descriptions, table cells. |
| `small` | 13px / 19px | 400 | 0 | Secondary lines, hints, step details, table sub-lines. |
| `label` | 12px / 16px | 500 | 0 | Column headers, nav section names, field meta. Sentence case, ink-muted. The smallest text in the system. |
| `button` | 14px / 20px | 500 | 0 | Buttons, tabs and nav items. |

- Weight before size: 400 for reading, 500 for labels and actions, 600 only for notice titles and the workspace name.
- Micro-labels (`label`) are 12px, weight 500, `ink-muted`, sentence case.
- Nothing smaller than 12px.
- `display` is light (300), so it is never set below 32px.

---

## 4. Shape, spacing, layout

### Shape

| Token | Value | Use |
| --- | --- | --- |
| `radius-none` | 0px | Everything: buttons, inputs, panels, the browser frame, monograms, the switch. |
| `radius-dot` | 50% | The Live signal dot only. |

- **Square corners by default, everywhere.** The one round shape is the Live signal dot, because it is an operational signal, not decoration.

### Spacing (4px grid)

| Token | Value | Use |
| --- | --- | --- |
| `space-1` | 4px | Icon to label gap inside tight controls. |
| `space-2` | 8px | Icon to label gap in buttons; gap between buttons. |
| `space-3` | 12px | Table cell and step row vertical padding; gap between a switch and its text. |
| `space-4` | 16px | Panel and cell horizontal padding; button side padding. |
| `space-5` | 20px | Padding of roomy panels. |
| `space-6` | 24px | Gap between tabs; page gutter inside the content area. |
| `space-8` | 32px | Between sections of a page. |
| `space-12` | 48px | Website: between a headline block and what follows. |
| `space-16` | 64px | Website: between page sections. |

### Sizes

| Token | Value | Use |
| --- | --- | --- |
| `size-control` | 36px | Height of buttons and single-line inputs. |
| `size-control-sm` | 30px | Height of small buttons, the URL field and composer options. |
| `size-nav` | 232px | Width of the side nav. |
| `size-content` | 1120px | Maximum width of the app content area and the website. |

### Layout

- App frame: `SideNav` (`size-nav`, 232px) on `canvas` at the left, with a 1px `line` on its right edge. Content area on `canvas`, at most `size-content` wide, with panels on `panel`.
- Prefer flat lists and tables with hairline dividers over boxed cards. A boxed surface is only for something that floats (menus, dialogs) or something you type into (the task composer).
- Lay out with flex or grid and `gap`. No spacer elements.
- Panels pad `space-4` to `space-6`; sections sit `space-8` apart; the website opens up to `space-12` and `space-16`.
- Website: content left-aligned, `display` for the single headline, `lead` under it, sections separated by a `line` top rule rather than boxes.

---

## 5. Depth, states, motion

- **Flat.** Panels separate from the canvas by level and a `line` edge, never by shadow. `shadow-overlay` is only for menus, popovers and dialogs.
- **Hover:** `well` on neutral controls and clickable rows, `pine-strong` on primary buttons, `pine-tint` on quiet buttons, `danger-tint` on the Stop button.
- **Selected:** `pine-tint` fill with `pine-ink` text.
- **Focus:** `focus-ring` on every control: a 2px gap in the panel colour, then 2px of `focus` (pine), drawn as a box-shadow so it follows square edges. Replace the browser outline with it.
- **Disabled:** `opacity-disabled` (0.45), `cursor: not-allowed`, no hover change.
- **Motion:** 120 to 160 ms transitions of colour and position only. No looping animation, no spinners, no pulsing dots. The growing step log and the Live signal already show that the agent is working.

---

## 6. Components

The reference implementation is React 18 (`window.Fjord`, classes prefixed `fj-`), in the Fjord design system artifact.

- **Button.** Variants `primary` (pine fill), `secondary` (panel fill, `line-strong` edge), `quiet` (text only, `pine-ink`), `danger` (panel fill, `danger` label). Sizes 36px and 30px. One primary per view. Labels are verbs in sentence case. Icon-only buttons need an `aria-label`.
- **TextField.** Visible label above, 1px `line-strong` edge, hint below in `ink-muted`. An error replaces the hint, turns the edge `danger`, and says how to fix it ("Enter an amount in dollars, like 700."). The placeholder is never the label.
- **TaskComposer.** Where a person describes a task. Multi-line input in `lead`, then a bar with the browser profile and allowed sites (as quiet pickers) and the primary Start task button, disabled until there is text. No suggestion chips or sample prompts around it.
- **Switch.** Square track and thumb. Label plus one line on what the agent will do differently. Settings sit in a flat list with `line-faint` dividers. Takes effect immediately; use a checkbox in a form when a Save button is needed.
- **Tabs.** Two to five sentence-case labels with an optional count in `ink-muted`. The current tab is `ink` with a 2px pine rule under it. No boxes, fills or icons.
- **SideNav.** Workspace monogram and name, then a flat list of destinations with 16px icons; setup pages under a sentence-case section label. Active item on `pine-tint`. Counts are plain numbers, never badges or dots.
- **StatusLabel.** A run's state as an icon and a word: Queued, Running, Needs you, Done, Failed, Stopped. Only Running (`pine-ink`), Needs you (`attention`) and Failed (`danger`) carry colour; Done is `ink`; Queued and Stopped are `ink-muted`. Never a dot or a pill.
- **LiveSignal.** The real state of the agent's browser session: Live (pine dot), Paused (attention dot), Disconnected (hollow dot, muted text). The only coloured dot in the system. Not animated.
- **Notice.** Icon, title, text and actions on a tinted fill with no side stripe. `attention` is how the agent asks for a decision (Approve and continue, Take over, Stop here). `danger` says what failed and offers the fix. `info` is neutral, on `panel` with a `line` edge. One notice at a time.
- **Table.** On `panel`, header in `label` style with a `line` rule, rows divided by `line-faint`, numbers right-aligned in tabular figures. One optional secondary line per cell in `small`. Selected row on `pine-tint`. No zebra stripes, no boxed rows.
- **StepLog.** The agent's actions in order: step number, a kind-of-action icon, a short past-tense sentence, an optional detail line, and the time at the right. The current step is marked "Working" in `pine-ink`; a waiting step is marked "Waiting for you" in `attention`. Numbers count up; never show a total.
- **BrowserFrame.** Toolbar with the LiveSignal, the URL in a `well` field with a lock icon, and one or two small actions (Take over, Open in a new tab). The page below is the real site, never restyled. A 2px pine outline with a small pine label (Clicking, Typing, Waiting to click) marks the element the agent is acting on. No fake browser chrome, no shadow, no rounding.
- **Monogram.** Square initials tile: `panel` fill, 1px `line` edge, `pine-ink` letters, 24, 32 or 40px. Not a filled colour block.
- **Icon.** See section 9.

---

## 7. Content and voice

- Calm and plain. The app speaks to the person as "you".
- The agent's steps are short past-tense sentences naming what it did and to what: "Opened flights.example.com", "Entered Lisbon (LIS) as destination", "Filtered to nonstop flights". Details go on a second line: "14 nonstop fares, cheapest $642".
- Sentence case everywhere: buttons, headings, tabs, labels. No exclamation marks.
- Buttons are verbs that say what happens: "Start task", "Approve and continue", "Take over", "Stop run". Never "OK" or "Submit".
- Errors say what happened and what to do: "The site signed the agent out. Sign in again in your Personal profile, then retry." Never "Oops! Something went wrong."
- Numbers are exact: "3m 12s", "Started 10:42 AM", "14 results".
- The agent never claims more than it knows: "The cheapest nonstop fare I found", not "Guaranteed lowest price".
- Separate facts go in separate columns or on separate lines, or are joined with words. Never with separator dots.

---

## 8. Status, signals and progress

- Run state is always a word with an icon (StatusLabel).
- A coloured dot appears only for a real operational signal: the Live state of the browser session. Never for run status, categories or unread counts.
- Progress counts up. The step log numbers steps as they happen and the run header shows elapsed time.
- No fabricated progress: no fixed-length step tracks, no percentage bars, no "step 3 of 7". A run's length is unknown until it ends.
- Anything irreversible (payment, submission, sending a message) pauses the run: status becomes Needs you, the Live signal becomes Paused, and an attention Notice with the decision appears above the browser.

---

## 9. Iconography

- Line icons on a 24px grid, drawn at 16px (tables, nav, labels) or 18 to 20px (notices, toolbars).
- 1.5px stroke, square caps, mitred joins, no fills, coloured with `currentColor`.
- Step kinds: navigate, click (pointer), type, read (eye), scroll, download. Statuses: clock, play, flag, check, alert, stop.
- New icons match this geometry. Lucide at a 1.5px stroke with square caps is the closest library.
- An icon never carries meaning alone: status icons sit beside a word; icon-only buttons carry an `aria-label`.
- No emoji and no illustrations in the app. The browser viewport shows the real page the agent is on.

---

## 10. Accessibility

- `ink` and `ink-muted` hold at least 4.5:1 on `canvas`, `panel`, `well` and `pine-tint` in both themes.
- `pine-ink`, `attention` and `danger` hold at least 4.5:1 as text on every ground they are used on, including selected rows and their own tints.
- `on-pine` holds at least 4.5:1 on `pine` and `pine-strong`.
- `line-strong` and `focus` hold at least 3:1 on every ground, so control edges and the focus ring are always visible.
- Status is never colour alone (always a word and an icon). The switch uses `role="switch"`; tabs use `role="tablist"` and `aria-selected`; the danger Notice uses `role="alert"`; the current step uses `aria-current="step"`.

---

## 11. Things to AVOID (explicit)

- Pure black or pure white. `ink` and `panel` are tinted.
- Rounded corners as a default. The Live dot is the only round shape.
- Brutalist and terminal aesthetics, including monospace step logs.
- Left or side accent borders on cards and notices.
- Decorative status dots. A coloured dot is only for the Live signal.
- Excessive card usage. Use lists, tables or dividers instead.
- Large areas of pine or any accent colour.
- A third, fainter text grey.
- Emojis.
- Em dashes in UI copy.
- Separator middle dots in body or content text.
- Random italics.
- Uppercase or monospace labels.
- Fonts: Inter, Instrument Serif, or any second typeface, including for numbers.
- Fabricated progress: fixed-length step tracks, percentage bars, "step 3 of 7".
- Spinners, pulsing dots or any looping animation.
- Clutter and filler generally. Don't add content without a clear purpose.

---

## Appendix: CSS variables

Set `data-theme="dark"` on `<html>` for the dark theme.

```css
:root, [data-theme="light"] {
  --canvas: #edf1f0;
  --panel: #fbfcfc;
  --well: #f2f5f4;
  --line: #d3dcdb;
  --line-faint: #e3e9e8;
  --line-strong: #7b8b8f;
  --ink: #17262a;
  --ink-muted: #53646a;
  --pine: #2e5c4f;
  --pine-strong: #234a3f;
  --on-pine: #fbfcfc;
  --pine-tint: #dae7e2;
  --pine-ink: var(--pine);
  --attention: #7d5719;
  --attention-tint: #f3ead6;
  --danger: #9b3b3b;
  --danger-tint: #f6e3e1;
  --focus: var(--pine);
  --shadow-overlay: 0 1px 2px #17262a14, 0 8px 24px #17262a1f;
  --focus-ring: 0 0 0 2px #fbfcfc, 0 0 0 4px #2e5c4f;
}

[data-theme="dark"] {
  --canvas: #0f1719;
  --panel: #152023;
  --well: #1a272a;
  --line: #28373a;
  --line-faint: #1f2d30;
  --line-strong: #6d8084;
  --ink: #e2eae8;
  --ink-muted: #9aabae;
  --pine: #86c0a9;
  --pine-strong: #a2d3c0;
  --on-pine: #0f1f1a;
  --pine-tint: #1d3831;
  --pine-ink: #9fd4bf;
  --attention: #e2bc74;
  --attention-tint: #36291a;
  --danger: #ef9a92;
  --danger-tint: #3b2224;
  --focus: var(--pine);
  --shadow-overlay: 0 1px 2px #00000066, 0 12px 32px #00000099;
  --focus-ring: 0 0 0 2px #152023, 0 0 0 4px #86c0a9;
}

:root {
  --font-sans: "Hanken Grotesk", system-ui, sans-serif;
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 20px;
  --space-6: 24px;
  --space-8: 32px;
  --space-12: 48px;
  --space-16: 64px;
  --radius-none: 0px;
  --radius-dot: 50%;
  --size-control: 36px;
  --size-control-sm: 30px;
  --size-nav: 232px;
  --size-content: 1120px;
  --opacity-disabled: 0.45;
}
```
