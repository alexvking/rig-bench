/* global echarts */
const FALLBACK_PALETTE = ["#38bdf8", "#f97316", "#34d399", "#a78bfa"];

const state = {
  manifest: null,
  viewMode: "all", // "all" | "pc_oc_delta" | "m4_vs_oc" | "m4_vs_stock"
  lens: "raw", // "raw" | "ppw" | "ppd" | "ppwd"
  baselineId: null,
  challengerId: null,
  customWattsScale: {},
  customPriceUsd: {},
  charts: {},
  initialModeChosen: false,
};

function getChart(domId) {
  if (!state.charts[domId]) {
    const el = document.getElementById(domId);
    state.charts[domId] = echarts.init(el, null, { renderer: "canvas" });
  }
  return state.charts[domId];
}

window.addEventListener("resize", () => {
  Object.values(state.charts).forEach((c) => c && c.resize());
});

function profileKey(p) {
  return p.profile_id || p.machine_id;
}

function profileColor(p, idx = 0) {
  const k = profileKey(p);
  if (k.includes("m4")) return "#38bdf8";
  if (k.includes("souped") || p.tuning_state === "souped_up") return "#a78bfa";
  if (k.endsWith("-oc") || p.tuning_state === "overclocked") return "#34d399";
  if (k.includes("zen2") || k.includes("stock")) return "#f97316";
  return FALLBACK_PALETTE[idx % FALLBACK_PALETTE.length];
}

function shortLabel(p) {
  if (p.sysinfo?.short_name) return p.sysinfo.short_name;
  const k = profileKey(p);
  if (k.includes("m4")) return "M4 Mac Mini";
  if (k.includes("souped")) return "Desktop (Souped Up)";
  if (k.endsWith("-oc")) return "Desktop (After OC)";
  if (k.endsWith("-stock")) return "Desktop (Before OC)";
  return p.sysinfo?.display_name || k;
}

function allProfiles() {
  if (!state.manifest) return [];
  const src = state.manifest.profiles || state.manifest.machines || {};
  return Object.values(src);
}

function findProfileByKind(profiles, kind) {
  if (kind === "m4") return profiles.find((p) => profileKey(p).includes("m4"));
  if (kind === "pc_souped") {
    return profiles.find((p) => profileKey(p).includes("souped") || p.tuning_state === "souped_up");
  }
  if (kind === "pc_oc") {
    return profiles.find((p) => profileKey(p).endsWith("-oc") || p.tuning_state === "overclocked");
  }
  if (kind === "pc_stock") {
    return (
      profiles.find((p) => profileKey(p).endsWith("-stock")) ||
      profiles.find((p) => !profileKey(p).includes("m4") && p.tuning_state !== "overclocked" && p.tuning_state !== "souped_up")
    );
  }
  return null;
}

function activeMachines() {
  const all = allProfiles();
  if (!all.length) return [];
  const m4 = findProfileByKind(all, "m4");
  const pcStock = findProfileByKind(all, "pc_stock");
  const pcOc = findProfileByKind(all, "pc_oc");
  const pcSouped = findProfileByKind(all, "pc_souped");

  if (state.viewMode === "pc_souped_vs_stock" && pcStock && pcSouped) {
    return [pcStock, pcSouped];
  }
  if (state.viewMode === "pc_souped_vs_oc" && pcOc && pcSouped) {
    return [pcOc, pcSouped];
  }
  if (state.viewMode === "pc_oc_delta" && pcStock && pcOc) {
    return [pcStock, pcOc];
  }
  if (state.viewMode === "m4_vs_souped" && m4 && pcSouped) {
    return [m4, pcSouped];
  }
  if (state.viewMode === "m4_vs_oc" && m4 && pcOc) {
    return [m4, pcOc];
  }
  if (state.viewMode === "m4_vs_stock" && m4 && pcStock) {
    return [m4, pcStock];
  }
  return all;
}

function applyViewModeDefaults(mode) {
  const all = allProfiles();
  const m4 = findProfileByKind(all, "m4");
  const pcStock = findProfileByKind(all, "pc_stock");
  const pcOc = findProfileByKind(all, "pc_oc");
  const pcSouped = findProfileByKind(all, "pc_souped");

  state.viewMode = mode;
  if (mode === "pc_souped_vs_stock" && pcStock && pcSouped) {
    state.baselineId = profileKey(pcStock);
    state.challengerId = profileKey(pcSouped);
  } else if (mode === "pc_souped_vs_oc" && pcOc && pcSouped) {
    state.baselineId = profileKey(pcOc);
    state.challengerId = profileKey(pcSouped);
  } else if (mode === "pc_oc_delta" && pcStock && pcOc) {
    state.baselineId = profileKey(pcStock);
    state.challengerId = profileKey(pcOc);
  } else if (mode === "m4_vs_souped" && m4 && pcSouped) {
    state.baselineId = profileKey(pcSouped);
    state.challengerId = profileKey(m4);
  } else if (mode === "m4_vs_oc" && m4 && pcOc) {
    state.baselineId = profileKey(pcOc);
    state.challengerId = profileKey(m4);
  } else if (mode === "m4_vs_stock" && m4 && pcStock) {
    state.baselineId = profileKey(pcStock);
    state.challengerId = profileKey(m4);
  } else if (mode === "all") {
    const topPc = pcSouped || pcOc || pcStock;
    if (topPc && m4) {
      state.baselineId = profileKey(topPc);
      state.challengerId = profileKey(m4);
    }
  }
}

function getBaseAvgWatts(machine) {
  const ww = machine.sysinfo?.workload_wall_watts;
  if (ww) {
    const vals = Object.values(ww);
    return Math.round(vals.reduce((a, b) => a + b, 0) / vals.length);
  }
  return machine.sysinfo?.estimated_peak_watts || 100;
}

function getDisplayWatts(machine) {
  const id = profileKey(machine);
  const baseAvg = getBaseAvgWatts(machine);
  const scale = state.customWattsScale[id] ?? 1.0;
  return Math.round(baseAvg * scale);
}

function getCategoryWatts(machine, categoryName) {
  const id = profileKey(machine);
  const scale = state.customWattsScale[id] ?? 1.0;
  const ww = machine.sysinfo?.workload_wall_watts;
  const base = (ww && ww[categoryName]) || machine.sysinfo?.estimated_peak_watts || 100;
  return Math.max(1, base * scale);
}

function getPriceUsd(machine) {
  const id = profileKey(machine);
  if (state.customPriceUsd[id] != null) return state.customPriceUsd[id];
  return machine.sysinfo?.price_usd || 600;
}

function effectiveValue(machine, metricMeta) {
  const raw = machine.merged_metrics?.[metricMeta.id];
  if (raw == null) return null;
  if (state.lens === "raw") return raw;

  const watts = getCategoryWatts(machine, metricMeta.category);
  const hundredsUsd = Math.max(0.01, getPriceUsd(machine) / 100.0);

  if (state.lens === "ppw") {
    return metricMeta.higher_is_better ? raw / watts : raw * watts;
  }
  if (state.lens === "ppd") {
    return metricMeta.higher_is_better ? raw / hundredsUsd : raw * hundredsUsd;
  }
  if (state.lens === "ppwd") {
    return metricMeta.higher_is_better ? raw / (watts * hundredsUsd) : raw * watts * hundredsUsd;
  }
  return raw;
}

function effectiveUnit(metricMeta) {
  if (state.lens === "raw") return metricMeta.unit;
  if (state.lens === "ppw") {
    if (metricMeta.unit === "sec") return "Joules";
    return metricMeta.higher_is_better ? `${metricMeta.unit}/W` : `${metricMeta.unit}*W`;
  }
  if (state.lens === "ppd") {
    return metricMeta.higher_is_better ? `${metricMeta.unit}/100 USD` : `${metricMeta.unit}*100 USD`;
  }
  if (state.lens === "ppwd") {
    return metricMeta.higher_is_better ? `${metricMeta.unit}/(W*100 USD)` : `${metricMeta.unit}*W*100 USD`;
  }
  return metricMeta.unit;
}

function formatNum(val) {
  if (val == null || Number.isNaN(val)) return "—";
  const abs = Math.abs(val);
  if (abs >= 10000) return Math.round(val).toLocaleString();
  if (abs >= 100) return val.toFixed(1);
  if (abs >= 1) return val.toFixed(2);
  return val.toFixed(3);
}

function formatPct(ratio) {
  if (ratio == null || Number.isNaN(ratio)) return "—";
  const pct = (ratio - 1.0) * 100;
  return `${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%`;
}

function speedupRatio(targetVal, baseVal, higherIsBetter) {
  if (targetVal == null || baseVal == null || targetVal <= 0 || baseVal <= 0) return null;
  return higherIsBetter ? targetVal / baseVal : baseVal / targetVal;
}

function computeGeomeanStats(challenger, base, catalog, lensOverride = null, categoryFilter = null) {
  const savedLens = state.lens;
  if (lensOverride) state.lens = lensOverride;
  const logs = [];
  for (const meta of catalog) {
    if (meta.id === "blender_eevee_1440p_fps") continue;
    if (categoryFilter && !categoryFilter.includes(meta.category)) continue;
    const bVal = effectiveValue(base, meta);
    const cVal = effectiveValue(challenger, meta);
    const r = speedupRatio(cVal, bVal, meta.higher_is_better);
    if (r != null && r > 0) logs.push(Math.log(r));
  }
  state.lens = savedLens;
  if (!logs.length) return { ratio: null, count: 0 };
  return { ratio: Math.exp(logs.reduce((a, b) => a + b, 0) / logs.length), count: logs.length };
}

function computeGeomeanRatio(challenger, base, catalog, lensOverride = null, categoryFilter = null) {
  return computeGeomeanStats(challenger, base, catalog, lensOverride, categoryFilter).ratio;
}

function getComparisonPair(machines) {
  if (machines.length < 2) return { base: machines[0] || null, challenger: null };
  const base = machines.find((m) => profileKey(m) === state.baselineId) || machines[0];
  let challenger = machines.find(
    (m) => profileKey(m) === state.challengerId && profileKey(m) !== profileKey(base)
  );
  if (!challenger) {
    challenger = machines.find((m) => profileKey(m) !== profileKey(base)) || null;
  }
  return { base, challenger };
}

function isSameHardwareComparison(base, challenger) {
  if (!base || !challenger) return false;
  const hb = base.hardware_machine_id || base.sysinfo?.hardware_machine_id || base.machine_id;
  const hc = challenger.hardware_machine_id || challenger.sysinfo?.hardware_machine_id || challenger.machine_id;
  return hb === hc || (profileKey(base).includes("zen2") && profileKey(challenger).includes("zen2"));
}

function renderModeTabsAndOcBanner() {
  const all = allProfiles();
  const subtitleEl = document.getElementById("subtitle");
  if (subtitleEl) {
    subtitleEl.textContent = all.length
      ? `${all.length} profile${all.length === 1 ? "" : "s"} loaded: ${all.map((p) => shortLabel(p)).join(" · ")}`
      : "No benchmark runs recorded yet — run uv run bench.py run to capture your first profile";
  }

  const m4 = findProfileByKind(all, "m4");
  const pcStock = findProfileByKind(all, "pc_stock");
  const pcOc = findProfileByKind(all, "pc_oc");
  const pcSouped = findProfileByKind(all, "pc_souped");
  const hasOc = Boolean(pcOc);
  const hasSouped = Boolean(pcSouped);

  const modeBar = document.querySelector(".mode-bar");
  const hasPresetSuite = Boolean(m4 && pcStock && (hasOc || hasSouped));
  if (modeBar) {
    modeBar.classList.toggle("hidden", !hasPresetSuite);
  }

  document.querySelectorAll("#viewModeTabs .mode-tab").forEach((btn) => {
    const mode = btn.getAttribute("data-mode");
    const requiresOc = mode === "pc_oc_delta" || mode === "m4_vs_oc" || mode === "pc_souped_vs_oc";
    const requiresSouped = mode === "pc_souped_vs_stock" || mode === "m4_vs_souped" || mode === "pc_souped_vs_oc";
    btn.disabled = (requiresOc && !hasOc) || (requiresSouped && !hasSouped);
    btn.classList.toggle("active", state.viewMode === mode);
  });

  const statusEl = document.getElementById("ocRunStatus");
  if (statusEl) {
    statusEl.textContent = "";
  }

  const banner = document.getElementById("ocBanner");
  const topPc = pcSouped || pcOc;
  if (!pcStock || !topPc || !hasPresetSuite) {
    banner.classList.add("hidden");
    return;
  }

  const stockGpu = pcStock.sysinfo?.gpu_power_telemetry_nvidia_smi || {};
  const ocGpu = topPc.sysinfo?.gpu_power_telemetry_nvidia_smi || {};
  const stock1tMhz = pcStock.merged_suites?.cpu?.sevenzip?.single_thread?.peak_1t_mhz;
  const oc1tMhz = topPc.merged_suites?.cpu?.sevenzip?.single_thread?.peak_1t_mhz;
  const stockPl = stockGpu.default_tdp_limit_watts || 125;
  const ocPl = ocGpu.active_power_limit_watts || ocGpu.max_tdp_limit_watts || 150;
  const stockMem = stockGpu.mem_clock_mhz || 6801;
  const ocMem = ocGpu.mem_clock_mhz || 7650;
  const stockCore = stockGpu.boost_clock_mhz || 1935;
  const ocCore = ocGpu.boost_clock_mhz || ocGpu.max_boost_clock_mhz || 2130;

  const cpuDeltaNum = stock1tMhz && oc1tMhz ? ((oc1tMhz - stock1tMhz) / stock1tMhz) * 100 : null;
  const cpuColor = cpuDeltaNum == null || cpuDeltaNum >= -2.0 ? "var(--emerald)" : "var(--amber)";
  const cpuTuningNote = topPc.sysinfo?.cpu_tuning_note ? ` · ${topPc.sysinfo.cpu_tuning_note}` : "";
  const ramPill = topPc.sysinfo?.ram_tuning_note
    ? `<span class="oc-pill">Upgrades: <b style="color:var(--violet)">${topPc.sysinfo.ram_tuning_note}</b></span>`
    : "";
  const cpuPill =
    stock1tMhz && oc1tMhz
      ? `<span class="oc-pill">Ryzen 5 3600 Boost: ${stock1tMhz} &rarr; <b style="color:${cpuColor}">${oc1tMhz} MHz${cpuTuningNote}</b></span>`
      : "";
  const tdrPill = ocGpu.stability_note
    ? `<span class="oc-pill" title="${ocGpu.stability_note}">VRAM Stability: <b style="color:var(--amber)">TDR reset before llama-bench @ 7850 MHz</b></span>`
    : ocGpu.tdr_events === 0
      ? `<span class="oc-pill">Stability &amp; Thermals: <b>0 WHEA · 0 TDR · ${ocGpu.post_run_cpu_tdie_c ? `${ocGpu.post_run_cpu_tdie_c}&deg;C CPU · ` : ""}${ocGpu.post_run_temp_c ?? 46}&deg;C GPU${ocGpu.post_run_ssd_temp_c ? ` · ${ocGpu.post_run_ssd_temp_c}&deg;C SSD` : ""}</b></span>`
      : "";

  const bannerTitle = hasSouped
    ? "Desktop Souped Up Telemetry (Stock &rarr; Souped Up)"
    : "Desktop Gentle Overclock Telemetry";

  banner.classList.remove("hidden");
  banner.innerHTML = `
    <div>
      <span class="oc-banner-title">${bannerTitle}</span>
      <span>Before vs. After hardware envelope captured on <code>desktop-16hn1b0</code></span>
    </div>
    <div class="oc-pills">
      <span class="oc-pill">GTX 1660S Power Limit: ${stockPl}W &rarr; <b>${ocPl}W (+${Math.round(((ocPl - stockPl) / stockPl) * 100)}%)</b></span>
      <span class="oc-pill">GDDR6 Memory: ${stockMem} &rarr; <b>${ocMem} MHz (+${(((ocMem - stockMem) / stockMem) * 100).toFixed(1)}%)</b></span>
      <span class="oc-pill">GPU Core Ceiling: ${stockCore} &rarr; <b>${ocCore} MHz</b></span>
      ${cpuPill}
      ${ramPill}
      ${tdrPill}
    </div>
  `;
}

function renderKpiStrip() {
  const el = document.getElementById("kpiStrip");
  const machines = activeMachines();
  const all = allProfiles();
  const catalog = state.manifest?.metric_catalog || [];
  if (machines.length < 2) {
    el.innerHTML = "";
    return;
  }

  const { base, challenger } = getComparisonPair(machines);

  // Mode A: Dedicated Before vs. After Overclock Uplift KPI Strip
  if (state.viewMode === "pc_oc_delta" || state.viewMode === "pc_souped_vs_stock" || isSameHardwareComparison(base, challenger)) {
    const pcStock = base || findProfileByKind(all, "pc_stock");
    const pcOc = challenger || findProfileByKind(all, "pc_souped") || findProfileByKind(all, "pc_oc");
    if (!pcStock || !pcOc) return;

    const suiteGeo = computeGeomeanRatio(pcOc, pcStock, catalog, "raw");
    const gpuGeo = computeGeomeanRatio(pcOc, pcStock, catalog, "raw", ["GPU Graphics", "GPU Math"]);
    const cpuIoGeo = computeGeomeanRatio(pcOc, pcStock, catalog, "raw", ["CPU", "SSD and I/O Compute"]);
    const ppwGeo = computeGeomeanRatio(pcOc, pcStock, catalog, "ppw");

    // Find top single metric percentage gain
    let bestGain = { label: "—", ratio: 1.0 };
    for (const meta of catalog) {
      if (meta.id === "blender_eevee_1440p_fps") continue;
      const saved = state.lens;
      state.lens = "raw";
      const r = speedupRatio(effectiveValue(pcOc, meta), effectiveValue(pcStock, meta), meta.higher_is_better);
      state.lens = saved;
      if (r != null && r > bestGain.ratio) {
        bestGain = { label: meta.label, ratio: r };
      }
    }

    el.innerHTML = `
      <div class="kpi-card">
        <div class="kpi-title">Full-Suite Overclock Uplift (16 Tests)</div>
        <div class="kpi-value" style="color:#34d399">${formatPct(suiteGeo)} (${suiteGeo ? `${suiteGeo.toFixed(2)}x` : "—"})</div>
        <div class="kpi-sub">Geomean across CPU, CUDA compute, VRAM bandwidth, and NVMe pipeline</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-title">GPU Compute and VRAM Geomean</div>
        <div class="kpi-value" style="color:#38bdf8">${formatPct(gpuGeo)} (${gpuGeo ? `${gpuGeo.toFixed(2)}x` : "—"})</div>
        <div class="kpi-sub">Peak gain: ${bestGain.label} (${formatPct(bestGain.ratio)})</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-title">CPU and Out-of-Core Geomean</div>
        <div class="kpi-value" style="color:#fbbf24">${formatPct(cpuIoGeo)} (${cpuIoGeo ? `${cpuIoGeo.toFixed(2)}x` : "—"})</div>
        <div class="kpi-sub">7-Zip, FP64 GEMM, SHA-256, fio direct I/O, and 80M-row DuckDB spill</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-title">Perf per Watt Efficiency Delta</div>
        <div class="kpi-value" style="color:#a78bfa">${formatPct(ppwGeo)}</div>
        <div class="kpi-sub">125W &rarr; 150W board limit (${getDisplayWatts(pcStock)}W vs ${getDisplayWatts(pcOc)}W avg system wall draw)</div>
      </div>
    `;
    return;
  }

  // Mode B: Cross-Platform Executive KPI Strip (M4 vs active PC config + optional OC quick-jump card)
  const m4 = findProfileByKind(machines, "m4") || machines[0];
  const pcSouped = findProfileByKind(all, "pc_souped");
  const pcOc = findProfileByKind(all, "pc_oc");
  const pcStock = findProfileByKind(all, "pc_stock");
  const pcTarget =
    state.viewMode === "m4_vs_stock"
      ? pcStock
      : state.viewMode === "m4_vs_oc"
        ? pcOc
        : pcSouped || pcOc || pcStock || machines.find((m) => profileKey(m) !== profileKey(m4));

  if (!m4 || !pcTarget) return;

  const rawStats = computeGeomeanStats(challenger, base, catalog, "raw");
  const rawGeo = rawStats.ratio;
  const ppwGeo = computeGeomeanRatio(challenger, base, catalog, "ppw");
  const ppdGeo = computeGeomeanRatio(challenger, base, catalog, "ppd");

  const basePipeSec = base.merged_metrics?.pipeline_total_sec;
  const chalPipeSec = challenger.merged_metrics?.pipeline_total_sec;
  const baseJoules = basePipeSec ? (basePipeSec * getCategoryWatts(base, "SSD and I/O Compute")) / 1000 : null;
  const chalJoules = chalPipeSec ? (chalPipeSec * getCategoryWatts(challenger, "SSD and I/O Compute")) / 1000 : null;

  const baseIdle = base.sysinfo?.idle_wall_watts ?? "—";
  const chalIdle = challenger.sysinfo?.idle_wall_watts ?? "—";
  const baseShort = shortLabel(base);
  const chalShort = shortLabel(challenger);

  let ocQuickCard = "";
  const bestTunedPc = pcSouped || pcOc;
  if (pcStock && bestTunedPc && m4 && (profileKey(base) === profileKey(m4) || profileKey(challenger) === profileKey(m4))) {
    const ocSuite = computeGeomeanRatio(bestTunedPc, pcStock, catalog, "raw");
    const ocGpu = computeGeomeanRatio(bestTunedPc, pcStock, catalog, "raw", ["GPU Graphics", "GPU Math"]);
    const cardTitle = pcSouped ? "Desktop Souped Up Gain (Stock &rarr; Souped Up)" : "Desktop Gentle OC Gain (Before &rarr; After)";
    const accent = pcSouped ? "#a78bfa" : "#34d399";
    ocQuickCard = `
      <div class="kpi-card clickable" id="jumpToOcCard" title="Click to inspect Desktop Stock vs. ${pcSouped ? "Souped Up" : "GPU OC"}">
        <div class="kpi-title">${cardTitle}</div>
        <div class="kpi-value" style="color:${accent}">${formatPct(ocGpu)} GPU · ${formatPct(ocSuite)} All</div>
        <div class="kpi-sub">Click to switch into Stock vs. ${pcSouped ? "Souped Up" : "After OC"} view &rarr;</div>
      </div>
    `;
  }

  el.innerHTML = `
    <div class="kpi-card">
      <div class="kpi-title">Raw Throughput Geomean (${rawStats.count} Tests)</div>
      <div class="kpi-value">${rawGeo ? `${rawGeo.toFixed(2)}x` : "—"} ${chalShort} vs ${baseShort}</div>
      <div class="kpi-sub">Geometric mean across CPU, GPU compute, and SSD I/O suites</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Performance per Watt Geomean</div>
      <div class="kpi-value" style="color:#34d399">${ppwGeo ? `${ppwGeo.toFixed(2)}x` : "—"} ${chalShort}</div>
      <div class="kpi-sub">${getDisplayWatts(chalenger)}W avg vs ${getDisplayWatts(base)}W avg wall draw</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Performance per Dollar (${getPriceUsd(challenger)} vs ${getPriceUsd(base)} USD)</div>
      <div class="kpi-value" style="color:#fbbf24">${ppdGeo ? `${ppdGeo.toFixed(2)}x` : "—"} ${chalShort}</div>
      <div class="kpi-sub">Normalized to acquisition cost in USD (editable on rig cards)</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">80M-Row Job Energy and Idle</div>
      <div class="kpi-value" style="color:#a78bfa">${baseJoules && chalJoules ? `${(baseJoules / chalJoules).toFixed(2)}x` : "—"} Energy Ratio</div>
      <div class="kpi-sub">${chalJoules ? `${chalJoules.toFixed(1)} kJ` : "—"} (${chalShort}) vs ${baseJoules ? `${baseJoules.toFixed(1)} kJ` : "—"} (${baseShort}) · Idle: ${chalIdle}W vs ${baseIdle}W</div>
    </div>
    ${ocQuickCard}
  `;

  const jumpEl = document.getElementById("jumpToOcCard");
  if (jumpEl) {
    jumpEl.addEventListener("click", () => {
      applyViewModeDefaults(pcSouped ? "pc_souped_vs_stock" : "pc_oc_delta");
      renderAllCharts();
    });
  }
}

function renderControlsAndCards() {
  renderModeTabsAndOcBanner();

  const machines = activeMachines();
  const realCount = allProfiles().length;
  document.getElementById("syncBanner").classList.toggle("hidden", realCount >= 2);

  if (machines.length >= 2) {
    const hasBase = machines.some((m) => profileKey(m) === state.baselineId);
    if (!hasBase) {
      const pc = machines.find((m) => !profileKey(m).includes("m4"));
      state.baselineId = profileKey(pc || machines[0]);
    }
    const hasChal = machines.some(
      (m) => profileKey(m) === state.challengerId && profileKey(m) !== state.baselineId
    );
    if (!hasChal) {
      const m4 = machines.find((m) => profileKey(m).includes("m4") && profileKey(m) !== state.baselineId);
      const other = m4 || machines.find((m) => profileKey(m) !== state.baselineId);
      state.challengerId = other ? profileKey(other) : null;
    }
  }

  const baseSel = document.getElementById("baselineSelect");
  baseSel.innerHTML = machines
    .map((m) => {
      const k = profileKey(m);
      return `<option value="${k}" ${k === state.baselineId ? "selected" : ""}>${shortLabel(m)}</option>`;
    })
    .join("");

  const chalSel = document.getElementById("challengerSelect");
  const chalWrap = document.getElementById("challengerWrap");
  chalWrap.classList.toggle("hidden", machines.length < 2);
  chalSel.innerHTML = machines
    .filter((m) => profileKey(m) !== state.baselineId)
    .map((m) => {
      const k = profileKey(m);
      return `<option value="${k}" ${k === state.challengerId ? "selected" : ""}>${shortLabel(m)}</option>`;
    })
    .join("");

  const cardsEl = document.getElementById("rigCards");
  if (machines.length === 0) {
    cardsEl.innerHTML = `<div class="card">No benchmark runs recorded yet. Run <code>uv run bench.py run</code> to populate data.</div>`;
    return;
  }

  cardsEl.innerHTML = machines
    .map((m, idx) => {
      const pid = profileKey(m);
      const s = m.sysinfo || {};
      const color = profileColor(m, idx);
      const avgWatts = getDisplayWatts(m);
      const priceUsd = getPriceUsd(m);
      const ww = s.workload_wall_watts;
      const wwText = ww
        ? `CPU ${Math.round(getCategoryWatts(m, "CPU"))}W · GPU ${Math.round(getCategoryWatts(m, "GPU Math"))}W · I/O ${Math.round(getCategoryWatts(m, "SSD and I/O Compute"))}W · Idle ${s.idle_wall_watts ?? "—"}W`
        : `${avgWatts}W`;

      const th = m.latest_thermals?.overall || {};
      const thParts = [];
      if (th.cpu_temp_c) thParts.push(`CPU max ${th.cpu_temp_c.max}&deg;C (avg ${th.cpu_temp_c.avg}&deg;C)`);
      else if (gpuTel?.post_run_cpu_tdie_c) thParts.push(`CPU ${gpuTel.post_run_cpu_tdie_c}&deg;C`);
      if (th.gpu_temp_c) thParts.push(`GPU max ${th.gpu_temp_c.max}&deg;C`);
      else if (gpuTel?.post_run_temp_c) thParts.push(`GPU ${gpuTel.post_run_temp_c}&deg;C`);
      if (th.ssd_temp_c) thParts.push(`SSD max ${th.ssd_temp_c.max}&deg;C`);
      else if (gpuTel?.post_run_ssd_temp_c) thParts.push(`SSD ${gpuTel.post_run_ssd_temp_c}&deg;C`);
      const thermalLine = thParts.length
        ? `<div><span>Thermals</span>${thParts.join(" · ")}${m.latest_thermals?.thermal_throttle_detected ? " · Throttled" : ""}</div>`
        : "";

      const ramSpeedNote = s.ram_speed_mt_s ? ` @ ${s.ram_speed_mt_s} MT/s` : "";
      const gbUrl = m.merged_suites?.cpu?.geekbench6?.result_url;
      const gbHtml = gbUrl
        ? `<a href="${gbUrl}" target="_blank" rel="noopener" style="color:${color};text-decoration:none;font-weight:600">Open Geekbench 6 Report &nearr;</a>`
        : "—";

      const peak1tMhz = m.merged_suites?.cpu?.sevenzip?.single_thread?.peak_1t_mhz;
      const gpuTel = s.gpu_power_telemetry_nvidia_smi;
      let tuningLine = "";
      if (gpuTel) {
        const pl = gpuTel.active_power_limit_watts || gpuTel.default_tdp_limit_watts || 125;
        const boost = gpuTel.boost_clock_mhz || gpuTel.max_boost_clock_mhz || "—";
        const mem = gpuTel.mem_clock_mhz || "—";
        tuningLine = `<div><span>GPU / CPU Clocks</span>${pl}W PL · ${boost} MHz Core · ${mem} MHz Mem${peak1tMhz ? ` · ${peak1tMhz} MHz 1T` : ""}</div>`;
      } else if (peak1tMhz) {
        tuningLine = `<div><span>Measured Peak Clock</span>${peak1tMhz} MHz (7-Zip 1T) · Fanless UMA</div>`;
      }

      let tagClass = "rig-tag";
      let tagLabel = "Measured Full Suite";
      if (m.tuning_state === "souped_up" || pid.includes("souped")) {
        tagClass = "rig-tag souped";
        tagLabel = "Souped Up (CPU OC + DDR4-3600 + Cooling)";
      } else if (m.tuning_state === "overclocked" || pid.endsWith("-oc")) {
        tagClass = "rig-tag oc";
        tagLabel = "GPU OC + -50mV CPU";
      } else if (pid.endsWith("-stock")) {
        tagClass = "rig-tag stock-pc";
        tagLabel = "Stock Baseline (Before)";
      }

      return `
      <article class="rig-card" style="--rig-color:${color}">
        <div class="rig-card-header">
          <h3>${s.display_name || pid}</h3>
          <span class="${tagClass}">${tagLabel}</span>
        </div>
        <div class="rig-specs">
          <div><span>CPU</span>${s.cpu_model || "—"} ${s.cpu_topology_note ? `(${s.cpu_topology_note})` : `(${s.cpu_physical_cores}C/${s.cpu_logical_cores}T)`}</div>
          <div><span>GPU</span>${s.gpu_model || "—"}</div>
          <div><span>Memory</span>${s.ram_total_gb || "—"} GiB${ramSpeedNote} (${s.gpu_memory_type || "—"})</div>
          <div><span>Target SSD</span>${s.ssd_model || "—"} (${s.ssd_free_gb_at_start ?? "—"} GiB free)</div>
          ${tuningLine}
          ${thermalLine}
          <div><span>Geekbench 6.4</span>${gbHtml}</div>
          <div><span>Power Envelope</span>${wwText}</div>
          <div>
            <span>Avg Load Power · Price</span>
            <input type="number" class="watts-input" data-mid="${pid}" value="${avgWatts}" min="5" max="1000" title="Average load wall watts" /> W ·
            <input type="number" class="watts-input price-input" data-mid="${pid}" value="${priceUsd}" min="50" max="10000" step="25" title="Acquisition price USD" /> USD
          </div>
        </div>
      </article>`;
    })
    .join("");

  cardsEl.querySelectorAll(".watts-input:not(.price-input)").forEach((inp) => {
    inp.addEventListener("change", (ev) => {
      const mid = ev.target.getAttribute("data-mid");
      const machine = machines.find((x) => profileKey(x) === mid);
      const v = parseFloat(ev.target.value);
      if (v > 0 && machine) {
        state.customWattsScale[mid] = v / getBaseAvgWatts(machine);
        renderAllCharts();
      }
    });
  });

  cardsEl.querySelectorAll(".price-input").forEach((inp) => {
    inp.addEventListener("change", (ev) => {
      const mid = ev.target.getAttribute("data-mid");
      const v = parseFloat(ev.target.value);
      if (v > 0) {
        state.customPriceUsd[mid] = v;
        renderAllCharts();
      }
    });
  });
}

function renderButterflyChart() {
  const chart = getChart("butterflyChart");
  const machines = activeMachines();
  const catalog = state.manifest?.metric_catalog || [];
  const { base, challenger } = getComparisonPair(machines);

  if (!base || !challenger) {
    chart.clear();
    return;
  }

  const sameHw = isSameHardwareComparison(base, challenger);
  const baseName = shortLabel(base);
  const chalName = shortLabel(challenger);
  const chalColor = profileColor(challenger, 1);
  const baseColor = profileColor(base, 0);

  document.getElementById("butterflyTitle").textContent = sameHw
    ? `Overclock Uplift per Benchmark (${baseName} -> ${chalName})`
    : `Relative Speedup Butterfly (${baseName} vs. ${chalName})`;
  document.getElementById("butterflyHint").textContent = `Right (>1.0x): ${chalName} leads | Left (<1.0x): ${baseName} leads`;

  const rows = [];
  for (const meta of catalog) {
    if (meta.id === "blender_eevee_1440p_fps") continue;
    const bVal = effectiveValue(base, meta);
    const cVal = effectiveValue(challenger, meta);
    const ratio = speedupRatio(cVal, bVal, meta.higher_is_better);
    if (ratio != null) {
      rows.push({
        label: meta.label,
        ratio,
        log2: Math.log2(ratio),
        bVal,
        cVal,
        unit: effectiveUnit(meta),
      });
    }
  }
  rows.sort((a, b) => a.log2 - b.log2);

  chart.setOption(
    {
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        backgroundColor: "#121826",
        borderColor: "#253047",
        textStyle: { color: "#e6edf7", fontSize: 12 },
        formatter: (params) => {
          const idx = params[0].dataIndex;
          const r = rows[idx];
          const pctStr = formatPct(r.ratio);
          const winner =
            r.ratio >= 1
              ? `${chalName} by ${r.ratio.toFixed(2)}x (${pctStr})`
              : `${baseName} by ${(1 / r.ratio).toFixed(2)}x (${pctStr})`;
          return `<strong>${r.label}</strong><br/>
            ${baseName}: <b>${formatNum(r.bVal)} ${r.unit}</b><br/>
            ${chalName}: <b>${formatNum(r.cVal)} ${r.unit}</b><br/>
            Delta: <span style="color:${r.ratio >= 1 ? chalColor : baseColor}">${winner}</span>`;
        },
      },
      grid: { left: 210, right: 86, top: 16, bottom: 28 },
      xAxis: {
        type: "value",
        axisLabel: {
          color: "#8b9bb4",
          formatter: (val) => {
            const mult = Math.pow(2, val);
            return sameHw ? formatPct(mult) : `${mult.toFixed(val < 0 ? 2 : 1)}x`;
          },
        },
        splitLine: { lineStyle: { color: "#1e293b" } },
      },
      yAxis: {
        type: "category",
        data: rows.map((r) => r.label),
        axisLabel: { color: "#cbd5e1", fontSize: 11 },
        axisTick: { show: false },
      },
      series: [
        {
          type: "bar",
          data: rows.map((r) => ({
            value: Number(r.log2.toFixed(4)),
            itemStyle: {
              color: r.log2 >= 0 ? chalColor : baseColor,
              borderRadius: r.log2 >= 0 ? [0, 4, 4, 0] : [4, 0, 0, 4],
            },
          })),
          label: {
            show: true,
            position: "right",
            color: "#cbd5e1",
            fontSize: 10,
            formatter: (p) => {
              const r = rows[p.dataIndex];
              if (sameHw) {
                return `${formatPct(r.ratio)} (${r.ratio.toFixed(2)}x)`;
              }
              return r.ratio >= 1 ? `${r.ratio.toFixed(2)}x` : `${(1 / r.ratio).toFixed(2)}x base`;
            },
          },
        },
      ],
    },
    true
  );
}

function renderRadarChart() {
  const chart = getChart("radarChart");
  const machines = activeMachines();
  const catalog = Object.fromEntries((state.manifest?.metric_catalog || []).map((m) => [m.id, m]));

  const axes = [
    { name: "1T CPU IPC", candidates: ["sevenzip_1t_mips", "sha256_1t_mb_s"] },
    { name: "Multi-Core CPU", candidates: ["cpu_fp64_gemm_gflops", "sevenzip_mt_mips"] },
    { name: "GPU Path Trace RT", candidates: ["cycles_gpu_spm"] },
    { name: "GPU Dense Compute", candidates: ["gpu_fp32_gemm_tflops", "llama_7b_pp512"] },
    { name: "GPU Memory BW", candidates: ["gpu_mem_triad_gb_s", "llama_7b_tg128"] },
    { name: "NVMe + Out-of-Core", candidates: ["fio_seq_read_mb_s", "pipeline_total_sec"] },
  ];

  const scoresPerMachine = machines.map(() => []);
  axes.forEach((ax) => {
    let chosenMeta = null;
    for (const cid of ax.candidates) {
      if (catalog[cid] && machines.some((m) => effectiveValue(m, catalog[cid]) != null)) {
        chosenMeta = catalog[cid];
        break;
      }
    }
    if (!chosenMeta) {
      machines.forEach((_, mi) => scoresPerMachine[mi].push(0));
      return;
    }
    const vals = machines.map((m) => effectiveValue(m, chosenMeta));
    const valid = vals.filter((v) => v != null && v > 0);
    const best = valid.length
      ? chosenMeta.higher_is_better
        ? Math.max(...valid)
        : Math.min(...valid)
      : 1;

    vals.forEach((v, mi) => {
      if (v == null || v <= 0) {
        scoresPerMachine[mi].push(0);
      } else {
        const pct = chosenMeta.higher_is_better ? (v / best) * 100 : (best / v) * 100;
        scoresPerMachine[mi].push(Number(Math.min(100, pct).toFixed(1)));
      }
    });
  });

  chart.setOption(
    {
      tooltip: { backgroundColor: "#121826", borderColor: "#253047", textStyle: { color: "#e6edf7" } },
      legend: {
        bottom: 0,
        textStyle: { color: "#cbd5e1", fontSize: 11 },
        data: machines.map((m) => shortLabel(m)),
      },
      radar: {
        radius: "62%",
        center: ["50%", "45%"],
        indicator: axes.map((a) => ({ name: a.name, max: 100 })),
        axisName: { color: "#cbd5e1", fontSize: 11 },
        splitArea: { areaStyle: { color: ["rgba(30,41,59,0.2)", "rgba(15,23,42,0.4)"] } },
        splitLine: { lineStyle: { color: "#253047" } },
        axisLine: { lineStyle: { color: "#253047" } },
      },
      series: [
        {
          type: "radar",
          data: machines.map((m, idx) => {
            const c = profileColor(m, idx);
            return {
              name: shortLabel(m),
              value: scoresPerMachine[idx],
              lineStyle: { width: 2.2, color: c },
              itemStyle: { color: c },
              areaStyle: { opacity: machines.length > 2 ? 0.12 : 0.18 },
            };
          }),
        },
      ],
    },
    true
  );
}

function renderDomainChart(domId, categoryName, customMetricIds = null) {
  const chart = getChart(domId);
  const machines = activeMachines();
  let catalog = state.manifest?.metric_catalog || [];
  if (customMetricIds) {
    catalog = customMetricIds.map((id) => catalog.find((m) => m.id === id)).filter(Boolean);
  } else {
    catalog = catalog.filter((m) => m.category === categoryName);
  }

  const activeMetrics = catalog.filter((meta) => machines.some((m) => effectiveValue(m, meta) != null));
  if (!activeMetrics.length) {
    chart.clear();
    return;
  }

  const barMax = machines.length >= 3 ? 11 : 17;
  const labelFont = machines.length >= 3 ? 9.5 : 10.5;

  const series = machines.map((m, mIdx) => {
    const color = profileColor(m, mIdx);
    const data = activeMetrics.map((meta) => {
      const val = effectiveValue(m, meta);
      if (val == null || val <= 0) return { value: 0, raw: null, unit: effectiveUnit(meta) };
      const allVals = machines.map((om) => effectiveValue(om, meta)).filter((x) => x != null && x > 0);
      const leader = meta.higher_is_better ? Math.max(...allVals) : Math.min(...allVals);
      const pct = meta.higher_is_better ? (val / leader) * 100 : (leader / val) * 100;
      return {
        value: Number(pct.toFixed(1)),
        raw: val,
        unit: effectiveUnit(meta),
      };
    });

    return {
      name: shortLabel(m),
      type: "bar",
      barMaxWidth: barMax,
      barGap: "18%",
      itemStyle: {
        color,
        borderRadius: [0, 4, 4, 0],
      },
      label: {
        show: true,
        position: "right",
        color: "#e2e8f0",
        fontSize: labelFont,
        formatter: (p) => (p.data.raw != null ? `${formatNum(p.data.raw)} ${p.data.unit}` : ""),
      },
      data,
    };
  });

  chart.setOption(
    {
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        backgroundColor: "#121826",
        borderColor: "#253047",
        textStyle: { color: "#e6edf7", fontSize: 12 },
        formatter: (params) => {
          const title = params[0]?.axisValueLabel || "";
          const lines = params.map(
            (p) =>
              `${p.marker} ${p.seriesName}: <b>${
                p.data.raw != null ? `${formatNum(p.data.raw)} ${p.data.unit}` : "—"
              }</b> (${p.value}%)`
          );
          return `<strong>${title}</strong><br/>` + lines.join("<br/>");
        },
      },
      legend: { top: 0, textStyle: { color: "#cbd5e1", fontSize: 11 } },
      grid: { left: 195, right: 138, top: 32, bottom: 18 },
      xAxis: {
        type: "value",
        max: 108,
        axisLabel: { color: "#8b9bb4", formatter: "{value}%" },
        splitLine: { lineStyle: { color: "#1e293b" } },
      },
      yAxis: {
        type: "category",
        inverse: true,
        data: activeMetrics.map((m) => m.label),
        axisLabel: { color: "#cbd5e1", fontSize: 11 },
        axisTick: { show: false },
      },
      series,
    },
    true
  );
}

function renderTable() {
  const machines = activeMachines();
  const catalog = state.manifest?.metric_catalog || [];
  const { base, challenger } = getComparisonPair(machines);
  const sameHw = isSameHardwareComparison(base, challenger);

  const headRow = document.getElementById("tableHeadRow");
  let deltaHeaders = "";
  if (challenger && base) {
    if (sameHw) {
      deltaHeaders = `<th class="num">OC Uplift (${shortLabel(challenger)} vs Base)</th><th class="num">Perf/Watt Delta</th>`;
    } else {
      deltaHeaders = `<th class="num">${shortLabel(challenger)} vs ${shortLabel(base)}</th><th class="num">Perf/Watt Winner</th><th class="num">Perf/USD Winner</th>`;
    }
  }

  headRow.innerHTML =
    `<th>Category</th><th>Benchmark Metric</th><th>Active Unit</th>` +
    machines.map((m) => `<th class="num">${shortLabel(m)}</th>`).join("") +
    deltaHeaders;

  const tbody = document.getElementById("tableBody");
  const rowsHtml = [];

  for (const meta of catalog) {
    if (meta.id === "blender_eevee_1440p_fps") continue;
    const vals = machines.map((m) => effectiveValue(m, meta));
    if (vals.every((v) => v == null)) continue;

    const unit = effectiveUnit(meta);
    let extraCells = "";
    if (challenger && base) {
      const activeRatio = speedupRatio(
        effectiveValue(challenger, meta),
        effectiveValue(base, meta),
        meta.higher_is_better
      );
      const savedLens = state.lens;
      state.lens = "ppw";
      const ppwRatio = speedupRatio(
        effectiveValue(challenger, meta),
        effectiveValue(base, meta),
        meta.higher_is_better
      );
      state.lens = "ppd";
      const ppdRatio = speedupRatio(
        effectiveValue(challenger, meta),
        effectiveValue(base, meta),
        meta.higher_is_better
      );
      state.lens = savedLens;

      if (sameHw) {
        const fmtPctCell = (r) => {
          if (r == null) return `<td class="num">—</td>`;
          const pct = (r - 1.0) * 100;
          const cls = pct > 0.4 ? "delta-pos" : pct < -0.4 ? "delta-neg" : "delta-neutral";
          return `<td class="num ${cls}">${formatPct(r)} (${r.toFixed(2)}x)</td>`;
        };
        extraCells = fmtPctCell(activeRatio) + fmtPctCell(ppwRatio);
      } else {
        const fmtBadge = (r) => {
          if (r == null) return `<td class="num">—</td>`;
          const winName = r >= 1 ? shortLabel(challenger) : shortLabel(base);
          const mult = r >= 1 ? r : 1 / r;
          const cls = r >= 1 ? "delta-pos" : "delta-neg";
          return `<td class="num ${cls}">${winName} ${mult.toFixed(2)}x</td>`;
        };
        extraCells = fmtBadge(activeRatio) + fmtBadge(ppwRatio) + fmtBadge(ppdRatio);
      }
    }

    rowsHtml.push(`
      <tr>
        <td><span class="cat-chip">${meta.category}</span></td>
        <td><strong>${meta.label}</strong></td>
        <td>${unit} (${meta.higher_is_better ? "Higher=Better" : "Lower=Better"})</td>
        ${vals.map((v) => `<td class="num">${v != null ? `${formatNum(v)} ${unit}` : "—"}</td>`).join("")}
        ${extraCells}
      </tr>
    `);
  }

  tbody.innerHTML = rowsHtml.join("");
  const gen = state.manifest?.generated_at_utc;
  document.getElementById("manifestTimestamp").textContent = gen ? `Manifest updated: ${gen}` : "";
}

function renderAllCharts() {
  renderControlsAndCards();
  renderKpiStrip();
  renderButterflyChart();
  renderRadarChart();
  renderDomainChart("cpuChart", "CPU");
  renderDomainChart("gpuRasterChart", "GPU Graphics", [
    "cycles_gpu_spm",
    "gpu_fp32_gemm_tflops",
    "gpu_fft2d_gflops",
  ]);
  renderDomainChart("gpuMathChart", "GPU Math", [
    "gpu_mem_triad_gb_s",
    "llama_7b_pp512",
    "llama_7b_tg128",
    "gpu_fp16_gemm_tflops",
  ]);
  renderDomainChart("storageChart", "SSD and I/O Compute");
  renderTable();
}

async function loadManifest() {
  try {
    const res = await fetch(`./data/manifest.json?t=${Date.now()}`);
    state.manifest = await res.json();
    const all = allProfiles();
    const pcSouped = findProfileByKind(all, "pc_souped");
    const pcOc = findProfileByKind(all, "pc_oc");
    const topTuned = pcSouped || pcOc;
    if (!state.initialModeChosen || (topTuned && state.viewMode === "all" && !state.challengerId)) {
      applyViewModeDefaults("all");
      state.initialModeChosen = true;
    } else if (topTuned) {
      const m4 = findProfileByKind(all, "m4");
      if (
        state.viewMode === "all" &&
        m4 &&
        (state.baselineId === "zen2-1660s-stock" || (pcSouped && state.baselineId === "zen2-1660s-oc"))
      ) {
        state.baselineId = profileKey(topTuned);
        state.challengerId = profileKey(m4);
      }
    }
    renderAllCharts();
  } catch (err) {
    console.error("Failed to load manifest.json:", err);
  }
}

document.querySelectorAll("#viewModeTabs .mode-tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (btn.disabled) return;
    const mode = btn.getAttribute("data-mode");
    applyViewModeDefaults(mode);
    renderAllCharts();
  });
});

document.getElementById("lensSelect").addEventListener("change", (e) => {
  state.lens = e.target.value;
  renderAllCharts();
});

document.getElementById("baselineSelect").addEventListener("change", (e) => {
  state.baselineId = e.target.value;
  if (state.challengerId === state.baselineId) {
    const others = activeMachines().filter((m) => profileKey(m) !== state.baselineId);
    state.challengerId = others[0] ? profileKey(others[0]) : null;
  }
  renderAllCharts();
});

document.getElementById("challengerSelect").addEventListener("change", (e) => {
  state.challengerId = e.target.value;
  renderAllCharts();
});

document.getElementById("refreshBtn").addEventListener("click", () => loadManifest());

loadManifest();
