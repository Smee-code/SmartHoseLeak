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

  /* ------------------------------------------------------- side navigation */

  /* Below the drawer breakpoint the sidebar is off-canvas and needs a toggle.
     Above it the sidebar is always visible and none of this runs, so the
     drawer state is cleared whenever the viewport grows past the breakpoint. */

  var sidenav = $('#sidenav');
  var navToggle = $('#nav-toggle');
  var navScrim = $('#sidenav-scrim');
  var navClose = $('#sidenav-close');
  var drawerQuery = window.matchMedia('(max-width: 1024px)');

  function setNav(open) {
    if (!sidenav) return;
    sidenav.classList.toggle('is-open', open);
    if (navScrim) navScrim.hidden = !open;
    if (navToggle) navToggle.setAttribute('aria-expanded', String(open));
    document.body.classList.toggle('nav-open', open);
  }

  if (sidenav && navToggle) {
    navToggle.addEventListener('click', function () {
      var opening = !sidenav.classList.contains('is-open');
      setNav(opening);
      if (opening) {
        var first = $('a', sidenav);
        if (first) first.focus();
      }
    });

    if (navClose) {
      navClose.addEventListener('click', function () {
        setNav(false);
        navToggle.focus();
      });
    }
    if (navScrim) navScrim.addEventListener('click', function () { setNav(false); });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && sidenav.classList.contains('is-open')) {
        setNav(false);
        navToggle.focus();
      }
    });

    // Following a link in the drawer should not leave it open behind the
    // next page in browsers that restore scroll/DOM state on back.
    $$('a', sidenav).forEach(function (link) {
      link.addEventListener('click', function () { setNav(false); });
    });

    // Rotating a tablet to landscape can cross the breakpoint while the
    // drawer is open; drop the drawer state so the sticky sidebar returns.
    var onBreakpoint = function (event) { if (!event.matches) setNav(false); };
    if (drawerQuery.addEventListener) drawerQuery.addEventListener('change', onBreakpoint);
    else if (drawerQuery.addListener) drawerQuery.addListener(onBreakpoint);
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

  /* ---------------------------------------------------------------- modal */

  // Progressive enhancement: [data-modal-open="id"] is an ordinary link to the
  // standalone form page. With JS it opens the inline modal instead, so the
  // page still works if this script fails to load.

  var openModalEl = null;
  var lastFocused = null;

  function focusables(root) {
    return $$('a[href], button:not([disabled]), input:not([type=hidden]), select, textarea, [tabindex]:not([tabindex="-1"])', root)
      .filter(function (el) { return el.offsetParent !== null; });
  }

  function closeModal() {
    if (!openModalEl) return;
    openModalEl.hidden = true;
    document.body.classList.remove('modal-open');
    if (lastFocused && lastFocused.focus) lastFocused.focus();
    openModalEl = null;
    lastFocused = null;
  }

  function openModal(el) {
    if (!el) return;
    lastFocused = document.activeElement;
    el.hidden = false;
    openModalEl = el;
    document.body.classList.add('modal-open');
    var first = focusables(el)[0];
    if (first) first.focus();
  }

  SHL.openModal = openModal;
  SHL.closeModal = closeModal;

  $$('[data-modal-open]').forEach(function (trigger) {
    trigger.addEventListener('click', function (event) {
      var target = document.getElementById(trigger.getAttribute('data-modal-open'));
      if (!target) return;           // no modal on this page: follow the link
      event.preventDefault();
      openModal(target);
    });
  });

  $$('[data-modal-close]').forEach(function (btn) {
    btn.addEventListener('click', function (event) {
      event.preventDefault();
      closeModal();
    });
  });

  $$('.modal-backdrop').forEach(function (backdrop) {
    backdrop.addEventListener('mousedown', function (event) {
      // Only a click on the backdrop itself, not one that started inside the
      // dialog and drifted out while selecting text.
      if (event.target === backdrop) closeModal();
    });
  });

  document.addEventListener('keydown', function (event) {
    if (!openModalEl) return;
    if (event.key === 'Escape') {
      closeModal();
      return;
    }
    if (event.key !== 'Tab') return;

    // Keep focus inside the dialog while it is open.
    var items = focusables(openModalEl);
    if (!items.length) return;
    var first = items[0];
    var last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });

  /* ------------------------------------------- row-driven household dialogs */

  // One edit dialog and one delete dialog are shared by every row and filled
  // from the clicked row's data attributes. Rendering a pair per row would
  // otherwise put fifty forms in a twenty-five row page.

  function setValue(id, value) {
    var field = document.getElementById(id);
    if (field) field.value = value === undefined || value === null ? '' : value;
  }

  $$('[data-edit-household]').forEach(function (trigger) {
    trigger.addEventListener('click', function (event) {
      var modal = $('#household-edit-modal');
      var form = $('#household-edit-form');
      if (!modal || !form) return;          // no dialog here: follow the link
      event.preventDefault();

      form.setAttribute('action', trigger.getAttribute('data-action'));
      setValue('edit_owner_name', trigger.getAttribute('data-owner'));
      setValue('edit_address', trigger.getAttribute('data-address'));
      setValue('edit_contact_number', trigger.getAttribute('data-contact'));
      setValue('edit_segment', trigger.getAttribute('data-segment'));
      setValue('edit_account_status', trigger.getAttribute('data-status'));
      setValue('edit_latitude', trigger.getAttribute('data-lat'));
      setValue('edit_longitude', trigger.getAttribute('data-lng'));
      setValue('edit_notes', trigger.getAttribute('data-notes'));

      var subtitle = $('[data-edit-subtitle]', modal);
      if (subtitle) subtitle.textContent = trigger.getAttribute('data-hid');

      openModal(modal);
    });
  });

  $$('[data-delete-household]').forEach(function (trigger) {
    trigger.addEventListener('click', function () {
      var modal = $('#household-delete-modal');
      var form = $('#household-delete-form');
      if (!modal || !form) return;
      form.setAttribute('action', trigger.getAttribute('data-action'));

      var hid = trigger.getAttribute('data-hid');
      var owner = trigger.getAttribute('data-owner');
      var subtitle = $('[data-delete-subtitle]', modal);
      if (subtitle) subtitle.textContent = hid + ' · ' + owner;
      var name = $('[data-delete-name]', modal);
      if (name) name.textContent = hid + ' (' + owner + ')';

      // Spell out what goes with it. Counts come from the row so the dialog
      // never claims records that are not there.
      var list = $('[data-delete-list]', modal);
      if (list) {
        var items = [];
        var complaints = Number(trigger.getAttribute('data-complaints') || 0);
        var notices = Number(trigger.getAttribute('data-notices') || 0);
        var login = trigger.getAttribute('data-login');
        items.push(complaints + (complaints === 1 ? ' complaint' : ' complaints'));
        items.push(notices + (notices === 1 ? ' notice' : ' notices'));
        if (login) items.push('the Owner Portal login “' + login + '”');
        list.innerHTML = '';
        items.forEach(function (text) {
          var li = document.createElement('li');
          li.textContent = text;
          list.appendChild(li);
        });
      }

      openModal(modal);
    });
  });

  /* ----------------------------------------------- row-driven line dialogs */

  function setChecked(root, name, value) {
    var radio = root.querySelector('input[name="' + name + '"][value="' + value + '"]');
    if (radio) radio.checked = true;
  }

  $$('[data-edit-segment]').forEach(function (trigger) {
    trigger.addEventListener('click', function (event) {
      var modal = $('#line-edit-modal');
      var form = $('#line-edit-form');
      if (!modal || !form) return;           // no dialog here: follow the link
      event.preventDefault();

      form.setAttribute('action', trigger.getAttribute('data-action'));
      setValue('edit_name', trigger.getAttribute('data-name'));
      setValue('edit_low_pressure_bar', trigger.getAttribute('data-lowbar'));
      setValue('edit_start_lat', trigger.getAttribute('data-startlat'));
      setValue('edit_start_lng', trigger.getAttribute('data-startlng'));
      setValue('edit_end_lat', trigger.getAttribute('data-endlat'));
      setValue('edit_end_lng', trigger.getAttribute('data-endlng'));
      setValue('edit_maintenance_note', trigger.getAttribute('data-note'));

      // Colour is a radio group and is_active a checkbox, so neither takes a
      // plain .value assignment.
      setChecked(modal, 'color', trigger.getAttribute('data-color'));
      var active = document.getElementById('edit_is_active');
      if (active) active.checked = trigger.getAttribute('data-active') === '1';

      var subtitle = $('[data-edit-subtitle]', modal);
      if (subtitle) subtitle.textContent = trigger.getAttribute('data-module');

      openModal(modal);
    });
  });

  /*
   * Copy a device id or key to the clipboard.
   *
   * The key is 43 mixed-case characters with dashes and underscores, and the
   * realistic way it reaches a board is on a phone: copy here, join the
   * board's hotspot, paste. Typing it is how a working board gets rejected
   * with a 401 that looks exactly like a network fault.
   */
  /* Show only the fields that belong to the device option chosen. Both
     stay in the DOM so a failed submission still renders its own error. */
  (function () {
    var radios = $$('input[name="device_choice"]');
    if (!radios.length) return;

    function sync() {
      var chosen = radios.filter(function (r) { return r.checked; })[0];
      var value = chosen ? chosen.value : 'none';
      $$('[data-device-when]').forEach(function (block) {
        block.hidden = block.getAttribute('data-device-when') !== value;
      });
    }
    radios.forEach(function (radio) { radio.addEventListener('change', sync); });
    sync();
  })();

  $$('[data-copy]').forEach(function (button) {
    button.addEventListener('click', function () {
      var value = button.getAttribute('data-copy') || '';
      var done = function (ok) {
        var original = button.textContent;
        button.textContent = ok ? 'Copied' : 'Press Ctrl+C';
        button.classList.toggle('is-copied', ok);
        setTimeout(function () {
          button.textContent = original;
          button.classList.remove('is-copied');
        }, 1600);
      };

      // navigator.clipboard needs a secure context, and this system is
      // served over plain HTTP on a LAN by design - so the old selection
      // route is the one that actually runs most of the time, not a
      // curiosity to fall back to.
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(value).then(function () { done(true); },
                                                  function () { done(false); });
        return;
      }
      var scratch = document.createElement('textarea');
      scratch.value = value;
      scratch.setAttribute('readonly', '');
      scratch.style.position = 'fixed';
      scratch.style.opacity = '0';
      document.body.appendChild(scratch);
      scratch.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      document.body.removeChild(scratch);
      done(ok);
    });
  });

  /* Issuing a new key silences a board until somebody walks to it, so it is
     confirmed like a deletion and the dialog names what goes quiet. */
  $$('[data-newkey-device]').forEach(function (trigger) {
    trigger.addEventListener('click', function () {
      var modal = $('#device-key-modal');
      var form = $('#device-key-form');
      if (!modal || !form) return;
      form.setAttribute('action', trigger.getAttribute('data-action'));

      var name = trigger.getAttribute('data-name');
      var line = trigger.getAttribute('data-line');
      var subtitle = $('[data-newkey-device-name]', modal);
      if (subtitle) subtitle.textContent = name;

      var consequence = $('[data-newkey-consequence]', modal);
      if (consequence) {
        consequence.textContent = line
          ? line + ' will stop reporting until you enter the new key on the board.'
          : 'This board is not fitted to a line yet, so nothing stops reporting.';
      }
      openModal(modal);
    });
  });

  $$('[data-delete-segment]').forEach(function (trigger) {
    trigger.addEventListener('click', function () {
      var modal = $('#line-delete-modal');
      var form = $('#line-delete-form');
      if (!modal || !form) return;
      form.setAttribute('action', trigger.getAttribute('data-action'));

      var module = trigger.getAttribute('data-module');
      var name = trigger.getAttribute('data-name');
      var subtitle = $('[data-delete-subtitle]', modal);
      if (subtitle) subtitle.textContent = module + ' · ' + name;
      var target = $('[data-delete-name]', modal);
      if (target) target.textContent = module + ' — ' + name;

      var rollups = Number(trigger.getAttribute('data-rollups') || 0);
      var events = Number(trigger.getAttribute('data-events') || 0);
      var list = $('[data-delete-list]', modal);
      if (list) {
        list.innerHTML = '';
        [rollups + (rollups === 1 ? ' minute of pressure history' : ' minutes of pressure history'),
         events + (events === 1 ? ' leak event' : ' leak events')]
          .forEach(function (text) {
            var li = document.createElement('li');
            li.textContent = text;
            list.appendChild(li);
          });
      }

      // Households survive the delete, so say what actually happens to them
      // rather than leaving the facilitator to guess.
      var households = Number(trigger.getAttribute('data-households') || 0);
      var note = $('[data-delete-households]', modal);
      if (note) {
        note.textContent = households
          ? households + (households === 1 ? ' household is' : ' households are') +
            ' served by this line. They are kept, but become Unassigned and stop ' +
            'receiving alerts until you move them to another line.'
          : 'No households are assigned to this line.';
      }

      openModal(modal);
    });
  });

  // Reopened by the server after a validation error, so the typed values and
  // the messages stay in front of the user instead of on a separate page.
  var autoOpen = $('[data-modal-autoopen]');
  if (autoOpen) openModal(autoOpen);

  /* ------------------------------------------------- conversation search */

  // Filters the rows already rendered rather than asking the server, so it
  // narrows within whatever status filter is active and responds on every
  // keystroke. Fine at this scale; if the list ever outgrows one page this
  // has to move server-side, because it can only match what was sent.

  var threadSearch = $('[data-thread-search]');
  if (threadSearch) {
    var searchable = $$('.thread-item[data-search]');
    var noMatch = $('[data-search-empty]');

    var runSearch = function () {
      var term = threadSearch.value.trim().toLowerCase();
      var shown = 0;

      searchable.forEach(function (item) {
        var hit = !term || item.getAttribute('data-search').indexOf(term) !== -1;
        item.hidden = !hit;
        if (hit) shown += 1;
      });

      if (noMatch) noMatch.hidden = !(term && shown === 0);
    };

    threadSearch.addEventListener('input', runSearch);
    threadSearch.addEventListener('search', runSearch);   // the native clear (x)
    threadSearch.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') {
        threadSearch.value = '';
        runSearch();
      }
    });
  }

  /* --------------------------------------------------------------- toasts */

  var toastBox = null;

  function toastContainer() {
    if (toastBox) return toastBox;
    toastBox = document.createElement('div');
    toastBox.className = 'toasts';
    // polite: announced at the next pause rather than interrupting a screen
    // reader mid-sentence. Device state is important, not urgent.
    toastBox.setAttribute('aria-live', 'polite');
    toastBox.setAttribute('role', 'status');
    document.body.appendChild(toastBox);
    return toastBox;
  }

  // SHL.toast({title, body, type: ok|warn|danger|info, timeout: ms})
  // timeout 0 keeps it until dismissed.
  SHL.toast = function (options) {
    var opts = options || {};
    var el = document.createElement('div');
    el.className = 'toast toast-' + (opts.type || 'info');

    var dot = document.createElement('span');
    dot.className = 'toast-dot';
    dot.setAttribute('aria-hidden', 'true');

    var text = document.createElement('div');
    text.className = 'toast-text';

    var title = document.createElement('div');
    title.className = 'toast-title';
    title.textContent = opts.title || '';
    text.appendChild(title);

    if (opts.body) {
      var body = document.createElement('div');
      body.className = 'toast-body';
      body.textContent = opts.body;
      text.appendChild(body);
    }

    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'toast-close';
    close.setAttribute('aria-label', 'Dismiss');
    close.innerHTML = '&times;';

    var ms = opts.timeout === undefined ? 8000 : opts.timeout;
    var timer = null;

    function dismiss() {
      if (timer) clearTimeout(timer);
      if (el.parentNode) el.parentNode.removeChild(el);
    }
    close.addEventListener('click', dismiss);

    // Pause the countdown while the pointer is over the toast, so a message
    // being read - or copied from - does not vanish mid-sentence.
    el.addEventListener('mouseenter', function () {
      if (timer) { clearTimeout(timer); timer = null; }
    });
    el.addEventListener('mouseleave', function () {
      if (ms > 0 && !timer) timer = setTimeout(dismiss, ms);
    });

    el.appendChild(dot);
    el.appendChild(text);
    el.appendChild(close);
    toastContainer().appendChild(el);

    if (ms > 0) timer = setTimeout(dismiss, ms);
    return dismiss;
  };

  /* ------------------------------------------- server messages as toasts */

  // Django renders its messages framework into .messages blocks. They are
  // real markup rather than data so the page still works without this
  // script; here they are lifted into toasts and the inline block removed.
  //
  // [data-keep-inline] opts a block out - the temporary-password banner is
  // a standing warning, not a one-off event, and should not float away.

  var TAG_TO_TOAST = {
    success: 'ok',
    error: 'danger',
    warning: 'warn',
    info: 'info',
    debug: 'info'
  };

  $$('.messages').forEach(function (block) {
    if (block.hasAttribute('data-keep-inline')) return;

    var converted = 0;
    $$('.message', block).forEach(function (node) {
      var body = $('span', node);
      var content = (body || node).textContent.trim();
      if (!content) return;

      // Any message carrying a credential has to be read and written down,
      // so it waits to be dismissed rather than timing out. Same for
      // anything that went wrong - those are not glanceable.
      var type = 'info';
      Object.keys(TAG_TO_TOAST).forEach(function (tag) {
        if (node.classList.contains('message-' + tag)) type = TAG_TO_TOAST[tag];
      });
      var sticky = type === 'danger' || type === 'warn' ||
                   /password|username|api key/i.test(content);

      SHL.toast({
        type: type,
        title: content,
        // Long text needs longer on screen; capped so it cannot linger.
        timeout: sticky ? 0 : Math.min(14000, 5000 + content.length * 45)
      });
      converted += 1;
    });

    if (converted) block.remove();
  });

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

  /* ------------------------------------------------- device online/offline */

  // null until the first payload: the very first reading establishes what
  // "current" is, so opening a page on an already-offline device does not
  // announce a transition that did not happen.
  var deviceOnline = null;

  SHL.onLive(function (data) {
    var device = data.device;
    if (!device) return;

    var online = !!device.online;

    // The single "Device status" tile this used to keep current is gone -
    // one board per household made one global answer meaningless, and each
    // row now carries its own. The toast below stays: a board dropping off
    // is worth interrupting for wherever the facilitator happens to be.
    if (deviceOnline === null) {
      deviceOnline = online;
      return;
    }
    if (online === deviceOnline) return;
    deviceOnline = online;

    if (online) {
      SHL.toast({
        type: 'ok',
        title: 'Device is online',
        body: 'Readings are arriving from the ESP32 again.'
      });
    } else {
      SHL.toast({
        type: 'danger',
        title: 'Device went offline',
        body: 'No readings received recently. Check the board’s power and Wi-Fi.',
        timeout: 12000
      });
    }
  });

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

  /* ------------------------------------------------------------ sms length */

  /*
   * Count a notice the way the network will bill it.
   *
   * A text is delivered in 160-character units, and the facilitator cannot
   * see that boundary while typing. Showing it here turns "why did that
   * arrive as three messages" into a decision made before sending, on a
   * system where every message costs load credit.
   */
  (function () {
    var body = $('[data-sms-counter]');
    var out = $('[data-sms-count]');
    var smsBox = $('#id_by_sms');
    if (!body || !out) return;

    var limit = parseInt(out.getAttribute('data-limit'), 10) || 160;
    var PART = 153;   // parts spend 7 characters on the "(1/2)" marker

    function update() {
      // Only relevant when the message is actually going out as a text.
      if (smsBox && !smsBox.checked) { out.hidden = true; return; }
      out.hidden = false;

      var length = body.value.length;
      var parts = length <= limit ? 1 : Math.ceil(length / PART);
      out.textContent = length + ' characters' +
        (parts > 1 ? ' - sends as ' + parts + ' text messages' : ' - one text message');
      out.classList.toggle('warn-text', parts > 1);
    }

    body.addEventListener('input', update);
    if (smsBox) smsBox.addEventListener('change', update);
    update();
  })();

  /* --------------------------------------------------- recipient picker */

  /*
   * A token field over the real checkboxes.
   *
   * The checkboxes are what the form posts and they never go away - this
   * hides them and drives them. With no JavaScript the plain grid is simply
   * visible and works as it always did, so nothing depends on this having
   * loaded.
   *
   * A grid was fine at seven households and stops working at fifty: finding
   * one becomes hunting, and the compose form grows with the barangay. A
   * modal would have fixed the space but hidden the choice behind a dialog,
   * so you could not see who you had picked while writing the message.
   */
  (function () {
    var root = $('[data-recipients]');
    if (!root) return;

    var all = $('#id_to_all');
    var grid = $('[data-household-picker]', root);
    var field = $('[data-token-field]', root);
    var chips = $('[data-token-chips]', root);
    var input = $('[data-token-input]', root);
    var menu = $('[data-token-menu]', root);
    var summary = $('[data-token-summary]', root);
    if (!grid || !field || !input || !menu) return;

    var boxes = $$('input[type="checkbox"]', grid);
    if (!boxes.length) return;

    // Take over only now that everything needed is present. Hiding the grid
    // any earlier would leave no way to choose if the rest failed.
    grid.hidden = true;
    field.hidden = false;
    if (summary) summary.hidden = false;

    var options = boxes.map(function (box) {
      var label = box.closest('label');
      return { box: box, text: (label ? label.textContent : '').trim() };
    });

    var activeIndex = -1;

    function chosen() {
      return options.filter(function (o) { return o.box.checked; });
    }

    function renderChips() {
      chips.innerHTML = '';
      chosen().forEach(function (option) {
        var chip = document.createElement('span');
        chip.className = 'token-chip';
        chip.textContent = option.text;

        var remove = document.createElement('button');
        remove.type = 'button';           // never submit the form
        remove.className = 'token-x';
        remove.setAttribute('aria-label', 'Remove ' + option.text);
        remove.textContent = '×';
        remove.addEventListener('click', function () {
          option.box.checked = false;
          renderChips();
          input.focus();
        });

        chip.appendChild(remove);
        chips.appendChild(chip);
      });

      if (summary) {
        var count = chosen().length;
        summary.textContent = all && all.checked
          ? 'Sending to every active household.'
          : count
            ? count + ' household' + (count === 1 ? '' : 's') + ' selected.'
            : 'Nobody selected yet.';
      }
    }

    function closeMenu() {
      menu.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      activeIndex = -1;
    }

    function matches() {
      var query = input.value.trim().toLowerCase();
      return options.filter(function (o) {
        return !o.box.checked && (!query || o.text.toLowerCase().indexOf(query) >= 0);
      });
    }

    function openMenu() {
      var found = matches();
      menu.innerHTML = '';

      if (!found.length) {
        var none = document.createElement('li');
        none.className = 'token-none';
        none.textContent = input.value.trim()
          ? 'No household matches that.'
          : 'Everyone is already selected.';
        menu.appendChild(none);
      } else {
        found.forEach(function (option, index) {
          var item = document.createElement('li');
          item.className = 'token-opt';
          item.setAttribute('role', 'option');
          item.textContent = option.text;
          // mousedown, not click: blur would close the menu first and the
          // click would land on nothing.
          item.addEventListener('mousedown', function (event) {
            event.preventDefault();
            pick(option);
          });
          item.addEventListener('mouseenter', function () { highlight(index); });
          menu.appendChild(item);
        });
      }

      menu.hidden = false;
      input.setAttribute('aria-expanded', 'true');
      highlight(found.length ? 0 : -1);
    }

    function highlight(index) {
      activeIndex = index;
      $$('.token-opt', menu).forEach(function (item, i) {
        item.classList.toggle('is-active', i === index);
        item.setAttribute('aria-selected', i === index ? 'true' : 'false');
      });
    }

    function pick(option) {
      option.box.checked = true;
      input.value = '';
      renderChips();
      openMenu();
    }

    input.addEventListener('focus', openMenu);
    input.addEventListener('input', openMenu);

    input.addEventListener('keydown', function (event) {
      var found = matches();
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        if (menu.hidden) { openMenu(); return; }
        var step = event.key === 'ArrowDown' ? 1 : -1;
        highlight((activeIndex + step + found.length) % found.length);
      } else if (event.key === 'Enter') {
        // Only swallow Enter when it is choosing something. Otherwise it
        // should still submit the form, the way Enter does everywhere else.
        if (!menu.hidden && activeIndex >= 0 && found[activeIndex]) {
          event.preventDefault();
          pick(found[activeIndex]);
        }
      } else if (event.key === 'Escape') {
        closeMenu();
      } else if (event.key === 'Backspace' && input.value === '') {
        // Backspace on an empty box removes the last chip, which is what
        // every other token field does.
        var picked = chosen();
        if (picked.length) {
          picked[picked.length - 1].box.checked = false;
          renderChips();
        }
      }
    });

    input.addEventListener('blur', function () {
      // A tick, so a click on an option is not cancelled by the blur.
      setTimeout(closeMenu, 120);
    });

    /*
     * "All active households" and picking individuals are alternatives, not
     * things to combine. Disabling one while the other is on says so without
     * an error message after the fact.
     */
    function syncAll() {
      if (!all) return;
      field.classList.toggle('is-disabled', all.checked);
      input.disabled = all.checked;
      boxes.forEach(function (box) { box.disabled = all.checked; });
      if (all.checked) closeMenu();
      renderChips();
    }

    if (all) all.addEventListener('change', syncAll);
    renderChips();
    syncAll();
  })();

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
      setText('valve', segment.has_device ? (segment.valve_label || '--') : '--');

      // A line waiting for hardware is not the same as one whose board has
      // stopped answering, so only lines that HAVE a device get a live/down
      // indicator. The no-device case is rendered server-side and left alone.
      var deviceDot = $('[data-field="device_dot"]', tile);
      if (deviceDot) {
        var live = segment.device_online && !segment.stale;
        deviceDot.className = 'dot ' + (live ? 'dot-live' : 'dot-down');
        setText('device', live ? 'live' : 'not reporting');
      }

      // A link to the board's own page. Hidden until it has reported at
      // least once, so it never offers an address that goes nowhere - and
      // kept current by the poll, because the address moves whenever the
      // network does and that is precisely when the link is needed.
      var ipLink = $('[data-field="device_ip"]', tile);
      if (ipLink) {
        if (segment.device_ip) {
          ipLink.hidden = false;
          ipLink.textContent = segment.device_ip;
          ipLink.setAttribute('href', 'http://' + segment.device_ip + '/');
        } else {
          ipLink.hidden = true;
        }
      }

      // The valve controls stay hidden until a board actually reports a
      // valve. The hardware is on order, and a button that silently does
      // nothing teaches people to distrust the ones that work.
      var valveForm = $('[data-valve-form]', tile);
      if (valveForm) {
        valveForm.hidden = !segment.valve_state || segment.valve_state === 'not_fitted';
      }

      var note = $('[data-field="note"]', tile);
      if (note) {
        if (segment.status === 'POSSIBLE_LEAK') {
          // Which test fired matters more than the verdict: sustained low
          // flow is as likely to be a tap left open as a leak, while night
          // flow on a line the family says was idle is hard to explain away.
          var detail = [];
          if (segment.severity_label && segment.severity !== 'none') detail.push(segment.severity_label);
          if (segment.trigger_label && segment.trigger !== 'none') detail.push(segment.trigger_label);
          note.textContent = detail.length ? detail.join(' - ') : 'Suspected leak.';
        } else if (segment.status === 'MAINTENANCE') {
          note.textContent = segment.maintenance_note || 'Under maintenance - alerts paused.';
        } else if (segment.status === 'OFFLINE') {
          note.textContent = 'No readings received. Check device power and Wi-Fi.';
        } else if (segment.status === 'LOW_SUPPLY') {
          // Naming the figure turns a verdict into something checkable:
          // the facilitator can see at a glance whether the line is genuinely
          // weak or the threshold is simply set too high for this line.
          note.textContent = segment.low_pressure_bar
            ? 'Pressure is under this line’s ' + fmt(segment.low_pressure_bar, 2) + ' bar threshold.'
            : 'Pressure below the normal operating threshold.';
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
