#Requires -Version 7.0
<#
.SYNOPSIS
    Dynamic Hardware Temperature & Fan Monitoring Daemon + Web UI Server.
    Reads sensor definitions from config/sensors.json and logs to data/temperatures_YYYY-MM-DD.csv.
#>
[CmdletBinding()]
param(
    [int]$Port = 0,
    [int]$IntervalMs = 0,
    [switch]$NoElevate
)

$ErrorActionPreference = 'Stop'
[System.Globalization.CultureInfo]::CurrentCulture = [System.Globalization.CultureInfo]::InvariantCulture

$RepoRoot       = Split-Path -Parent $PSScriptRoot
$DataDir        = Join-Path $RepoRoot 'data'
$WebDir         = Join-Path $RepoRoot 'web'
$ConfigDir      = Join-Path $RepoRoot 'config'
$LibDir         = Join-Path $RepoRoot 'lib\LHM'
$ConfigPath     = Join-Path $ConfigDir 'sensors.json'
$StatusJsonPath = Join-Path $DataDir 'service_status.json'
$DaemonLogPath  = Join-Path $DataDir 'daemon.log'

New-Item -ItemType Directory -Force -Path $DataDir, $WebDir, $ConfigDir | Out-Null

function Write-DaemonLog([string]$msg) {
    try {
        $ts = [DateTimeOffset]::Now.ToString('yyyy-MM-dd HH:mm:ss.fff zzz')
        Add-Content -Path $DaemonLogPath -Value "[$ts] $msg" -Encoding UTF8
    } catch { }
}

# Self-elevate so LibreHardwareMonitorLib + PawnIO can read CPU SMU, Super I/O, and NVMe SMART
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin -and -not $NoElevate) {
    $pwshExe = (Get-Process -Id $PID).Path
    if (-not $pwshExe) { $pwshExe = 'C:\Program Files\PowerShell\7\pwsh.exe' }
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', "`"$PSCommandPath`"")
    if ($Port -gt 0) { $argList += @('-Port', $Port) }
    if ($IntervalMs -gt 0) { $argList += @('-IntervalMs', $IntervalMs) }
    $argList += '-NoElevate'
    Start-Process -FilePath $pwshExe -Verb RunAs -ArgumentList $argList -WindowStyle Hidden
    exit 0
}

# Stop any prior instance of thermal_service.ps1 cleanly before binding port
try {
    Get-CimInstance Win32_Process -Filter "Name = 'pwsh.exe' OR Name = 'powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match '-File\s+"?[^"]*thermal_service\.ps1' } |
        ForEach-Object {
            Write-DaemonLog "Stopping prior instance PID=$($_.ProcessId)"
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
    Start-Sleep -Milliseconds 250
} catch { }

# Auto-run probe if config/sensors.json doesn't exist yet
if (-not (Test-Path $ConfigPath)) {
    Write-DaemonLog "config/sensors.json missing; running probe.ps1..."
    & (Join-Path $PSScriptRoot 'probe.ps1')
}

$cfgRaw = Get-Content -Path $ConfigPath -Raw -Encoding UTF8
$cfg    = $cfgRaw | ConvertFrom-Json

if ($Port -le 0)       { $Port       = if ($cfg.host.port) { [int]$cfg.host.port } else { 8095 } }
if ($IntervalMs -le 0) { $IntervalMs = if ($cfg.host.intervalMs) { [int]$cfg.host.intervalMs } else { 2000 } }

Write-DaemonLog "Starting thermal_service.ps1 (PID=$PID, IsAdmin=$isAdmin, Port=$Port, IntervalMs=$IntervalMs)"

# Ensure Windows Firewall inbound rule for LAN & Tailscale access
if ($isAdmin) {
    try {
        $fwName = "RigThermalMonitor-TCP-$Port"
        if (-not (Get-NetFirewallRule -Name $fwName -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -Name $fwName -DisplayName "Rig Thermal Monitor (TCP $Port)" `
                -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Any | Out-Null
        }
    } catch { }
}

# Build dynamic channel table from config/sensors.json
$channelKeys     = [System.Collections.Generic.List[string]]::new()
$channelDecimals = [System.Collections.Generic.List[int]]::new()
$channelIsTemp   = [System.Collections.Generic.List[bool]]::new()
$idToChannels    = @{}

function Add-ChannelMapping([string]$key, [string]$sensorId, [int]$dec, [bool]$isTemp) {
    $idx = $channelKeys.Count
    $channelKeys.Add($key)
    $channelDecimals.Add($dec)
    $channelIsTemp.Add($isTemp)
    if ($sensorId) {
        if (-not $idToChannels.ContainsKey($sensorId)) {
            $idToChannels[$sensorId] = [System.Collections.Generic.List[int]]::new()
        }
        $idToChannels[$sensorId].Add($idx)
    }
}

foreach ($t in $cfg.temps) { Add-ChannelMapping $t.key $t.id 1 $true }
foreach ($f in $cfg.fans)  { Add-ChannelMapping $f.key $f.id 0 $false }
if ($cfg.metrics) {
    foreach ($prop in $cfg.metrics.PSObject.Properties) {
        if ($prop.Value) {
            Add-ChannelMapping $prop.Name ([string]$prop.Value) 1 $false
        }
    }
}

# 1. Compile pure-BCL C# Dynamic Time-Series Engine & Multi-threaded HTTP Server
if (-not ('ThermalMonitor.Daemon' -as [type])) {
    $csCode = @'
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;

namespace ThermalMonitor {
    public struct Sample {
        public long TsMs;
        public bool IsCached;
        public float[] Values;
    }

    public sealed class Daemon : IDisposable {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

        private readonly string _dataDir;
        private readonly string _webDir;
        private readonly string _configJson;
        private readonly int _port;
        private readonly int _intervalMs;
        private readonly bool _isAdmin;
        private readonly long _bootTimeMs;
        private readonly string _hostName;

        private readonly string[] _keys;
        private readonly int[] _decimals;
        private readonly bool[] _isTemp;
        private readonly int _numChannels;
        private readonly string _csvHeader;

        private readonly object _lock = new object();
        private readonly List<Sample> _samples = new List<Sample>(65536);
        private long _totalDiskSamples = 0;

        private TcpListener _listener;
        private Thread _acceptThread;
        private volatile bool _running;

        private string _activeCsvDate = "";
        private StreamWriter _csvWriter;
        private long _recordCount = 0;

        public Daemon(string dataDir, string webDir, string configJson, int port, int intervalMs, bool isAdmin, long bootTimeMs,
                      string[] keys, int[] decimals, bool[] isTemp) {
            _dataDir = Path.GetFullPath(dataDir);
            _webDir = Path.GetFullPath(webDir);
            _configJson = configJson;
            _port = port;
            _intervalMs = intervalMs;
            _isAdmin = isAdmin;
            _bootTimeMs = bootTimeMs;
            _hostName = Environment.MachineName;

            _keys = keys;
            _decimals = decimals;
            _isTemp = isTemp;
            _numChannels = keys.Length;
            _csvHeader = "ts_ms,iso,source," + string.Join(",", keys);
        }

        public int LoadedCount {
            get { lock (_lock) { return _samples.Count; } }
        }

        public void InitHistory() {
            LoadExistingHistory();
        }

        public void StartHttp() {
            _running = true;
            _listener = new TcpListener(IPAddress.Any, _port);
            _listener.Start();
            _acceptThread = new Thread(AcceptLoop) { IsBackground = true, Name = "ThermalHttpAccept" };
            _acceptThread.Start();
        }

        public void RecordSample(float[] vals) {
            DateTimeOffset now = DateTimeOffset.Now;
            long tsMs = now.ToUnixTimeMilliseconds();
            float[] copy = new float[_numChannels];
            for (int i = 0; i < _numChannels; i++) {
                copy[i] = (vals != null && i < vals.Length) ? vals[i] : float.NaN;
            }
            var snap = new Sample { TsMs = tsMs, IsCached = false, Values = copy };

            AppendCsvRow(now, snap);

            lock (_lock) {
                _samples.Add(snap);
                _totalDiskSamples++;
                _recordCount++;
                if ((_recordCount & 1023) == 0) {
                    CompactMemoryIfNeeded();
                }
            }
        }

        private static float ParseOptFloat(string s) {
            if (string.IsNullOrWhiteSpace(s)) return float.NaN;
            float v;
            return float.TryParse(s.Trim(), NumberStyles.Float, Inv, out v) ? v : float.NaN;
        }

        private static string FmtOpt(float v, int dec) {
            if (float.IsNaN(v) || float.IsInfinity(v)) return "";
            return v.ToString(dec == 0 ? "F0" : "F1", Inv);
        }

        private static void AppendJsonFloat(StringBuilder sb, float v, int dec) {
            if (float.IsNaN(v) || float.IsInfinity(v)) {
                sb.Append("null");
            } else {
                sb.Append(v.ToString(dec == 0 ? "F0" : "F1", Inv));
            }
        }

        private static string EscapeJson(string s) {
            if (string.IsNullOrEmpty(s)) return "";
            return s.Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\r", "").Replace("\n", " ");
        }

        private void LoadExistingHistory() {
            var keyToIdx = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
            for (int i = 0; i < _numChannels; i++) keyToIdx[_keys[i]] = i;

            var files = Directory.GetFiles(_dataDir, "temperatures_*.csv");
            Array.Sort(files, StringComparer.OrdinalIgnoreCase);
            var temp = new List<Sample>(65536);

            foreach (string file in files) {
                try {
                    using (var fs = new FileStream(file, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
                    using (var sr = new StreamReader(fs, Encoding.UTF8)) {
                        string headerLine = sr.ReadLine();
                        if (string.IsNullOrWhiteSpace(headerLine)) continue;
                        string[] cols = headerLine.Split(',');
                        int[] colMap = new int[cols.Length];
                        for (int c = 0; c < cols.Length; c++) {
                            int idx;
                            colMap[c] = keyToIdx.TryGetValue(cols[c].Trim(), out idx) ? idx : -1;
                        }

                        string line;
                        while ((line = sr.ReadLine()) != null) {
                            if (string.IsNullOrWhiteSpace(line)) continue;
                            string[] p = line.Split(',');
                            if (p.Length < 4) continue;
                            long ts;
                            if (!long.TryParse(p[0].Trim(), NumberStyles.Integer, Inv, out ts)) continue;

                            float[] vals = new float[_numChannels];
                            for (int i = 0; i < _numChannels; i++) vals[i] = float.NaN;
                            for (int c = 3; c < p.Length && c < colMap.Length; c++) {
                                if (colMap[c] >= 0) vals[colMap[c]] = ParseOptFloat(p[c]);
                            }
                            bool isCached = p.Length > 2 && p[2].StartsWith("cached", StringComparison.OrdinalIgnoreCase);
                            temp.Add(new Sample { TsMs = ts, IsCached = isCached, Values = vals });
                        }
                    }
                } catch { }
            }

            temp.Sort((a, b) => a.TsMs.CompareTo(b.TsMs));
            lock (_lock) {
                _samples.Clear();
                long prevTs = -1;
                foreach (var item in temp) {
                    if (item.TsMs == prevTs) _samples[_samples.Count - 1] = item;
                    else { _samples.Add(item); prevTs = item.TsMs; }
                }
                _totalDiskSamples = _samples.Count;
                CompactMemoryIfNeeded();
            }
        }

        private void CompactMemoryIfNeeded() {
            if (_samples.Count < 250000) return;
            long cutoff72h = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() - (72L * 3600L * 1000L);
            var compacted = new List<Sample>(_samples.Count / 2);
            int i = 0;
            while (i < _samples.Count && _samples[i].TsMs < cutoff72h) {
                long bucket = _samples[i].TsMs / 60000L;
                Sample minS = _samples[i];
                Sample maxS = _samples[i];
                float minScore = PeakScore(minS, false);
                float maxScore = PeakScore(maxS, true);
                i++;
                while (i < _samples.Count && (_samples[i].TsMs / 60000L) == bucket && _samples[i].TsMs < cutoff72h) {
                    Sample cur = _samples[i];
                    float lo = PeakScore(cur, false);
                    float hi = PeakScore(cur, true);
                    if (lo < minScore) { minScore = lo; minS = cur; }
                    if (hi > maxScore) { maxScore = hi; maxS = cur; }
                    i++;
                }
                if (minS.TsMs <= maxS.TsMs) {
                    compacted.Add(minS);
                    if (maxS.TsMs != minS.TsMs) compacted.Add(maxS);
                } else {
                    compacted.Add(maxS);
                    compacted.Add(minS);
                }
            }
            while (i < _samples.Count) compacted.Add(_samples[i++]);
            _samples.Clear();
            _samples.AddRange(compacted);
        }

        private string ResolveCsvPathForDate(string dateKey) {
            string basePath = Path.Combine(_dataDir, "temperatures_" + dateKey + ".csv");
            if (!File.Exists(basePath) || new FileInfo(basePath).Length == 0) return basePath;
            try {
                using (var sr = new StreamReader(basePath, Encoding.UTF8)) {
                    string first = sr.ReadLine();
                    if (string.Equals(first, _csvHeader, StringComparison.Ordinal)) return basePath;
                }
            } catch { }
            int ver = 2;
            while (true) {
                string alt = Path.Combine(_dataDir, "temperatures_" + dateKey + "_v" + ver + ".csv");
                if (!File.Exists(alt) || new FileInfo(alt).Length == 0) return alt;
                try {
                    using (var sr = new StreamReader(alt, Encoding.UTF8)) {
                        string first = sr.ReadLine();
                        if (string.Equals(first, _csvHeader, StringComparison.Ordinal)) return alt;
                    }
                } catch { }
                ver++;
            }
        }

        private void AppendCsvRow(DateTimeOffset now, Sample s) {
            try {
                string dateKey = now.ToString("yyyy-MM-dd", Inv);
                if (_csvWriter == null || _activeCsvDate != dateKey) {
                    if (_csvWriter != null) {
                        try { _csvWriter.Flush(); _csvWriter.Dispose(); } catch { }
                        _csvWriter = null;
                    }
                    string csvPath = ResolveCsvPathForDate(dateKey);
                    bool needHeader = !File.Exists(csvPath) || (new FileInfo(csvPath).Length == 0);
                    var fs = new FileStream(csvPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite);
                    _csvWriter = new StreamWriter(fs, new UTF8Encoding(false)) { AutoFlush = true };
                    if (needHeader) _csvWriter.WriteLine(_csvHeader);
                    _activeCsvDate = dateKey;
                }

                var sb = new StringBuilder(192);
                sb.Append(s.TsMs.ToString(Inv)).Append(',');
                sb.Append(now.ToString("yyyy-MM-ddTHH:mm:ss.fffzzz", Inv)).Append(",live");
                for (int i = 0; i < _numChannels; i++) {
                    sb.Append(',').Append(FmtOpt(s.Values[i], _decimals[i]));
                }
                _csvWriter.WriteLine(sb.ToString());
            } catch { }
        }

        private void AcceptLoop() {
            while (_running) {
                try {
                    TcpClient client = _listener.AcceptTcpClient();
                    ThreadPool.QueueUserWorkItem(HandleClient, client);
                } catch {
                    if (!_running) break;
                }
            }
        }

        private static Dictionary<string, string> ParseQuery(string qs) {
            var d = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            if (string.IsNullOrEmpty(qs)) return d;
            foreach (string part in qs.Split('&')) {
                if (string.IsNullOrEmpty(part)) continue;
                int eq = part.IndexOf('=');
                if (eq > 0) d[Uri.UnescapeDataString(part.Substring(0, eq))] = Uri.UnescapeDataString(part.Substring(eq + 1));
                else d[Uri.UnescapeDataString(part)] = "1";
            }
            return d;
        }

        private static void SendHttp(NetworkStream ns, int code, string status, string contentType, byte[] body, string extraHeaders) {
            var sb = new StringBuilder(256);
            sb.Append("HTTP/1.1 ").Append(code).Append(' ').Append(status).Append("\r\n");
            sb.Append("Content-Type: ").Append(contentType).Append("\r\n");
            sb.Append("Content-Length: ").Append(body != null ? body.Length : 0).Append("\r\n");
            sb.Append("Access-Control-Allow-Origin: *\r\n");
            sb.Append("Connection: close\r\n");
            if (!string.IsNullOrEmpty(extraHeaders)) sb.Append(extraHeaders);
            sb.Append("\r\n");
            byte[] hdr = Encoding.ASCII.GetBytes(sb.ToString());
            ns.Write(hdr, 0, hdr.Length);
            if (body != null && body.Length > 0) ns.Write(body, 0, body.Length);
            ns.Flush();
        }

        private void HandleClient(object state) {
            using (var client = (TcpClient)state) {
                try {
                    client.ReceiveTimeout = 5000;
                    client.SendTimeout = 15000;
                    using (NetworkStream ns = client.GetStream()) {
                        byte[] buf = new byte[4096];
                        int read = ns.Read(buf, 0, buf.Length);
                        if (read <= 0) return;
                        string reqText = Encoding.ASCII.GetString(buf, 0, read);
                        int lineEnd = reqText.IndexOf("\r\n", StringComparison.Ordinal);
                        if (lineEnd < 0) return;
                        string[] parts = reqText.Substring(0, lineEnd).Split(' ');
                        if (parts.Length < 2) return;

                        string rawUrl = parts[1];
                        int qIdx = rawUrl.IndexOf('?');
                        string path = Uri.UnescapeDataString(qIdx >= 0 ? rawUrl.Substring(0, qIdx) : rawUrl);
                        var query = ParseQuery(qIdx >= 0 ? rawUrl.Substring(qIdx + 1) : "");

                        if (path == "/api/config") {
                            SendHttp(ns, 200, "OK", "application/json; charset=utf-8", Encoding.UTF8.GetBytes(_configJson), "Cache-Control: no-store\r\n");
                            return;
                        }

                        if (path == "/api/history") {
                            double hours = 24.0;
                            if (query.ContainsKey("hours")) double.TryParse(query["hours"], NumberStyles.Float, Inv, out hours);
                            int maxPts = 1600;
                            if (query.ContainsKey("maxPoints")) int.TryParse(query["maxPoints"], NumberStyles.Integer, Inv, out maxPts);
                            if (maxPts < 100) maxPts = 100;
                            if (maxPts > 10000) maxPts = 10000;
                            SendHttp(ns, 200, "OK", "application/json; charset=utf-8", BuildHistoryJson(hours, maxPts), "Cache-Control: no-store\r\n");
                            return;
                        }

                        if (path == "/api/live") {
                            long sinceMs = 0;
                            if (query.ContainsKey("since")) long.TryParse(query["since"], NumberStyles.Integer, Inv, out sinceMs);
                            SendHttp(ns, 200, "OK", "application/json; charset=utf-8", BuildLiveJson(sinceMs), "Cache-Control: no-store\r\n");
                            return;
                        }

                        if (path == "/api/export.csv") {
                            double hours = 24.0;
                            if (query.ContainsKey("hours")) double.TryParse(query["hours"], NumberStyles.Float, Inv, out hours);
                            byte[] csvBytes = BuildExportCsv(hours);
                            string fn = hours <= 0 ? "temperatures_all.csv" : string.Format(Inv, "temperatures_{0:F0}h.csv", hours);
                            SendHttp(ns, 200, "OK", "text/csv; charset=utf-8", csvBytes,
                                "Cache-Control: no-store\r\nContent-Disposition: attachment; filename=\"" + fn + "\"\r\n");
                            return;
                        }

                        if (path == "/" || path == "/index.html") {
                            string indexFile = Path.Combine(_webDir, "index.html");
                            if (!File.Exists(indexFile)) {
                                SendHttp(ns, 404, "Not Found", "text/plain; charset=utf-8", Encoding.UTF8.GetBytes("UI not found"), "");
                                return;
                            }
                            SendHttp(ns, 200, "OK", "text/html; charset=utf-8", File.ReadAllBytes(indexFile), "Cache-Control: no-cache\r\n");
                            return;
                        }

                        SendHttp(ns, 404, "Not Found", "application/json; charset=utf-8", Encoding.UTF8.GetBytes("{\"error\":\"not_found\"}"), "");
                    }
                } catch { }
            }
        }

        private float PeakScore(Sample s, bool high) {
            float best = high ? -999f : 999f;
            bool any = false;
            for (int c = 0; c < _numChannels; c++) {
                if (!_isTemp[c]) continue;
                float v = s.Values[c];
                if (!float.IsNaN(v)) {
                    any = true;
                    if (high && v > best) best = v;
                    if (!high && v < best) best = v;
                }
            }
            return any ? best : 0f;
        }

        private List<Sample> DownsamplePeakPreserving(List<Sample> src, int maxPts) {
            if (src.Count <= maxPts) return src;
            var result = new List<Sample>(maxPts + 32);
            int idx = 0;
            while (idx < src.Count && src[idx].IsCached) { result.Add(src[idx]); idx++; }

            int remaining = src.Count - idx;
            int budget = Math.Max(60, maxPts - result.Count);
            if (remaining <= budget) {
                for (int k = idx; k < src.Count; k++) result.Add(src[k]);
                return result;
            }

            int buckets = Math.Max(1, budget / 2);
            double step = (double)remaining / buckets;

            for (int b = 0; b < buckets; b++) {
                int sIdx = idx + (int)Math.Floor(b * step);
                int eIdx = idx + (int)Math.Floor((b + 1) * step);
                if (eIdx <= sIdx) eIdx = sIdx + 1;
                if (eIdx > src.Count) eIdx = src.Count;

                Sample minS = src[sIdx], maxS = src[sIdx];
                float minM = PeakScore(minS, false), maxM = PeakScore(maxS, true);
                for (int j = sIdx + 1; j < eIdx; j++) {
                    Sample c = src[j];
                    float lo = PeakScore(c, false), hi = PeakScore(c, true);
                    if (lo < minM) { minM = lo; minS = c; }
                    if (hi > maxM) { maxM = hi; maxS = c; }
                }
                if (minS.TsMs <= maxS.TsMs) {
                    result.Add(minS);
                    if (maxS.TsMs != minS.TsMs) result.Add(maxS);
                } else {
                    result.Add(maxS);
                    result.Add(minS);
                }
            }
            Sample last = src[src.Count - 1];
            if (result.Count == 0 || result[result.Count - 1].TsMs != last.TsMs) result.Add(last);
            return result;
        }

        private void AppendSampleArrays(StringBuilder sb, List<Sample> list) {
            sb.Append("\"ts\":[");
            for (int i = 0; i < list.Count; i++) {
                if (i > 0) sb.Append(',');
                sb.Append(list[i].TsMs.ToString(Inv));
            }
            sb.Append("],\"cached\":[");
            for (int i = 0; i < list.Count; i++) {
                if (i > 0) sb.Append(',');
                sb.Append(list[i].IsCached ? '1' : '0');
            }
            sb.Append(']');

            for (int c = 0; c < _numChannels; c++) {
                int dec = _decimals[c];
                sb.Append(",\"").Append(_keys[c]).Append("\":[");
                for (int i = 0; i < list.Count; i++) {
                    if (i > 0) sb.Append(',');
                    AppendJsonFloat(sb, list[i].Values[c], dec);
                }
                sb.Append(']');
            }
        }

        private void AppendStatsObject(StringBuilder sb, List<Sample> window) {
            sb.Append('{');
            for (int c = 0; c < _numChannels; c++) {
                if (c > 0) sb.Append(',');
                int dec = _decimals[c];
                float min = float.MaxValue, max = float.MinValue, last = float.NaN;
                double sum = 0;
                int cnt = 0;
                for (int i = 0; i < window.Count; i++) {
                    float v = window[i].Values[c];
                    if (!float.IsNaN(v)) {
                        if (v < min) min = v;
                        if (v > max) max = v;
                        sum += v;
                        cnt++;
                        last = v;
                    }
                }
                sb.Append('"').Append(_keys[c]).Append("\":{");
                sb.Append("\"now\":"); AppendJsonFloat(sb, last, dec);
                sb.Append(",\"min\":"); AppendJsonFloat(sb, cnt > 0 ? min : float.NaN, dec);
                sb.Append(",\"avg\":"); AppendJsonFloat(sb, cnt > 0 ? (float)(sum / cnt) : float.NaN, dec);
                sb.Append(",\"max\":"); AppendJsonFloat(sb, cnt > 0 ? max : float.NaN, dec);
                sb.Append('}');
            }
            sb.Append('}');
        }

        private long GetLogBytes() {
            long bytes = 0;
            try {
                foreach (string f in Directory.GetFiles(_dataDir, "temperatures_*.csv")) bytes += new FileInfo(f).Length;
            } catch { }
            return bytes;
        }

        private byte[] BuildHistoryJson(double hours, int maxPts) {
            long nowMs = DateTimeOffset.Now.ToUnixTimeMilliseconds();
            long cutoffMs = hours <= 0 ? 0 : nowMs - (long)(hours * 3600000.0);

            List<Sample> window;
            long totalDisk;
            lock (_lock) {
                totalDisk = _totalDiskSamples;
                window = new List<Sample>(_samples.Count);
                for (int i = 0; i < _samples.Count; i++) {
                    if (_samples[i].TsMs >= cutoffMs) window.Add(_samples[i]);
                }
            }

            List<Sample> plot = DownsamplePeakPreserving(window, maxPts);
            var sb = new StringBuilder(32768);
            sb.Append('{');
            sb.Append("\"host\":\"").Append(EscapeJson(_hostName)).Append("\",");
            sb.Append("\"isAdmin\":").Append(_isAdmin ? "true" : "false").Append(',');
            sb.Append("\"intervalMs\":").Append(_intervalMs.ToString(Inv)).Append(',');
            sb.Append("\"bootTimeMs\":").Append(_bootTimeMs.ToString(Inv)).Append(',');
            sb.Append("\"nowMs\":").Append(nowMs.ToString(Inv)).Append(',');
            sb.Append("\"windowCount\":").Append(window.Count.ToString(Inv)).Append(',');
            sb.Append("\"totalSamples\":").Append(totalDisk.ToString(Inv)).Append(',');
            sb.Append("\"logBytes\":").Append(GetLogBytes().ToString(Inv)).Append(',');
            sb.Append("\"stats\":");
            AppendStatsObject(sb, window);
            sb.Append(",\"series\":{");
            AppendSampleArrays(sb, plot);
            sb.Append("}}");
            return Encoding.UTF8.GetBytes(sb.ToString());
        }

        private byte[] BuildLiveJson(long sinceMs) {
            var delta = new List<Sample>(16);
            long totalDisk;
            lock (_lock) {
                totalDisk = _totalDiskSamples;
                for (int i = _samples.Count - 1; i >= 0; i--) {
                    if (_samples[i].TsMs > sinceMs) {
                        delta.Add(_samples[i]);
                        if (delta.Count >= 120) break;
                    } else break;
                }
            }
            delta.Reverse();

            var sb = new StringBuilder(2048);
            sb.Append('{');
            sb.Append("\"nowMs\":").Append(DateTimeOffset.Now.ToUnixTimeMilliseconds().ToString(Inv)).Append(',');
            sb.Append("\"totalSamples\":").Append(totalDisk.ToString(Inv)).Append(',');
            sb.Append("\"logBytes\":").Append(GetLogBytes().ToString(Inv)).Append(',');
            sb.Append("\"series\":{");
            AppendSampleArrays(sb, delta);
            sb.Append("}}");
            return Encoding.UTF8.GetBytes(sb.ToString());
        }

        private byte[] BuildExportCsv(double hours) {
            long cutoffMs = hours <= 0 ? 0 : DateTimeOffset.Now.ToUnixTimeMilliseconds() - (long)(hours * 3600000.0);
            var sb = new StringBuilder(65536);
            sb.AppendLine(_csvHeader);

            var keyToIdx = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
            for (int i = 0; i < _numChannels; i++) keyToIdx[_keys[i]] = i;

            var files = Directory.GetFiles(_dataDir, "temperatures_*.csv");
            Array.Sort(files, StringComparer.OrdinalIgnoreCase);
            foreach (string file in files) {
                try {
                    using (var fs = new FileStream(file, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
                    using (var sr = new StreamReader(fs, Encoding.UTF8)) {
                        string headerLine = sr.ReadLine();
                        if (string.IsNullOrWhiteSpace(headerLine)) continue;
                        bool exactMatch = string.Equals(headerLine, _csvHeader, StringComparison.Ordinal);
                        int[] colMap = null;
                        if (!exactMatch) {
                            string[] cols = headerLine.Split(',');
                            colMap = new int[cols.Length];
                            for (int c = 0; c < cols.Length; c++) {
                                int idx;
                                colMap[c] = keyToIdx.TryGetValue(cols[c].Trim(), out idx) ? idx : -1;
                            }
                        }

                        string line;
                        while ((line = sr.ReadLine()) != null) {
                            if (string.IsNullOrWhiteSpace(line)) continue;
                            if (cutoffMs > 0) {
                                int comma = line.IndexOf(',');
                                if (comma > 0) {
                                    long ts;
                                    if (long.TryParse(line.Substring(0, comma), NumberStyles.Integer, Inv, out ts) && ts < cutoffMs) continue;
                                }
                            }
                            if (exactMatch) {
                                sb.AppendLine(line);
                            } else {
                                string[] p = line.Split(',');
                                if (p.Length < 4) continue;
                                string[] outCols = new string[_numChannels];
                                for (int c = 3; c < p.Length && c < colMap.Length; c++) {
                                    if (colMap[c] >= 0) outCols[colMap[c]] = p[c];
                                }
                                sb.Append(p[0]).Append(',').Append(p[1]).Append(',').Append(p[2]);
                                for (int i = 0; i < _numChannels; i++) sb.Append(',').Append(outCols[i] ?? "");
                                sb.AppendLine();
                            }
                        }
                    }
                } catch { }
            }
            return Encoding.UTF8.GetBytes(sb.ToString());
        }

        public void Dispose() {
            _running = false;
            try { if (_listener != null) _listener.Stop(); } catch { }
            try { if (_csvWriter != null) { _csvWriter.Flush(); _csvWriter.Dispose(); } } catch { }
        }
    }
}
'@
    Add-Type -TypeDefinition $csCode -Language CSharp
}

# 2. Load LibreHardwareMonitorLib.dll
$lhmCandidates = @(
    $LibDir,
    (Join-Path $env:USERPROFILE 'Tools\FanControl'),
    'C:\Program Files\FanControl',
    'C:\Program Files (x86)\FanControl',
    'C:\Program Files\LibreHardwareMonitor'
)
$LhmDir = $lhmCandidates | Where-Object { Test-Path (Join-Path $_ 'LibreHardwareMonitorLib.dll') } | Select-Object -First 1
if (-not $LhmDir) { throw "LibreHardwareMonitorLib.dll not found. Run scripts/setup_deps.ps1 first." }
$LhmDll = Join-Path $LhmDir 'LibreHardwareMonitorLib.dll'

$resolveHandler = [System.ResolveEventHandler]{
    param($sender, $eventArgs)
    $asmShortName = ([System.Reflection.AssemblyName]::new($eventArgs.Name)).Name
    $candidate = Join-Path $LhmDir ($asmShortName + '.dll')
    if (Test-Path $candidate) { return [System.Reflection.Assembly]::LoadFrom($candidate) }
    return $null
}
[System.AppDomain]::CurrentDomain.add_AssemblyResolve($resolveHandler)
Add-Type -Path $LhmDll

$hasControllerSensor = $idToChannels.Keys | Where-Object { $_ -notmatch '^/(amdcpu|intelcpu|lpc|gpu-|nvme|hdd)' } | Select-Object -First 1

$comp = [LibreHardwareMonitor.Hardware.Computer]::new()
$comp.IsCpuEnabled         = $true
$comp.IsGpuEnabled         = $true
$comp.IsMotherboardEnabled = $true
$comp.IsStorageEnabled     = $true
$comp.IsControllerEnabled  = [bool]($null -ne $hasControllerSensor)
$comp.IsMemoryEnabled      = $false
$comp.IsPsuEnabled         = $false
$comp.Open()

# 3. Static host metadata
$bootTimeMs = [DateTimeOffset]::Now.ToUnixTimeMilliseconds() - [Environment]::TickCount64
try {
    $osCim = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue
    if ($osCim -and $osCim.LastBootUpTime) {
        $bootTimeMs = ([DateTimeOffset]$osCim.LastBootUpTime).ToUnixTimeMilliseconds()
    }
} catch { }

$daemon = [ThermalMonitor.Daemon]::new(
    $DataDir, $WebDir, $cfgRaw, $Port, $IntervalMs, $isAdmin, $bootTimeMs,
    $channelKeys.ToArray(), $channelDecimals.ToArray(), $channelIsTemp.ToArray()
)
$daemon.InitHistory()

$numCh = $channelKeys.Count
$script:lastStorageVals = @{}
$script:tickIdx = 0L

function Read-And-RecordSample {
    $vals = [float[]]::new($numCh)
    for ($k = 0; $k -lt $numCh; $k++) { $vals[$k] = [float]::NaN }

    $pollStorage = (($script:tickIdx % 2) -eq 0) -or ($script:lastStorageVals.Count -eq 0)
    $script:tickIdx++

    foreach ($hw in $comp.Hardware) {
        $hwType = $hw.HardwareType.ToString()
        if ($hwType -eq 'Storage' -and -not $pollStorage) { continue }
        $hw.Update()

        foreach ($s in $hw.Sensors) {
            if ($null -eq $s.Value) { continue }
            $id = $s.Identifier.ToString()
            if ($idToChannels.ContainsKey($id)) {
                $v = [float]$s.Value
                if (-not [float]::IsNaN($v) -and -not [float]::IsInfinity($v)) {
                    foreach ($idx in $idToChannels[$id]) {
                        $vals[$idx] = $v
                        if ($hwType -eq 'Storage') { $script:lastStorageVals[$idx] = $v }
                    }
                }
            }
        }

        foreach ($sub in $hw.SubHardware) {
            $sub.Update()
            foreach ($s in $sub.Sensors) {
                if ($null -eq $s.Value) { continue }
                $id = $s.Identifier.ToString()
                if ($idToChannels.ContainsKey($id)) {
                    $v = [float]$s.Value
                    if (-not [float]::IsNaN($v) -and -not [float]::IsInfinity($v)) {
                        foreach ($idx in $idToChannels[$id]) { $vals[$idx] = $v }
                    }
                }
            }
        }
    }

    if (-not $pollStorage) {
        foreach ($kv in $script:lastStorageVals.GetEnumerator()) {
            if ([float]::IsNaN($vals[$kv.Key])) { $vals[$kv.Key] = $kv.Value }
        }
    }

    $daemon.RecordSample($vals)
}

Read-And-RecordSample
$daemon.StartHttp()

$lanIps = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -ne '127.0.0.1' -and $_.IPAddress -notmatch '^169\.254' } |
    Select-Object -ExpandProperty IPAddress)

[ordered]@{
    status        = 'running'
    pid           = $PID
    isAdmin       = $isAdmin
    port          = $Port
    intervalMs    = $IntervalMs
    channels      = $numCh
    startedAt     = [DateTimeOffset]::Now.ToString('o')
    loadedSamples = $daemon.LoadedCount
    urls          = @("http://localhost:$Port/") + ($lanIps | ForEach-Object { "http://$($_):$Port/" })
} | ConvertTo-Json -Depth 4 | Set-Content -Path $StatusJsonPath -Encoding UTF8

Write-DaemonLog "HTTP listening on 0.0.0.0:$Port (Channels=$numCh, LoadedSamples=$($daemon.LoadedCount))"

$sw = [System.Diagnostics.Stopwatch]::StartNew()
$iter = 1L
try {
    while ($true) {
        $targetMs = $iter * $IntervalMs
        $sleepMs  = [int]($targetMs - $sw.ElapsedMilliseconds)
        if ($sleepMs -gt 10) { Start-Sleep -Milliseconds ([Math]::Min($sleepMs, 5000)) }
        else { Start-Sleep -Milliseconds 10 }
        Read-And-RecordSample
        $iter++
    }
}
finally {
    Write-DaemonLog "Shutting down thermal_service.ps1"
    try { $daemon.Dispose() } catch { }
    try { $comp.Close() } catch { }
}
