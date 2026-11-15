  // ---------- price chart (shared by the Value Screener and Small-Cap Growth pages) ----------
  // Source: price_chart.js, pasted into both pages at build time. Needs from the page:
  // RAW, PAGE_KEY (the key that unlocked the page, or null), esc(), px(), b64d().
  // Price files: px/<SHA-256(key + ticker), 24 hex>.enc (or px/<TICKER>.json when unlocked),
  // written by smallcap_prices.py.
  var pxCache = {}, pxRange = "1D";
  function hex(buf) {
    return Array.prototype.map.call(new Uint8Array(buf), function (b) { return ("0" + b.toString(16)).slice(-2); }).join("");
  }
  function loadPrices(ticker) {
    if (pxCache[ticker]) return Promise.resolve(pxCache[ticker]);
    var p;
    if (!RAW.locked) {
      p = fetch("px/" + encodeURIComponent(ticker.toUpperCase()) + ".json", { cache: "no-cache" })
        .then(function (r) { if (!r.ok) throw 0; return r.json(); });
    } else {
      if (!PAGE_KEY) return Promise.reject();
      // File name = SHA-256(raw key + ticker), same as smallcap_prices.py, so the
      // public file list doesn't reveal which tickers are on the list.
      p = crypto.subtle.exportKey("raw", PAGE_KEY).then(function (raw) {
        var tk = new TextEncoder().encode(ticker.toUpperCase());
        var both = new Uint8Array(raw.byteLength + tk.length);
        both.set(new Uint8Array(raw), 0); both.set(tk, raw.byteLength);
        return crypto.subtle.digest("SHA-256", both);
      }).then(function (h) {
        return fetch("px/" + hex(h).slice(0, 24) + ".enc", { cache: "no-cache" });
      }).then(function (r) { if (!r.ok) throw 0; return r.json(); })
        .then(function (b) {
          return crypto.subtle.decrypt({ name: "AES-GCM", iv: b64d(b.iv) }, PAGE_KEY, b64d(b.ct));
        }).then(function (pt) { return JSON.parse(new TextDecoder().decode(pt)); });
    }
    return p.then(function (d) { pxCache[ticker] = d; return d; });
  }

  var RANGE_LABEL = { "1D": "Today", "1W": "Past week", "1M": "Past month", "1Y": "Past year",
                      "5Y": "Past 5 years", "ALL": "All time" };
  function when(ts, range) {
    var d = new Date(ts * 1000);
    var o = range === "1D" ? { hour: "numeric", minute: "2-digit", timeZone: "America/New_York" }
      : range === "1W" ? { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "America/New_York" }
      : { month: "short", day: "numeric", year: "numeric", timeZone: range === "1M" || range === "1Y" || range === "5Y" || range === "ALL" ? "UTC" : "America/New_York" };
    return d.toLocaleString("en-US", o) + (range === "1D" ? " ET" : "");
  }

  function priceChart(el, d, c) {
    if (!el) return;
    var R = d.ranges || {}, order = ["1D", "1W", "1M", "1Y", "5Y", "ALL"].filter(function (k) { return R[k]; });
    if (!order.length) throw 0;
    if (!R[pxRange]) pxRange = order.indexOf("1Y") >= 0 ? "1Y" : order[0];
    var W = 340, H = 170, pad = 8;
    el.innerHTML = '<div class="pxtop"><b id="pxNow"></b><span id="pxChg"></span></div>' +
      '<div class="pxsvg" id="pxSvgBox"></div>' +
      '<div class="pxranges">' + order.map(function (k) {
        return '<button type="button" data-r="' + k + '"' + (k === pxRange ? ' class="on"' : '') + '>' + k + '</button>';
      }).join('') + '</div>';
    var chg = el.querySelector("#pxChg"), box = el.querySelector("#pxSvgBox");

    function draw() {
      var r = R[pxRange], cs = r.c, ts = r.t, n = cs.length;
      var base = r.base != null ? r.base : cs[0], last = cs[n - 1];
      var up = last >= base, color = up ? "var(--good-ink)" : "var(--crit)";
      var vals = cs.concat(pxRange === "1D" && r.base != null ? [r.base] : []);
      var hi = Math.max.apply(null, vals), lo = Math.min.apply(null, vals);
      if (hi === lo) { hi += 0.01; lo -= 0.01; }
      // 1D: spread the session across the full trading day (9:30–4:00 ET), like Robinhood.
      var t0 = ts[0], t1 = ts[n - 1];
      if (pxRange === "1D") t1 = Math.max(t1, t0 + 6.5 * 3600 - 300);
      var x = function (i) {
        if (pxRange === "1D") return pad + (ts[i] - t0) / Math.max(1, t1 - t0) * (W - 2 * pad);
        return pad + (n === 1 ? 0 : i / (n - 1)) * (W - 2 * pad);
      };
      var y = function (v) { return pad + (hi - v) / (hi - lo) * (H - 2 * pad); };
      var path = cs.map(function (v, i) { return (i ? "L" : "M") + x(i).toFixed(1) + "," + y(v).toFixed(1); }).join("");
      var svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="' + esc(c.ticker) + ' price, ' + RANGE_LABEL[pxRange] + '">';
      if (pxRange === "1D" && r.base != null)
        svg += '<line x1="0" x2="' + W + '" y1="' + y(r.base) + '" y2="' + y(r.base) + '" stroke="var(--text-3)" stroke-width="1" stroke-dasharray="2 4"/>';
      svg += '<path d="' + path + '" fill="none" stroke="' + color + '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>' +
        '<line id="pxCur" x1="0" x2="0" y1="0" y2="' + H + '" stroke="var(--text-3)" stroke-width="1" visibility="hidden"/>' +
        '<circle id="pxDot" r="4.5" fill="' + color + '" visibility="hidden"/></svg>';
      box.innerHTML = svg;

      function show(i) {
        var v = cs[i], diff = v - base, p = base ? diff / base * 100 : 0;
        var hb = document.querySelector(".pxhead b"); if (hb) hb.textContent = px(v);
        chg.innerHTML = '<span class="' + (diff >= 0 ? "pos" : "neg") + '">' + (diff >= 0 ? "+" : "−") + "$" +
          Math.abs(diff).toFixed(Math.abs(diff) < 1 ? 3 : 2) + " (" + (p >= 0 ? "+" : "−") + Math.abs(p).toFixed(2) + "%)</span> " + esc(i === n - 1 && !scrub ? RANGE_LABEL[pxRange] : when(ts[i], pxRange));
      }
      var scrub = false, s = box.querySelector("svg"), cur = s.querySelector("#pxCur"), dot = s.querySelector("#pxDot");
      function at(ev) {
        var rc = s.getBoundingClientRect(), gx = (ev.clientX - rc.left) / rc.width * W, best = 0, bd = 1e9;
        for (var i = 0; i < n; i++) { var dd = Math.abs(x(i) - gx); if (dd < bd) { bd = dd; best = i; } }
        scrub = true;
        cur.setAttribute("x1", x(best)); cur.setAttribute("x2", x(best)); cur.setAttribute("visibility", "visible");
        dot.setAttribute("cx", x(best)); dot.setAttribute("cy", y(cs[best])); dot.setAttribute("visibility", "visible");
        show(best);
      }
      function done() {
        scrub = false; cur.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); show(n - 1);
      }
      s.addEventListener("pointerdown", function (e) { s.setPointerCapture && s.setPointerCapture(e.pointerId); at(e); });
      s.addEventListener("pointermove", function (e) { if (scrub || e.pointerType === "mouse") at(e); });
      s.addEventListener("pointerup", done);
      s.addEventListener("pointercancel", done);
      s.addEventListener("pointerleave", function (e) { if (e.pointerType === "mouse") done(); });
      show(n - 1);
      Array.prototype.forEach.call(el.querySelectorAll(".pxranges button"), function (b) {
        b.classList.toggle("on", b.getAttribute("data-r") === pxRange);
        b.style.color = b.getAttribute("data-r") === pxRange ? color : "";
      });
    }
    el.querySelector(".pxranges").addEventListener("click", function (e) {
      var r = e.target.getAttribute && e.target.getAttribute("data-r");
      if (r && R[r]) { pxRange = r; draw(); }
    });
    // Headline price = latest close in the chart data (fresher than the weekly list).
    var d1 = R["1D"] || R["1M"] || R[order[0]], lt = d1.t[d1.t.length - 1];
    var head = document.querySelector(".pxhead");
    if (head) head.innerHTML = '<b>' + px(d1.c[d1.c.length - 1]) + '</b><span>last price ' +
      esc(new Date(lt * 1000).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "America/New_York" })) +
      ' ET · <a href="https://finance.yahoo.com/quote/' + encodeURIComponent(c.ticker.replace(".", "-")) +
      '" target="_blank" rel="noopener">live price</a></span>';
    draw();
  }


  function startPriceChart(c) {
    loadPrices(c.ticker).then(function (d) {
      if (decodeURIComponent(location.hash.replace(/^#\//, "")) !== c.ticker) return;   // moved on
      priceChart(document.getElementById("pxchart"), d, c);
    }).catch(function () {
      var el = document.getElementById("pxchart");
      if (el) el.innerHTML = '<p class="muted">Price chart not available yet. It appears after the next daily update.</p>';
    });
  }
