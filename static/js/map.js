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
  var households = window.SHL_HOUSEHOLDS || [];
  var source = window.SHL_SOURCE || null;
  var leafletMap = null;
  var leafletLayers = {};
  var mode = 'schematic';
  var userForced = false;

  // Each line carries its own colour, chosen by the facilitator on the Lines
  // page, so households on the same line read as a group. A household's fill
  // says WHICH LINE serves it; its ring says WHAT STATE that line is in.
  var MODULE_FALLBACK = '#64748b';   // unassigned household

  function lineColor(entity) {
    return (entity && entity.color) || MODULE_FALLBACK;
  }

  var STATUS_LABEL = {
    NORMAL: 'Normal',
    LOW_SUPPLY: 'Low Water Supply',
    POSSIBLE_LEAK: 'Possible Leak',
    MAINTENANCE: 'Under Maintenance',
    OFFLINE: 'Offline'
  };

  function hasCoordinates() {
    var lines = segments.some(function (segment) {
      return segment.coords && segment.coords.start && segment.coords.end;
    });
    // Surveyed households alone are enough to justify a real map, even before
    // anyone has put coordinates on the supply lines themselves - and so is
    // the municipal outline, which gives the map something to show while the
    // survey is still being done.
    return lines || households.length > 0 || !!window.SHL_BOUNDARY_URL;
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

  // Draws the municipal outline and frames the map on it. Runs after the
  // markers so the fit below is not overridden, and fails quietly: a missing
  // boundary file should cost the outline, not the whole map.
  function addBoundary(hasOwnBounds) {
    var url = window.SHL_BOUNDARY_URL;
    if (!url || !leafletMap) return;

    fetch(url, { credentials: 'same-origin' })
      .then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.json();
      })
      .then(function (geojson) {
        var layer = window.L.geoJSON(geojson, {
          style: {
            color: '#0b6ea8',
            weight: 2,
            opacity: 0.85,
            fillColor: '#0b6ea8',
            fillOpacity: 0.06,
            dashArray: '5 4'
          },
          interactive: false      // never steal a click from a household pin
        }).addTo(leafletMap);

        var props = (geojson.features && geojson.features[0] &&
                     geojson.features[0].properties) || {};
        if (props.name) {
          layer.bindTooltip(props.name + ', ' + (props.province || ''), {
            permanent: false, sticky: true
          });
        }

        // Only take over the view when there is nothing of our own to frame.
        if (!hasOwnBounds) leafletMap.fitBounds(layer.getBounds(), { padding: [20, 20] });
      })
      .catch(function () {
        // Boundary unavailable: the map is still usable, just without it.
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

    // The water source: the 0 m mark every distance is measured from. Drawn as
    // a filled square so it reads as infrastructure at a glance, distinct from
    // the round household pins and the round line markers.
    if (source && source.coords && source.coords[0] !== null && source.coords[1] !== null) {
      var sourceIcon = window.L.divIcon({
        className: 'src-pin',
        // A pulsing halo behind a large ringed marker: the one thing on the
        // map that should be findable at a glance from across the room.
        html: '<span class="src-pin-halo" aria-hidden="true"></span>' +
              '<span class="src-pin-dot" aria-hidden="true"></span>',
        iconSize: [34, 34],
        iconAnchor: [17, 17]
      });
      window.L.marker(source.coords, {
        icon: sourceIcon,
        zIndexOffset: 1000,
        title: source.name
      })
        .addTo(leafletMap)
        .bindTooltip(source.name, {
          permanent: true,          // always readable, not only on hover
          direction: 'right',
          offset: [14, 0],
          className: 'src-tip'
        })
        .bindPopup(
          '<strong>' + escapeHtml(source.name) + '</strong><br>' +
          'Start of the main line (0 m)' +
          (source.notes ? '<br>' + escapeHtml(source.notes) : '')
        );
      bounds.push(source.coords);
    }

    // One branch per supply line: the valve, then that line's households in
    // order of how far they are from it. Drawn before the pins so the pins
    // sit on top of the run rather than under it.
    //
    // Three branches all starting at the valve, rather than one continuous
    // pipe - the households on a line are what the line is defined by, so a
    // branch ends at its last household.
    var haveSource = source && source.coords &&
                     source.coords[0] !== null && source.coords[1] !== null;

    function metresBetween(a, b) {
      // Equirectangular approximation: plenty at barangay distances and it
      // avoids trigonometry on every comparison while sorting.
      var latScale = 110574;
      var lngScale = 111320 * Math.cos(a[0] * Math.PI / 180);
      var dy = (b[0] - a[0]) * latScale;
      var dx = (b[1] - a[1]) * lngScale;
      return Math.sqrt(dx * dx + dy * dy);
    }

    if (haveSource) {
      var byModule = {};
      households.forEach(function (house) {
        if (!house.coords || house.coords[0] === null || house.coords[1] === null) return;
        if (house.module === null || house.module === undefined) return;
        (byModule[house.module] = byModule[house.module] || []).push(house);
      });

      Object.keys(byModule).forEach(function (module) {
        var group = byModule[module].slice().sort(function (a, b) {
          return metresBetween(source.coords, a.coords) -
                 metresBetween(source.coords, b.coords);
        });

        var path = [source.coords].concat(group.map(function (h) { return h.coords; }));
        var colour = lineColor(group[0]);   // every household on a line shares it

        // A wider translucent casing under the line, so it stays readable
        // over dark tiles, forest green and pale roads alike.
        window.L.polyline(path, {
          color: '#ffffff', weight: 7, opacity: 0.75, interactive: false
        }).addTo(leafletMap);

        window.L.polyline(path, {
          color: colour, weight: 3.5, opacity: 0.95, lineJoin: 'round', lineCap: 'round'
        })
          .addTo(leafletMap)
          .bindTooltip('M' + module + ' &mdash; ' + group.length + ' household(s)', { sticky: true });
      });
    }

    // Household pins. Fill = which supply line, ring = that line's status, so
    // the grouping survives every line being in the same state at once.
    households.forEach(function (house) {
      if (!house.coords || house.coords[0] === null || house.coords[1] === null) return;
      var pin = window.L.circleMarker(house.coords, {
        radius: 7,
        color: window.SHL.statusColor(house.status),
        weight: 3,
        fillColor: lineColor(house),
        fillOpacity: 1
      }).addTo(leafletMap);

      pin.bindPopup(
        '<strong>' + escapeHtml(house.household_id) + '</strong><br>' +
        escapeHtml(house.owner_name) + '<br>' +
        escapeHtml(house.address) +
        (house.segment
          ? '<br>Line: M' + escapeHtml(house.module) + ' &mdash; ' + escapeHtml(house.segment) +
            '<br>Status: ' + escapeHtml(STATUS_LABEL[house.status] || house.status)
          : '')
      );
      bounds.push(house.coords);
    });

    if (bounds.length) {
      // The monitored run is only ~50 m end to end, so the pins sit in a
      // tight cluster. 17 is close enough to separate neighbouring houses
      // while still stopping a single pin from filling the whole panel.
      leafletMap.fitBounds(bounds, { padding: [30, 30], maxZoom: 17 });
    } else {
      // Sison town centre, until addBoundary() reframes on the municipality.
      leafletMap.setView([9.6590, 125.5269], 12);
    }
    addBoundary(bounds.length > 0);
  }

  // Popups are built as HTML strings, so anything typed by the facilitator is
  // escaped before it goes in.
  function escapeHtml(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
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

  // Leaflet measures its container once and caches the result. Nothing tells
  // it when that box changes - resizing the window, or crossing the 1024px
  // breakpoint where the sidebar stops taking 250px of the row - so the map
  // keeps drawing at its old size and the tiles no longer line up with the
  // card. invalidateSize() re-measures; it is debounced because a drag-resize
  // fires continuously.
  var sizeTimer = null;
  function refreshMapSize() {
    if (!leafletMap || mode !== 'leaflet') return;
    clearTimeout(sizeTimer);
    sizeTimer = setTimeout(function () { leafletMap.invalidateSize(); }, 120);
  }

  if (mapNode && window.ResizeObserver) {
    // Watches the element itself, so it also catches layout changes that do
    // not resize the window - the drawer opening, a card above it growing.
    new window.ResizeObserver(refreshMapSize).observe(mapNode);
  } else {
    window.addEventListener('resize', refreshMapSize);
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
