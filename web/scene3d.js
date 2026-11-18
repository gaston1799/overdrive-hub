// 3D view of the track and cars (Three.js). Units: metres. Grid cell = 0.56 m.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { CSS2DRenderer, CSS2DObject } from "three/addons/renderers/CSS2DRenderer.js";

const CELL = 0.56;
const R = CELL / 2;
const ROAD_W = 0.21;
const HEAD = [[1, 0], [0, 1], [-1, 0], [0, -1]];

const cellsOf = (p) => (p.cells !== undefined ? p.cells : p.kind === "jump" ? 2 : p.kind === "landing" ? 0 : 1);

// grid rotation by k quarter turns (heading 0=+x, 1=+z), matching world.rotation.y = -k*90°
const rot = (k, x, z) => { for (let i = 0; i < k; i++) [x, z] = [-z, x]; return { x, z }; };
const startPose = (geo) => {
  const p = geo.pieces.find((q) => q.kind === "start");
  return p ? { x: p.x, z: p.z, h: p.heading_in } : null;
};

// ------------------------------------------------------------------ track geometry
export class TrackGeo {
  constructor(track) {
    this.track = track;
    this.pieces = track.pieces;
    this.lengths = this.pieces.map((p) => (p.kind === "turn" ? (Math.PI / 2) * R : CELL * Math.max(cellsOf(p), 0.002)));
    this.cum = [];
    let s = 0;
    for (const L of this.lengths) { this.cum.push(s); s += L; }
    this.total = s;
    this.closed = !!track.closed;     // a live-scan chain is open: never wrap from its end to its start
  }
  // point on piece i at fraction f (in track direction), lane in metres (negative = left)
  point(i, f, lane = 0) {
    const n = this.pieces.length;
    const p = this.pieces[((i % n) + n) % n];
    f = Math.min(1, Math.max(0, f));
    const hin = HEAD[p.heading_in], hout = HEAD[p.heading_out];
    const cx = p.x * CELL, cz = p.z * CELL;
    const ex = cx - hin[0] * CELL / 2, ez = cz - hin[1] * CELL / 2;
    if (p.kind !== "turn" || !p.turn) {
      const h = hout;
      const rx = -h[1], rz = h[0];
      const L = CELL * cellsOf(p);
      return { x: ex + h[0] * L * f + rx * lane, z: ez + h[1] * L * f + rz * lane, yaw: Math.atan2(h[1], h[0]) };
    }
    const ccx = ex + hout[0] * R, ccz = ez + hout[1] * R;
    const a0 = Math.atan2(ez - ccz, ex - ccx);
    const right = p.turn === "R";
    const sweep = right ? Math.PI / 2 : -Math.PI / 2;
    const r = right ? R - lane : R + lane;
    const a = a0 + sweep * f;
    const yaw0 = Math.atan2(hin[1], hin[0]);
    return { x: ccx + r * Math.cos(a), z: ccz + r * Math.sin(a), yaw: yaw0 + sweep * f };
  }
  // distance along track (m) -> point
  at(s, lane) {
    s = this.closed ? ((s % this.total) + this.total) % this.total : Math.min(Math.max(s, 0), this.total - 1e-4);
    let i = 0;
    while (i < this.pieces.length - 1 && this.cum[i + 1] <= s) i++;
    return this.point(i, (s - this.cum[i]) / this.lengths[i], lane);
  }
  distance(i, f) { return this.cum[i] + this.lengths[i] * f; }
}

// ------------------------------------------------------------------ textures
function stripeTexture(start = false, special = false) {
  const c = document.createElement("canvas");
  c.width = 128; c.height = 256;
  const g = c.getContext("2d");
  g.fillStyle = special ? "#5a3412" : "#3a3f49"; g.fillRect(0, 0, 128, 256);
  if (special) {                         // Fast & Furious bonus piece: orange chevrons
    g.strokeStyle = "#ff8a1f"; g.lineWidth = 9;
    for (let y = 24; y < 256; y += 56) { g.beginPath(); g.moveTo(20, y + 26); g.lineTo(64, y); g.lineTo(108, y + 26); g.stroke(); }
  }
  g.fillStyle = "#f2f2f2"; g.fillRect(0, 0, 6, 256); g.fillRect(122, 0, 6, 256);
  g.fillStyle = "#7d8592";
  for (const x of [34, 64, 94]) for (let y = 0; y < 256; y += 32) g.fillRect(x - 1, y, 2, 16);
  if (start) {
    for (let r = 0; r < 4; r++) for (let k = 0; k < 8; k++) {
      g.fillStyle = (r + k) % 2 ? "#111" : "#fff"; g.fillRect(6 + k * 14.5, 112 + r * 8, 14.5, 8);
    }
  }
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 8;
  return t;
}

// ------------------------------------------------------------------ car model
function carMesh(color) {
  const g = new THREE.Group();
  const col = new THREE.Color(color);
  const body = new THREE.Mesh(new THREE.BoxGeometry(0.085, 0.022, 0.048),
    new THREE.MeshStandardMaterial({ color: 0x22262d, metalness: 0.6, roughness: 0.35 }));
  body.position.y = 0.016; body.castShadow = true; g.add(body);
  const shell = new THREE.Mesh(new THREE.BoxGeometry(0.06, 0.014, 0.036),
    new THREE.MeshStandardMaterial({ color: col, emissive: col, emissiveIntensity: 0.55, metalness: 0.3, roughness: 0.4 }));
  shell.position.set(-0.006, 0.033, 0); shell.castShadow = true; g.add(shell);
  const nose = new THREE.Mesh(new THREE.BoxGeometry(0.02, 0.008, 0.04),
    new THREE.MeshStandardMaterial({ color: col, emissive: col, emissiveIntensity: 0.9 }));
  nose.position.set(0.04, 0.024, 0); g.add(nose);
  const glow = new THREE.PointLight(col, 0.25, 0.4);
  glow.position.y = 0.05; g.add(glow);
  g.userData = { shell, nose, glow };
  return g;
}

function crownMesh() {
  const m = new THREE.Mesh(new THREE.CylinderGeometry(0.022, 0.018, 0.02, 5, 1, true),
    new THREE.MeshStandardMaterial({ color: 0xffd34d, emissive: 0xffaa00, emissiveIntensity: 0.8, side: THREE.DoubleSide }));
  m.position.y = 0.075;
  return m;
}

// ------------------------------------------------------------------ scene
export class Scene3D {
  constructor(canvas) {
    this.canvas = canvas;
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: "high-performance" });
    this.renderer.setPixelRatio(Math.min(2, window.devicePixelRatio));
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.labels = new CSS2DRenderer();
    Object.assign(this.labels.domElement.style, { position: "absolute", top: "0", left: "0", pointerEvents: "none" });
    canvas.parentElement.appendChild(this.labels.domElement);

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x0d1117);
    this.scene.fog = new THREE.Fog(0x0d1117, 8, 20);
    this.camera = new THREE.PerspectiveCamera(45, 1, 0.01, 100);
    this.camera.position.set(2.2, 2.4, 3.2);
    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.enableDamping = true;
    this.controls.maxPolarAngle = Math.PI / 2.05;
    this.controls.minDistance = 0.3;
    this.controls.maxDistance = 12;

    this.scene.add(new THREE.HemisphereLight(0xbcd3ff, 0x1a1410, 0.9));
    const sun = new THREE.DirectionalLight(0xffffff, 1.6);
    sun.position.set(3, 6, 2);
    sun.castShadow = true;
    sun.shadow.mapSize.set(2048, 2048);
    Object.assign(sun.shadow.camera, { left: -5, right: 5, top: 5, bottom: -5, near: 0.5, far: 20 });
    this.scene.add(sun);
    this.sun = sun;

    const floor = new THREE.Mesh(new THREE.PlaneGeometry(40, 40),
      new THREE.MeshStandardMaterial({ color: 0x0b0e13, roughness: 0.95 }));
    floor.rotation.x = -Math.PI / 2; floor.receiveShadow = true;
    this.scene.add(floor);
    const grid = new THREE.GridHelper(40, 40 / CELL * 1, 0x223040, 0x1a222c);
    grid.position.y = 0.0005;
    this.scene.add(grid);

    // everything that lives in track coordinates sits in `world`, so a whole track can be
    // re-aligned (e.g. live scan chain -> finished loop) without anything visibly jumping
    this.world = new THREE.Group();
    this.scene.add(this.world);
    this.xf = { k: 0, tx: 0, tz: 0 };
    this.trackGroup = new THREE.Group();
    this.world.add(this.trackGroup);
    this.fxGroup = new THREE.Group();
    this.world.add(this.fxGroup);
    this.controls.addEventListener("start", () => { this.camGoal = null; });   // user takes over
    this.geo = null;
    this.cars = new Map();        // address -> {mesh, label, s, lane, ...}
    this.mines = [];
    this.camMode = "orbit";
    this.follow = null;
    this.clock = new THREE.Clock();
    this.texStraight = stripeTexture(false);
    this.texStart = stripeTexture(true);
    this.texSpecial = stripeTexture(false, true);

    new ResizeObserver(() => this.resize()).observe(canvas.parentElement);
    this.resize();
    this.renderer.setAnimationLoop(() => this.frame());
  }

  resize() {
    const el = this.canvas.parentElement;
    const w = el.clientWidth, h = el.clientHeight;
    this.renderer.setSize(w, h, false);
    this.labels.setSize(w, h);
    this.camera.aspect = w / Math.max(1, h);
    this.camera.updateProjectionMatrix();
  }

  // ---------------------------------------------------------------- track
  setTrack(track, opts = {}) {
    this.trackGroup.traverse((o) => { if (o.geometry) o.geometry.dispose(); if (o.material && o.material.dispose) o.material.dispose(); });
    this.trackGroup.clear();
    this.pieceMeshes = [];
    if (this.hl) { this.world.remove(this.hl); this.hl = null; }
    this.hlIndex = null;
    const prevGeo = this.geo, prevLive = this.liveTrack, prevXf = this.xf;
    this.geo = track && track.pieces.length ? new TrackGeo(track) : null;
    for (const c of this.cars.values()) c.s = null;      // re-place cars (invisible when aligned)
    // keep the start line where it was on screen when a live scan chain becomes the finished loop
    let xf = { k: 0, tx: 0, tz: 0 };
    let aligned = false;
    if (this.geo && prevGeo && (prevLive || opts.live)) {
      const a = startPose(prevGeo), b = startPose(this.geo);
      if (a && b) {
        const k = (((a.h + prevXf.k - b.h) % 4) + 4) % 4;
        const wa = rot(prevXf.k, a.x * CELL, a.z * CELL);
        const rb = rot(k, b.x * CELL, b.z * CELL);
        xf = { k, tx: wa.x + prevXf.tx - rb.x, tz: wa.z + prevXf.tz - rb.z };
        aligned = true;
      } else if (opts.live && prevLive) {
        xf = prevXf;                                     // chain growing from the same first piece
        aligned = true;
      }
    }
    this.xf = xf;
    this.liveTrack = !!opts.live;
    this.world.rotation.y = -xf.k * Math.PI / 2;
    this.world.position.set(xf.tx, 0, xf.tz);
    this.world.updateMatrixWorld(true);
    if (!this.geo) return;
    const roadMat = (tex) => new THREE.MeshStandardMaterial({ map: tex, roughness: 0.8, metalness: 0.05, side: THREE.DoubleSide });
    const matStraight = roadMat(this.texStraight), matStart = roadMat(this.texStart), matSpecial = roadMat(this.texSpecial);
    const edgeMat = new THREE.MeshStandardMaterial({ color: 0xd8dde5, roughness: 0.5 });
    const g = this.geo;
    const crossSeen = new Set();
    g.pieces.forEach((p, i) => {
      const steps = p.kind === "turn" ? 16 : 1;
      let lift = p.kind === "jump" ? 0.02 : 0;
      if (p.kind === "crisscross") {          // the '+' piece is driven twice; draw the 2nd pass a hair higher
        const cell = `${p.x},${p.z}`;
        if (crossSeen.has(cell)) lift = 0.0015;
        crossSeen.add(cell);
      }
      const pos = [], uv = [], idx = [];
      for (let k = 0; k <= steps; k++) {
        const f = k / steps;
        const a = g.point(i, f, -ROAD_W / 2), b = g.point(i, f, ROAD_W / 2);
        const y = 0.004 + (p.kind === "jump" ? lift * f : lift);
        pos.push(a.x, y, a.z, b.x, y, b.z);
        uv.push(0, f, 1, f);
        if (k < steps) { const o = k * 2; idx.push(o, o + 2, o + 1, o + 1, o + 2, o + 3); }
      }
      const geom = new THREE.BufferGeometry();
      geom.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
      geom.setAttribute("uv", new THREE.Float32BufferAttribute(uv, 2));
      geom.setIndex(idx);
      geom.computeVertexNormals();
      const mesh = new THREE.Mesh(geom, p.kind === "start" ? matStart : p.kind === "special" ? matSpecial : matStraight);
      if (p.kind === "start") {                // arrows show which way the start line faces
        for (const f of [0.2, 0.8]) {
          const q = g.point(i, f, 0);
          const tri = new THREE.Mesh(new THREE.ConeGeometry(0.03, 0.06, 3),
            new THREE.MeshBasicMaterial({ color: 0x3fb950 }));
          tri.rotation.set(0, -q.yaw + (p.reversed ? Math.PI : 0), -Math.PI / 2, "YXZ");
          tri.position.set(q.x, 0.012, q.z);
          tri.scale.set(0.25, 1, 1);
          this.trackGroup.add(tri);
        }
      }
      mesh.receiveShadow = true;
      mesh.userData.index = i;
      this.trackGroup.add(mesh);
      this.pieceMeshes.push(mesh);
      // raised edges
      for (const side of [-1, 1]) {
        const pts = [];
        for (let k = 0; k <= steps; k++) {
          const q = g.point(i, k / steps, side * (ROAD_W / 2 + 0.006));
          pts.push(new THREE.Vector3(q.x, 0.008 + (p.kind === "jump" ? lift * k / steps : lift), q.z));
        }
        const curve = new THREE.CatmullRomCurve3(pts);
        const tube = new THREE.Mesh(new THREE.TubeGeometry(curve, Math.max(2, steps), 0.006, 6, false), edgeMat);
        tube.castShadow = true;
        this.trackGroup.add(tube);
      }
    });
    // fit camera; glide instead of jumping while scanning or right after a scan
    this.world.updateMatrixWorld(true);
    const box = new THREE.Box3().setFromObject(this.trackGroup);
    const c = box.getCenter(new THREE.Vector3()), size = box.getSize(new THREE.Vector3());
    const r = Math.max(size.x, size.z) * 0.55 + 0.25;
    const smooth = aligned && !!this.radius;
    const refit = !opts.live || !this.radius || r > this.radius * 1.15 || !prevLive;
    this.center = c;
    if (refit) {
      this.radius = Math.max(r, opts.live ? 1.2 : 0);
      if (this.camMode !== "chase") this.setCamera(this.camMode, smooth);
    } else if (this.camMode !== "chase") {
      // re-centre: move the camera WITH its target, so the view slides instead of spinning
      const shift = c.clone().sub(this.controls.target);
      this.camGoal = { pos: this.camera.position.clone().add(shift), target: c.clone() };
    }
    this.sun.position.set(c.x + 3, 6, c.z + 2);
    this.sun.target.position.copy(c);
    this.scene.add(this.sun.target);
  }

  // glowing overlay on one piece (the piece the scanning car is on)
  highlight(index) {
    if (index === this.hlIndex && this.hl) return;
    if (this.hl) { this.world.remove(this.hl); this.hl = null; }
    this.hlIndex = index;
    const src = index != null && this.pieceMeshes && this.pieceMeshes[index];
    if (!src) return;
    this.hl = new THREE.Mesh(src.geometry, new THREE.MeshBasicMaterial({
      color: 0xffb02e, transparent: true, opacity: 0.35, side: THREE.DoubleSide, depthWrite: false }));
    this.hl.position.y = 0.002;
    this.world.add(this.hl);
  }

  setCamera(mode, smooth = false) {
    this.camMode = mode;
    const c = this.center || new THREE.Vector3(), r = this.radius || 3;
    this.controls.enabled = mode !== "chase";
    if (mode === "chase") { this.camGoal = null; return; }
    const pos = mode === "top" ? new THREE.Vector3(c.x, r * 1.9, c.z + r * 0.04)   // fixed offset: no spinning
      : new THREE.Vector3(c.x + r * 0.8, r * 1.35, c.z + r * 1.45);
    if (smooth) { this.camGoal = { pos, target: c.clone() }; return; }
    this.camGoal = null;
    this.camera.position.copy(pos);
    this.controls.target.copy(c);
  }

  // ---------------------------------------------------------------- cars
  syncCars(list) {
    const seen = new Set();
    for (const car of list) {
      if (!car.connected) continue;
      seen.add(car.address);
      let c = this.cars.get(car.address);
      if (!c) {
        const mesh = carMesh(car.color);
        const el = document.createElement("div");
        el.className = "label3d";
        const label = new CSS2DObject(el);
        label.position.y = 0.11;
        mesh.add(label);
        this.world.add(mesh);
        c = { mesh, label, el, s: null, lane: 0, color: car.color, crown: null };
        this.cars.set(car.address, c);
      }
      if (c.color !== car.color) {
        c.color = car.color;
        const col = new THREE.Color(car.color);
        for (const m of [c.mesh.userData.shell, c.mesh.userData.nose]) { m.material.color = col; m.material.emissive = col; }
        c.mesh.userData.glow.color = col;
      }
      c.el.textContent = car.nickname;
      c.el.style.borderBottom = `2px solid ${car.color}`;
      c.data = car;
      c.recv = performance.now();
    }
    for (const [a, c] of this.cars) {
      if (!seen.has(a)) { this.world.remove(c.mesh); c.label.element.remove(); this.cars.delete(a); }
    }
  }

  setGame(game) {
    this.game = game;
    for (const [a, c] of this.cars) {
      const king = game && game.king === a;
      if (king && !c.crown) { c.crown = crownMesh(); c.mesh.add(c.crown); }
      if (!king && c.crown) { c.mesh.remove(c.crown); c.crown = null; }
    }
    // mines
    const mines = (game && game.mines) || [];
    while (this.mines.length > mines.length) this.fxGroup.remove(this.mines.pop());
    while (this.mines.length < mines.length) {
      const m = new THREE.Mesh(new THREE.CylinderGeometry(0.018, 0.018, 0.006, 16),
        new THREE.MeshStandardMaterial({ color: 0xff2222, emissive: 0xff0000, emissiveIntensity: 0.8 }));
      this.fxGroup.add(m); this.mines.push(m);
    }
    if (this.geo) mines.forEach((mn, k) => {
      const p = this.geo.point(mn.index, 0.5, mn.lane / 1000);
      this.mines[k].position.set(p.x, 0.01, p.z);
    });
  }

  laser(from, to, hit) {
    const a = this.cars.get(from), b = this.cars.get(to);
    if (!a) return;
    const p0 = a.mesh.position.clone().setY(0.035);
    let p1;
    if (b) p1 = b.mesh.position.clone().setY(0.035);
    else { const dir = new THREE.Vector3(Math.cos(a.mesh.rotation.y), 0, -Math.sin(a.mesh.rotation.y)); p1 = p0.clone().addScaledVector(dir, 0.6); }
    const geom = new THREE.BufferGeometry().setFromPoints([p0, p1]);
    const line = new THREE.Line(geom, new THREE.LineBasicMaterial({ color: hit ? 0xff3355 : 0x66ccff, transparent: true }));
    line.userData.ttl = 0.35;
    this.fxGroup.add(line);
    if (hit && b) {
      const burst = new THREE.Mesh(new THREE.SphereGeometry(0.04, 12, 8),
        new THREE.MeshBasicMaterial({ color: 0xff5533, transparent: true, opacity: 0.8 }));
      burst.position.copy(p1); burst.userData.ttl = 0.4; burst.userData.grow = true;
      this.fxGroup.add(burst);
    }
  }

  // ---------------------------------------------------------------- per frame
  frame() {
    const dt = Math.min(0.1, this.clock.getDelta());
    const g = this.geo;
    for (const c of this.cars.values()) {
      const d = c.data;
      if (!d) continue;
      const placed = g && d.index !== null && d.index !== undefined;
      c.mesh.visible = !!placed;
      c.label.visible = !!placed;
      if (!placed) { c.s = null; continue; }
      const dir = d.direction || 1;
      // server position (m along the track), extrapolated by time since the message
      const since = (performance.now() - c.recv) / 1000;
      const f = dir === 1 ? d.frac : 1 - d.frac;
      const v = (d.speed_est != null ? d.speed_est : d.speed) / 1000;     // m/s, model-based between reports
      let target = g.distance(d.index, f) + dir * v * Math.min(since, 0.3);
      const laneT = (dir === 1 ? d.offset : -d.offset) / 1000;
      if (c.s === null) { c.s = target; c.lane = laneT; }
      // advance by our own speed estimate, then ease toward the server value (wrap-aware)
      if (d.frac < 0.985) c.s += dir * v * dt;   // held at a piece end: do not run ahead
      if (!g.closed) target = Math.min(target, g.total - 1e-4);
      let err = target - c.s;
      if (g.closed) err -= Math.round(err / g.total) * g.total;
      if (Math.abs(err) > 0.6) c.s = target; else c.s += err * Math.min(1, dt * 6);
      c.lane += (laneT - c.lane) * Math.min(1, dt * 5);
      const p = g.at(c.s, c.lane);
      const p2 = g.at(c.s + dir * 0.02, c.lane);
      c.mesh.position.set(p.x, d.delocalized ? 0.04 : 0, p.z);
      c.mesh.rotation.y = -Math.atan2(p2.z - p.z, p2.x - p.x);
      if (c.crown) c.crown.rotation.y += dt * 2;
      const disabled = this.game && this.game.players && this.game.players.find((x) => x.address === d.address && x.disabled);
      c.mesh.userData.shell.material.emissiveIntensity = disabled ? 0.1 + 0.4 * (Math.sin(performance.now() / 80) > 0) : 0.55;
    }
    for (const o of [...this.fxGroup.children]) {
      if (o.userData.ttl === undefined) continue;
      o.userData.ttl -= dt;
      if (o.material) o.material.opacity = Math.max(0, o.userData.ttl / 0.35);
      if (o.userData.grow) o.scale.multiplyScalar(1 + dt * 4);
      if (o.userData.ttl <= 0) { this.fxGroup.remove(o); o.geometry.dispose(); }
    }
    if (this.camMode === "chase") {
      const c = this.cars.get(this.follow) || [...this.cars.values()].find((x) => x.mesh.visible);
      if (c) {
        const yaw = c.mesh.rotation.y + this.world.rotation.y;
        const at = c.mesh.getWorldPosition(new THREE.Vector3());
        const back = new THREE.Vector3(-Math.cos(yaw), 0, Math.sin(yaw));
        const want = at.clone().addScaledVector(back, 0.45).setY(0.22);
        this.camera.position.lerp(want, Math.min(1, dt * 4));
        this.camera.lookAt(at.clone().addScaledVector(back, -0.3).setY(0.03));
      }
    } else {
      if (this.camGoal) {                    // smooth camera moves (no jumps while the track grows)
        const k = Math.min(1, dt * 2.5);
        this.camera.position.lerp(this.camGoal.pos, k);
        this.controls.target.lerp(this.camGoal.target, k);
        if (this.camera.position.distanceTo(this.camGoal.pos) < 0.003 && this.controls.target.distanceTo(this.camGoal.target) < 0.003) this.camGoal = null;
      }
      if (this.hl) this.hl.material.opacity = 0.25 + 0.2 * Math.sin(performance.now() / 150);
      this.controls.update();
    }
    this.renderer.render(this.scene, this.camera);
    this.labels.render(this.scene, this.camera);
  }
}
