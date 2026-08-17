(function (global) {
  "use strict";

  var REDUCED_MOTION =
    global.matchMedia && global.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function mdliteInline(text) {
    var t = text.replace(/\*\*([\s\S]+?)\*\*/g, "<strong>$1</strong>");
    return t.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  }

  var LIST_ITEM_RE = /^[-*]\s+(.*)$/;

  function mdliteProse(text) {
    var parts = [];
    var paraLines = [];
    var listItems = [];

    function flushPara() {
      if (paraLines.length) {
        var joined = mdliteInline(paraLines.join("\n"));
        parts.push(joined.replace(/\n/g, "<br>"));
        paraLines = [];
      }
    }
    function flushList() {
      if (listItems.length) {
        parts.push("<ul>" + listItems.join("") + "</ul>");
        listItems = [];
      }
    }

    text.split("\n").forEach(function (line) {
      var m = LIST_ITEM_RE.exec(line);
      if (m) {
        flushPara();
        listItems.push("<li>" + mdliteInline(m[1]) + "</li>");
        return;
      }
      flushList();
      paraLines.push(line);
    });
    flushPara();
    flushList();
    return parts.join("<br>");
  }

  function splitFences(text) {
    var segments = [];
    var pos = 0;
    while (true) {
      var openIdx = text.indexOf("```", pos);
      if (openIdx === -1) {
        segments.push({ code: false, text: text.slice(pos) });
        break;
      }
      if (openIdx > pos) segments.push({ code: false, text: text.slice(pos, openIdx) });
      var nl = text.indexOf("\n", openIdx + 3);
      var bodyStart = nl === -1 ? text.length : nl + 1;
      var closeIdx = text.indexOf("```", bodyStart);
      if (closeIdx === -1) {
        segments.push({ code: true, text: text.slice(bodyStart) });
        break;
      }
      segments.push({ code: true, text: text.slice(bodyStart, closeIdx) });
      pos = closeIdx + 3;
      if (text[pos] === "\n") pos += 1;
    }
    return segments;
  }

  // @nonobvious(mirrors): app/dashboard/routes.py's _mdlite, same escape,
  // fence, bold, code and list rules, kept in sync by hand on either side.
  function renderMdLite(rawText) {
    var escaped = escapeHtml(rawText || "");
    return splitFences(escaped)
      .map(function (seg) {
        return seg.code ? "<pre><code>" + seg.text + "</code></pre>" : mdliteProse(seg.text);
      })
      .join("");
  }

  function revealOpen(el) {
    if (!el) return;
    el.classList.add("ob-reveal", "is-open");
  }

  function revealClose(el, opts) {
    if (!el) return;
    var remove = !!(opts && opts.remove);
    el.classList.remove("is-open");
    if (!remove) return;
    if (REDUCED_MOTION) {
      el.remove();
      return;
    }
    var done = false;
    var finish = function () {
      if (done) return;
      done = true;
      el.remove();
    };
    el.addEventListener("transitionend", finish, { once: true });
    setTimeout(finish, 400);
  }

  var MIN_CHARS_PER_SEC = 80;

  function Typewriter(onFrame) {
    this._target = "";
    this._shown = 0;
    this._speed = MIN_CHARS_PER_SEC;
    this._lastSet = null;
    this._rafId = null;
    this._onFrame = onFrame;
  }

  Typewriter.prototype.setTarget = function (text) {
    text = text || "";
    var now = performance.now();
    if (text.length < this._shown) this._shown = text.length;
    var interval = this._lastSet == null ? 0.25 : Math.max(0.05, (now - this._lastSet) / 1000);
    this._lastSet = now;
    var outstanding = Math.max(0, text.length - this._shown);
    this._speed = Math.max(MIN_CHARS_PER_SEC, outstanding / interval);
    this._target = text;
    if (REDUCED_MOTION) {
      this.finishInstantly();
      return;
    }
    this._ensureLoop();
  };

  Typewriter.prototype.finishInstantly = function () {
    this._stopLoop();
    this._shown = this._target.length;
    this._onFrame(this._target, true);
  };

  Typewriter.prototype._ensureLoop = function () {
    if (this._rafId != null) return;
    var self = this;
    var last = performance.now();
    var step = function (t) {
      var dt = Math.max(0, (t - last) / 1000);
      last = t;
      if (self._shown < self._target.length) {
        self._shown = Math.min(self._target.length, self._shown + self._speed * dt);
      }
      var shownInt = Math.floor(self._shown);
      var done = shownInt >= self._target.length;
      self._onFrame(self._target.slice(0, shownInt), done);
      if (done) {
        self._rafId = null;
        return;
      }
      self._rafId = requestAnimationFrame(step);
    };
    this._rafId = requestAnimationFrame(step);
  };

  Typewriter.prototype._stopLoop = function () {
    if (this._rafId != null) cancelAnimationFrame(this._rafId);
    this._rafId = null;
  };

  function buildSpinner() {
    var span = document.createElement("span");
    span.className = "ob-spin";
    for (var i = 0; i < 8; i++) {
      var bar = document.createElement("i");
      bar.style.transform = "rotate(" + i * 45 + "deg)";
      bar.style.animationDelay = -(7 - i) * 100 + "ms";
      span.appendChild(bar);
    }
    return span;
  }

  function StreamingResponse(container) {
    this.container = container;
    this._open = false;
    this._lastRawText = "";
    this._settled = false;
    this._build();
    this.typewriter = new Typewriter(this._onFrame.bind(this));
  }

  StreamingResponse.prototype._build = function () {
    this.container.classList.add("ob-reveal", "ob-stream");
    this.container.innerHTML =
      '<div class="ob-reveal-inner ob-stream-inner"><div class="ob-stream-card">' +
      '<span class="ob-stream-indicator"></span>' +
      '<div class="ob-stream-body"><div class="ob-stream-text"></div></div>' +
      '<div class="ob-stream-actions">' +
      '<button type="button" class="ob-stream-copy">Copy</button>' +
      "</div></div></div>";
    this.indicator = this.container.querySelector(".ob-stream-indicator");
    this.textEl = this.container.querySelector(".ob-stream-text");
    this.copyBtn = this.container.querySelector(".ob-stream-copy");
    this.copyBtn.addEventListener("click", this._onCopy.bind(this));
    this.container.setAttribute("role", "log");
    this.container.setAttribute("aria-live", "polite");
    this.container.setAttribute("aria-busy", "false");
  };

  StreamingResponse.prototype._setIndicator = function (spinning) {
    var wantSpin = !!spinning;
    if (this._indicatorSpin === wantSpin) return;
    this._indicatorSpin = wantSpin;
    this.indicator.innerHTML = "";
    if (wantSpin) {
      this.indicator.appendChild(buildSpinner());
    } else {
      var dot = document.createElement("span");
      dot.className = "ob-dot";
      this.indicator.appendChild(dot);
    }
  };

  StreamingResponse.prototype._onFrame = function (shownText, done) {
    this.textEl.innerHTML =
      renderMdLite(shownText) + (done && this._settled ? "" : '<span class="ob-caret"></span>');
    if (this._pinToBottom) this.textEl.scrollTop = this.textEl.scrollHeight;
  };

  StreamingResponse.prototype._onCopy = function () {
    var text = this._lastRawText || "";
    var btn = this.copyBtn;
    var restore = btn.textContent;
    var flash = function (label) {
      btn.textContent = label;
      setTimeout(function () {
        btn.textContent = restore;
      }, 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () {
          flash("Copied");
        },
        function () {
          flash("Copy failed");
        }
      );
      return;
    }
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand("copy");
      flash("Copied");
    } catch (e) {
      flash("Copy failed");
    }
    document.body.removeChild(ta);
  };

  StreamingResponse.prototype.update = function (activity) {
    var text = activity && activity.stream;
    if (!text) {
      this.hide();
      return;
    }
    this._lastRawText = text;
    if (!this._open) {
      this._open = true;
      this._settled = false;
      this.container.classList.remove("hidden");
      revealOpen(this.container);
    }
    this.container.setAttribute("aria-busy", activity.spin ? "true" : "false");
    this._setIndicator(!!activity.spin);
    this._pinToBottom = !!activity.spin;
    this.typewriter.setTarget(text);
    if (!activity.spin) {
      this.typewriter.finishInstantly();
      this._settled = true;
      this.container.classList.add("is-settled");
    } else {
      this._settled = false;
      this.container.classList.remove("is-settled");
    }
  };

  StreamingResponse.prototype.hasLiveContent = function () {
    return this._open && !!this._lastRawText;
  };

  StreamingResponse.prototype.hide = function () {
    if (!this._open) return;
    this._open = false;
    this._settled = false;
    this.container.classList.remove("is-settled");
    this.typewriter._stopLoop();
    revealClose(this.container);
  };

  function revealCardsForHandoff(rowEl) {
    var cards = rowEl && rowEl.querySelector(".msg-cards");
    if (!cards) return;
    rowEl.classList.add("expanded");
    if (REDUCED_MOTION) return;
    cards.classList.add("ob-handoff-grow");
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        cards.classList.add("ob-handoff-grow-open");
      });
    });
    var done = false;
    var cleanup = function (e) {
      if (e && e.propertyName !== "grid-template-rows") return;
      if (done) return;
      done = true;
      cards.classList.remove("ob-handoff-grow", "ob-handoff-grow-open");
      cards.removeEventListener("transitionend", cleanup);
    };
    cards.addEventListener("transitionend", cleanup);
    setTimeout(cleanup, 500);
  }

  function fadeRowIn(rowEl) {
    if (REDUCED_MOTION) return;
    rowEl.classList.add("ob-row-enter");
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        rowEl.classList.add("ob-row-enter-in");
      });
    });
    setTimeout(function () {
      rowEl.classList.remove("ob-row-enter", "ob-row-enter-in");
    }, 400);
  }

  global.OpenBrowseAgents = {
    renderMdLite: renderMdLite,
    reveal: { open: revealOpen, close: revealClose },
    Typewriter: Typewriter,
    StreamingResponse: StreamingResponse,
    handoff: { revealCards: revealCardsForHandoff, fadeRowIn: fadeRowIn },
  };
})(window);
