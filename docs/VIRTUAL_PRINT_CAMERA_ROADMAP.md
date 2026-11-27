# Virtual print camera: live progress and future position tracking

## Available now

The printer's reported, one-based `layer_num` selects the corresponding
G-code layer. Previous layers remain visible, the whole current layer is
highlighted as a planned path, and later layers remain hidden. The print
context is checked every five seconds against the connected printer and the
identified archive and plate. Layer markers and the printer count must refer to
the same sliced plate. The current layer is not a measurement of how much of
that layer has already been extruded.

For the A1 mini observed on 2026-11-27, the active plate had 108 layer markers
and the printer reported 108 layers. A passive MQTT sample showed the layer
counter advancing but did not expose live XYZ or the executing G-code line.
This observation does not establish what every firmware version can report.

## Target: a position-driven virtual camera

Display a calibrated A1 mini toolhead model at measured XYZ. Reveal deposited
segments only up to a verified execution position and retain completed layers.
Use the printer's actual position and execution state; elapsed time, percentage
and the last coordinate in a layer are not substitutes for either measurement.

Before implementation, a position source must supply:

- Printer and active job identity, a monotonic sample timestamp, XYZ in a
  documented coordinate frame and units, measurement quality, and freshness.
- An execution cursor tied to the exact executed G-code, or a separately
  validated way to associate measured positions with path segments. Position
  alone cannot disambiguate revisited coordinates, travels, retracts, or
  out-of-order and repeated paths.
- A known transform from that frame to the sliced plate, including the A1
  mini's moving bed, plus the nozzle offset for the actual head model.

The renderer should interpolate between fresh samples only within measured
time bounds, freeze or hide the head when samples become stale, and reset its
cursor on job, plate, homing, or coordinate-frame changes. Never infer extrusion
from position alone. Pauses, skips, injected commands and print recovery need
explicit handling before claiming that a segment was deposited.

## Validation before enabling

Capture time-aligned telemetry and camera footage for an A1 mini print with
straight moves, direction changes, travel, retraction, a tool change, a pause
and a layer transition. Measure nozzle-to-model error in millimetres, timing
lag and recovery after signal loss. Verify that no segment appears before its
confirmed execution and that the current job and plate are matched. If only
layer progress is available, keep the layer-level view rather than animate an
invented position.
