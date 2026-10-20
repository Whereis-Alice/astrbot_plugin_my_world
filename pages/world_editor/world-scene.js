/** Decorative, local vector scenery. Runtime labels always come from the state snapshot. */
export const WORLD_SCENE = `<svg viewBox="0 0 640 360" fill="none" aria-hidden="true" focusable="false">
  <defs>
    <linearGradient id="island-top" x1="200" y1="160" x2="470" y2="290" gradientUnits="userSpaceOnUse"><stop class="land-light"/><stop offset="1" class="land-dark"/></linearGradient>
    <linearGradient id="island-base" x1="320" y1="235" x2="320" y2="330" gradientUnits="userSpaceOnUse"><stop class="rock-light"/><stop offset="1" class="rock-dark"/></linearGradient>
    <linearGradient id="ribbon" x1="90" y1="0" x2="540" y2="200" gradientUnits="userSpaceOnUse"><stop class="ribbon-a" stop-opacity="0"/><stop offset=".5" class="ribbon-b" stop-opacity=".5"/><stop offset="1" class="ribbon-a" stop-opacity="0"/></linearGradient>
    <radialGradient id="moon-halo"><stop class="moon-fill" stop-opacity=".22"/><stop offset="1" class="moon-fill" stop-opacity="0"/></radialGradient>
  </defs>
  <g class="scene-stars" fill="currentColor"><circle cx="120" cy="74" r="1.7"/><circle cx="217" cy="39" r="1.1"/><circle cx="420" cy="37" r="1.8"/><circle cx="549" cy="129" r="1.3"/><circle cx="568" cy="58" r="2"/><circle cx="345" cy="66" r="1.1"/><path d="M174 100v10m-5-5h10M507 37v12m-6-6h12" stroke="currentColor" stroke-width="1.2"/></g>
  <g class="scene-ribbons"><path d="M40 142C170-48 300 170 595 42" stroke="url(#ribbon)" stroke-width="50"/><path d="M80 157C210-28 350 192 615 58" stroke="url(#ribbon)" stroke-width="12"/></g>
  <circle cx="450" cy="97" r="90" fill="url(#moon-halo)"/>
  <circle class="scene-moon" cx="450" cy="97" r="33"/>
  <g class="scene-sun-rings" stroke="currentColor" opacity=".25"><circle cx="450" cy="97" r="48"/><circle cx="450" cy="97" r="62"/><path d="M450 16v12m0 138v12M369 97h12m138 0h12M393 40l9 9m96 96 9 9M393 154l9-9m96-96 9-9"/></g>
  <g class="scene-clouds" stroke="currentColor" stroke-linecap="round"><path d="M363 144h127m-109 9h77M108 129h93m-65 9h98"/><path d="M456 191h95m-65 9h45"/></g>
  <g class="scene-orbits" stroke="currentColor"><ellipse cx="324" cy="239" rx="252" ry="73" transform="rotate(-13 324 239)"/><ellipse cx="324" cy="239" rx="268" ry="88" transform="rotate(-13 324 239)" opacity=".25" stroke-dasharray="2 9"/></g>
  <path d="m158 237 167-65 171 65-75 62-96 31-96-31z" fill="url(#island-base)"/>
  <path d="m158 237 167-65 171 65-171 70z" fill="url(#island-top)"/>
  <path d="m158 237 167 70 171-70M325 307v23m-57-45 19 32m91-33-17 36" class="island-seam" stroke="currentColor"/>
  <path d="m194 235 90-35 54 21-91 36z" class="scene-garden"/>
  <path d="m275 270 68-27 34 13-68 27" class="scene-path" stroke-width="9"/>
  <g class="scene-house">
    <path d="m294 183 58-24 57 24v64l-57 24-58-25z" class="house-wall"/>
    <path d="m352 159 57 24v64l-57 24z" class="house-side"/>
    <path d="m278 187 72-72 77 44-18 24-57-33-58 49z" class="house-roof"/>
    <path d="m350 115 77 44-18 24-57-33-58 49-16-12z" stroke="currentColor" opacity=".25"/>
    <path d="m380 139 10 6v-23l-10-6z" class="house-side"/>
    <path d="m315 214 18-7v30l-18 8z" class="house-door"/>
    <path d="m363 198 25 14v21l-25-14z" class="house-window"/>
    <path d="m375 205 0 21m-12-18 25 14" stroke="currentColor" opacity=".35"/>
  </g>
  <g class="scene-trees"><path d="M232 225v-59m-3 34-15-13m19 4 15-18M443 245v-57m0 24-13-12m14 2 12-10" stroke="currentColor" stroke-width="3"/><path d="m232 142-26 45h52zm0 24-33 43h66zM443 166l-22 39h44zm0 21-28 38h56z" fill="currentColor"/></g>
  <g class="scene-bamboo" stroke="currentColor" stroke-width="2"><path d="m160 270 11-119m-5 48-22-25m24 13 24-23M515 250l-8-99m4 42 22-25m-24 7-19-19"/><path d="m167 198-24-7 10-10zm2-11 23-23-5 20zM510 192l23-24-3 20zm-2-18-18-18 3 19z" fill="currentColor"/></g>
  <g class="scene-petals" fill="currentColor"><path d="M180 68q22-4 11 14-14 4-11-14M522 180q20-5 12 12-15 6-12-12M272 117q18 3 5 14-13-2-5-14M466 279q17-6 11 10-12 5-11-10M396 55q15 1 6 13-12-1-6-13"/></g>
  <g class="scene-orbit-dots"><circle cx="88" cy="258" r="5"/><circle cx="555" cy="198" r="4"/><circle cx="482" cy="296" r="2.5"/></g>
</svg>`;

export function sceneMarkup(namespace) {
  return WORLD_SCENE.replace(/id="([^"]+)"/g, `id="${namespace}-$1"`)
    .replace(/url\(#([^)]+)\)/g, `url(#${namespace}-$1)`);
}

export function mountWorldScene() {
  const host = document.querySelector(".world-illustration");
  if (host) host.innerHTML = sceneMarkup("world");
  const sceneWindow = document.querySelector(".world-window");
  if (!sceneWindow) return;
  const motion = matchMedia("(prefers-reduced-motion: reduce)");
  const pointer = matchMedia("(hover: hover) and (pointer: fine)");
  const reset = () => {
    sceneWindow.style.removeProperty("--scene-shift-x");
    sceneWindow.style.removeProperty("--scene-shift-y");
  };
  sceneWindow.addEventListener("pointermove", event => {
    if (motion.matches || !pointer.matches) return;
    const rect = sceneWindow.getBoundingClientRect();
    sceneWindow.style.setProperty("--scene-shift-x", `${(event.clientX - rect.left - rect.width / 2) * .012}px`);
    sceneWindow.style.setProperty("--scene-shift-y", `${(event.clientY - rect.top - rect.height / 2) * .012}px`);
  });
  sceneWindow.addEventListener("pointerleave", reset);
  motion.addEventListener("change", reset);
}

export function updateWorldScene(data, stateLabel = "") {
  const text = (id, value) => { const node = document.getElementById(id); if (node) node.textContent = value; };
  text("scene-state", data ? (stateLabel || data.state || "生活中") : "等待会话");
  text("scene-clock", data ? (String(data.clock_text || "").replace(/^现在是：/, "") || "她的此刻") : "从一个会话，开始一个世界");
  document.querySelector(".world-window")?.classList.toggle("is-sleeping", Boolean(data?.night?.sleeping));
}
