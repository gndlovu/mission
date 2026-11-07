/* Ambient constellation field — the drifting particle background from
   gladwelln.dev, rebuilt in plain canvas 2D (no Three.js, no deps).
   Deliberately cheap: ~55 nodes, capped DPR, pauses when the tab is hidden
   and when the user prefers reduced motion. */
(() => {
  const cv = document.getElementById("bgfx");
  if (!cv) return;
  const ctx = cv.getContext("2d", { alpha: true });
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

  const CYAN = [41, 245, 255];
  const VIOLET = [168, 85, 247];
  const LINK_DIST = 175;

  let w = 0, h = 0, dpr = 1, nodes = [], raf = null;

  function size() {
    dpr = Math.min(devicePixelRatio || 1, 2);
    w = innerWidth; h = innerHeight;
    cv.width = Math.floor(w * dpr);
    cv.height = Math.floor(h * dpr);
    cv.style.width = w + "px";
    cv.style.height = h + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function seed() {
    const count = Math.max(34, Math.min(70, Math.round((w * h) / 24000)));
    nodes = Array.from({ length: count }, () => ({
      x: Math.random() * w,
      y: Math.random() * h,
      vx: (Math.random() - 0.5) * 0.17,
      vy: (Math.random() - 0.5) * 0.17,
      s: Math.random() * 2.6 + 2.0,
      c: Math.random() > 0.55 ? VIOLET : CYAN,
    }));
  }

  function draw() {
    ctx.clearRect(0, 0, w, h);

    // links first, so dots sit on top
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j];
        const dx = a.x - b.x, dy = a.y - b.y;
        const d = Math.hypot(dx, dy);
        if (d > LINK_DIST) continue;
        const t = 1 - d / LINK_DIST;
        ctx.strokeStyle = `rgba(41, 245, 255, ${(t * 0.17).toFixed(3)})`;
        ctx.lineWidth = 0.7;
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
      }
    }

    for (const n of nodes) {
      ctx.fillStyle = `rgba(${n.c[0]}, ${n.c[1]}, ${n.c[2]}, 0.62)`;
      ctx.fillRect(n.x - n.s / 2, n.y - n.s / 2, n.s, n.s);
    }
  }

  function step() {
    for (const n of nodes) {
      n.x += n.vx; n.y += n.vy;
      if (n.x < -20) n.x = w + 20; else if (n.x > w + 20) n.x = -20;
      if (n.y < -20) n.y = h + 20; else if (n.y > h + 20) n.y = -20;
    }
    draw();
    raf = requestAnimationFrame(step);
  }

  function start() { if (raf == null && !reduce) raf = requestAnimationFrame(step); }
  function stop() { if (raf != null) { cancelAnimationFrame(raf); raf = null; } }

  size(); seed(); draw();
  if (!reduce) start();

  let rt = null;
  addEventListener("resize", () => {
    clearTimeout(rt);
    rt = setTimeout(() => { size(); seed(); draw(); }, 160);
  });
  document.addEventListener("visibilitychange", () => {
    document.hidden ? stop() : start();
  });
})();
