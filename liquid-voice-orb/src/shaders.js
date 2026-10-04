// ─────────────────────────────────────────────────────────────────────────────
// GLSL for the liquid-frequency orb.
// The same displacement function drives BOTH the glass shell and the particles,
// so the particles always sit exactly on the liquid surface.
// ─────────────────────────────────────────────────────────────────────────────

// 3D simplex noise (Ashima Arts / Ian McEwan, MIT)
export const NOISE = /* glsl */ `
vec3 mod289(vec3 x){ return x - floor(x * (1.0 / 289.0)) * 289.0; }
vec4 mod289(vec4 x){ return x - floor(x * (1.0 / 289.0)) * 289.0; }
vec4 permute(vec4 x){ return mod289(((x * 34.0) + 1.0) * x); }
vec4 taylorInvSqrt(vec4 r){ return 1.79284291400159 - 0.85373472095314 * r; }

float snoise(vec3 v){
  const vec2 C = vec2(1.0 / 6.0, 1.0 / 3.0);
  const vec4 D = vec4(0.0, 0.5, 1.0, 2.0);

  vec3 i  = floor(v + dot(v, C.yyy));
  vec3 x0 = v - i + dot(i, C.xxx);

  vec3 g  = step(x0.yzx, x0.xyz);
  vec3 l  = 1.0 - g;
  vec3 i1 = min(g.xyz, l.zxy);
  vec3 i2 = max(g.xyz, l.zxy);

  vec3 x1 = x0 - i1 + C.xxx;
  vec3 x2 = x0 - i2 + C.yyy;
  vec3 x3 = x0 - D.yyy;

  i = mod289(i);
  vec4 p = permute(permute(permute(
            i.z + vec4(0.0, i1.z, i2.z, 1.0))
          + i.y + vec4(0.0, i1.y, i2.y, 1.0))
          + i.x + vec4(0.0, i1.x, i2.x, 1.0));

  float n_ = 0.142857142857;
  vec3 ns = n_ * D.wyz - D.xzx;

  vec4 j  = p - 49.0 * floor(p * ns.z * ns.z);
  vec4 x_ = floor(j * ns.z);
  vec4 y_ = floor(j - 7.0 * x_);

  vec4 x = x_ * ns.x + ns.yyyy;
  vec4 y = y_ * ns.x + ns.yyyy;
  vec4 h = 1.0 - abs(x) - abs(y);

  vec4 b0 = vec4(x.xy, y.xy);
  vec4 b1 = vec4(x.zw, y.zw);

  vec4 s0 = floor(b0) * 2.0 + 1.0;
  vec4 s1 = floor(b1) * 2.0 + 1.0;
  vec4 sh = -step(h, vec4(0.0));

  vec4 a0 = b0.xzyw + s0.xzyw * sh.xxyy;
  vec4 a1 = b1.xzyw + s1.xzyw * sh.zzww;

  vec3 p0 = vec3(a0.xy, h.x);
  vec3 p1 = vec3(a0.zw, h.y);
  vec3 p2 = vec3(a1.xy, h.z);
  vec3 p3 = vec3(a1.zw, h.w);

  vec4 norm = taylorInvSqrt(vec4(dot(p0, p0), dot(p1, p1), dot(p2, p2), dot(p3, p3)));
  p0 *= norm.x; p1 *= norm.y; p2 *= norm.z; p3 *= norm.w;

  vec4 m = max(0.6 - vec4(dot(x0, x0), dot(x1, x1), dot(x2, x2), dot(x3, x3)), 0.0);
  m = m * m;
  return 42.0 * dot(m * m, vec4(dot(p0, x0), dot(p1, x1), dot(p2, x2), dot(p3, x3)));
}
`;

// Surface displacement + analytic-ish normals (finite differences on the sphere).
export const SURFACE = /* glsl */ `
uniform float uTime;
uniform float uLevel;      // overall loudness      0..1
uniform float uBass;       // low band              0..1  -> broad, slow swells
uniform float uMid;        // mid band              0..1  -> medium waves
uniform float uTreble;     // high band             0..1  -> fine, fast ripples
uniform float uHist[32];   // recent loudness, [0] = newest -> travelling waves
uniform float uHistMean;
uniform vec3  uSrc;        // where the waves are born on the surface
uniform float uInset;      // shell sits fractionally under the particles

float histAt(float x){
  float f  = clamp(x, 0.0, 1.0) * 31.0;
  int   i0 = int(floor(f));
  int   i1 = min(i0 + 1, 31);
  return mix(uHist[i0], uHist[i1], f - float(i0));
}

// p = unit-sphere direction. Returns radial offset (radius = 1 + disp).
float disp(vec3 p){
  float t = uTime;

  // Idle: slow floating, breathing water-blob.
  float idle = snoise(p * 0.80 + vec3(0.0, t * 0.08, t * 0.06)) * 0.050
             + snoise(p * 1.50 - vec3(t * 0.06, 0.0, t * 0.04)) * 0.016;

  // Voice bands: broad, slow swells like a drop of water, only a faint fine ripple on top.
  float bass = snoise(p * 0.70 + vec3(t * 0.18, 0.0, -t * 0.14)) * uBass   * 0.22;
  float mid  = snoise(p * 1.50 + vec3(-t * 0.38, t * 0.28, 0.0)) * uMid    * 0.09;
  float treb = snoise(p * 3.20 + vec3(t * 0.90, -t * 0.70, t * 0.6)) * uTreble * 0.025;

  // Changes in speech travel across the body like waves through water:
  // angular distance from the source = how far back in time we look.
  float a      = acos(clamp(dot(p, uSrc), -1.0, 1.0)) / 3.14159265;
  float h      = histAt(a);
  float ripple = 0.75 + 0.25 * sin(a * 9.0 - t * 2.0);
  float wave   = (h - uHistMean) * 0.18 * ripple;   // above average = bulge, below = contract

  float breathe = uLevel * 0.035;

  return idle + bass + mid + treb + wave + breathe - uInset;
}

vec3 displaced(vec3 p){ return p * (1.0 + disp(p)); }

void surface(vec3 p, out vec3 pos, out vec3 nrm){
  pos = displaced(p);
  vec3 up = abs(p.y) < 0.99 ? vec3(0.0, 1.0, 0.0) : vec3(1.0, 0.0, 0.0);
  vec3 t1 = normalize(cross(up, p));
  vec3 t2 = normalize(cross(p, t1));
  float e = 0.02;
  vec3 pa = displaced(normalize(p + t1 * e));
  vec3 pb = displaced(normalize(p + t2 * e));
  nrm = normalize(cross(pa - pos, pb - pos));
  if (dot(nrm, p) < 0.0) nrm = -nrm;
}
`;

// ── Glass shell ──────────────────────────────────────────────────────────────
export const SHELL_VERT = /* glsl */ `
${NOISE}
${SURFACE}

varying vec3  vNormal;
varying vec3  vView;
varying float vDisp;

void main(){
  vec3 p = normalize(position);
  vec3 pos, nrm;
  surface(p, pos, nrm);

  vec4 mv = modelViewMatrix * vec4(pos, 1.0);
  vNormal = normalMatrix * nrm;
  vView   = -mv.xyz;
  vDisp   = length(pos) - 1.0;
  gl_Position = projectionMatrix * mv;
}
`;

export const SHELL_FRAG = /* glsl */ `
uniform float uTime;
uniform float uLevel;
uniform float uGlow;
uniform vec3  uDeep;
uniform vec3  uElectric;
uniform vec3  uViolet;
uniform vec3  uIce;

varying vec3  vNormal;
varying vec3  vView;
varying float vDisp;

void main(){
  vec3  n   = normalize(vNormal);
  vec3  v   = normalize(vView);
  float ndv = dot(n, v);
  bool  back = ndv < 0.0;

  float fres = pow(1.0 - clamp(abs(ndv), 0.0, 1.0), 2.6);
  vec3  r    = reflect(-v, n);
  float up   = n.y * 0.5 + 0.5;

  // soft studio highlights
  vec3 L1 = normalize(vec3(-0.55,  0.80, 0.65));
  vec3 L2 = normalize(vec3( 0.75, -0.25, 0.55));
  vec3 L3 = normalize(vec3( 0.10,  0.95, 0.20));
  float s1 = pow(max(dot(n, normalize(L1 + v)), 0.0), 90.0);
  float s2 = pow(max(dot(n, normalize(L2 + v)), 0.0), 50.0);
  float s3 = pow(max(dot(n, normalize(L3 + v)), 0.0), 140.0);

  // liquid-glass refraction streaks, warped by the live surface displacement
  float streak = pow(0.5 + 0.5 * sin(dot(r, vec3(3.0, 6.0, 2.0)) + vDisp * 6.0 + uTime * 0.18), 7.0);
  float env    = smoothstep(-0.2, 1.0, r.y);

  vec3 col = uDeep * 0.40;
  col += uElectric * (fres * 0.95 + env * 0.22);
  col += uViolet   * fres * smoothstep(0.55, 0.0, up) * 0.9;
  col += uIce      * (s1 * 1.3 + s2 * 0.7 + s3 * 0.9 + streak * fres * 0.55);
  col *= uGlow;

  float alpha = (0.10 + fres * 0.55) * (back ? 0.55 : 1.0);
  alpha = clamp(alpha + (s1 + s2 * 0.6 + s3) * 0.8, 0.0, 1.0);

  gl_FragColor = vec4(col, alpha);
}
`;

// ── Surface particles ────────────────────────────────────────────────────────
export const POINTS_VERT = /* glsl */ `
${NOISE}
${SURFACE}

attribute float aPhase;

uniform float uSize;
uniform float uPixelRatio;

varying vec3  vNormal;
varying vec3  vView;
varying float vDisp;

void main(){
  vec3 p = normalize(position);
  vec3 pos, nrm;
  surface(p, pos, nrm);

  // each particle drifts gently along the surface normal with the voice (kept tiny: more reads as shimmer)
  float vib = sin(uTime * 9.0 + aPhase * 6.2831853) * (uTreble * 0.5 + uLevel * 0.2);
  pos += nrm * vib * 0.0035;

  vec4 mv  = modelViewMatrix * vec4(pos, 1.0);
  vec3 nv  = normalize(normalMatrix * nrm);
  vec3 vv  = normalize(-mv.xyz);

  vNormal = nv;
  vView   = vv;
  vDisp   = length(pos) - 1.0;

  float facing = clamp(dot(nv, vv), 0.0, 1.0);
  gl_PointSize = uSize * uPixelRatio * (5.4 / -mv.z) * (0.65 + 0.55 * facing) * (1.0 + uLevel * 0.10);
  gl_Position  = projectionMatrix * mv;
}
`;

export const POINTS_FRAG = /* glsl */ `
uniform float uGlow;
uniform vec3  uDeep;
uniform vec3  uElectric;
uniform vec3  uViolet;
uniform vec3  uIce;

varying vec3  vNormal;
varying vec3  vView;
varying float vDisp;

void main(){
  vec2  c = gl_PointCoord - 0.5;
  float d = length(c) * 2.0;
  if (d > 1.0) discard;
  float soft = 1.0 - d;
  soft = soft * soft * (3.0 - 2.0 * soft);

  vec3  n   = normalize(vNormal);
  vec3  v   = normalize(vView);
  float ndv = dot(n, v);

  float front = smoothstep(-0.25, 0.55, ndv);                 // far side is dimmer -> see-through volume
  float fres  = pow(1.0 - clamp(abs(ndv), 0.0, 1.0), 2.2);
  float up    = n.y * 0.5 + 0.5;

  vec3 L1 = normalize(vec3(-0.50, 0.80, 0.70));
  vec3 L2 = normalize(vec3( 0.70, -0.30, 0.50));
  float s1 = pow(max(dot(n, normalize(L1 + v)), 0.0), 40.0);
  float s2 = pow(max(dot(n, normalize(L2 + v)), 0.0), 24.0);

  // deep blue -> electric blue, violet pooling at the underside and on stretched crests
  vec3 col = mix(uDeep, uElectric, smoothstep(0.10, 0.90, up * 0.6 + fres * 0.6 + 0.2));
  float vio = smoothstep(0.45, 0.05, up) * 0.8 + smoothstep(0.08, 0.30, vDisp) * 0.35;
  col = mix(col, uViolet, clamp(vio, 0.0, 1.0) * (0.35 + fres));
  col += uIce * (s1 * 1.4 + s2 * 0.8);
  col += uIce * pow(soft, 3.0) * 0.25 * front;

  float lum = (0.35 + 0.65 * front + 0.70 * fres) * uGlow;
  gl_FragColor = vec4(col * lum, soft * (0.35 + 0.65 * front));
}
`;
