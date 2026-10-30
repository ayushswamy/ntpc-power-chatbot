/*
 * NTPC Power Data Assistant - chat widget (plain JavaScript, no build step).
 *
 * Add ONE line to any web page:
 *   <script src="https://<chatbot-server>/widget/ntpc-chat.js" data-endpoint="/chatbot"></script>
 *
 * Settings (data-* attributes on the script tag):
 *   data-endpoint  where to send questions. Production: the ASP.NET proxy path (e.g. "/chatbot"),
 *                  which adds the API key on the server. Default: the server this file came from.
 *   data-api-key   ONLY for local testing (demo.html). Never put the real key in a production page.
 *   data-title     panel title (default "Power Data Assistant")
 *   data-accent    main colour (default #1d5f7c)
 *   data-position  "right" (default) or "left"
 *
 * The widget draws itself inside a Shadow DOM, so the page's CSS cannot change it and its
 * CSS cannot change the page. All server text is inserted as plain text (never as HTML).
 */
(function () {
  "use strict";
  if (window.__ntpcChatLoaded) return;          // the tag was added twice
  window.__ntpcChatLoaded = true;

  var script = document.currentScript;
  var scriptUrl = new URL(script.src, window.location.href);
  var BASE = scriptUrl.href.replace(/[^/]*$/, "");                  // folder of this file (for Chart.js)
  var cfg = {
    endpoint: (script.dataset.endpoint || scriptUrl.origin).replace(/\/$/, ""),
    apiKey: script.dataset.apiKey || "",
    title: script.dataset.title || "Power Data Assistant",
    accent: script.dataset.accent || "#1d5f7c",
    left: script.dataset.position === "left"
  };
  var SESSION_KEY = "ntpcChatSession";
  var STEPS = {
    waiting: "Waiting for the previous question to finish…",
    started: "Starting…",
    planning: "Understanding your question…",
    querying: "Fetching data…",
    "writing answer": "Writing the answer…"
  };
  var EXAMPLES = ["Current total generation of NTPC", "PLF of Singrauli yesterday", "Top 5 coal plants by PLF this month"];

  // ------------------------------------------------------------------ styles
  var CSS = [
    ":host{all:initial}",
    "*{box-sizing:border-box;font-family:'Segoe UI',Roboto,Arial,sans-serif}",
    ".launcher{position:fixed;bottom:20px;" + (cfg.left ? "left" : "right") + ":20px;width:56px;height:56px;border-radius:50%;border:0;",
    "background:var(--accent);color:#fff;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.25);z-index:2147483000;display:grid;place-items:center}",
    ".launcher:hover{filter:brightness(1.1)}.launcher svg{width:26px;height:26px}",
    ".panel{position:fixed;bottom:88px;" + (cfg.left ? "left" : "right") + ":20px;width:400px;max-width:calc(100vw - 32px);",
    "height:640px;max-height:calc(100vh - 110px);background:#fff;color:#17212c;border:1px solid #d6dde5;border-radius:12px;",
    "box-shadow:0 12px 40px rgba(0,0,0,.22);display:flex;flex-direction:column;overflow:hidden;z-index:2147483001;font-size:14px}",
    ".panel[hidden]{display:none}",
    "header{display:flex;align-items:center;gap:8px;padding:12px 14px;background:var(--accent);color:#fff}",
    "header .title{flex:1;font-weight:600;font-size:15px}",
    "header button{background:rgba(255,255,255,.15);color:#fff;border:0;border-radius:6px;padding:6px 10px;cursor:pointer;font-size:13px}",
    "header button:hover{background:rgba(255,255,255,.28)}",
    ".messages{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:12px;background:#f5f7fa}",
    ".msg{max-width:92%;padding:10px 12px;border-radius:10px;line-height:1.45;white-space:pre-wrap;word-wrap:break-word}",
    ".user{align-self:flex-end;background:var(--accent);color:#fff;border-bottom-right-radius:3px}",
    ".bot{align-self:flex-start;background:#fff;border:1px solid #dde3ea;border-bottom-left-radius:3px;width:92%}",
    ".status{color:#566273;font-style:italic;display:flex;align-items:center;gap:8px}",
    ".dot{width:8px;height:8px;border-radius:50%;background:var(--accent);animation:pulse 1s infinite alternate}",
    "@keyframes pulse{from{opacity:.25}to{opacity:1}}",
    ".badge{display:inline-block;font-size:11px;font-weight:600;letter-spacing:.03em;padding:3px 7px;border-radius:4px;margin-bottom:6px}",
    ".badge.live{background:#e2f1e8;color:#1f6b40}.badge.daily{background:#e8eef4;color:#3a4a5c}.badge.mixed{background:#f3ead8;color:#7a5410}",
    ".error{color:#a3392d}",
    ".chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}",
    ".chip{border:1px solid var(--accent);color:var(--accent);background:#fff;border-radius:16px;padding:5px 10px;cursor:pointer;font-size:13px}",
    ".chip:hover{background:var(--accent);color:#fff}",
    ".tablewrap{overflow-x:auto;margin-top:8px;border:1px solid #e3e8ee;border-radius:6px}",
    "table{border-collapse:collapse;width:100%;font-size:12.5px;white-space:nowrap}",
    "th,td{padding:5px 8px;border-bottom:1px solid #eef1f4;text-align:left}",
    "th{background:#f0f3f6;color:#566273;font-weight:600}td.num{text-align:right;font-variant-numeric:tabular-nums}",
    ".more{margin-top:6px;background:none;border:0;color:var(--accent);cursor:pointer;padding:0;font-size:13px}",
    ".chart{position:relative;height:220px;margin-top:10px}",
    "details{margin-top:8px;font-size:12.5px;color:#566273}summary{cursor:pointer;color:var(--accent)}",
    "details code{display:block;white-space:pre-wrap;background:#f5f7fa;border:1px solid #e3e8ee;border-radius:4px;padding:6px;margin-top:4px;font-size:11.5px}",
    "form{display:flex;gap:8px;padding:10px;border-top:1px solid #e3e8ee;background:#fff}",
    "textarea{flex:1;resize:none;border:1px solid #c9d2dc;border-radius:8px;padding:9px 10px;font-size:14px;max-height:110px;color:#17212c}",
    "textarea:focus,button:focus-visible{outline:2px solid var(--accent);outline-offset:1px}",
    "form button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:0 16px;cursor:pointer;font-size:14px}",
    "form button:disabled,textarea:disabled{opacity:.55;cursor:not-allowed}",
    ".foot{font-size:11px;color:#7a8594;padding:0 12px 8px;background:#fff}",
    "@media (max-width:600px){.panel{inset:0;width:100%;max-width:none;height:100%;max-height:none;border-radius:0}",
    ".launcher.open{display:none}}",
    "@media (prefers-reduced-motion:reduce){.dot{animation:none}}"
  ].join("");

  // ------------------------------------------------------------------ small helpers
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function getSession() { try { return sessionStorage.getItem(SESSION_KEY); } catch (e) { return null; } }
  function setSession(id) { try { id ? sessionStorage.setItem(SESSION_KEY, id) : sessionStorage.removeItem(SESSION_KEY); } catch (e) { /* storage blocked */ } }
  function fmt(v) {
    if (typeof v === "number" && isFinite(v)) return v.toLocaleString("en-US", { maximumFractionDigits: 3 });
    return v === null || v === undefined ? "" : String(v);
  }
  var UNITS = { mw: "MW", mu: "MU", plf: "PLF", ag: "AG", dc: "DC", sg: "SG" };
  function label(col) {   // "capacity_mw" -> "Capacity MW"
    var text = String(col).split("_").map(function (w) { return UNITS[w.toLowerCase()] || w; }).join(" ");
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  var chartLib = null;
  function loadChartJs() {
    if (window.Chart) return Promise.resolve(window.Chart);
    if (chartLib) return chartLib;
    chartLib = new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = BASE + "vendor/chart.umd.min.js";
      s.onload = function () { resolve(window.Chart); };
      s.onerror = function () { reject(new Error("chart library not found")); };
      document.head.appendChild(s);
    });
    return chartLib;
  }

  // Read a Server-Sent Events stream from fetch(): calls onEvent(name, data) per event.
  function readEvents(body, onEvent) {
    var reader = body.getReader(), decoder = new TextDecoder(), buf = "";
    function pump() {
      return reader.read().then(function (r) {
        if (r.done) return;
        buf += decoder.decode(r.value, { stream: true });
        var m;
        while ((m = buf.match(/\r?\n\r?\n/))) {
          var block = buf.slice(0, m.index), name = "message", data = [];
          buf = buf.slice(m.index + m[0].length);
          block.split(/\r?\n/).forEach(function (line) {
            if (line.indexOf("event:") === 0) name = line.slice(6).trim();
            else if (line.indexOf("data:") === 0) data.push(line.slice(5).replace(/^ /, ""));
          });
          if (data.length) {
            var parsed = {};
            try { parsed = JSON.parse(data.join("\n")); } catch (e) { /* ignore a broken event */ }
            onEvent(name, parsed);
          }
        }
        return pump();
      });
    }
    return pump();
  }

  // ------------------------------------------------------------------ build the UI
  var host = document.createElement("div");
  host.id = "ntpc-chat-widget";
  document.body.appendChild(host);
  var root = host.attachShadow({ mode: "open" });
  var style = el("style");
  style.textContent = CSS;
  root.appendChild(style);
  host.style.setProperty("--accent", cfg.accent);

  var launcher = el("button", "launcher");
  launcher.setAttribute("aria-label", "Open " + cfg.title);
  launcher.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">' +
    '<path d="M4 5h16v11H8l-4 4z" stroke-linejoin="round"/><path d="M8 10h8M8 13h5" stroke-linecap="round"/></svg>';

  var panel = el("section", "panel");
  panel.hidden = true;
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", cfg.title);
  var header = el("header");
  var newBtn = el("button", "", "New chat");
  var closeBtn = el("button", "", "✕");
  closeBtn.setAttribute("aria-label", "Close");
  header.appendChild(el("span", "title", cfg.title));
  header.appendChild(newBtn);
  header.appendChild(closeBtn);
  var messages = el("div", "messages");
  messages.setAttribute("role", "log");
  messages.setAttribute("aria-live", "polite");
  var form = el("form");
  var input = el("textarea");
  input.id = "ntpc-chat-input";
  input.rows = 1;
  input.placeholder = "Ask about generation, PLF, outages, coal…";
  input.setAttribute("aria-label", "Your question");
  input.maxLength = 1000;
  var sendBtn = el("button", "", "Send");
  sendBtn.type = "submit";
  form.appendChild(input);
  form.appendChild(sendBtn);
  var foot = el("div", "foot", "Answers come from NTPC's plant database. Live values show the time of the data.");
  panel.appendChild(header);
  panel.appendChild(messages);
  panel.appendChild(form);
  panel.appendChild(foot);
  root.appendChild(launcher);
  root.appendChild(panel);

  function scrollDown() { messages.scrollTop = messages.scrollHeight; }

  function welcome() {
    var box = el("div", "msg bot");
    box.appendChild(el("div", "", "Hello! Ask me about NTPC power data: live generation, today so far, PLF, availability, outages, trips, coal stock and capacity."));
    var chips = el("div", "chips");
    EXAMPLES.forEach(function (q) {
      var c = el("button", "chip", q);
      c.type = "button";
      c.addEventListener("click", function () { send(q); });
      chips.appendChild(c);
    });
    box.appendChild(chips);
    messages.appendChild(box);
  }

  function open() { panel.hidden = false; launcher.classList.add("open"); input.focus(); }
  function close() { panel.hidden = true; launcher.classList.remove("open"); launcher.focus(); }
  launcher.addEventListener("click", function () { panel.hidden ? open() : close(); });
  closeBtn.addEventListener("click", close);
  panel.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });
  newBtn.addEventListener("click", function () {
    setSession(null);
    messages.textContent = "";
    welcome();
    input.focus();
  });

  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event("submit")); }
  });
  input.addEventListener("input", function () { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 110) + "px"; });
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var q = input.value.trim();
    if (q) send(q);
  });

  var busy = false;
  function setBusy(b) { busy = b; input.disabled = b; sendBtn.disabled = b; if (!b) input.focus(); }

  // ------------------------------------------------------------------ rendering one answer
  function renderTable(box, table) {
    var wrap = el("div", "tablewrap"), t = el("table"), thead = el("thead"), tr = el("tr");
    table.columns.forEach(function (c) { tr.appendChild(el("th", "", label(c))); });
    thead.appendChild(tr);
    t.appendChild(thead);
    var tbody = el("tbody");
    function fill(limit) {
      tbody.textContent = "";
      table.rows.slice(0, limit).forEach(function (row) {
        var r = el("tr");
        row.forEach(function (v) { r.appendChild(el("td", typeof v === "number" ? "num" : "", fmt(v))); });
        tbody.appendChild(r);
      });
    }
    fill(10);
    t.appendChild(tbody);
    wrap.appendChild(t);
    box.appendChild(wrap);
    if (table.rows.length > 10) {
      var more = el("button", "more", "Show all " + table.rows.length + " rows");
      more.type = "button";
      more.addEventListener("click", function () { fill(table.rows.length); more.remove(); });
      box.appendChild(more);
    }
  }

  function renderChart(box, chart) {
    var holder = el("div", "chart"), canvas = el("canvas");
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", chart.title || "chart");
    holder.appendChild(canvas);
    box.appendChild(holder);
    loadChartJs().then(function (Chart) {
      new Chart(canvas, {
        type: chart.type === "line" ? "line" : "bar",
        data: {
          labels: chart.x,
          datasets: chart.series.map(function (s) {
            return { label: s.name, data: s.data, borderColor: cfg.accent, backgroundColor: cfg.accent + "55",
                     borderWidth: 2, pointRadius: chart.x.length > 40 ? 0 : 2, tension: 0.2 };
          })
        },
        options: {
          responsive: true, maintainAspectRatio: false, animation: false,
          plugins: { legend: { display: chart.series.length > 1 }, title: { display: true, text: chart.title, color: "#17212c" } },
          scales: { x: { ticks: { autoSkip: true, maxRotation: 0, color: "#566273" } }, y: { ticks: { color: "#566273" } } }
        }
      });
    }).catch(function () { holder.replaceWith(el("div", "error", "(The chart could not be drawn.)")); });
  }

  function renderDetails(box, a) {
    var d = el("details"), s = el("summary", "", "Show details");
    d.appendChild(s);
    var kind = { live: "Live data", daily: "Daily reports", mixed: "Live + daily reports", none: "No data query" }[a.source] || a.source;
    var lines = [];
    if (kind) lines.push("Data: " + kind);
    if (a.as_of) lines.push("Data time: " + a.as_of);
    if (a.sources && a.sources.length) lines.push("Tables: " + a.sources.join(", "));
    if (a.timings && a.timings.total) lines.push("Answered in " + a.timings.total + " s");
    lines.forEach(function (l) { d.appendChild(el("div", "", l)); });
    (a.sql_debug || []).forEach(function (q) { d.appendChild(el("code", "", q)); });
    box.appendChild(d);
  }

  function renderAnswer(box, textEl, a) {
    box.textContent = "";
    if (a.is_live && a.as_of) {
      box.appendChild(el("span", "badge " + (a.source === "mixed" ? "mixed" : "live"),
        (a.source === "mixed" ? "Live + daily · as of " : "Live · as of ") + String(a.as_of).slice(-5)));
    } else if (a.source === "daily") {
      box.appendChild(el("span", "badge daily", "Daily reports"));
    }
    textEl.textContent = a.text || "";
    box.appendChild(textEl);
    if (a.options && a.options.length) {
      var chips = el("div", "chips");
      a.options.forEach(function (o) {
        var c = el("button", "chip", o);
        c.type = "button";
        c.addEventListener("click", function () { send(o); });
        chips.appendChild(c);
      });
      box.appendChild(chips);
    }
    if (a.table && a.table.rows && a.table.rows.length) renderTable(box, a.table);
    if (a.chart && a.chart.x && a.chart.x.length) renderChart(box, a.chart);
    if (a.source && a.source !== "none") renderDetails(box, a);
  }

  // ------------------------------------------------------------------ sending a question
  function send(question) {
    if (busy) return;
    input.value = "";
    input.style.height = "auto";
    messages.appendChild(el("div", "msg user", question));
    var box = el("div", "msg bot");
    var status = el("div", "status");
    status.appendChild(el("span", "dot"));
    var statusText = el("span", "", "Sending…");
    status.appendChild(statusText);
    box.appendChild(status);
    var textEl = el("div", "");
    messages.appendChild(box);
    scrollDown();
    setBusy(true);

    var headers = { "Content-Type": "application/json" };
    if (cfg.apiKey) headers["X-API-Key"] = cfg.apiKey;
    var gotAnswer = false;

    fetch(cfg.endpoint + "/chat", {
      method: "POST", headers: headers, credentials: "same-origin",
      body: JSON.stringify({ message: question, session_id: getSession() })
    }).then(function (res) {
      if (!res.ok) {
        return res.json().catch(function () { return {}; }).then(function (j) {
          throw new Error(j.detail || ("The chatbot could not be reached (error " + res.status + ")."));
        });
      }
      return readEvents(res.body, function (name, data) {
        if (name === "status") {
          statusText.textContent = STEPS[data.step] || "Working…";
        } else if (name === "token") {
          if (status.parentNode) { status.remove(); box.appendChild(textEl); }
          textEl.textContent += data.text || "";
        } else if (name === "answer") {
          gotAnswer = true;
          if (data.session_id) setSession(data.session_id);
          renderAnswer(box, textEl, data);
        } else if (name === "error") {
          box.textContent = "";
          box.appendChild(el("div", "error", data.message || "Something went wrong."));
        }
        scrollDown();
      });
    }).then(function () {
      if (!gotAnswer && status.parentNode) {
        box.textContent = "";
        box.appendChild(el("div", "error", "No answer was received. Please try again."));
      }
    }).catch(function (err) {
      box.textContent = "";
      box.appendChild(el("div", "error", err.message || "The chatbot could not be reached."));
    }).then(function () { setBusy(false); scrollDown(); });
  }

  welcome();
})();
