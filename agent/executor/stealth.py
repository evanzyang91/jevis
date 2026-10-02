"""Shims for the bundled headless shell, injected on every new document of a
launched browser.

Nothing here defeats a real anti-bot service. The shims exist for Playwright's
old headless shell, which reports values no person's browser does: an empty
`navigator.plugins`, no `window.chrome`, a software (SwiftShader) WebGL
renderer. They paper over those so trivial checks do not flag the tab.

A full Chrome or Chromium (headed, new headless, or the real Chrome channel
via AGENT_CHROME_CHANNEL) already reports real, consistent values, and
replacing them with fakes is itself the giveaway: a non-native `webdriver`
getter that returns `undefined`, three plain-object "plugins" that are not a
PluginArray, an invented `chrome.runtime`, a WebGL1 renderer of "Intel Iris"
next to a WebGL2 renderer of the machine's real GPU. So the shims run only in
the headless shell, recognised by its own tells, and change nothing anywhere
else. Injected via `context.add_init_script` in Playwright, which fires before
any script on the page. Never injected into an attached Chrome (see
`PlaywrightExecutor.__aenter__`).
"""

_INIT_SCRIPT = r"""
(() => {
  // Only the headless shell has no plugins and no window.chrome. Leave a full
  // Chrome or Chromium exactly as it is.
  if (!(navigator.plugins.length === 0 && !('chrome' in window))) return;

  const define = (obj, prop, value) => {
    try {
      Object.defineProperty(obj, prop, {get: () => value, configurable: true});
    } catch (_) {}
  };
  define(Navigator.prototype, 'webdriver', undefined);
  define(navigator, 'languages', ['en-US', 'en']);

  const plugin = {name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: ''};
  const plugins = [plugin, plugin, plugin];
  define(navigator, 'plugins', plugins);
  define(navigator, 'mimeTypes', plugins);

  if (navigator.permissions && navigator.permissions.query) {
    const original = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (params) =>
      params && params.name === 'notifications'
        ? Promise.resolve({state: (window.Notification && window.Notification.permission) || 'default'})
        : original(params);
  }

  Object.defineProperty(window, 'chrome', {value: {runtime: {}}, configurable: true});

  const proto = window.WebGLRenderingContext && WebGLRenderingContext.prototype;
  if (proto) {
    const original = proto.getParameter;
    proto.getParameter = function (parameter) {
      if (parameter === 37445) return 'Intel Inc.';
      if (parameter === 37446) return 'Intel Iris OpenGL Engine';
      return original.call(this, parameter);
    };
  }
})();
"""


def init_script() -> str:
    return _INIT_SCRIPT
