/* =========================================================================
   Pressure / flow history chart.

   Drawn as inline SVG rather than pulling in a charting library, so the
   dashboard renders with no external requests and works on a weak link.
   Reads /api/history/, which serves the minute rollups.
   ========================================================================= */
(function () {
  'use strict';

  var host = document.getElementById('history-chart');
  if (!host) return;

  var rangeSelect = document.getElementById('chart-range');
  var metricSelect = document.getElementById('chart-metric');
  var legendNode = document.getElementById('chart-legend');
  var segmentFilter = host.getAttribute('data-segment') || '';

  var SERIES_COLORS = ['#0b6ea8', '#17794c', '#a8620b', '#7a3fb5'];
  var cache = null;

  function fetchHistory() {
    var hours = rangeSelect ? rangeSelect.value : '6';
    var url = '/api/history/?hours=' + encodeURIComponent(hours);
    if (segmentFilter) url += '&segment=' + encodeURIComponent(segmentFilter);

    host.innerHTML = '<p class="empty">Loading history...</p>';
    fetch(url, { credentials: 'same-origin' })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        cache = data;
        draw();
      })
      .catch(function () {
        host.innerHTML = '<p class="empty">History could not be loaded.</p>';
      });
  }

  function draw() {
    if (!cache) return;
    var series = (cache.series || []).filter(function (s) { return s.points.length > 1; });

    if (!series.length) {
      host.innerHTML =
        '<p class="empty">No readings logged for this period yet.<br>' +
        '<span class="small">Data appears once the device starts posting.</span></p>';
      if (legendNode) legendNode.innerHTML = '';
      return;
    }

    var metric = metricSelect ? metricSelect.value : 'pressure_bar';
    var unit = { pressure_bar: 'bar', pressure_psi: 'psi', flow_lpm: 'L/min' }[metric] || '';

    var width = 900;
    var height = 260;
    var padL = 52, padR = 16, padT = 14, padB = 34;
    var plotW = width - padL - padR;
    var plotH = height - padT - padB;

    // Shared scales across all series so the lines are comparable.
    var times = [], values = [];
    series.forEach(function (s) {
      s.points.forEach(function (point) {
        times.push(new Date(point.t).getTime());
        values.push(point[metric]);
      });
    });

    var tMin = Math.min.apply(null, times);
    var tMax = Math.max.apply(null, times);
    var vMin = Math.min.apply(null, values);
    var vMax = Math.max.apply(null, values);

    // Pad the value axis so a flat line does not sit on the frame.
    var span = vMax - vMin;
    if (span < 1e-6) { vMin -= 0.5; vMax += 0.5; }
    else { vMin -= span * 0.12; vMax += span * 0.12; }
    if (vMin < 0) vMin = 0;

    var x = function (t) {
      if (tMax === tMin) return padL + plotW / 2;
      return padL + ((t - tMin) / (tMax - tMin)) * plotW;
    };
    var y = function (v) { return padT + plotH - ((v - vMin) / (vMax - vMin)) * plotH; };

    var parts = ['<svg class="chart" viewBox="0 0 ' + width + ' ' + height +
                 '" preserveAspectRatio="none" role="img" aria-label="History chart">'];

    // Horizontal gridlines + value labels.
    for (var i = 0; i <= 4; i++) {
      var value = vMin + ((vMax - vMin) * i) / 4;
      var gy = y(value);
      parts.push('<line x1="' + padL + '" y1="' + gy + '" x2="' + (width - padR) + '" y2="' + gy +
                 '" stroke="var(--border)" stroke-width="1"/>');
      parts.push('<text x="' + (padL - 8) + '" y="' + (gy + 4) + '" text-anchor="end" font-size="10" ' +
                 'fill="var(--text-muted)">' + value.toFixed(2) + '</text>');
    }

    // Time labels at both ends and the midpoint.
    [[tMin, padL, 'start'], [(tMin + tMax) / 2, padL + plotW / 2, 'middle'], [tMax, width - padR, 'end']]
      .forEach(function (entry) {
        var label = new Date(entry[0]).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        parts.push('<text x="' + entry[1] + '" y="' + (height - 10) + '" text-anchor="' + entry[2] +
                   '" font-size="10" fill="var(--text-muted)">' + label + '</text>');
      });

    var legendParts = [];
    series.forEach(function (s, index) {
      var color = SERIES_COLORS[index % SERIES_COLORS.length];
      var d = s.points.map(function (point, pointIndex) {
        return (pointIndex ? 'L' : 'M') + x(new Date(point.t).getTime()).toFixed(1) + ',' +
               y(point[metric]).toFixed(1);
      }).join(' ');

      parts.push('<path d="' + d + '" fill="none" stroke="' + color +
                 '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>');

      // Mark minutes where the device flagged a leak.
      s.points.forEach(function (point) {
        if (point.leak_samples > 0) {
          parts.push('<circle cx="' + x(new Date(point.t).getTime()).toFixed(1) + '" cy="' +
                     y(point[metric]).toFixed(1) + '" r="2.8" fill="#b3261e"/>');
        }
      });

      legendParts.push('<span><i style="background:' + color + '"></i>M' + s.module + ' ' + s.segment + '</span>');
    });

    legendParts.push('<span><i style="background:#b3261e;border-radius:50%;width:8px;height:8px"></i>' +
                     'Minute containing a leak reading</span>');
    legendParts.push('<span class="muted">Values in ' + unit + '</span>');

    parts.push('</svg>');
    host.innerHTML = parts.join('');
    if (legendNode) legendNode.innerHTML = legendParts.join('');
  }

  if (rangeSelect) rangeSelect.addEventListener('change', fetchHistory);
  if (metricSelect) metricSelect.addEventListener('change', draw);
  document.addEventListener('shl:theme', draw);

  fetchHistory();
  // Rollups are minute-grained, so refreshing every minute is enough.
  setInterval(fetchHistory, 60000);
})();
