import { useEffect, useRef } from 'react';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';

interface Props {
  layers: Float32Array[];
  toolColor: (tool: number) => string;
  onUnavailable: () => void;
}

// Keep each packed XY segment independent. Travel between records is unknown.
function stackGeometry(layers: Float32Array[], toolColor: Props['toolColor']) {
  const extent = new THREE.Box2();
  const point = new THREE.Vector2();
  let count = 0;
  for (const layer of layers) {
    for (let i = 0; i + 4 < layer.length; i += 5) {
      extent.expandByPoint(point.set(layer[i], layer[i + 1]));
      extent.expandByPoint(point.set(layer[i + 2], layer[i + 3]));
      count++;
    }
  }
  const center = extent.getCenter(point);
  const scale = Math.max(extent.max.x - extent.min.x, extent.max.y - extent.min.y, 1);
  const positions = new Float32Array(count * 6);
  const colors = new Float32Array(count * 6);
  const palette = new Map<number, THREE.Color>();
  let offset = 0;
  layers.forEach((layer, index) => {
    const height = (index - (layers.length - 1) / 2) * 0.018;
    for (let i = 0; i + 4 < layer.length; i += 5) {
      const tool = layer[i + 4];
      let color = palette.get(tool);
      if (!color) {
        color = new THREE.Color(toolColor(tool));
        palette.set(tool, color);
      }
      for (let endpoint = 0; endpoint < 4; endpoint += 2) {
        positions[offset] = (layer[i + endpoint] - center.x) / scale;
        positions[offset + 1] = height;
        positions[offset + 2] = -(layer[i + endpoint + 1] - center.y) / scale;
        color.toArray(colors, offset);
        offset += 3;
      }
    }
  });
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3));
  return geometry;
}

export function LayerStackView({ layers, toolColor, onUnavailable }: Props) {
  const mount = useRef<HTMLDivElement>(null);
  const unavailable = useRef(onUnavailable);
  useEffect(() => { unavailable.current = onUnavailable; }, [onUnavailable]);
  const view = useRef<{ paths: THREE.LineSegments; draw: () => void } | null>(null);

  useEffect(() => {
    const host = mount.current;
    if (!host) return;
    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    } catch {
      unavailable.current();
      return;
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    const canvas = renderer.domElement;
    host.appendChild(canvas);
    const lost = () => unavailable.current();
    canvas.addEventListener('webglcontextlost', lost);
    const scene = new THREE.Scene();
    const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.01, 100);
    camera.position.set(1.3, 1.05, 1.65);
    const controls = new OrbitControls(camera, canvas);
    controls.enablePan = false;
    controls.minZoom = 0.4;
    controls.maxZoom = 20;
    controls.update();
    const material = new THREE.LineBasicMaterial({ vertexColors: true });
    const paths = new THREE.LineSegments(new THREE.BufferGeometry(), material);
    scene.add(paths);
    const draw = () => renderer.render(scene, camera);
    view.current = { paths, draw };
    controls.addEventListener('change', draw);
    const resize = () => {
      const width = Math.max(1, host.clientWidth);
      const height = Math.max(1, host.clientHeight);
      const aspect = width / height;
      camera.right = 0.8 * Math.max(1, aspect);
      camera.left = -camera.right;
      camera.top = 0.8 * Math.max(1, 1 / aspect);
      camera.bottom = -camera.top;
      camera.updateProjectionMatrix();
      renderer.setSize(width, height);
      draw();
    };
    const observer = new ResizeObserver(resize);
    observer.observe(host);
    resize();
    return () => {
      view.current = null;
      observer.disconnect();
      controls.removeEventListener('change', draw);
      controls.dispose();
      canvas.removeEventListener('webglcontextlost', lost);
      paths.geometry.dispose();
      material.dispose();
      renderer.dispose();
      renderer.forceContextLoss();
      canvas.remove();
    };
  }, []);

  useEffect(() => {
    if (!view.current) return;
    const { paths, draw } = view.current;
    paths.geometry.dispose();
    paths.geometry = stackGeometry(layers, toolColor);
    draw();
  }, [layers, toolColor]);

  return <>
    <div ref={mount} role="img" aria-label="3D stack of planned extrusion paths" className="h-72 w-full overflow-hidden rounded-lg bg-bambu-dark/60" />
    <p className="text-xs text-bambu-gray-light">Drag to rotate · Scroll or pinch to zoom. Vertical spacing shows layer order only. The preview data contains no Z heights.</p>
  </>;
}
