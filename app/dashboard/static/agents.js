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
    // @nonobvious(forced-by): finishInstantly renders synchronously and reads
    // _settled to decide on the caret, and it stops the loop, so nothing paints
    // again afterwards — the flag has to be true before the call, not after.
    this._settled = !activity.spin;
    if (this._settled) {
      this.typewriter.setTarget(text);
      this.typewriter.finishInstantly();
      this.container.classList.add("is-settled");
    } else {
      this.container.classList.remove("is-settled");
      this.typewriter.setTarget(text);
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

  // @nonobvious(must-hold): the collapsed panel is only clipped, never removed
  // from the layout, so its Copy button and source links stay focusable and
  // readable unless it is also made inert.
  function setRowExpanded(rowEl, open) {
    var cards = rowEl && rowEl.querySelector(".msg-cards");
    if (!cards) return;
    rowEl.classList.toggle("expanded", !!open);
    cards.inert = !open;
    var caret = rowEl.querySelector(".msg-caret");
    if (caret) caret.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function revealCardsForHandoff(rowEl) {
    setRowExpanded(rowEl, true);
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

  var PY_RE = new RegExp(
    [
      "(#[^\\n]*)",
      "(\"\"\"[\\s\\S]*?\"\"\"|'''[\\s\\S]*?'''|\"(?:\\\\.|[^\"\\\\])*\"|'(?:\\\\.|[^'\\\\])*')",
      "(@[A-Za-z_]\\w*)",
      "\\b(False|None|True|and|as|assert|async|await|break|class|continue|def|del|" +
        "elif|else|except|finally|for|from|global|if|import|in|is|lambda|nonlocal|" +
        "not|or|pass|raise|return|try|while|with|yield)\\b",
      "\\b(\\d+\\.?\\d*)\\b",
    ].join("|"),
    "g"
  );

  var JSON_RE = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;

  function highlightPython(code) {
    var out = "";
    var last = 0;
    var m;
    PY_RE.lastIndex = 0;
    while ((m = PY_RE.exec(code)) !== null) {
      if (m.index > last) out += escapeHtml(code.slice(last, m.index));
      var cls = m[1] ? "com" : m[2] ? "str" : m[3] ? "dec" : m[4] ? "kw" : "num";
      out += '<span class="ob-t-' + cls + '">' + escapeHtml(m[0]) + "</span>";
      last = m.index + m[0].length;
    }
    return out + escapeHtml(code.slice(last));
  }

  function highlightJson(code) {
    var out = "";
    var last = 0;
    var m;
    JSON_RE.lastIndex = 0;
    while ((m = JSON_RE.exec(code)) !== null) {
      if (m.index > last) out += escapeHtml(code.slice(last, m.index));
      if (m[1]) {
        out += '<span class="ob-t-' + (m[2] ? "key" : "str") + '">' + escapeHtml(m[1]) + "</span>";
        if (m[2]) out += escapeHtml(m[2]);
      } else if (m[3]) {
        out += '<span class="ob-t-lit">' + escapeHtml(m[3]) + "</span>";
      } else {
        out += '<span class="ob-t-num">' + escapeHtml(m[4]) + "</span>";
      }
      last = m.index + m[0].length;
    }
    return out + escapeHtml(code.slice(last));
  }

  function highlight(code, lang) {
    if (lang === "python") return highlightPython(code);
    if (lang === "json") return highlightJson(code);
    return escapeHtml(code);
  }

  // @nonobvious(means): each line is highlighted on its own, so a half-written
  // line arriving mid-stream degrades to plain text instead of miscolouring
  // everything after it, and no re-parse of the whole buffer is needed per frame.
  // The cost is that a string spanning several lines is not carried across them.
  function renderCode(code, lang, opts) {
    opts = opts || {};
    var lines = String(code == null ? "" : code).split("\n");
    var body = lines
      .map(function (line, i) {
        var num = opts.lineNumbers === false ? "" : '<span class="ob-code-num">' + (i + 1) + "</span>";
        return '<span class="ob-code-line">' + num + '<span class="ob-code-text">' +
          (highlight(line, lang) || "&nbsp;") + "</span></span>";
      })
      .join("");
    return '<pre class="ob-code"><code>' + body + "</code></pre>";
  }

  var SEND_ICON =
    '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" ' +
    'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M12 19V5M5 12l7-7 7 7"/></svg>';
  var STOP_ICON =
    '<svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor">' +
    '<rect x="6" y="6" width="12" height="12" rx="2"/></svg>';

  function PromptInput(form, opts) {
    opts = opts || {};
    this.form = form;
    this.input = form.querySelector(".ob-composer-input");
    this.mirror = form.querySelector(".ob-composer-mirror");
    this.sendBtn = form.querySelector(".ob-composer-send");
    this.minRows = opts.minRows || 1;
    this.maxRows = opts.maxRows || 8;
    this.onSubmit = opts.onSubmit;
    this.onStop = opts.onStop;
    this._native = !!opts.native;
    this._loading = null;
    this._chrome = null;
    this._bind();
    this.setLoading(false);
    this.resize();
  }

  PromptInput.prototype._bind = function () {
    var self = this;
    this.input.addEventListener("input", function () {
      self.resize();
    });
    this.input.addEventListener("keydown", function (e) {
      if (e.key !== "Enter" || e.shiftKey) return;
      // @nonobvious(forced-by): an IME candidate window commits on Enter, and
      // submitting there would send half-typed text in Japanese or Chinese.
      if (e.isComposing || (e.nativeEvent && e.nativeEvent.isComposing)) return;
      e.preventDefault();
      self.submit();
    });
    if (!this._native) {
      this.form.addEventListener("submit", function (e) {
        e.preventDefault();
        self.submit();
      });
    }
    if (this.sendBtn) {
      this.sendBtn.addEventListener("click", function (e) {
        if (!self._loading) return;
        e.preventDefault();
        if (self.onStop) self.onStop();
      });
    }
    if (global.ResizeObserver) {
      this._ro = new ResizeObserver(function () {
        self._metrics = null;
        self.resize();
      });
      this._ro.observe(this.input);
    }
  };

  var MIRRORED_STYLES = [
    "fontFamily",
    "fontSize",
    "fontWeight",
    "fontStyle",
    "letterSpacing",
    "wordSpacing",
    "lineHeight",
    "textTransform",
    "paddingTop",
    "paddingRight",
    "paddingBottom",
    "paddingLeft",
    "borderTopWidth",
    "borderRightWidth",
    "borderBottomWidth",
    "borderLeftWidth",
    "boxSizing",
  ];

  // @nonobvious(must-hold): the twin only measures the true height while its box
  // matches the real one exactly, so it copies the computed metrics rather than
  // assuming any, and each host page is free to style its own field.
  PromptInput.prototype._measure = function () {
    if (this._metrics) return this._metrics;
    var cs = global.getComputedStyle(this.input);
    var style = this.mirror.style;
    MIRRORED_STYLES.forEach(function (key) {
      style[key] = cs[key];
    });
    style.borderStyle = "solid";
    style.borderColor = "transparent";
    this._metrics = {
      lineHeight: parseFloat(cs.lineHeight) || 20,
      chrome:
        parseFloat(cs.paddingTop) +
        parseFloat(cs.paddingBottom) +
        parseFloat(cs.borderTopWidth) +
        parseFloat(cs.borderBottomWidth),
    };
    return this._metrics;
  };

  // @nonobvious(forced-by): a textarea cannot report the height its content
  // wants without first being shrunk, which flickers; an off-screen twin with
  // the same box measures it instead. The zero-width space keeps a trailing
  // newline from collapsing.
  PromptInput.prototype.resize = function () {
    if (!this.mirror) return;
    this.mirror.textContent = this.input.value + "​";
    var m = this._measure();
    var min = this.minRows * m.lineHeight + m.chrome;
    var max = this.maxRows * m.lineHeight + m.chrome;
    var wanted = this.mirror.scrollHeight;
    this.input.style.height = Math.min(Math.max(wanted, min), max) + "px";
    this.input.style.overflowY = wanted > max ? "auto" : "hidden";
  };

  PromptInput.prototype.value = function () {
    return this.input.value.trim();
  };

  PromptInput.prototype.clear = function () {
    this.input.value = "";
    this.resize();
  };

  PromptInput.prototype.submit = function () {
    if (this._loading || !this.value()) return;
    if (this._native) {
      if (this.form.requestSubmit) this.form.requestSubmit();
      else this.form.submit();
      return;
    }
    if (this.onSubmit) this.onSubmit(this.value());
  };

  PromptInput.prototype.setLoading = function (loading) {
    if (this._loading === !!loading) return;
    this._loading = !!loading;
    this.form.classList.toggle("is-loading", this._loading);
    this.input.readOnly = this._loading;
    if (!this.sendBtn) return;
    this.sendBtn.innerHTML = this._loading ? STOP_ICON : SEND_ICON;
    this.sendBtn.setAttribute("aria-label", this._loading ? "Stop" : "Send");
    this.sendBtn.title = this._loading ? "Stop the agent" : "Send";
    this.sendBtn.type = this._loading ? "button" : "submit";
    this.sendBtn.disabled = this._loading && !this.onStop;
  };

  var TODO_CIRCUMFERENCE = 2 * Math.PI * 8;

  function todoIcon(state, fraction) {
    var ring =
      '<circle cx="10" cy="10" r="8" fill="none" stroke="currentColor" stroke-width="1.6"' +
      (state === "pending" ? ' stroke-dasharray="2 3"' : "") +
      ' opacity="' + (state === "pending" ? "0.5" : "0.28") + '"/>';
    var mark = "";
    if (state === "done") {
      mark =
        '<path d="M6.2 10.2 8.8 12.8 13.8 7.4" fill="none" stroke="currentColor" ' +
        'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>';
    } else if (state === "absent") {
      mark =
        '<path d="M7 7 13 13M13 7 7 13" fill="none" stroke="currentColor" ' +
        'stroke-width="1.7" stroke-linecap="round"/>';
    } else if (state === "partial") {
      var filled = Math.max(0, Math.min(1, fraction || 0)) * TODO_CIRCUMFERENCE;
      mark =
        '<circle cx="10" cy="10" r="8" fill="none" stroke="currentColor" ' +
        'stroke-width="1.9" stroke-linecap="round" transform="rotate(-90 10 10)" ' +
        'stroke-dasharray="' + filled + " " + TODO_CIRCUMFERENCE + '"/>';
    }
    return '<svg class="ob-todo-ico" viewBox="0 0 20 20" width="15" height="15">' + ring + mark + "</svg>";
  }

  function TodoList(container) {
    this.container = container;
    this.container.className = "ob-todo hidden";
    this.container.innerHTML =
      '<button type="button" class="ob-todo-head">' +
      '<span class="ob-todo-title">Schema coverage</span>' +
      '<span class="ob-todo-count"></span><span class="ob-todo-chev"></span></button>' +
      '<div class="ob-todo-body ob-reveal is-open"><ol class="ob-reveal-inner ob-todo-list"></ol></div>';
    this.head = this.container.querySelector(".ob-todo-head");
    this.count = this.container.querySelector(".ob-todo-count");
    this.body = this.container.querySelector(".ob-todo-body");
    this.list = this.container.querySelector(".ob-todo-list");
    this._signature = null;
    var self = this;
    this.head.addEventListener("click", function () {
      self.body.classList.toggle("is-open");
      self.container.classList.toggle("is-closed", !self.body.classList.contains("is-open"));
    });
  }

  TodoList.prototype.update = function (items) {
    if (!items || !items.length) {
      this.container.classList.add("hidden");
      return;
    }
    var signature = JSON.stringify(items);
    if (signature === this._signature) return;
    this._signature = signature;
    this.container.classList.remove("hidden");
    var done = 0;
    var html = items
      .map(function (it) {
        if (it.state === "done") done += 1;
        var detail = it.detail ? '<span class="ob-todo-detail">' + escapeHtml(it.detail) + "</span>" : "";
        return (
          '<li class="ob-todo-row is-' + escapeHtml(it.state) + '">' +
          todoIcon(it.state, it.fraction) +
          '<span class="ob-todo-field">' + escapeHtml(it.field) + "</span>" +
          detail +
          "</li>"
        );
      })
      .join("");
    this.list.innerHTML = html;
    this.count.textContent = done + "/" + items.length;
  };

  global.OpenBrowseAgents = {
    renderMdLite: renderMdLite,
    reveal: { open: revealOpen, close: revealClose },
    Typewriter: Typewriter,
    StreamingResponse: StreamingResponse,
    PromptInput: PromptInput,
    renderCode: renderCode,
    highlight: highlight,
    TodoList: TodoList,
    setRowExpanded: setRowExpanded,
    handoff: { revealCards: revealCardsForHandoff, fadeRowIn: fadeRowIn },
  };
})(window);
