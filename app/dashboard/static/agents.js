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

  Typewriter.prototype.reset = function () {
    this._stopLoop();
    this._target = "";
    this._shown = 0;
    this._lastSet = null;
  };

  Typewriter.prototype._stopLoop = function () {
    if (this._rafId != null) cancelAnimationFrame(this._rafId);
    this._rafId = null;
  };

  var ACTIVITY_MAX_HEIGHT = 208;

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

  function formatDuration(seconds) {
    if (seconds >= 60) {
      var mins = Math.floor(seconds / 60);
      var rest = Math.round(seconds % 60);
      return mins + "m " + rest + "s";
    }
    return (seconds >= 10 ? Math.round(seconds) : seconds.toFixed(1)) + "s";
  }

  function AgentActivity(container) {
    this.container = container;
    this._open = false;
    this._expanded = false;
    this._settled = false;
    this._complete = false;
    this._lastRawText = "";
    this._label = "";
    this._startedAt = null;
    this._seconds = null;
    this._build();
    this.typewriter = new Typewriter(this._onFrame.bind(this));
    var self = this;
    this._timerId = setInterval(function () {
      self._renderTimer();
    }, 100);
  }

  AgentActivity.prototype._build = function () {
    this.container.classList.add("ob-reveal", "ob-activity");
    this.container.style.setProperty("--ob-activity-max", ACTIVITY_MAX_HEIGHT + "px");
    this.container.innerHTML =
      '<div class="ob-reveal-inner"><div class="ob-activity-card">' +
      '<div class="ob-activity-body">' +
      '<button type="button" class="ob-activity-head">' +
      '<span class="ob-activity-indicator"></span>' +
      '<span class="ob-activity-label"></span>' +
      '<span class="ob-activity-timer"></span>' +
      '<span class="ob-activity-chev"></span>' +
      "</button>" +
      '<div class="ob-activity-viewport"><div class="ob-activity-track">' +
      '<div class="ob-activity-text"></div>' +
      "</div></div></div>" +
      '<div class="ob-activity-actions">' +
      '<button type="button" class="ob-activity-copy">Copy</button>' +
      "</div></div></div>";
    this.indicatorEl = this.container.querySelector(".ob-activity-indicator");
    this.labelEl = this.container.querySelector(".ob-activity-label");
    this.timerEl = this.container.querySelector(".ob-activity-timer");
    this.headEl = this.container.querySelector(".ob-activity-head");
    this.viewportEl = this.container.querySelector(".ob-activity-viewport");
    this.trackEl = this.container.querySelector(".ob-activity-track");
    this.textEl = this.container.querySelector(".ob-activity-text");
    this.copyBtn = this.container.querySelector(".ob-activity-copy");
    this.copyBtn.addEventListener("click", this._onCopy.bind(this));
    this.headEl.addEventListener("click", this._onToggle.bind(this));
    this.container.setAttribute("role", "log");
    this.container.setAttribute("aria-live", "polite");
    this.container.setAttribute("aria-busy", "false");
  };

  AgentActivity.prototype._setSpinner = function (spinning) {
    if (this._spinnerShown === spinning) return;
    this._spinnerShown = spinning;
    this.indicatorEl.innerHTML = "";
    if (spinning) this.indicatorEl.appendChild(buildSpinner());
  };

  AgentActivity.prototype._onToggle = function () {
    if (!this._complete) return;
    this._expanded = !this._expanded;
    this.container.classList.toggle("is-expanded", this._expanded);
    this.headEl.setAttribute("aria-expanded", this._expanded ? "true" : "false");
    if (this._expanded) this.viewportEl.scrollTop = 0;
  };

  AgentActivity.prototype._renderTimer = function () {
    if (!this._open) return;
    if (this._complete) return;
    if (this._startedAt == null) {
      this.timerEl.textContent = "";
      return;
    }
    var secs = Math.max(0, (Date.now() - this._startedAt) / 1000);
    this.timerEl.textContent = secs.toFixed(1) + "s";
  };

  // @nonobvious(forced-by): the viewport clips to a fixed height, so keeping the
  // newest line visible means sliding the track up by the overflow rather than
  // scrolling, which would fight the fade mask pinned to the viewport edges.
  AgentActivity.prototype._syncScroll = function () {
    var overflow = this.trackEl.scrollHeight - ACTIVITY_MAX_HEIGHT;
    var masked = overflow > 0;
    this.viewportEl.classList.toggle("is-masked", masked);
    if (this._complete) {
      this.trackEl.style.transform = "";
      return;
    }
    this.trackEl.style.transform = masked ? "translateY(" + -overflow + "px)" : "";
  };

  AgentActivity.prototype._onFrame = function (shownText, done) {
    this.textEl.innerHTML =
      renderMdLite(shownText) + (done && this._settled ? "" : '<span class="ob-caret"></span>');
    this._syncScroll();
  };

  AgentActivity.prototype._onCopy = function () {
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

  AgentActivity.prototype.update = function (activity) {
    if (!activity || !activity.label) {
      this.hide();
      return;
    }
    var text = activity.stream || "";
    var working = !!activity.spin;
    var done = !working && !!text;

    if (!this._open) {
      this._open = true;
      this.container.classList.remove("hidden");
      revealOpen(this.container);
    }
    if (activity.label !== this._label) {
      this._label = activity.label;
      this._startedAt = Date.parse(activity.startedAt) || Date.now();
    }

    var reasoning = activity.kind === "reasoning";
    this.container.classList.toggle("is-working", working);
    this.container.classList.toggle("is-reasoning", reasoning);
    this._setSpinner(working && !reasoning);
    this.container.setAttribute("aria-busy", working ? "true" : "false");

    // @nonobvious(forced-by): finishInstantly renders synchronously and reads
    // _settled to decide on the caret, and it stops the loop, so nothing paints
    // again afterwards — the flag has to be true before the call, not after.
    this._settled = !working;
    this._complete = done;
    this.container.classList.toggle("is-settled", this._settled);
    this.container.classList.toggle("has-prose", !!text);
    this.container.classList.toggle("is-complete", done);

    if (text) {
      this._lastRawText = text;
      if (done) {
        this.typewriter.setTarget(text);
        this.typewriter.finishInstantly();
      } else {
        this.typewriter.setTarget(text);
      }
    } else {
      this.typewriter.reset();
      this.textEl.innerHTML = "";
    }

    if (done) {
      var secs =
        activity.seconds != null
          ? Number(activity.seconds)
          : Math.max(0, (Date.now() - this._startedAt) / 1000);
      this._seconds = secs;
      this.labelEl.textContent = "Thought for " + formatDuration(secs);
      this.timerEl.textContent = "";
      this._expanded = false;
      this.container.classList.remove("is-expanded");
      this.headEl.setAttribute("aria-expanded", "false");
    } else {
      this.labelEl.textContent = activity.label + (activity.step ? " · step " + activity.step : "");
      this.headEl.removeAttribute("aria-expanded");
      this._renderTimer();
    }
    this._syncScroll();
  };

  AgentActivity.prototype.hasLiveContent = function () {
    return this._open && !!this._lastRawText;
  };

  AgentActivity.prototype.hide = function () {
    if (!this._open) return;
    this._open = false;
    this._settled = false;
    this._complete = false;
    this._label = "";
    this._startedAt = null;
    this.container.classList.remove(
      "is-settled",
      "is-complete",
      "is-working",
      "is-reasoning",
      "is-expanded"
    );
    this._setSpinner(false);
    this.typewriter.reset();
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
    AgentActivity: AgentActivity,
    handoff: { revealCards: revealCardsForHandoff, fadeRowIn: fadeRowIn },
  };
})(window);
