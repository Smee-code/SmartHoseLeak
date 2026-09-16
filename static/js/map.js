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

  // Each household's hose, keyed by the line it belongs to, so the live poll
  // can restyle it in place. Without this the hoses were painted once at
  // load and never again: a leak arriving two seconds later left the map
  // showing a healthy white outline until somebody reloaded the page.
  var hoseLayers = {};
  var mode = 'schematic';
  var userForced = false;

  // Each line carries its own colour, chosen by the facilitator on the Lines
  // page, so households on the same line read as a group. A household's fill
  // says WHICH LINE serves it; its ring says WHAT STATE that line is in.
  var MODULE_FALLBACK = '#64748b';   // unassigned household

  function lineColor(entity) {
    return (entity && entity.color) || MODULE_FALLBACK;
  }

  /*
   * How a household's hose is outlined, from its status alone.
   *
   * Normally white: the casing is there only to keep a coloured hose
   * readable over dark tiles, forest green and pale roads alike. On a leak
   * it turns red and widens into a halo.
   *
   * The alarm goes in the OUTLINE rather than the hose itself so both read
   * at once - the hose keeps its own colour, so whose it is stays obvious,
   * while the red says something is wrong with it. Recolouring the hose
   * would throw away the household identity at the moment it matters most.
   *
   * One function, used by the first paint and by every live update, so the
   * two cannot drift apart.
   */
  function casingStyle(status) {
    var leaking = status === 'POSSIBLE_LEAK';
    return {
      color: leaking ? window.SHL.statusColor('POSSIBLE_LEAK') : '#ffffff',
      weight: leaking ? 11 : 7,
      opacity: leaking ? 0.9 : 0.75,
      interactive: false
    };
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

    // A fan, not a ruler.
    //
    // This used to be a distance line: one horizontal run with the three
    // modules laid along it at their marked metres. That drawing described
    // the old rig honestly and describes the new one not at all - each house
    // now has its own hose from the main source, and none of them share a
    // length of pipe. Drawing them as a fan from a single source is the
    // shape of the real plumbing, and it is also the shape of the question
    // the facilitator is asking: which branch is in trouble.
    var width = 900;
    var rowHeight = 46;
    var topPad = 34;
    var height = Math.max(180, topPad * 2 + Math.max(segments.length, 1) * rowHeight);
    var sourceX = 96;
    var sourceY = height / 2;
    var houseX = width - 150;

    var parts = [];
    parts.push(
      '<svg class="schematic" viewBox="0 0 ' + width + ' ' + height + '" ' +
      'preserveAspectRatio="xMidYMid meet" role="img" ' +
      'aria-label="Each household’s hose line from the main source, with its current status">'
    );

    segments.forEach(function (segment, index) {
      var color = window.SHL.statusColor(segment.status);
      var y = topPad + index * rowHeight + rowHeight / 2;
      var label = segment.household ? segment.household.id : segment.name;
      var owner = segment.household ? segment.household.owner : '';
      var isLeak = segment.status === 'POSSIBLE_LEAK';

      parts.push('<g class="schematic-seg">');
      parts.push('<title>' + label + (owner ? ' - ' + owner : '') + ': ' +
                 (STATUS_LABEL[segment.status] || segment.status) + '</title>');

      // The hose: out of the source, then along to the house. Drawn as an
      // elbow rather than a diagonal so parallel branches stay readable
      // when a dozen of them leave the same point.
      var elbowX = sourceX + 70;
      parts.push(
        '<path d="M' + sourceX + ',' + sourceY + ' L' + elbowX + ',' + sourceY +
        ' L' + elbowX + ',' + y + ' L' + houseX + ',' + y + '" ' +
        'fill="none" stroke="' + color + '" stroke-width="7" stroke-linecap="round" ' +
        'stroke-linejoin="round" opacity="' + (segment.status === 'OFFLINE' ? 0.4 : 1) + '"/>'
      );

      // A line with no board is drawn dashed over the top: the hose exists,
      // nothing is watching it. That reads as "not yet" rather than "fault".
      if (segment.has_device === false) {
        parts.push(
          '<path d="M' + elbowX + ',' + y + ' L' + houseX + ',' + y + '" fill="none" ' +
          'stroke="var(--surface)" stroke-width="3" stroke-dasharray="5 5"/>'
        );
      }

      parts.push(
        '<circle cx="' + houseX + '" cy="' + y + '" r="8" fill="' + color + '" ' +
        'stroke="var(--surface)" stroke-width="2"/>'
      );
      parts.push(
        '<text x="' + (houseX + 16) + '" y="' + (y + 4) + '" font-size="11" ' +
        'font-weight="600" fill="var(--text)">' + label + '</text>'
      );

      if (isLeak) {
        parts.push(
          '<circle cx="' + houseX + '" cy="' + y + '" r="14" fill="none" ' +
          'stroke="' + color + '" stroke-width="2" opacity="0.55"/>'
        );
      }

      parts.push('</g>');
    });

    // The source is drawn last so the branches pass behind it.
    parts.push(
      '<g>' +
      '<rect x="' + (sourceX - 17) + '" y="' + (sourceY - 17) + '" width="34" height="34" rx="6" ' +
      'fill="var(--brand-soft)" stroke="var(--brand)" stroke-width="1.5"/>' +
      '<text x="' + sourceX + '" y="' + (sourceY + 5) + '" text-anchor="middle" ' +
      'font-size="14" fill="var(--brand-strong)">&#9678;</text>' +
      '<text x="' + sourceX + '" y="' + (sourceY + 34) + '" text-anchor="middle" ' +
      'font-size="10" fill="var(--text-muted)">Main source</text>' +
      '</g>'
    );

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
          'Every household&rsquo;s hose starts here' +
          (source.notes ? '<br>' + escapeHtml(source.notes) : '')
        );
      bounds.push(source.coords);
    }

    // One hose per household, drawn straight from the main source to the
    // house. Not a chain through several houses: nothing is shared any more,
    // so a run visiting them in turn would draw plumbing that does not exist
    // and imply that a leak upstream affects everyone below it.
    //
    // Drawn before the pins so each pin sits on top of its own hose.
    var haveSource = source && source.coords &&
                     source.coords[0] !== null && source.coords[1] !== null;

    if (haveSource) {
      households.forEach(function (house) {
        if (!house.coords || house.coords[0] === null || house.coords[1] === null) return;

        var path = [source.coords, house.coords];
        var colour = lineColor(house);
        var offline = house.status === 'OFFLINE';
        var leaking = house.status === 'POSSIBLE_LEAK';

        // The casing under the line does double duty. Normally it is white,
        // there only to keep the hose readable over dark tiles, forest green
        // and pale roads alike. On a leak it turns red and widens into a
        // halo.
        //
        // Putting the alarm in the OUTLINE rather than the line itself is
        // what makes both readable at once: the hose keeps its own colour,
        // so it is still obvious whose it is, while the red says something
        // is wrong with it. Recolouring the line would have thrown away the
        // household identity at exactly the moment it matters most.
        var casing = window.L.polyline(path, casingStyle(house.status))
          .addTo(leafletMap);

        var hose = window.L.polyline(path, {
          color: colour,
          weight: 3.5,
          opacity: offline ? 0.55 : 0.95,
          // A hose with no board fitted is dashed: the pipe is there, nothing
          // is watching it. That has to read as "not yet", not as a fault.
          dashArray: house.has_device === false ? '6 6' : null,
          lineJoin: 'round',
          lineCap: 'round'
        })
          .addTo(leafletMap)
          .bindTooltip(
            escapeHtml(house.household_id) + ' &mdash; ' + escapeHtml(house.owner_name) +
            (house.has_device === false ? '<br>no device fitted' : ''),
            { sticky: true }
          );

        if (house.segment_id !== null && house.segment_id !== undefined) {
          hoseLayers[house.segment_id] = { casing: casing, hose: hose, house: house };
        }
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
    var title = segment.household
      ? escapeHtml(segment.household.id) + ' &mdash; ' + escapeHtml(segment.household.owner)
      : escapeHtml(segment.name);
    var html =
      '<strong>' + title + '</strong><br>' +
      (STATUS_LABEL[segment.status] || segment.status) + '<br>' +
      (segment.has_device
        ? (segment.device_online ? 'Device reporting' : 'Device not reporting')
        : 'No device fitted') + '<br>' +
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
          // A household's hose: the outline carries the alarm, the hose keeps
          // its own colour. Repainted here rather than only at load, so a
          // leak reported two seconds ago shows up without a reload.
          var hoseLayer = hoseLayers[segment.id];
          if (hoseLayer) {
            hoseLayer.casing.setStyle(casingStyle(segment.status));
            hoseLayer.hose.setStyle({
              opacity: segment.status === 'OFFLINE' ? 0.55 : 0.95
            });
          }

          // The line drawn from the line's own coordinates, where those are
          // set. Status-coloured, since it carries no household identity.
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
