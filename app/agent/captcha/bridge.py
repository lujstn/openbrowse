"""Pre-navigation capture of CAPTCHA provider initialisation state."""

from __future__ import annotations

from browser_use import BrowserSession


_BRIDGE_JS = r"""(function () {
  if (window.__openbrowseCaptchaBridge) return;

  var state = {
    geetestV3: { config: null, instance: null, success: [], source: null },
    geetestV4: { config: null, instance: null, success: [] }
  };
  Object.defineProperty(window, "__openbrowseCaptchaBridge", {
    value: state, configurable: false, enumerable: false, writable: false
  });

  var challengeResponses = [];
  function findGeetestConfig(value, depth) {
    if (!value || typeof value !== "object" || depth > 6) return null;
    if (typeof value.gt === "string" && typeof value.challenge === "string" &&
        value.gt && value.challenge) {
      return { gt: value.gt, challenge: value.challenge,
        geetestApiServer: value.api_server || value.apiServer || "" };
    }
    var keys = Object.keys(value);
    for (var i = 0; i < keys.length; i++) {
      var found = findGeetestConfig(value[keys[i]], depth + 1);
      if (found) return found;
    }
    return null;
  }

  function parseResponse(text) {
    try { return findGeetestConfig(JSON.parse(text), 0); } catch (e) { return null; }
  }

  function rememberConfig(config, request) {
    if (!request || request.method !== "GET") return;
    if (!config) return;
    var candidate = { request: request, config: config };
    challengeResponses.push(candidate);
    if (challengeResponses.length > 12) challengeResponses.shift();
    var current = state.geetestV3.config;
    if (!state.geetestV3.source && current && current.challenge === config.challenge) {
      state.geetestV3.source = request;
    }
  }

  function rememberChallenge(text, request) {
    rememberConfig(parseResponse(text), request);
  }

  function associateChallengeSource(slot, config) {
    if (!config || !config.challenge || slot.source) return;
    for (var i = challengeResponses.length - 1; i >= 0; i--) {
      if (challengeResponses[i].config.challenge === config.challenge) {
        slot.source = challengeResponses[i].request;
        return;
      }
    }
  }

  var originalFetch = window.fetch;
  if (typeof originalFetch === "function") {
    window.fetch = function (input, options) {
      var request = null;
      try {
        var normalised = new Request(input, options);
        request = {
          url: normalised.url,
          method: normalised.method.toUpperCase(),
          credentials: normalised.credentials,
          headers: Array.from(normalised.headers.entries())
        };
      } catch (e) {}
      var result = originalFetch.apply(this, arguments);
      if (request && request.method === "GET") {
        result.then(function (response) {
          try {
            response.clone().text().then(function (text) {
              rememberChallenge(text, request);
            }, function () {});
          } catch (e) {}
        }, function () {});
      }
      return result;
    };
  }

  var XHR = window.XMLHttpRequest;
  if (XHR && XHR.prototype) {
    var originalOpen = XHR.prototype.open;
    var originalSend = XHR.prototype.send;
    var originalSetRequestHeader = XHR.prototype.setRequestHeader;
    XHR.prototype.open = function (method, url) {
      try {
        this.__openbrowseRequest = {
          method: String(method || "GET").toUpperCase(),
          url: new URL(url, location.href).href,
          credentials: "include",
          headers: []
        };
      } catch (e) {}
      return originalOpen.apply(this, arguments);
    };
    XHR.prototype.setRequestHeader = function (name, value) {
      if (this.__openbrowseRequest) {
        this.__openbrowseRequest.headers.push([String(name), String(value)]);
      }
      return originalSetRequestHeader.apply(this, arguments);
    };
    XHR.prototype.send = function () {
      var xhr = this;
      var request = xhr.__openbrowseRequest;
      if (request && request.method === "GET") {
        xhr.addEventListener("load", function () {
          try {
            if (xhr.responseType === "json") {
              rememberConfig(findGeetestConfig(xhr.response, 0), request);
            } else {
              rememberChallenge(xhr.responseText, request);
            }
          } catch (e) {}
        }, { once: true });
      }
      return originalSend.apply(this, arguments);
    };
  }

  state.refreshGeetestV3 = async function () {
    var source = state.geetestV3.source;
    if (!source || typeof originalFetch !== "function") return null;
    var response = await originalFetch.call(window, source.url, {
      method: "GET",
      credentials: source.credentials || "same-origin",
      headers: source.headers || [],
      cache: "no-store"
    });
    if (!response.ok) return null;
    var config = parseResponse(await response.text());
    if (!config) return null;
    state.geetestV3.config = Object.assign({}, state.geetestV3.config || {}, config);
    return config;
  };

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
        associateChallengeSource(slot, config);
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
