import { useEffect, useMemo, useRef, useState } from 'react';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { useTranslation } from 'react-i18next';
import { Play, Pause } from 'lucide-react';
import {
  parseGcodeMotion,
  motionBounds,
  totalDurationMs,
  type MotionSegment,
  type Vec3,
} from '../utils/gcodeMotion';
import { getPrinterGeometry, type PrinterGeometry } from '../utils/printerGeometry';

interface GcodeMotionPreviewProps {
  gcode: string;
  printerModel?: string;
  className?: string;
}

const SPEEDS = [1, 2, 4, 8];

/** World frame is fixed to the printer chassis: +X right, +Y up (gcode z),
 * +Z toward the user standing in front of the machine.
 *
 * Plate-relative points are expressed in bed-local coordinates as
 * (x, height, -y): gcode y increases toward the BACK of the plate, i.e.
 * toward -Z. On a bed-slinger the nozzle stays at world Z = 0 and the bed
 * group translates to +Z by the gcode y value, so the nozzle is always over
 * plate coordinate y — the bed physically slides toward the user as y grows,
 * which is what an A1/A1 mini does. On CoreXY the bed group is static at
 * +Z = buildVolume.y (plate front at the user side) and the head does the
 * front-back travel instead. */
function bedLocal(p: Vec3): THREE.Vector3 {
  return new THREE.Vector3(p.x, p.z, -p.y);
}

function formatTime(ms: number): string {
  const totalSec = Math.max(0, Math.round(ms / 1000));
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return `${m}:${s.toString().padStart(2, '0')}`;
}

/** Interpolate the tool position at time `tMs` along the segment timeline. */
function positionAt(segments: MotionSegment[], tMs: number): Vec3 {
  if (segments.length === 0) return { x: 0, y: 0, z: 0 };
  let acc = 0;
  for (const seg of segments) {
    if (tMs <= acc + seg.durationMs || seg === segments[segments.length - 1]) {
      const local = seg.durationMs > 0 ? Math.min(1, Math.max(0, (tMs - acc) / seg.durationMs)) : 1;
      return {
        x: seg.from.x + (seg.to.x - seg.from.x) * local,
        y: seg.from.y + (seg.to.y - seg.from.y) * local,
        z: seg.from.z + (seg.to.z - seg.from.z) * local,
      };
    }
    acc += seg.durationMs;
  }
  const last = segments[segments.length - 1];
  return { ...last.to };
}

interface SceneRefs {
  renderer: THREE.WebGLRenderer;
  scene: THREE.Scene;
  camera: THREE.PerspectiveCamera;
  controls: OrbitControls;
  bed: THREE.Object3D;
  beam: THREE.Object3D;
  toolhead: THREE.Object3D;
  trailGeom: THREE.BufferGeometry;
  trailPositions: Float32Array;
  geometry: PrinterGeometry;
}

export function GcodeMotionPreview({ gcode, printerModel, className = '' }: GcodeMotionPreviewProps) {
  const { t } = useTranslation();
  const mountRef = useRef<HTMLDivElement>(null);
  const sceneRef = useRef<SceneRefs | null>(null);
  const rafRef = useRef<number | null>(null);

  const geometry = useMemo(() => getPrinterGeometry(printerModel), [printerModel]);
  const segments = useMemo(() => parseGcodeMotion(gcode), [gcode]);
  const total = useMemo(() => totalDurationMs(segments), [segments]);
  const bounds = useMemo(() => motionBounds(segments), [segments]);

  // Physical bed overhang: on a bed-slinger the bed body moves by -y in world Z.
  // Front overhang = how far the bed's front edge extends beyond the frame at
  // max travel; back overhang similarly. For corexy the bed doesn't translate.
  const overhang = useMemo(() => {
    if (geometry.kinematics !== 'bedslinger') return null;
    // Bed centre nominal at gcode y = bedSize/2 (bed centred under build volume).
    // World Z of bed centre = -(y). Bed plate half-depth = bedSize.y / 2.
    // Overhang beyond the printer footprint. At the rest position the bed is
    // centred (gcode y = buildVolume.y/2); front/back overhang measure how far
    // the bed body swings past that rest footprint at the motion extremes.
    // Gcode y+ physically slides the bed FORWARD (toward the user), so the
    // max y in the motion sets the front excursion and the min y the back.
    const restY = geometry.buildVolume.y / 2;
    const front = Math.max(0, bounds.max.y - restY);
    const back = Math.max(0, restY - bounds.min.y);
    return {
      front: Math.round(front),
      back: Math.round(back),
    };
  }, [geometry, bounds]);

  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [elapsed, setElapsed] = useState(0);

  // Refs mirror state for the RAF loop without re-subscribing.
  const playingRef = useRef(playing);
  const speedRef = useRef(speed);
  const elapsedRef = useRef(elapsed);
  playingRef.current = playing;
  speedRef.current = speed;
  elapsedRef.current = elapsed;

  // ---- Scene setup (once, plus rebuild when geometry/segments change) ----
  useEffect(() => {
    const mount = mountRef.current;
    if (!mount) return;

    const width = mount.clientWidth || 480;
    const height = mount.clientHeight || 320;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x1a1a1a);

    const bv = geometry.buildVolume;
    const maxDim = Math.max(bv.x, bv.y, bv.z);

    const camera = new THREE.PerspectiveCamera(45, width / height, 1, maxDim * 20);

    // World extent of the bed plate over the whole motion (see bedLocal):
    // bedslinger — plate centred at local -bv.y/2, group translated by +y;
    // corexy — plate static with the group at +bv.y.
    const bedHalf = geometry.bedSize.y / 2;
    const isBedslinger = geometry.kinematics === 'bedslinger';
    const plateZMin = isBedslinger ? bounds.min.y - bv.y / 2 - bedHalf : bv.y / 2 - bedHalf;
    const plateZMax = isBedslinger ? bounds.max.y - bv.y / 2 + bedHalf : bv.y / 2 + bedHalf;

    // Target: centre of the union of the build volume and the bed sweep.
    const targetX = bv.x / 2;
    const targetY = bv.z / 2;
    const targetZ = (plateZMin + plateZMax) / 2;
    const target = new THREE.Vector3(targetX, targetY, targetZ);
    const zSweepMax = plateZMax - plateZMin;

    // Front-facing view: stand in front of the machine (+worldZ, where the bed
    // slides out toward the user), elevated ~30 degrees above the bed plane,
    // looking slightly down at the build-volume centre.
    const frameRadius = Math.max(maxDim, zSweepMax) / 2;
    const elevationDeg = 30;
    const elevation = (elevationDeg * Math.PI) / 180;
    // ~45° vertical FOV: distance ≈ radius / tan(fov/2), plus a small margin.
    const dist = (frameRadius / Math.tan((camera.fov * Math.PI) / 360)) * 1.25;
    camera.position.set(
      targetX,
      targetY + dist * Math.sin(elevation),
      targetZ + dist * Math.cos(elevation),
    );
    camera.lookAt(target);

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setSize(width, height);
    renderer.setPixelRatio(window.devicePixelRatio);
    mount.appendChild(renderer.domElement);

    // Full mouse orbit: rotate + zoom + pan all enabled (defaults, set explicitly).
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.05;
    controls.enableRotate = true;
    controls.enableZoom = true;
    controls.enablePan = true;
    controls.target.copy(target);
    controls.update();

    scene.add(new THREE.AmbientLight(0xffffff, 0.7));
    const dir = new THREE.DirectionalLight(0xffffff, 0.7);
    dir.position.set(1, 2, 1).multiplyScalar(maxDim);
    scene.add(dir);

    // --- Bed group: plate, sheet, build-volume wireframe, nozzle trail ---
    // Children live in bed-local coordinates (see bedLocal): the plate is
    // centred at local (bv.x/2, ·, -bv.y/2) so plate coordinate y=0 sits at
    // local z=0. The printable volume and the traced path belong to the
    // plate, so they ride along when the bed slides.
    const bedGroup = new THREE.Group();
    const bedPlate = new THREE.Mesh(
      new THREE.BoxGeometry(geometry.bedSize.x, 4, geometry.bedSize.y),
      new THREE.MeshStandardMaterial({ color: 0x333844 }),
    );
    bedPlate.position.set(bv.x / 2, -2, -bv.y / 2);
    bedGroup.add(bedPlate);
    const sheet = new THREE.Mesh(
      new THREE.BoxGeometry(geometry.bedSize.x - 8, 1.5, geometry.bedSize.y - 8),
      new THREE.MeshStandardMaterial({ color: 0x6b7280 }),
    );
    sheet.position.set(bv.x / 2, 0.75, -bv.y / 2);
    bedGroup.add(sheet);

    const bvGeom = new THREE.BoxGeometry(bv.x, bv.z, bv.y);
    const bvEdges = new THREE.LineSegments(
      new THREE.EdgesGeometry(bvGeom),
      new THREE.LineBasicMaterial({ color: 0x00ae42 }),
    );
    bvEdges.position.set(bv.x / 2, bv.z / 2, -bv.y / 2);
    bedGroup.add(bvEdges);

    // Rest pose: bedslinger centred under the gantry; corexy static.
    bedGroup.position.z = isBedslinger ? bv.y / 2 : bv.y;
    scene.add(bedGroup);

    // --- Gantry crossbeam (moves along Z / world Y) ---
    const beam = new THREE.Mesh(
      new THREE.BoxGeometry(bv.x + 40, 12, 16),
      new THREE.MeshStandardMaterial({ color: 0x4b5563 }),
    );
    scene.add(beam);

    // --- Toolhead block on the beam (moves along X) + nozzle indicator ---
    const toolhead = new THREE.Group();
    const block = new THREE.Mesh(
      new THREE.BoxGeometry(24, 24, 24),
      new THREE.MeshStandardMaterial({ color: 0xd97706 }),
    );
    block.position.y = 14;
    toolhead.add(block);
    const nozzle = new THREE.Mesh(
      new THREE.ConeGeometry(4, 10, 16),
      new THREE.MeshStandardMaterial({ color: 0xfbbf24 }),
    );
    nozzle.rotation.x = Math.PI; // point down
    nozzle.position.y = -3;
    toolhead.add(nozzle);
    scene.add(toolhead);

    // --- Bed sweep envelope (translucent box over full physical excursion) ---
    if (isBedslinger) {
      const env = new THREE.Mesh(
        new THREE.BoxGeometry(geometry.bedSize.x, 6, plateZMax - plateZMin),
        new THREE.MeshBasicMaterial({
          color: 0x38bdf8,
          transparent: true,
          opacity: 0.15,
          depthWrite: false,
        }),
      );
      env.position.set(bv.x / 2, 0, (plateZMin + plateZMax) / 2);
      scene.add(env);
    }

    // --- Nozzle trail over the plate (polyline, grown incrementally) ---
    const maxTrailPoints = 4096;
    const trailPositions = new Float32Array(maxTrailPoints * 3);
    const trailGeom = new THREE.BufferGeometry();
    trailGeom.setAttribute('position', new THREE.BufferAttribute(trailPositions, 3));
    trailGeom.setDrawRange(0, 0);
    const trail = new THREE.Line(
      trailGeom,
      new THREE.LineBasicMaterial({ color: 0xfbbf24 }),
    );
    bedGroup.add(trail);

    sceneRef.current = {
      renderer,
      scene,
      camera,
      controls,
      bed: bedGroup,
      beam,
      toolhead,
      trailGeom,
      trailPositions,
      geometry,
    };

    // Handle resize
    const onResize = () => {
      const w = mount.clientWidth || width;
      const h = mount.clientHeight || height;
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      renderer.setSize(w, h);
    };
    window.addEventListener('resize', onResize);

    return () => {
      window.removeEventListener('resize', onResize);
      controls.dispose();
      renderer.dispose();
      if (renderer.domElement.parentNode === mount) {
        mount.removeChild(renderer.domElement);
      }
      scene.traverse((obj) => {
        const mesh = obj as THREE.Mesh;
        if (mesh.geometry) mesh.geometry.dispose();
        const mat = mesh.material;
        if (Array.isArray(mat)) mat.forEach((m) => m.dispose());
        else if (mat) (mat as THREE.Material).dispose();
      });
      sceneRef.current = null;
    };
    // Rebuild when the physical scene inputs change.
  }, [geometry, bounds]);

  // ---- Apply a tool position to the scene bodies at gcode coords ----
  const applyPosition = (p: Vec3) => {
    const refs = sceneRef.current;
    if (!refs) return;
    const bv = refs.geometry.buildVolume;

    if (refs.geometry.kinematics === 'bedslinger') {
      // The gantry never travels front-back: head at world Z = 0, the BED
      // slides to +Z (toward the user) by the gcode y value so the nozzle is
      // over plate coordinate y. Head X and beam height do the rest.
      refs.toolhead.position.set(p.x, p.z, 0);
      refs.beam.position.set(bv.x / 2, p.z, 0);
      refs.bed.position.z = p.y;
    } else {
      // corexy: bed static (plate front at world Z = bv.y); the head does the
      // front-back travel, y+ moving away from the user.
      const headZ = bv.y - p.y;
      refs.toolhead.position.set(p.x, p.z, headZ);
      refs.beam.position.set(bv.x / 2, p.z, headZ);
      refs.bed.position.z = bv.y;
    }
  };

  // ---- Rebuild trail up to time tMs ----
  const rebuildTrail = (tMs: number) => {
    const refs = sceneRef.current;
    if (!refs) return;
    const positions = refs.trailPositions;
    const maxPoints = positions.length / 3;
    let count = 0;
    let acc = 0;

    // Trail points are bed-local (the trail is a child of the bed group), so
    // the traced path shows where the nozzle travelled over the plate.
    const push = (p: Vec3) => {
      if (count >= maxPoints) return;
      const w = bedLocal(p);
      positions[count * 3] = w.x;
      positions[count * 3 + 1] = w.y;
      positions[count * 3 + 2] = w.z;
      count++;
    };

    if (segments.length > 0) push(segments[0].from);
    for (const seg of segments) {
      if (acc + seg.durationMs <= tMs) {
        push(seg.to);
        acc += seg.durationMs;
      } else {
        // Partial segment: interpolate to current point.
        push(positionAt(segments, tMs));
        break;
      }
    }

    refs.trailGeom.setDrawRange(0, count);
    (refs.trailGeom.attributes.position as THREE.BufferAttribute).needsUpdate = true;
    refs.trailGeom.computeBoundingSphere();
  };

  // ---- Animation loop ----
  useEffect(() => {
    let last = performance.now();

    const loop = (now: number) => {
      rafRef.current = requestAnimationFrame(loop);
      const refs = sceneRef.current;
      if (!refs) return;

      const dt = now - last;
      last = now;

      if (playingRef.current && total > 0) {
        let next = elapsedRef.current + dt * speedRef.current;
        if (next >= total) {
          next = total;
          setPlaying(false);
        }
        elapsedRef.current = next;
        setElapsed(next);
      }

      const p = positionAt(segments, elapsedRef.current);
      applyPosition(p);
      rebuildTrail(elapsedRef.current);

      refs.controls.update();
      refs.renderer.render(refs.scene, refs.camera);
    };

    rafRef.current = requestAnimationFrame(loop);
    return () => {
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [segments, total, geometry]);

  const handleScrub = (v: number) => {
    setPlaying(false);
    elapsedRef.current = v;
    setElapsed(v);
  };

  const togglePlay = () => {
    if (elapsedRef.current >= total) {
      elapsedRef.current = 0;
      setElapsed(0);
    }
    setPlaying((p) => !p);
  };

  return (
    <div className={`flex flex-col gap-2 ${className}`}>
      <div
        ref={mountRef}
        className="relative w-full rounded-lg overflow-hidden bg-[#1a1a1a] border border-gray-700"
        style={{ height: 320 }}
      >
        {/* Overhang / clearance overlay */}
        <div className="absolute top-2 left-2 rounded bg-black/60 px-2 py-1 text-xs text-gray-200 space-y-0.5 pointer-events-none">
          {geometry.kinematics === 'bedslinger' && overhang ? (
            <>
              <div>
                {t('gcodeMotion.frontOverhang', 'Front overhang')}: {overhang.front} mm
              </div>
              <div>
                {t('gcodeMotion.backOverhang', 'Back overhang')}: {overhang.back} mm
              </div>
            </>
          ) : (
            <div>{t('gcodeMotion.corexyNoSwing', 'CoreXY: bed does not swing (Z only)')}</div>
          )}
          {!geometry.known && (
            <div className="text-amber-400">
              {t('gcodeMotion.unknownGeometry', 'Geometry unknown — using generic 256mm printer')}
            </div>
          )}
        </div>
      </div>

      {/* Playback controls */}
      <div className="flex items-center gap-2 text-sm text-gray-200">
        <button
          type="button"
          onClick={togglePlay}
          className="flex items-center justify-center w-8 h-8 rounded bg-gray-700 hover:bg-gray-600 text-white"
          aria-label={playing ? t('gcodeMotion.pause', 'Pause') : t('gcodeMotion.play', 'Play')}
        >
          {playing ? <Pause size={16} /> : <Play size={16} />}
        </button>

        <input
          type="range"
          min={0}
          max={Math.max(1, total)}
          step={10}
          value={Math.min(elapsed, total)}
          onChange={(e) => handleScrub(Number(e.target.value))}
          className="flex-1 accent-green-500"
          aria-label={t('gcodeMotion.scrubber', 'Timeline')}
        />

        <span className="tabular-nums text-xs text-gray-400 w-20 text-right">
          {formatTime(elapsed)} / {formatTime(total)}
        </span>

        <select
          value={speed}
          onChange={(e) => setSpeed(Number(e.target.value))}
          className="bg-gray-700 text-white text-xs rounded px-1 py-1 border border-gray-600"
          aria-label={t('gcodeMotion.speed', 'Speed')}
        >
          {SPEEDS.map((s) => (
            <option key={s} value={s}>
              {s}×
            </option>
          ))}
        </select>
      </div>

      <p className="text-xs text-gray-500">
        {t(
          'gcodeMotion.accelDisclaimer',
          'Durations ignore acceleration, so real motion is slower than shown.',
        )}
      </p>
    </div>
  );
}

export default GcodeMotionPreview;
