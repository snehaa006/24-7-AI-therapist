import { useEffect, useRef } from 'react';
import * as THREE from 'three';
import { EffectComposer } from 'three/examples/jsm/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/examples/jsm/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/examples/jsm/postprocessing/OutputPass.js';
import { SHELL_VERT, SHELL_FRAG, POINTS_VERT, POINTS_FRAG } from './shaders.js';

// ── Tunables ────────────────────────────────────────────────────────────────
const RINGS = 130;            // particle rings pole-to-pole (~21k particles). Lower on weak GPUs.
const HIST_STEP = 1 / 45;     // seconds between history samples (wave travel speed)
const BLOOM_BASE = 0.55;      // glow at rest
const BLOOM_VOICE = 0.22;     // extra glow while speaking (kept low so it glows, not flickers)
const BASE_DISTANCE = 5.8;    // camera distance the particle size was tuned at
const PALETTE = {
  deep: '#0a2470',
  electric: '#2e7bff',
  violet: '#9b6bff',
  ice: '#9fd8ff',
};

// Particles laid out in latitude rings → reads as fine flowing contour lines
// while still being a cloud of points (like the second reference).
function buildParticleGeometry(rings) {
  const pos = [];
  const phase = [];
  for (let i = 0; i < rings; i++) {
    const theta = ((i + 0.5) / rings) * Math.PI;
    const s = Math.sin(theta);
    const c = Math.cos(theta);
    const n = Math.max(6, Math.round(rings * 2 * s)); // equal spacing along and across rings
    const offset = (i % 2) * 0.5;
    for (let j = 0; j < n; j++) {
      const phi = ((j + offset) / n) * Math.PI * 2;
      pos.push(s * Math.cos(phi), c, s * Math.sin(phi));
      phase.push(Math.random());
    }
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  g.setAttribute('aPhase', new THREE.Float32BufferAttribute(phase, 1));
  return g;
}

/**
 * <LiquidOrb engine={audioEngine} />
 * `engine` only needs an `update(dt)` method returning { level, bass, mid, treble } in 0..1.
 * `distance` sets the camera distance (bigger = smaller orb); changes ease in smoothly.
 * `lift` moves the orb up by that fraction of the viewport height.
 * `background` must match the page colour behind the canvas.
 */
export default function LiquidOrb({ engine, className = 'orb', distance = BASE_DISTANCE, lift = 0, background = '#000000' }) {
  const hostRef = useRef(null);
  const engineRef = useRef(engine);
  engineRef.current = engine;
  const viewRef = useRef({ distance, lift });
  viewRef.current = { distance, lift };

  useEffect(() => {
    const host = hostRef.current;
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);

    // renderer + post
    const renderer = new THREE.WebGLRenderer({ antialias: false, powerPreference: 'high-performance' });
    renderer.setPixelRatio(dpr);
    // The scene renders into a linear float target that OutputPass encodes to sRGB, so the
    // clear value has to be linear for the canvas to match the CSS background.
    renderer.setClearColor(new THREE.Color(background).convertSRGBToLinear(), 1);
    host.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(32, 1, 0.1, 50);
    const view = { ...viewRef.current };
    camera.position.set(0, 0, view.distance);

    const target = new THREE.WebGLRenderTarget(1, 1, { type: THREE.HalfFloatType, samples: 4 });
    const composer = new EffectComposer(renderer, target);
    composer.addPass(new RenderPass(scene, camera));
    const bloom = new UnrealBloomPass(new THREE.Vector2(1, 1), BLOOM_BASE, 0.6, 0.1);
    composer.addPass(bloom);
    composer.addPass(new OutputPass());

    // uniforms shared by shell + particles (same {value} objects → updated once per frame)
    const hist = new Float32Array(32);
    const shared = {
      uTime: { value: 0 },
      uLevel: { value: 0 },
      uBass: { value: 0 },
      uMid: { value: 0 },
      uTreble: { value: 0 },
      uHist: { value: hist },
      uHistMean: { value: 0 },
      uSrc: { value: new THREE.Vector3(0, 1, 0) },
      uGlow: { value: 1 },
      uDeep: { value: new THREE.Color(PALETTE.deep) },
      uElectric: { value: new THREE.Color(PALETTE.electric) },
      uViolet: { value: new THREE.Color(PALETTE.violet) },
      uIce: { value: new THREE.Color(PALETTE.ice) },
    };
    const sizeUniform = { value: 2.4 };

    // glass shell
    const shellGeo = new THREE.SphereGeometry(1, 160, 120);
    const shellMat = new THREE.ShaderMaterial({
      vertexShader: SHELL_VERT,
      fragmentShader: SHELL_FRAG,
      uniforms: { ...shared, uInset: { value: 0.016 } },
      transparent: true,
      depthWrite: false,
      side: THREE.DoubleSide,
      blending: THREE.AdditiveBlending,
    });
    const shell = new THREE.Mesh(shellGeo, shellMat);
    shell.frustumCulled = false;
    shell.renderOrder = 0;

    // particles that define the surface
    const pointsGeo = buildParticleGeometry(RINGS);
    const pointsMat = new THREE.ShaderMaterial({
      vertexShader: POINTS_VERT,
      fragmentShader: POINTS_FRAG,
      uniforms: { ...shared, uInset: { value: 0 }, uSize: sizeUniform, uPixelRatio: { value: dpr } },
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
    const points = new THREE.Points(pointsGeo, pointsMat);
    points.frustumCulled = false;
    points.renderOrder = 1;

    const group = new THREE.Group();
    group.add(shell, points);
    scene.add(group);

    // sizing
    let viewW = 1;
    let viewH = 1;
    const applyView = () => {
      camera.position.z = view.distance / Math.min(1, camera.aspect);
      camera.setViewOffset(viewW, viewH, 0, view.lift * viewH, viewW, viewH);
      // smaller orb on screen → finer particles so it keeps the same texture
      const scale = Math.pow(BASE_DISTANCE / view.distance, 0.6);
      sizeUniform.value = THREE.MathUtils.clamp((viewH / 800) * 2.6, 1.5, 3.6) * scale;
    };
    const resize = () => {
      viewW = host.clientWidth || 1;
      viewH = host.clientHeight || 1;
      renderer.setSize(viewW, viewH);
      composer.setSize(viewW, viewH);
      camera.aspect = viewW / viewH;
      applyView();
    };
    const ro = new ResizeObserver(resize);
    ro.observe(host);
    resize();

    // pointer: drag to spin (with inertia), hover to tilt
    const rot = { yaw: 0, vYaw: 0, pitch: 0, tPitch: 0, dragging: false, lx: 0, ly: 0 };
    const onDown = (e) => {
      rot.dragging = true;
      rot.lx = e.clientX;
      rot.ly = e.clientY;
      host.setPointerCapture?.(e.pointerId);
    };
    const onMove = (e) => {
      if (rot.dragging) {
        const dx = e.clientX - rot.lx;
        const dy = e.clientY - rot.ly;
        rot.lx = e.clientX;
        rot.ly = e.clientY;
        rot.yaw += dx * 0.008;
        rot.vYaw = dx * 0.008 * 60;
        rot.tPitch = THREE.MathUtils.clamp(rot.tPitch + dy * 0.004, -0.7, 0.7);
      } else {
        const r = host.getBoundingClientRect();
        rot.tPitch = ((e.clientY - r.top) / r.height - 0.5) * 0.35;
      }
    };
    const onUp = (e) => {
      rot.dragging = false;
      host.releasePointerCapture?.(e.pointerId);
    };
    host.addEventListener('pointerdown', onDown);
    host.addEventListener('pointermove', onMove);
    host.addEventListener('pointerup', onUp);
    host.addEventListener('pointercancel', onUp);

    // loop
    let raf = 0;
    let last = performance.now();
    let elapsed = 0;
    let accum = 0;
    const speed = reducedMotion ? 0.45 : 1;

    const tick = (now) => {
      raf = requestAnimationFrame(tick);
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      elapsed += dt * speed;

      const b = engineRef.current?.update(dt) ?? { level: 0, bass: 0, mid: 0, treble: 0 };

      // loudness history → waves that travel across the surface
      const energy = Math.min(1, b.level * 0.9 + b.mid * 0.5 + b.bass * 0.3);
      accum += dt;
      while (accum >= HIST_STEP) {
        accum -= HIST_STEP;
        hist.copyWithin(1, 0, 31);
        hist[0] = energy;
      }
      let mean = 0;
      for (let i = 0; i < 32; i++) mean += hist[i];

      shared.uTime.value = elapsed;
      shared.uLevel.value = b.level;
      shared.uBass.value = b.bass;
      shared.uMid.value = b.mid;
      shared.uTreble.value = b.treble;
      shared.uHistMean.value = mean / 32;
      shared.uGlow.value = 0.9 + b.level * 0.22;
      shared.uSrc.value
        .set(Math.sin(elapsed * 0.11) * 0.8, 0.55 + Math.sin(elapsed * 0.07) * 0.3, Math.cos(elapsed * 0.09) * 0.8)
        .normalize();

      bloom.strength = BLOOM_BASE + b.level * BLOOM_VOICE;

      if (!rot.dragging) {
        rot.yaw += (rot.vYaw + 0.07 * speed) * dt;
        rot.vYaw *= Math.exp(-dt * 2.5);
      }
      rot.pitch += (rot.tPitch - rot.pitch) * (1 - Math.exp(-dt * 4));

      const goal = viewRef.current;
      if (Math.abs(goal.distance - view.distance) > 1e-3 || Math.abs(goal.lift - view.lift) > 1e-4) {
        const k = 1 - Math.exp(-dt * 3);
        view.distance += (goal.distance - view.distance) * k;
        view.lift += (goal.lift - view.lift) * k;
        applyView();
      }
      group.rotation.set(rot.pitch, rot.yaw, 0);

      composer.render();
    };
    raf = requestAnimationFrame(tick);

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      host.removeEventListener('pointerdown', onDown);
      host.removeEventListener('pointermove', onMove);
      host.removeEventListener('pointerup', onUp);
      host.removeEventListener('pointercancel', onUp);
      shellGeo.dispose();
      shellMat.dispose();
      pointsGeo.dispose();
      pointsMat.dispose();
      bloom.dispose();
      composer.dispose();
      target.dispose();
      renderer.dispose();
      if (renderer.domElement.parentNode === host) host.removeChild(renderer.domElement);
    };
  }, []);

  return <div ref={hostRef} className={className} aria-hidden="true" />;
}
