/*
 * Quarterline UI behaviour (wave F4, deterministic app).
 *
 * - Charts: reads the server-rendered JSON in #chart-data (values are
 *   Decimal-precision strings; they are converted to Numbers only for
 *   plotting -- the tables keep the exact strings) and draws Chart.js
 *   line charts for revenue, net income, margins and FCF.
 * - If Chart.js (vendored) failed to load or JavaScript data is missing,
 *   the server-rendered summary tables remain the fallback; nothing here
 *   is required for the page to be readable (SPEC §25: with the model
 *   stopped, tables and charts still work).
 *
 * No fetching of third-party resources: everything is served from /static.
 */
(function () {
  "use strict";

  function toNumber(entry) {
    if (!entry || entry.value === null || entry.value === undefined) return null;
    var parsed = Number(entry.value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function lineDataset(label, values, dash) {
    return {
      label: label,
      data: values,
      spanGaps: true,
      tension: 0.25,
      borderDash: dash || undefined,
      fill: false,
    };
  }

  function renderCharts(payload) {
    var quarters = (payload && payload.quarters) || [];
    var labels = quarters.map(function (q) { return q.label || q.period_end; });

    function values(metricId, scale) {
      scale = scale || 1;
      return quarters.map(function (q) {
        var v = toNumber(q.values[metricId]);
        return v === null ? null : v / scale;
      });
    }

    function draw(canvasId, datasets, yTitle) {
      var canvas = document.getElementById(canvasId);
      if (!canvas) return;
      /* eslint-disable no-undef */
      new Chart(canvas, {
        type: "line",
        data: { labels: labels, datasets: datasets },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: "index", intersect: false },
          scales: {
            y: { title: { display: true, text: yTitle } },
          },
        },
      });
    }

    draw("chart-revenue", [lineDataset("Revenue (USD M)", values("revenue", 1e6))], "USD millions");
    draw("chart-net-income", [lineDataset("Net income (USD M)", values("net_income", 1e6))], "USD millions");
    draw(
      "chart-margins",
      [
        lineDataset("Operating margin (%)", values("operating_margin", 0.01)),
        lineDataset("Net margin (%)", values("net_margin", 0.01)),
      ],
      "percent"
    );
    draw("chart-fcf", [lineDataset("FCF (USD M)", values("fcf", 1e6))], "USD millions");
  }

  function init() {
    var dataEl = document.getElementById("chart-data");
    if (!dataEl) return;
    var payload;
    try {
      payload = JSON.parse(dataEl.textContent);
    } catch (err) {
      return; // keep the server-rendered tables as the fallback
    }
    if (typeof Chart === "undefined") {
      // Vendored Chart.js unavailable: chart panels degrade to the tables.
      document.querySelectorAll(".chart-panel").forEach(function (panel) {
        panel.classList.add("chart-unavailable");
      });
      return;
    }
    renderCharts(payload);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
