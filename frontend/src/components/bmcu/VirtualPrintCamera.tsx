import { useEffect, useRef, useState } from 'react';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { getAuthToken } from '../../api/client';
import { makeToolpath, type ToolpathHandle, type SegmentData } from '../../lib/vendor/toolpathRenderer.js';
import type { CameraResult } from './virtualPrintCamera.worker';

interface Props {
  gcodeUrl: string;
  reportedLayer: number;
  toolColor?: (tool: number) => string;
}

function createView(host: HTMLDivElement, data: SegmentData, onLost: () => void) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  const canvas = renderer.domElement;
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(35, 1, 0.01, 10000);
  const controls = new OrbitControls(camera, canvas);
  const { min, max } = data.bbox!;
  const center = new THREE.Vector3((min[0] + max[0]) / 2, (Math.min(0, min[2]) + max[2]) / 2, -(min[1] + max[1]) / 2);
  const radius = Math.max(5, Math.hypot(max[0] - min[0], max[1] - min[1], max[2] - Math.min(0, min[2])) / 2);
  const grid = new THREE.GridHelper(radius * 3, 18, 0x516064, 0x344245);
  grid.position.set(center.x, -0.1, center.z);
  scene.add(grid);
  let handle: ToolpathHandle | undefined;
  const draw = () => renderer.render(scene, camera);
  const resize = () => {
    const width = Math.max(host.clientWidth, 1), height = Math.max(host.clientHeight, 1);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    renderer.setSize(width, height);
    draw();
  };
  const observer = new ResizeObserver(resize);
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let onScreen = false;
  let animation: number | undefined;
  const phase = { value: 0 };
  const syncAnimation = () => {
    window.clearInterval(animation);
    animation = undefined;
    if (onScreen && !document.hidden && !reducedMotion.matches && handle?.mesh.visible) {
      animation = window.setInterval(() => {
        phase.value = (phase.value + 1 / 240) % 1;
        draw();
      }, 125);
    }
  };
  const visibility = new IntersectionObserver(([entry]) => {
    onScreen = entry.isIntersecting;
    syncAnimation();
  });
  const dispose = () => {
    window.clearInterval(animation);
    visibility.disconnect();
    reducedMotion.removeEventListener('change', syncAnimation);
    document.removeEventListener('visibilitychange', syncAnimation);
    observer.disconnect();
    canvas.removeEventListener('webglcontextlost', onLost);
    controls.removeEventListener('change', draw);
    controls.dispose();
    handle?.dispose();
    grid.dispose();
    renderer.dispose();
    renderer.forceContextLoss();
    canvas.remove();
  };
  try {
    if (Math.ceil(data.nV / 2048) > renderer.capabilities.maxTextureSize) throw new Error('This plate exceeds the 3D texture capacity of this device.');
    handle = makeToolpath(THREE, data);
    // The vendored shader exposes both the segment layer and the upper range
    // uniform. Changing the live layer only updates a uniform and draw count.
    const material = handle.mesh.material as THREE.RawShaderMaterial;
    // Color in model coordinates so orbiting the camera preserves the pattern.
    // Phase cycles across the whole layer independently of extrusion order.
    material.uniforms.plan_phase = phase;
    material.uniforms.plan_min = { value: min[0] + min[1] };
    material.uniforms.plan_span = { value: Math.max(1, max[0] - min[0] + max[1] - min[1]) };
    material.vertexShader = material.vertexShader.replace('uniform int layer_hi;', `
      uniform float plan_phase;
      uniform float plan_min;
      uniform float plan_span;
      uniform int layer_hi;`).replace(
      'float half_width = 0.5 * height_width_angle.y;',
      'float half_width = (seg_layer == layer_hi ? 0.20 : 0.50) * height_width_angle.y;',
    ).replace(
      'vec3 color_base = decode_color(hwa_color.w);',
      `vec3 color_base = decode_color(hwa_color.w);
       if (seg_layer == layer_hi) {
         float band = (endpoint_pos.x + endpoint_pos.y - plan_min) / plan_span + plan_phase;
         color_base = 0.63 + 0.37 * cos(6.2831853 * (band + vec3(0.0, 0.667, 0.333)));
       }`,
    );
    handle.mesh.rotation.x = -Math.PI / 2;
    handle.mesh.visible = false;
    scene.add(handle.mesh);
    host.appendChild(canvas);
    resize();
    const distance = radius * 1.1 / Math.sin(Math.atan(Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) * Math.min(camera.aspect, 1)));
    camera.position.copy(center).addScaledVector(new THREE.Vector3(1, 0.76, 1.15).normalize(), distance);
    camera.far = Math.max(1000, distance * 10);
    camera.updateProjectionMatrix();
    controls.target.copy(center);
    controls.maxDistance = camera.far / 2;
    controls.update();
    controls.addEventListener('change', draw);
    canvas.addEventListener('webglcontextlost', onLost);
    observer.observe(host);
    visibility.observe(host);
    reducedMotion.addEventListener('change', syncAnimation);
    document.addEventListener('visibilitychange', syncAnimation);
    return { handle, draw, dispose, syncAnimation };
  } catch (error) {
    dispose();
    throw error;
  }
}

export function VirtualPrintCamera({ gcodeUrl, reportedLayer, toolColor }: Props) {
  const mount = useRef<HTMLDivElement>(null);
  const [result, setResult] = useState<{ url: string; value: CameraResult }>();
  const [failure, setFailure] = useState('');
  const view = useRef<ReturnType<typeof createView> | null>(null);
  const value = result?.url === gcodeUrl ? result.value : undefined;
  const data = value && 'data' in value ? value.data : undefined;

  useEffect(() => {
    setFailure('');
    setResult(undefined);
    let worker: Worker;
    try {
      worker = new Worker(new URL('./virtualPrintCamera.worker.ts', import.meta.url), { type: 'module' });
    } catch {
      setFailure('Could not start the G-code worker.');
      return;
    }
    worker.onmessage = (event: MessageEvent<CameraResult>) => {
      setResult({ url: gcodeUrl, value: event.data });
      worker.terminate();
    };
    worker.onerror = () => {
      setFailure('Could not prepare the virtual camera.');
      worker.terminate();
    };
    worker.postMessage({ url: gcodeUrl, token: getAuthToken() });
    return () => worker.terminate();
  }, [gcodeUrl]);

  useEffect(() => {
    if (!mount.current || !data) return;
    try {
      view.current = createView(mount.current, data, () => setFailure('3D graphics context lost.'));
    } catch (error) {
      setFailure(error instanceof Error ? error.message : '3D graphics unavailable.');
    }
    return () => {
      view.current?.dispose();
      view.current = null;
    };
  }, [data]);

  // The parent may refresh telemetry or supply a new callback each second.
  // Upload vertex colors only when a logical tool's displayed color changes.
  const paletteKey = Array.from({ length: 16 }, (_, slot) =>
    new THREE.Color(toolColor?.(slot) || '#b2c1c6').getHexString()).join(',');
  useEffect(() => {
    if (!view.current || !data) return;
    const palette = paletteKey.split(',').map((color) => Number.parseInt(color, 16));
    const colors = new Float32Array(data.nV * 4);
    for (let i = 0; i < data.nV; i++) colors[i * 4] = palette[Math.max(0, data.meta.vType[i] - 1)];
    view.current.handle.setColors(colors);
    view.current.draw();
  }, [data, paletteKey]);

  const mismatch = !!data && (!Number.isInteger(reportedLayer) || reportedLayer < 0 || reportedLayer > data.layerCount);
  useEffect(() => {
    if (!view.current || !data) return;
    const { handle, draw, syncAnimation } = view.current;
    handle.mesh.visible = !mismatch && reportedLayer > 0;
    if (handle.mesh.visible) handle.setVisibleLayers(reportedLayer);
    syncAnimation();
    draw();
  }, [reportedLayer, data, mismatch]);

  const error = failure || (value && 'error' in value && value.error) || (mismatch && 'Reported layer does not match this plate’s G-code.');
  return <>
    <div ref={mount} role="img" aria-label={`Print plan through layer ${reportedLayer}. A rainbow shows the full planned path for this layer. Earlier layers use filament colors.`} hidden={!data || !!error} className="h-80 w-full overflow-hidden rounded-lg bg-bambu-dark/60" />
    {(error || !data || reportedLayer === 0) && <p role="status" className="text-xs text-bambu-gray-light">{error || (!data ? 'Loading the sliced plate…' : 'Waiting for the first layer.')}</p>}
  </>;
}
