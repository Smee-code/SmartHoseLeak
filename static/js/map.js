/* =========================================================================
   Leak map.

   Two renderers behind one interface:
     - Leaflet, when the connection probe reports a strong link AND the
       facilitator has filled in coordinates for at least one segment.
     - An inline SVG schematic of the hose run, which needs no network at
       all and therefore still works during a field demo on a weak signal.

   The schematic is not a degraded placeholder: it shows the same three
   segments against their real distance markers, which is the information
   the facilitator actually acts on.
   ========================================================================= */
(function () {
  'use strict';

  var container = document.getElementById('map-panel');
  if (!container) return;

  var mapNode = document.getElementById('leak-map');
  var schematicNode = document.getElementById('map-schematic');
  var noteNode = document.getElementById('map-note');
  var toggleBtn = document.getElementById('map-toggle');

  var segments = window.SHL_SEGMENTS || [];
  var leafletMap = null;
  var leafletLayers = {};
  var mode = 'schematic';
  var userForced = false;

  var STATUS_LABEL = {
    NORMAL: 'Normal',
    LOW_SUPPLY: 'Low Water Supply',
    POSSIBLE_LEAK: 'Possible Leak',
    MAINTENANCE: 'Under Maintenance',
    OFFLINE: 'Offline'
  };

  function hasCoordinates() {
    return segments.some(function (segment) {
      return segment.coords && segment.coords.start && segment.coords.end;
    });
  }

  function setNote(text) {
    if (noteNode) noteNode.textContent = text;
  }

  /* ------------------------------------------------------------ schematic */

  function drawSchematic() {
    if (!schematicNode) return;

    var width = 900;
    var height = 210;
    var padLeft = 70;
    var padRight = 40;
    var trackY = 96;
    var maxMetres = Math.max.apply(null, segments.map(function (s) { return s.end_m; }).concat([1]));
    var scale = (width - padLeft - padRight) / maxMetres;
    var x = function (metres) { return padLeft + metres * scale; };

    var parts = [];
    parts.push(
      '<svg class="schematic" viewBox="0 0 ' + width + ' ' + height + '" ' +
      'preserveAspectRatio="xMidYMid meet" role="img" ' +
      'aria-label="Schematic of the monitored hose lines with their current status">'
    );

    // Main valve marker.
    parts.push(
      '<g>' +
      '<rect x="' + (padLeft - 34) + '" y="' + (trackY - 17) + '" width="26" height="34" rx="5" ' +
      'fill="var(--brand-soft)" stroke="var(--brand)" stroke-width="1.5"/>' +
      '<text x="' + (padLeft - 21) + '" y="' + (trackY + 5) + '" text-anchor="middle" ' +
      'font-size="13" fill="var(--brand-strong)">&#9678;</text>' +
      '<text x="' + (padLeft - 21) + '" y="' + (trackY + 34) + '" text-anchor="middle" ' +
      'font-size="10" fill="var(--text-muted)">Main valve</text>' +
      '<text x="' + (padLeft - 21) + '" y="' + (trackY + 46) + '" text-anchor="middle" ' +
      'font-size="10" fill="var(--text-muted)">0 m</text>' +
      '</g>'
    );

    segments.forEach(function (segment) {
      var color = window.SHL.statusColor(segment.status);
      var x1 = x(segment.start_m);
      var x2 = x(segment.end_m);
      var midX = (x1 + x2) / 2;
      var isLeak = segment.status === 'POSSIBLE_LEAK';

      parts.push('<g class="schematic-seg">');
      parts.push('<title>M' + segment.module + ' ' + segment.name + ' - ' +
                 (STATUS_LABEL[segment.status] || segment.status) + '</title>');

      // The hose run itself.
      parts.push(
        '<line x1="' + x1 + '" y1="' + trackY + '" x2="' + x2 + '" y2="' + trackY + '" ' +
        'stroke="' + color + '" stroke-width="13" stroke-linecap="butt" opacity="' +
        (segment.status === 'OFFLINE' ? 0.45 : 1) + '"/>'
      );

      // Distance-marker ticks at both ends.
      [[x1, segment.start_m], [x2, segment.end_m]].forEach(function (pair) {
        parts.push(
          '<line x1="' + pair[0] + '" y1="' + (trackY - 13) + '" x2="' + pair[0] + '" y2="' +
          (trackY + 13) + '" stroke="var(--text-muted)" stroke-width="1.5"/>' +
          '<text x="' + pair[0] + '" y="' + (trackY + 30) + '" text-anchor="middle" ' +
          'font-size="10" fill="var(--text-muted)">' + pair[1] + ' m</text>'
        );
      });

      // Module label above the run.
      parts.push(
        '<text x="' + midX + '" y="' + (trackY - 24) + '" text-anchor="middle" ' +
        'font-size="11" font-weight="600" fill="var(--text)">M' + segment.module + '</text>'
      );
      parts.push(
        '<text x="' + midX + '" y="' + (trackY - 38) + '" text-anchor="middle" ' +
        'font-size="10" fill="' + color + '" font-weight="600">' +
        (STATUS_LABEL[segment.status] || segment.status) + '</text>'
      );

      // A leak pin sits at the middle of the suspected span.
      if (isLeak) {
        parts.push(
          '<g transform="translate(' + midX + ',' + (trackY + 46) + ')">' +
          '<path d="M0,-12 C7,-12 12,-7 12,0 C12,8 0,20 0,20 C0,20 -12,8 -12,0 C-12,-7 -7,-12 0,-12 Z" ' +
          'fill="' + color + '"/>' +
          '<circle cx="0" cy="0" r="4.5" fill="#fff"/>' +
          '</g>' +
          '<text x="' + midX + '" y="' + (trackY + 80) + '" text-anchor="middle" ' +
          'font-size="10" font-weight="600" fill="' + color + '">' +
          segment.start_m + '-' + segment.end_m + ' m</text>'
        );
      }

      parts.push('</g>');
    });

    parts.push('</svg>');
    schematicNode.innerHTML = parts.join('');
  }

  /* -------------------------------------------------------------- Leaflet */

  function loadLeaflet() {
    if (window.L) return Promise.resolve(true);

    return new Promise(function (resolve) {
      var css = document.createElement('link');
      css.rel = 'stylesheet';
      css.href = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css';
      document.head.appendChild(css);

      var script = document.createElement('script');
      script.src = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';
      script.async = true;

      // Never leave the panel blank: if the CDN stalls, fall back.
      var timer = setTimeout(function () { resolve(false); }, 6000);
      script.onload = function () { clearTimeout(timer); resolve(true); };
      script.onerror = function () { clearTimeout(timer); resolve(false); };
      document.head.appendChild(script);
    });
  }

  function buildLeaflet() {
    var bounds = [];
    leafletMap = window.L.map(mapNode, { scrollWheelZoom: false });
    window.L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; OpenStreetMap contributors'
    }).addTo(leafletMap);

    segments.forEach(function (segment) {
      if (!segment.coords || !segment.coords.start || !segment.coords.end) return;
      var line = window.L.polyline([segment.coords.start, segment.coords.end], {
        color: window.SHL.statusColor(segment.status),
        weight: 7,
        opacity: 0.9
      }).addTo(leafletMap);

      var marker = window.L.circleMarker(segment.coords.start, {
        radius: 7,
        color: '#fff',
        weight: 2,
        fillColor: window.SHL.statusColor(segment.status),
        fillOpacity: 1
      }).addTo(leafletMap);

      leafletLayers[segment.id] = { line: line, marker: marker };
      bindPopup(segment);
      bounds.push(segment.coords.start, segment.coords.end);
    });

    if (bounds.length) leafletMap.fitBounds(bounds, { padding: [30, 30] });
    else leafletMap.setView([9.7167, 125.5333], 13);
  }

  function bindPopup(segment) {
    var layers = leafletLayers[segment.id];
    if (!layers) return;
    var html =
      '<strong>M' + segment.module + ' &mdash; ' + segment.name + '</strong><br>' +
      (STATUS_LABEL[segment.status] || segment.status) + '<br>' +
      segment.start_m + '-' + segment.end_m + ' m from main valve<br>' +
      (segment.pressure_bar === null || segment.pressure_bar === undefined
        ? 'No readings'
        : segment.pressure_bar + ' bar (' + segment.pressure_psi + ' psi), ' +
          segment.flow_lpm + ' L/min');
    layers.line.bindPopup(html);
    layers.marker.bindPopup(html);
  }

  /* ------------------------------------------------------------ switching */

  function showSchematic(reason) {
    mode = 'schematic';
    if (mapNode) mapNode.hidden = true;
    if (schematicNode) schematicNode.hidden = false;
    drawSchematic();
    setNote(reason);
    if (toggleBtn) toggleBtn.textContent = 'Try interactive map';
  }

  function showLeaflet() {
    mode = 'leaflet';
    if (schematicNode) schematicNode.hidden = true;
    if (mapNode) mapNode.hidden = false;
    if (!leafletMap) buildLeaflet();
    else setTimeout(function () { leafletMap.invalidateSize(); }, 60);
    setNote('Interactive map. Tap a line for its current readings.');
    if (toggleBtn) toggleBtn.textContent = 'Switch to offline schematic';
  }

  function decide() {
    if (!hasCoordinates()) {
      showSchematic('Showing the offline schematic. Add map coordinates to a supply line to enable the interactive map.');
      return;
    }
    window.SHL.checkConnection().then(function (quality) {
      if (quality !== 'strong') {
        showSchematic(quality === 'offline'
          ? 'No internet connection detected - showing the offline schematic view.'
          : 'Weak connection detected - showing the offline schematic view so the page stays responsive.');
        return;
      }
      loadLeaflet().then(function (ok) {
        if (ok) showLeaflet();
        else showSchematic('Map tiles could not be loaded - showing the offline schematic view.');
      });
    });
  }

  if (toggleBtn) {
    toggleBtn.addEventListener('click', function () {
      userForced = true;
      if (mode === 'schematic') {
        if (!hasCoordinates()) {
          setNote('No coordinates are set on any supply line yet, so the interactive map has nothing to draw.');
          return;
        }
        setNote('Loading map tiles...');
        loadLeaflet().then(function (ok) {
          if (ok) showLeaflet();
          else showSchematic('Map tiles could not be loaded - showing the offline schematic view.');
        });
      } else {
        showSchematic('Offline schematic view (selected manually).');
      }
    });
  }

  /* Live updates repaint whichever renderer is active. */
  window.SHLMap = {
    update: function (fresh) {
      segments = fresh.length ? fresh : segments;
      if (mode === 'schematic') {
        drawSchematic();
      } else if (leafletMap) {
        segments.forEach(function (segment) {
          var layers = leafletLayers[segment.id];
          if (!layers) return;
          var color = window.SHL.statusColor(segment.status);
          layers.line.setStyle({ color: color });
          layers.marker.setStyle({ fillColor: color });
          bindPopup(segment);
        });
      }
    }
  };

  document.addEventListener('shl:theme', function () {
    if (mode === 'schematic') drawSchematic();
  });

  decide();
})();
