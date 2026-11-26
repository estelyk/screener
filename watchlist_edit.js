  // ---------- watchlist editing (shared by both pages; source: watchlist_edit.js) ----------
  // Writes the page's watchlist file (WLCFG.file) through the GitHub API. Needs a fine-grained
  // GitHub token for this one repo (Contents + Actions: read and write). The token is stored on
  // this device only, encrypted with the page key, sent only to api.github.com, and shared by
  // both pages. Each page defines WLCFG and wlRefresh() before this code.
  var TOKSTORE = "valueScreener.ghToken.v1", WLFILE = WLCFG.file;
  var WL = { list: null, sha: null, text: null, busy: false, msg: "", setup: false };

  function repoPath() {
    var h = location.hostname.match(/^([a-z0-9-]+)\.github\.io$/i);
    var seg = location.pathname.split("/").filter(Boolean)[0];
    return h && seg ? h[1] + "/" + seg : null;
  }
  function wlEnabled() { return !!(repoPath() && PAGE_KEY && window.crypto && crypto.subtle); }
  function tokenKey() {
    return crypto.subtle.exportKey("raw", PAGE_KEY).then(function (raw) {
      var tag = new TextEncoder().encode("watchlist-token");
      var both = new Uint8Array(raw.byteLength + tag.length);
      both.set(new Uint8Array(raw)); both.set(tag, raw.byteLength);
      return crypto.subtle.digest("SHA-256", both);
    }).then(function (k) { return crypto.subtle.importKey("raw", k, "AES-GCM", false, ["encrypt", "decrypt"]); });
  }
  function saveToken(tok) {
    var iv = crypto.getRandomValues(new Uint8Array(12));
    return tokenKey().then(function (k) {
      return crypto.subtle.encrypt({ name: "AES-GCM", iv: iv }, k, new TextEncoder().encode(tok));
    }).then(function (ct) { localStorage.setItem(TOKSTORE, JSON.stringify({ iv: b64e(iv), ct: b64e(ct) })); });
  }
  function loadToken() {
    var s; try { s = JSON.parse(localStorage.getItem(TOKSTORE)); } catch (e) { s = null; }
    if (!s || !PAGE_KEY) return Promise.resolve(null);
    return tokenKey().then(function (k) {
      return crypto.subtle.decrypt({ name: "AES-GCM", iv: b64d(s.iv) }, k, b64d(s.ct));
    }).then(function (pt) { return new TextDecoder().decode(pt); }, function () { return null; });
  }
  function forgetToken() { try { localStorage.removeItem(TOKSTORE); } catch (e) {} }

  function gh(tok, method, path, body) {
    return fetch("https://api.github.com/repos/" + repoPath() + path, {
      method: method, cache: "no-store",
      headers: { "Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28" },
      body: body ? JSON.stringify(body) : undefined
    }).then(function (r) {
      if (r.status === 204) return null;
      return r.json().then(function (j) {
        if (!r.ok) { var e = new Error((j && j.message) || ("GitHub error " + r.status)); e.status = r.status; throw e; }
        return j;
      });
    });
  }
  function b64utf8(s) { return btoa(unescape(encodeURIComponent(s))); }
  function unb64utf8(s) { return decodeURIComponent(escape(atob(s.replace(/\s/g, "")))); }
  // Same rules as read_watchlist() in run_smallcap.py.
  function parseList(text) {
    var out = [];
    text.split("\n").forEach(function (line) {
      line.split("#")[0].replace(/,/g, " ").split(/\s+/).forEach(function (w) {
        w = w.trim().toUpperCase().replace(/^\$/, "");
        if (/^[A-Z][A-Z0-9.\-]{0,9}$/.test(w) && out.indexOf(w) < 0) out.push(w);
      });
    });
    return out;
  }
  function cleanTicker(s) {
    var w = String(s || "").trim().toUpperCase().replace(/^\$/, "");
    return /^[A-Z][A-Z0-9.\-]{0,9}$/.test(w) ? w : null;
  }
  function wlErr(e) {
    if (e && (e.status === 401 || e.status === 403)) return "GitHub didn’t accept the key (expired or missing a permission). Set it up again below.";
    if (e && e.status === 404) return "GitHub couldn’t find the watchlist file with this key. Check the key is for the screener repo.";
    if (e && e.status === 409) return "The list changed somewhere else at the same moment. Try again.";
    return "Couldn’t reach GitHub. Check your connection and try again.";
  }

  function wlLoad() {
    if (!repoPath()) return Promise.resolve(null);
    return loadToken().then(function (tok) {
      if (!tok) return null;
      return readFile(tok).then(function () { return tok; });
    });
  }
  function readFile(tok) {
    return gh(tok, "GET", "/contents/" + WLFILE + "?ref=main").then(function (j) {
      WL.sha = j.sha; WL.text = unb64utf8(j.content); WL.list = parseList(WL.text);
    }, function (e) {
      if (!e || e.status !== 404) throw e;
      WL.sha = null; WL.text = WLCFG.header; WL.list = [];          // not created yet
    });
  }
  function wlWrite(tok, text, message) {
    var body = { message: message, content: b64utf8(text), branch: "main" };
    if (WL.sha) body.sha = WL.sha;
    return gh(tok, "PUT", "/contents/" + WLFILE, body)
      .then(function (j) { WL.sha = j.content.sha; WL.text = text; WL.list = parseList(text); });
  }
  function wlChange(ticker, add) {
    if (WL.busy) return;
    WL.busy = true; WL.msg = (add ? "Adding " : "Removing ") + ticker + "…"; wlRefresh();
    wlLoad().then(function (tok) {
      if (!tok) { WL.setup = true; WL.msg = ""; return; }
      var lines = WL.text.replace(/\s+$/, "").split("\n");
      if (add) {
        if (WL.list.indexOf(ticker) >= 0) { WL.msg = ticker + " is already on your watchlist."; return; }
        var bad = WLCFG.reject ? WLCFG.reject(ticker) : null;
        if (bad) { WL.msg = bad; return; }
        if (WL.list.length >= 50) { WL.msg = "The watchlist holds up to 50 tickers. Remove one first."; return; }
        lines.push(ticker);
      } else {
        lines = lines.filter(function (l) { return parseList(l.split("#")[0]).indexOf(ticker) < 0 || parseList(l).length !== 1; });
        if (parseList(lines.join("\n")).indexOf(ticker) >= 0) { WL.msg = "Couldn’t remove " + ticker + " automatically; edit the file on GitHub."; return; }
      }
      return wlWrite(tok, lines.join("\n") + "\n", (add ? "Watch " : "Unwatch ") + ticker + " (from the app)").then(function () {
        WL.msg = add ? WLCFG.added(ticker) : WLCFG.removed(ticker);
      });
    }).catch(function (e) { WL.msg = wlErr(e); if (e && (e.status === 401 || e.status === 403)) WL.setup = true; })
      .then(function () { WL.busy = false; wlRefresh(); });
  }
  function wlCheckNow() {
    if (WL.busy) return;
    if (!WLCFG.check || !confirm(WLCFG.check.ask)) return;
    WL.busy = true; WL.msg = "Starting the check…"; wlRefresh();
    loadToken().then(function (tok) {
      if (!tok) { WL.setup = true; WL.msg = ""; return; }
      return gh(tok, "POST", "/actions/workflows/" + WLCFG.check.workflow + "/dispatches", { ref: "main" }).then(function () {
        WL.msg = WLCFG.check.started;
      });
    }).catch(function (e) { WL.msg = wlErr(e); })
      .then(function () { WL.busy = false; wlRefresh(); });
  }
  function wlSaveSetup() {
    var el = document.getElementById("wlTok"), tok = el && el.value.trim();
    if (!tok) return;
    WL.busy = true; WL.msg = "Checking the key…"; wlRefresh();
    gh(tok, "GET", "").then(function (repo) {                 // 404 here = key can't see this repo
      if (repo && repo.permissions && repo.permissions.push === false) { var e = new Error("read only"); e.status = 403; throw e; }
      return readFile(tok);
    }).then(function () {
      return saveToken(tok).then(function () { WL.setup = false; WL.msg = "Key saved on this device (works on both pages). You can now add and remove tickers here."; });
    }).catch(function (e) { WL.msg = wlErr(e); })
      .then(function () { WL.busy = false; wlRefresh(); });
  }

  function isWatched(c) { return WL.list ? WL.list.indexOf(c.ticker) >= 0 : WLCFG.baked().indexOf(c.ticker) >= 0; }

  function wlControls() {
    if (!wlEnabled()) return '';
    var h = '<div class="wlbox">';
    if (WL.setup) {
      h += '<p><b>One-time setup:</b> let this app edit your watchlist.</p><ol class="muted">' +
        '<li>Open <a href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noopener">GitHub → new fine-grained token</a> (sign in if asked).</li>' +
        '<li>Name it “Screener watchlist”; expiration up to 1 year.</li>' +
        '<li>Repository access: <b>Only select repositories</b> → <b>' + esc(repoPath().split("/")[1]) + '</b>.</li>' +
        '<li>Repository permissions: <b>Contents</b> → Read and write, and <b>Actions</b> → Read and write.</li>' +
        '<li>Tap Generate token, copy it, and paste it here.</li></ol>' +
        '<div class="wlrow"><input id="wlTok" type="password" autocomplete="off" placeholder="Paste GitHub key (github_pat_…)">' +
        '<button data-wl="savekey"' + (WL.busy ? ' disabled' : '') + '>Save</button></div>' +
        '<p class="muted">Stored on this device only, locked with your passphrase, and sent only to GitHub. ' +
        'It can only change files and start checks in this one repo. <a href="#" data-wl="cancel">Cancel</a></p>';
    } else {
      h += '<div class="wlrow"><input id="wlAdd" autocomplete="off" autocapitalize="characters" spellcheck="false" placeholder="' + esc(WLCFG.placeholder) + '">' +
        '<button data-wl="add"' + (WL.busy ? ' disabled' : '') + '>Add</button></div>' +
        '<p class="muted">' + (WLCFG.check ? '<a href="#" data-wl="check">Check now</a> · ' : '') +
        (localStorage.getItem(TOKSTORE) ? '<a href="#" data-wl="forget">Remove key from this device</a>' : '<a href="#" data-wl="setup">Set up editing</a>') + '</p>';
    }
    if (WL.msg) h += '<p class="wlmsg">' + esc(WL.msg) + '</p>';
    return h + '</div>';
  }
  function wlStarRefresh() {
    var b = document.getElementById("wlStar"), m = location.hash.match(/^#\/(.+)$/);
    var c = m && BY[decodeURIComponent(m[1])];
    if (b && c) b.outerHTML = starButton(c);
  }
  function starButton(c) {
    if (!wlEnabled()) return '';
    var on = isWatched(c);
    return '<div id="wlStar" class="wlstar"><button data-wl="' + (on ? 'unwatch' : 'watch') + '" data-t="' + esc(c.ticker) + '"' +
      (WL.busy ? ' disabled' : '') + '>' + (on ? '★ On watchlist · Remove' : '☆ Add to watchlist') + '</button>' +
      (WL.msg ? '<p class="wlmsg">' + esc(WL.msg) + '</p>' : '') +
      (WL.setup ? '<p class="muted">Editing isn’t set up on this device yet. Go back to the main list and follow the setup in the ★ Watchlist section.</p>' : '') + '</div>';
  }

  document.addEventListener("click", function (e) {
    var t = e.target && e.target.closest ? e.target.closest("[data-wl]") : null;
    if (!t) return;
    e.preventDefault();
    var a = t.getAttribute("data-wl");
    if (a === "add") {
      var v = cleanTicker((document.getElementById("wlAdd") || {}).value);
      if (!v) { WL.msg = "Type a ticker like GOGO."; wlRefresh(); return; }
      wlChange(v, true);
    } else if (a === "watch" || a === "unwatch") wlChange(t.getAttribute("data-t"), a === "watch");
    else if (a === "remove") { if (confirm("Remove " + t.getAttribute("data-t") + " from your watchlist?")) wlChange(t.getAttribute("data-t"), false); }
    else if (a === "check") wlCheckNow();
    else if (a === "setup") { WL.setup = true; WL.msg = ""; wlRefresh(); }
    else if (a === "cancel") { WL.setup = false; WL.msg = ""; wlRefresh(); }
    else if (a === "savekey") wlSaveSetup();
    else if (a === "forget") {
      if (confirm("Remove the GitHub key from this device? You can set it up again any time.")) {
        forgetToken(); WL.list = null; WL.msg = "Key removed from this device."; wlRefresh();
      }
    }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && e.target && (e.target.id === "wlAdd" || e.target.id === "wlTok")) {
      e.preventDefault();
      var b = document.querySelector(e.target.id === "wlAdd" ? '[data-wl="add"]' : '[data-wl="savekey"]');
      if (b) b.click();
    }
  });

