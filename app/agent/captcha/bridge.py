"""Pre-navigation capture of CAPTCHA provider initialisation state."""

from __future__ import annotations

from browser_use import BrowserSession


_BRIDGE_JS = r"""(function () {
  if (window.__openbrowseCaptchaBridge) return;

  var state = {
    geetestV3: { config: null, instance: null, success: [] },
    geetestV4: { config: null, instance: null, success: [] }
  };
  Object.defineProperty(window, "__openbrowseCaptchaBridge", {
    value: state, configurable: false, enumerable: false, writable: false
  });

  function captureInstance(slot, instance) {
    if (!instance || typeof instance !== "object") return;
    slot.instance = instance;
    var original = instance.onSuccess;
    if (typeof original !== "function" || original.__openbrowseWrapped) return;
    function onSuccess(callback) {
      if (typeof callback === "function") slot.success.push(callback);
      return original.apply(this, arguments);
    }
    Object.defineProperty(onSuccess, "__openbrowseWrapped", { value: true });
    instance.onSuccess = onSuccess;
  }

  function install(name, slot) {
    var assigned = window[name];
    function wrap(original) {
      if (typeof original !== "function" || original.__openbrowseWrapped) return original;
      function initialiser(config, callback) {
        slot.config = config || null;
        var wrappedCallback = callback;
        if (typeof callback === "function") {
          wrappedCallback = function (instance) {
            captureInstance(slot, instance);
            return callback.apply(this, arguments);
          };
        }
        var args = Array.prototype.slice.call(arguments);
        args[1] = wrappedCallback;
        return original.apply(this, args);
      }
      Object.defineProperty(initialiser, "__openbrowseWrapped", { value: true });
      return initialiser;
    }
    var current = wrap(assigned);
    try {
      Object.defineProperty(window, name, {
        configurable: true,
        enumerable: true,
        get: function () { return current; },
        set: function (value) { current = wrap(value); }
      });
    } catch (e) {
      if (assigned) window[name] = current;
    }
  }

  install("initGeetest", state.geetestV3);
  install("initGeetest4", state.geetestV4);
})()"""


async def install_captcha_bridge(
    browser_session: BrowserSession, target_id: str | None = None
) -> None:
    """Install the provider bridge before a target's next real document loads."""
    session = await browser_session.get_or_create_cdp_session(target_id, focus=False)
    await session.cdp_client.send.Page.addScriptToEvaluateOnNewDocument(
        params={"source": _BRIDGE_JS}, session_id=session.session_id
    )
    await session.cdp_client.send.Runtime.evaluate(
        params={"expression": _BRIDGE_JS, "returnByValue": True},
        session_id=session.session_id,
    )
