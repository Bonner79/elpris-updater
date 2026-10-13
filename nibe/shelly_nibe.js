// NIBE AUX controller v1.0 - ONE script owns all three outputs.
// Required wiring/config: 0=external adjustment, 1=temporary luxury,
// 2=SG Ready B. SG Ready A is assigned to an UNUSED, OPEN AUX4.
// Never run this on the earlier A/B wiring or on another appliance.
// Firmware 1.5+ provides Script.storage. Without it, cache is RAM-only.
let CONFIG = {
  gist_url: "PASTE_NIBE_GIST_RAW_URL_HERE",
  area: "SE3",
  dry_run: true,              // true: log only, NO relay commands
  allow_heat: false,          // enable only after NIBE heat settings are checked
  allow_luxury: false,        // enables manual boost(); NOT a daily 65 C schedule
  poll_seconds: 300,
  lease_seconds: 90,
  refresh_seconds: 30
};

let plan = null;
let fetching = false;
let fetchAt = 0;
let redirectDepth = 0;
let cacheLoaded = false;
let busy = false;
let step = 0;
let job = [0, 0, 0];
let target = [0, 0, 0];
let renewAt = [0, 0, 0];
let commandId = -1;
let commandOn = false;
let hadError = false;
let boostEnd = 0;
let paused = false;
let previousUnix = 0;
let previousUptime = 0;
let clockHoldUntil = 0;
let lastMessage = "";

function integer(n) {
  return typeof n === "number" && n === Math.floor(n) && Math.abs(n) < 100000000000;
}
function uptime() {
  let s = Shelly.getComponentStatus("sys");
  return s && typeof s.uptime === "number" ? s.uptime : 0;
}
function nowUTC() {
  let s = Shelly.getComponentStatus("sys");
  return s && integer(s.unixtime) && s.unixtime > 1700000000 ? s.unixtime : 0;
}
function say(s) {
  if (s !== lastMessage) { print(s); lastMessage = s; }
}
function clockReady() {
  let n = nowUTC();
  let u = uptime();
  if (!n) { boostEnd = 0; return false; }
  if (previousUnix && Math.abs((n - previousUnix) - (u - previousUptime)) > 10) {
    clockHoldUntil = u + 60;
    boostEnd = 0;
    print("Clock jumped: extra control suspended for 60 seconds.");
  }
  previousUnix = n;
  previousUptime = u;
  return u >= clockHoldUntil;
}
function overlap(a, b) {
  return a.length === 2 && b.length === 2 && a[0] < b[1] && b[0] < a[1];
}
function validWindow(w, d, maximum) {
  if (!Array.isArray(w)) return false;
  if (w.length === 0) return true;
  return w.length === 2 && integer(w[0]) && integer(w[1]) &&
    w[0] >= d.start && w[0] < w[1] && w[1] <= d.end && w[1] - w[0] <= maximum;
}
function validPlan(p, n) {
  if (!p || p.system !== "nibe-aux-v1" || p.area !== CONFIG.area ||
      !integer(p.generated) || !Array.isArray(p.days) || p.days.length > 2) return false;
  if (n && (p.generated > n + 300 || n - p.generated > 172800)) return false;
  let lastEnd = 0;
  for (let i = 0; i < p.days.length; i++) {
    let d = p.days[i];
    if (!d || typeof d.date !== "string" || d.date.length !== 10 ||
        !integer(d.start) || !integer(d.end) || d.start < lastEnd ||
        d.end - d.start < 82800 || d.end - d.start > 90000 ||
        d.start < p.generated - 90000 || d.end > p.generated + 180000 ||
        !validWindow(d.heat, d, 21600) || !validWindow(d.water, d, 14400) ||
        overlap(d.heat, d.water)) return false;
    lastEnd = d.end;
  }
  return true;
}
function cacheAvailable() {
  return typeof Script !== "undefined" && typeof Script.storage !== "undefined";
}
function loadCache() {
  cacheLoaded = true;
  if (!cacheAvailable()) { print("No Script.storage: cache survives network loss, not reboot."); return; }
  try {
    let text = Script.storage.getItem("nibe_plan_v1");
    if (typeof text === "string" && text.length <= 900) {
      let p = JSON.parse(text);
      if (validPlan(p, nowUTC())) { plan = p; print("Loaded cached NIBE plan."); }
    }
  } catch (e) { print("Cache unreadable; waiting for valid plan."); }
}
function installPlan(text) {
  if (typeof text !== "string" || text.length > 900) return false;
  let p = null;
  try { p = JSON.parse(text); } catch (e) { return false; }
  if (!validPlan(p, nowUTC())) return false;
  if (plan && p.generated < plan.generated) return false;
  let changed = !plan || JSON.stringify(plan.days) !== JSON.stringify(p.days);
  plan = p;
  if (changed && cacheAvailable()) {
    try { Script.storage.setItem("nibe_plan_v1", JSON.stringify(p)); }
    catch (e) { print("Cache write failed; using RAM plan."); }
  }
  if (changed) print("NIBE plan accepted:", JSON.stringify(p.days));
  return true;
}
function permittedURL(url) {
  return typeof url === "string" &&
    url.indexOf("https://gist.githubusercontent.com/") === 0;
}
function requestURL(url) {
  // No GitHub token on the Shelly. HTTPS certificate verification stays enabled.
  let suffix = (url.indexOf("?") < 0 ? "?" : "&") + "nibe=" + nowUTC();
  Shelly.call("HTTP.Request", {
    method: "GET", url: url + suffix, timeout: 15,
    headers: { "Accept": "application/json", "Cache-Control": "no-cache" }
  }, httpDone);
}
function httpDone(res, code, error) {
  try {
    if (code !== 0 || !res) {
      fetching = false; fetchAt = uptime() + 60;
      print("HTTP unavailable; keeping valid cached periods."); return;
    }
    if (res.code === 301 || res.code === 302 || res.code === 307 || res.code === 308) {
      let loc = res.headers ? (res.headers.location || res.headers.Location) : null;
      redirectDepth++;
      if (redirectDepth <= 3 && permittedURL(loc)) { requestURL(loc); return; }
    }
    fetching = false;
    if (res.code !== 200 || !installPlan(res.body)) {
      fetchAt = uptime() + 60;
      print("Rejected HTTP/JSON/plan; keeping only previously validated periods."); return;
    }
    fetchAt = uptime() + CONFIG.poll_seconds;
  } catch (e) {
    fetching = false; fetchAt = uptime() + 60;
    print("HTTP callback error; no new plan accepted.");
  }
}
function fetchPlan() {
  if (fetching || uptime() < fetchAt) return;
  if (!permittedURL(CONFIG.gist_url)) {
    fetchAt = uptime() + 60;
    print("Set gist_url to the NEW NIBE Gist raw URL."); return;
  }
  fetching = true; redirectDepth = 0;
  requestURL(CONFIG.gist_url);
}
function configSafe() {
  for (let i = 0; i < 3; i++) {
    let c = Shelly.getComponentConfig("switch:" + i);
    if (!c || c.in_mode !== "detached" || c.initial_state !== "off" ||
        c.auto_on !== false || c.auto_off !== true ||
        c.auto_off_delay < 90 || c.auto_off_delay > 180) return false;
  }
  return true;
}
function activeEnd(w, n) {
  return w.length === 2 && n >= w[0] && n < w[1] ? w[1] : 0;
}
function desired(n) {
  let t = [0, 0, 0];
  if (paused || !n) return t;
  if (plan && validPlan(plan, n)) {
    for (let i = 0; i < plan.days.length; i++) {
      let d = plan.days[i];
      if (n >= d.start && n < d.end) {
        if (CONFIG.allow_heat) t[0] = activeEnd(d.heat, n);
        t[2] = activeEnd(d.water, n);
      }
    }
  }
  if (CONFIG.allow_luxury && boostEnd > n) { t[1] = boostEnd; t[2] = 0; }
  if (t[1] || t[2]) t[0] = 0; // no EXTRA space-heating request during water boost
  return t;
}
function relayDone(res, code, error) {
  if (code !== 0) { hadError = true; print("Relay RPC failed:", commandId, error); }
  if (code === 0 && commandOn) renewAt[commandId] = uptime() + CONFIG.refresh_seconds;
  applyNext();
}
function applyNext() {
  // Iterative on purpose: the Shelly interpreter has a very shallow call stack.
  while (step < 6) {
    let phase = step < 3 ? 0 : 1;
    let id = step % 3;
    step++;
    let s = Shelly.getComponentStatus("switch:" + id);
    let n = nowUTC();
    let wanted = job[id] > n && n > 0;
    if (phase === 0) {
      // Phase 0: OPEN unwanted contacts before phase 1 can close anything.
      if (!wanted && s && s.output === true) {
        commandId = id; commandOn = false;
        Shelly.call("Switch.Set", { id: id, on: false }, relayDone);
        return;
      }
    } else if (wanted && !hadError && s && !(s.errors && s.errors.length)) {
      let blocked = false;
      if (id === 1 || id === 2) {
        let other = Shelly.getComponentStatus("switch:" + (id === 1 ? 2 : 1));
        blocked = !other || other.output !== false;
      }
      if (!blocked && (s.output === false || uptime() >= renewAt[id])) {
        let lease = Math.min(CONFIG.lease_seconds, job[id] - n);
        if (lease > 0) {
          commandId = id; commandOn = true;
          Shelly.call("Switch.Set", { id: id, on: true, toggle_after: lease }, relayDone);
          return;
        }
      }
    }
  }
  busy = false;
}
function reconcile(t) {
  if (busy) return;
  job = [t[0], t[1], t[2]];
  busy = true; step = 0; hadError = false;
  applyNext();
}
// Manual luxury, via the authenticated Script.Eval RPC: boost(60), boost(0).
// Not persisted across reboot, never enabled automatically on script startup.
function boost(minutes) {
  if (!integer(minutes) || minutes < 0 || minutes > 120) return "Use 0..120 whole minutes";
  if (minutes === 0) { boostEnd = 0; return "Luxury request cancelled"; }
  if (CONFIG.dry_run || !CONFIG.allow_luxury || paused || !nowUTC() || !configSafe())
    return "Not permitted: check dry_run, allow_luxury, clock and safety settings";
  boostEnd = nowUTC() + minutes * 60;
  return "Luxury requested until UTC epoch " + boostEnd;
}
function pauseControl() { paused = true; boostEnd = 0; return "Paused; outputs release on next tick"; }
function resumeControl() { paused = false; return "Automatic control resumed"; }
function tick() {
  try {
    let ok = clockReady();
    if (ok && !cacheLoaded) loadCache();
    if (ok) fetchPlan();
    let safe = configSafe();
    target = ok && safe ? desired(nowUTC()) : [0, 0, 0];
    let text = (CONFIG.dry_run ? "PREVIEW" : "CONTROL") +
      " heat=" + (target[0] > 0) + " luxury=" + (target[1] > 0) +
      " sg=" + (target[2] > 0) + " clock=" + ok + " config=" + safe;
    say(text);
    if (!CONFIG.dry_run) reconcile(target);
  } catch (e) {
    boostEnd = 0;
    print("Controller error; stopping additional requests.");
    if (!CONFIG.dry_run && !busy) reconcile([0, 0, 0]);
  }
}
Timer.set(5000, true, tick);
tick();
