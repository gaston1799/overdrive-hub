// Electron shell: starts the Python engine, waits for it, then opens the UI.
const { app, BrowserWindow, dialog } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const http = require("http");
const net = require("net");

const ROOT = path.resolve(__dirname, "..");
const PYTHON = process.platform === "win32"
  ? path.join(ROOT, ".venv", "Scripts", "python.exe")
  : path.join(ROOT, ".venv", "bin", "python");

let engine = null;
let win = null;

function freePort() {
  return new Promise((resolve) => {
    const s = net.createServer();
    s.listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => resolve(p)); });
  });
}

function waitForHealth(port, timeoutMs = 20000) {
  const t0 = Date.now();
  return new Promise((resolve, reject) => {
    const tryOnce = () => {
      const req = http.get({ host: "127.0.0.1", port, path: "/health", timeout: 1000 }, (res) => {
        res.resume();
        res.statusCode === 200 ? resolve() : retry();
      });
      req.on("error", retry);
      req.on("timeout", () => { req.destroy(); retry(); });
    };
    const retry = () => (Date.now() - t0 > timeoutMs ? reject(new Error("engine did not start")) : setTimeout(tryOnce, 250));
    tryOnce();
  });
}

async function start() {
  const port = await freePort();
  enginePort = port;
  const args = [path.join(ROOT, "run.py"), "--port", String(port)];
  const simIdx = process.argv.indexOf("--sim");
  if (simIdx > -1) args.push("--sim", process.argv[simIdx + 1] || "4");
  if (process.argv.includes("--no-ble")) args.push("--no-ble");
  if (process.env.ODHUB_DATA) args.push("--data", process.env.ODHUB_DATA);

  engine = spawn(PYTHON, args, { cwd: ROOT, windowsHide: true });
  let log = "";
  engine.stdout.on("data", (d) => { log += d; process.stdout.write(d); });
  engine.stderr.on("data", (d) => { log += d; process.stderr.write(d); });
  engine.on("exit", (code) => {
    if (win && !app.isQuitting) {
      dialog.showErrorBox("Engine stopped", `The car engine exited (code ${code}).\n\n${log.slice(-2000)}`);
      app.quit();
    }
  });

  win = new BrowserWindow({
    width: 1500, height: 920, minWidth: 1000, minHeight: 640,
    backgroundColor: "#0d1117", title: "Overdrive Hub", autoHideMenuBar: true,
    webPreferences: { backgroundThrottling: false, contextIsolation: true },
  });
  win.loadURL("data:text/html,<body style='background:#0d1117;color:#8b949e;font:16px system-ui;display:grid;place-items:center;height:100vh;margin:0'>Starting engine…</body>");

  try {
    await waitForHealth(port);
    await win.loadURL(`http://127.0.0.1:${port}/`);
    // dev aid: ODHUB_SCREENSHOT=path.png ODHUB_SCREENSHOT_JS="..." captures the window after a delay
    if (process.env.ODHUB_SCREENSHOT) {
      if (process.env.ODHUB_SCREENSHOT_JS) setTimeout(() => win.webContents.executeJavaScript(process.env.ODHUB_SCREENSHOT_JS).catch(() => {}), 2500);
      // ODHUB_SCREENSHOT_TIMES="4000,6000,..." takes a series (file name gets -1, -2, ...)
      const times = (process.env.ODHUB_SCREENSHOT_TIMES || String(process.env.ODHUB_SCREENSHOT_DELAY || 9000)).split(",").map(Number);
      times.forEach((ms, i) => setTimeout(async () => {
        const img = await win.webContents.capturePage();
        const out = times.length > 1 ? process.env.ODHUB_SCREENSHOT.replace(/\.png$/, `-${i + 1}.png`) : process.env.ODHUB_SCREENSHOT;
        require("fs").writeFileSync(out, img.toPNG());
        if (i === times.length - 1) app.quit();
      }, ms));
    }
  } catch (e) {
    dialog.showErrorBox("Could not start", `${e.message}\n\n${log.slice(-2000)}`);
    app.quit();
  }
}

let enginePort = null;
function killEngine() {
  if (engine && engine.exitCode === null) {
    if (process.platform === "win32") spawn("taskkill", ["/pid", String(engine.pid), "/f", "/t"]);
    else engine.kill();
  }
}

// Ask the engine to stop every car and disconnect cleanly, then make sure it is gone.
function stopEngine() {
  return new Promise((resolve) => {
    if (!engine || engine.exitCode !== null || !enginePort) { killEngine(); return resolve(); }
    const done = () => { clearTimeout(timer); killEngine(); resolve(); };
    const timer = setTimeout(done, 3000);
    engine.once("exit", done);
    const req = http.request({ host: "127.0.0.1", port: enginePort, path: "/shutdown", method: "POST" });
    req.on("error", done);
    req.end();
  });
}

// simulator / test runs get their own profile so they never clash with the real app window
if (process.argv.includes("--sim")) app.setPath("userData", path.join(app.getPath("userData"), "..", "overdrive-hub-sim"));
app.commandLine.appendSwitch("disable-renderer-backgrounding");
app.commandLine.appendSwitch("enable-gpu-rasterization");
app.whenReady().then(start);
app.on("before-quit", (ev) => {
  if (app.isQuitting) return;
  app.isQuitting = true;
  ev.preventDefault();
  stopEngine().then(() => app.quit());
});
app.on("window-all-closed", () => app.quit());
