"""Anti-detection shims injected on every new document.

Nothing here defeats a real anti-bot service; the goal is parity with what a
real user's Chrome reports so trivial `navigator.webdriver` checks do not flag
the tab. Injected via `context.add_init_script` in Playwright, which fires
before any script on the page.
"""

_INIT_SCRIPT = r"""
(() => {
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

  if (!('chrome' in window)) {
    Object.defineProperty(window, 'chrome', {value: {runtime: {}}, configurable: true});
  } else if (window.chrome && !window.chrome.runtime) {
    window.chrome.runtime = {};
  }

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
