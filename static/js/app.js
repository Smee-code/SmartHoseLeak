/* =========================================================================
   Smart Hose Leak Detection System - shared frontend behaviour.
   Plain ES5-compatible JavaScript, no build step and no framework.
   ========================================================================= */
(function () {
  'use strict';

  var SHL = window.SHL || {};
  SHL.pollMs = SHL.pollMs || 3000;

  /* ---------------------------------------------------------------- utils */

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function fmt(value, digits) {
    if (value === null || value === undefined || isNaN(value)) return '--';
    return Number(value).toFixed(digits === undefined ? 2 : digits);
  }

  function timeAgo(iso) {
    if (!iso) return 'no data yet';
    var seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (seconds < 10) return 'just now';
    if (seconds < 60) return Math.round(seconds) + 's ago';
    if (seconds < 3600) return Math.round(seconds / 60) + ' min ago';
    if (seconds < 86400) return Math.round(seconds / 3600) + ' hr ago';
    return Math.round(seconds / 86400) + ' d ago';
  }

  var STATUS_COLORS = {
    NORMAL: '#17794c',
    LOW_SUPPLY: '#a8620b',
    POSSIBLE_LEAK: '#b3261e',
    MAINTENANCE: '#a8620b',
    OFFLINE: '#6b7785'
  };
  SHL.statusColor = function (status) { return STATUS_COLORS[status] || STATUS_COLORS.OFFLINE; };

  /* ---------------------------------------------------------------- theme */

  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    try { localStorage.setItem('shl-theme', theme); } catch (e) { /* private mode */ }
    var icon = $('[data-theme-icon]');
    if (icon) icon.innerHTML = theme === 'dark' ? '&#9788;' : '&#9789;';
    document.dispatchEvent(new CustomEvent('shl:theme', { detail: { theme: theme } }));
  }

  var themeToggle = $('#theme-toggle');
  if (themeToggle) {
    applyTheme(document.documentElement.getAttribute('data-theme') || 'light');
    themeToggle.addEventListener('click', function () {
      var next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      applyTheme(next);
    });
  }

  /* ------------------------------------------------------------- user menu */

  var menuBtn = $('#usermenu-btn');
  var menuPanel = $('#usermenu-panel');
  if (menuBtn && menuPanel) {
    menuBtn.addEventListener('click', function (event) {
      event.stopPropagation();
      var open = menuPanel.hidden;
      menuPanel.hidden = !open;
      menuBtn.setAttribute('aria-expanded', String(open));
    });
    document.addEventListener('click', function (event) {
      if (!menuPanel.hidden && !menuPanel.contains(event.target)) {
        menuPanel.hidden = true;
        menuBtn.setAttribute('aria-expanded', 'false');
      }
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && !menuPanel.hidden) {
        menuPanel.hidden = true;
        menuBtn.setAttribute('aria-expanded', 'false');
        menuBtn.focus();
      }
    });
  }

  $$('.message-close').forEach(function (button) {
    button.addEventListener('click', function () {
      var message = button.closest('.message');
      if (message) message.remove();
    });
  });

  /* -------------------------------------------------- connection quality
     Decides whether the dashboard may load Leaflet tiles. Three signals,
     cheapest first: offline flag, the Network Information API, then an
     actual timed tile fetch, because a phone can report 4g and still sit
     behind a dead link. The result is cached briefly so every page load
     does not re-probe.                                                     */

  var CONN_CACHE_KEY = 'shl-conn';
  var CONN_CACHE_MS = 120000;

  function cachedConnection() {
    try {
      var raw = sessionStorage.getItem(CONN_CACHE_KEY);
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      if (Date.now() - parsed.at > CONN_CACHE_MS) return null;
      return parsed.quality;
    } catch (e) { return null; }
  }

  function cacheConnection(quality) {
    try {
      sessionStorage.setItem(CONN_CACHE_KEY, JSON.stringify({ quality: quality, at: Date.now() }));
    } catch (e) { /* ignore */ }
  }

  function probeTile(timeoutMs) {
    // One real map tile of Cantilan/Surigao at low zoom. If it does not
    // arrive quickly the full map would be painful, so we fall back.
    return new Promise(function (resolve) {
      var settled = false;
      var image = new Image();
      var started = Date.now();
      var timer = setTimeout(function () {
        if (!settled) { settled = true; image.src = ''; resolve({ ok: false, ms: timeoutMs }); }
      }, timeoutMs);

      image.onload = function () {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve({ ok: true, ms: Date.now() - started });
      };
      image.onerror = function () {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve({ ok: false, ms: Date.now() - started });
      };
      image.src = 'https://tile.openstreetmap.org/7/103/60.png?cb=' + Date.now();
    });
  }

  SHL.checkConnection = function () {
    var cached = cachedConnection();
    if (cached) return Promise.resolve(cached);

    if (navigator.onLine === false) {
      cacheConnection('offline');
      return Promise.resolve('offline');
    }

    var info = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
    if (info) {
      if (info.saveData) { cacheConnection('weak'); return Promise.resolve('weak'); }
      var type = info.effectiveType || '';
      if (type === 'slow-2g' || type === '2g') { cacheConnection('weak'); return Promise.resolve('weak'); }
    }

    return probeTile(2500).then(function (result) {
      var quality = !result.ok ? 'offline' : (result.ms > 1400 ? 'weak' : 'strong');
      cacheConnection(quality);
      return quality;
    });
  };

  function paintConnectionPill(quality) {
    var pill = $('#conn-pill');
    if (!pill) return;
    var labels = {
      strong: ['pill pill-ok', '&#8226; online'],
      weak: ['pill pill-warn', '&#8226; weak signal'],
      offline: ['pill pill-danger', '&#8226; offline']
    };
    var chosen = labels[quality] || labels.offline;
    pill.className = chosen[0];
    pill.innerHTML = chosen[1];
    pill.title = {
      strong: 'Internet connection is good. The interactive map is available.',
      weak: 'Weak connection detected. The offline schematic view is being used instead of the map.',
      offline: 'No internet connection. Showing the offline schematic view.'
    }[quality] || '';
  }

  SHL.checkConnection().then(paintConnectionPill);
  window.addEventListener('online', function () {
    try { sessionStorage.removeItem(CONN_CACHE_KEY); } catch (e) {}
    SHL.checkConnection().then(paintConnectionPill);
  });
  window.addEventListener('offline', function () { paintConnectionPill('offline'); });

  /* ------------------------------------------------------------ live poll */

  var liveHandlers = [];
  SHL.onLive = function (handler) { liveHandlers.push(handler); };

  var pollTimer = null;
  var failures = 0;

  function poll() {
    fetch('/api/live/', { headers: { 'X-Requested-With': 'fetch' }, credentials: 'same-origin' })
      .then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.json();
      })
      .then(function (data) {
        failures = 0;
        liveHandlers.forEach(function (handler) {
          try { handler(data); } catch (e) { console.error('live handler failed', e); }
        });
        SHL.renderTiles(data.segments || []);
        SHL.updateAlertBanner(data.open_events || []);
      })
      .catch(function () {
        failures += 1;
        if (failures === 3) paintConnectionPill('offline');
      })
      .then(function () {
        // Back off when the server is unreachable so a lost link does not
        // hammer the connection; return to normal cadence once it recovers.
        var delay = failures > 2 ? Math.min(SHL.pollMs * failures, 30000) : SHL.pollMs;
        pollTimer = setTimeout(poll, delay);
      });
  }

  SHL.startPolling = function () {
    if (pollTimer) return;
    poll();
    document.addEventListener('visibilitychange', function () {
      // Stop polling in a hidden tab; refresh immediately on return.
      if (document.hidden) {
        clearTimeout(pollTimer);
        pollTimer = null;
      } else if (!pollTimer) {
        poll();
      }
    });
  };

  /* ----------------------------------------------------------- tile render */

  SHL.renderTiles = function (segments) {
    segments.forEach(function (segment) {
      var tile = $('[data-segment-id="' + segment.id + '"]');
      if (!tile) return;

      tile.setAttribute('data-status', segment.status);

      var pill = $('[data-field="status"]', tile);
      if (pill) {
        pill.className = 'pill pill-' + segment.status;
        pill.textContent = segment.status_label;
      }

      var setText = function (field, value) {
        var node = $('[data-field="' + field + '"]', tile);
        if (node) node.textContent = value;
      };

      setText('pressure_bar', fmt(segment.pressure_bar, 2));
      setText('pressure_psi', segment.pressure_psi === null ? '--' : fmt(segment.pressure_psi, 1) + ' psi');
      setText('flow_lpm', fmt(segment.flow_lpm, 2));
      setText('updated', timeAgo(segment.updated_at));
      setText('households', segment.households);

      var note = $('[data-field="note"]', tile);
      if (note) {
        if (segment.status === 'POSSIBLE_LEAK') {
          note.textContent = 'Suspected leak ' + segment.location_label + '.';
        } else if (segment.status === 'MAINTENANCE') {
          note.textContent = segment.maintenance_note || 'Under maintenance - alerts paused.';
        } else if (segment.status === 'OFFLINE') {
          note.textContent = 'No readings received. Check device power and Wi-Fi.';
        } else if (segment.status === 'LOW_SUPPLY') {
          note.textContent = 'Pressure below the normal operating threshold.';
        } else {
          note.textContent = segment.simulated ? 'Simulated input.' : '';
        }
      }
    });

    if (window.SHLMap && typeof window.SHLMap.update === 'function') {
      window.SHLMap.update(segments);
    }
  };

  SHL.updateAlertBanner = function (events) {
    var banner = $('#alert-banner');
    if (!banner) return;
    var critical = events.filter(function (event) { return event.severity === 'critical'; });

    if (!critical.length) {
      banner.hidden = true;
      document.title = document.title.replace(/^\(\d+\)\s*/, '');
      return;
    }

    banner.hidden = false;
    banner.innerHTML = '';
    critical.forEach(function (event) {
      var row = document.createElement('div');
      row.className = 'message message-error';
      row.innerHTML =
        '<span><strong>' + event.status_label + '</strong> on ' + event.segment +
        ' &mdash; ' + event.location + '. ' +
        fmt(event.pressure_bar, 2) + ' bar (' + fmt(event.pressure_psi, 1) + ' psi), ' +
        fmt(event.flow_lpm, 2) + ' L/min, ' + timeAgo(event.detected_at) + '.</span>';
      banner.appendChild(row);
    });

    if (!/^\(\d+\)/.test(document.title)) {
      document.title = '(' + critical.length + ') ' + document.title;
    }
  };

  /* -------------------------------------------------------- relative times */

  function refreshTimestamps() {
    $$('[data-ago]').forEach(function (node) {
      node.textContent = timeAgo(node.getAttribute('data-ago'));
    });
  }
  refreshTimestamps();
  setInterval(refreshTimestamps, 30000);

  /* ----------------------------------------------------------- PWA worker */

  if ('serviceWorker' in navigator && location.protocol !== 'file:') {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/sw.js').catch(function () { /* not fatal */ });
    });
  }

  SHL.$ = $;
  SHL.$$ = $$;
  SHL.fmt = fmt;
  SHL.timeAgo = timeAgo;
  window.SHL = SHL;
})();
