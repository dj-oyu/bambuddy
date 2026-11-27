"""Bounded, conservative extraction of extrusion segments for BMCU previews.

The result is a slicer plan, never evidence of what was physically printed.
Unknown motion commands fail closed instead of producing an incomplete shape.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import re
import sys
import time
import zipfile
from array import array
from dataclasses import dataclass
from pathlib import Path

MAX_ENTRIES = 1024
MAX_GCODE_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 16384
MAX_SEGMENTS = 600_000
MAX_LAYERS = 10_000
MAX_SECONDS = 45
WORD = re.compile(r"([A-Za-z])\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))")
COMMAND = re.compile(r"^([GMT])\s*(\d+)(?:\s|$)", re.IGNORECASE)


class PreviewUnavailable(ValueError):
    """A file cannot be previewed without risking a misleading result."""


class PreviewContextChanged(PreviewUnavailable):
    """This viewer's print changed; another viewer's work remains valid."""


@dataclass(frozen=True)
class PlannedLayers:
    # Each segment is x0, y0, x1, y1, logical tool; physical tray is unknown.
    layers: tuple[array, ...]
    truncated_reason: str | None = None


def parse_plate(path: Path, plate: int | None) -> tuple[int, PlannedLayers]:
    deadline = time.monotonic() + MAX_SECONDS
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ENTRIES:
                raise PreviewUnavailable("Archive has too many entries")
            candidates = [entry for entry in entries if re.fullmatch(r"Metadata/plate_\d+\.gcode", entry.filename)]
            if plate is None:
                if len(candidates) != 1:
                    raise PreviewUnavailable("Current plate is unknown")
                selected = candidates[0]
                plate = int(re.search(r"plate_(\d+)\.gcode$", selected.filename).group(1))
            else:
                matches = [entry for entry in candidates if entry.filename == f"Metadata/plate_{plate}.gcode"]
                if len(matches) != 1:
                    raise PreviewUnavailable("Current plate G-code is unavailable")
                selected = matches[0]
            if selected.file_size > MAX_GCODE_BYTES:
                raise PreviewUnavailable("G-code exceeds preview size limit")
            with archive.open(selected) as source:
                return plate, _parse_lines(source, deadline)
    except (zipfile.BadZipFile, OSError, UnicodeError) as exc:
        raise PreviewUnavailable("G-code archive could not be read") from exc


def _parse_lines(source, deadline: float) -> PlannedLayers:
    layers: list[array] = []
    layer: array | None = None
    x = y = e = 0.0
    known_x = known_y = False
    relative_xyz = relative_e = False
    retracted = 0.0
    tool = 0
    count = total = 0
    skipping = False
    try:
        for raw in source:
            total += len(raw)
            if len(raw) > MAX_LINE_BYTES or total > MAX_GCODE_BYTES:
                raise PreviewUnavailable("G-code exceeds preview limits")
            if time.monotonic() > deadline:
                raise PreviewUnavailable("G-code preview timed out")
            line = raw.decode("utf-8").strip()
            if not line:
                continue
            upper = line.upper()
            if upper.startswith("; SKIPPABLE_START"):
                if skipping:
                    raise PreviewUnavailable("Nested conditional G-code is not supported")
                skipping = True
                continue
            if upper.startswith("; SKIPPABLE_END"):
                if not skipping:
                    raise PreviewUnavailable("Invalid conditional G-code boundary")
                skipping = False
                # Conditional firmware commands may move the head. Require fresh
                # absolute XY moves before drawing another segment.
                known_x = known_y = False
                continue
            if skipping:
                command = upper.split(";", 1)[0].strip()
                if not command:
                    continue
                name = command.split(None, 1)[0]
                allowed = {
                    "G0",
                    "G1",
                    "G2",
                    "G3",
                    "G39",
                    "G39.3",
                    "G392",
                    "G90",
                    "G91",
                    "G92",
                    "M73",
                    "M82",
                    "M83",
                    "M106",
                    "M204",
                    "M400",
                    "M622",
                    "M622.1",
                    "M623",
                    "M971",
                    "M1004",
                    "M1002",
                }
                if name not in allowed or (name == "M1002" and not command.startswith("M1002 JUDGE_FLAG ")):
                    raise PreviewUnavailable("Unsupported conditional G-code")
                if re.match(r"^G92(?:\s|$)", command) and re.search(r"[XY][-+0-9.]", command):
                    raise PreviewUnavailable("Conditional XY reset is not supported")
                if re.match(r"^T\s*\d", command):
                    raise PreviewUnavailable("Conditional tool change is not supported")
                if re.match(r"^G9[01](?:\s|$)", command) and (command.startswith("G91") != relative_xyz):
                    raise PreviewUnavailable("Conditional coordinate mode is not supported")
                if re.match(r"^M8[23](?:\s|$)", command) and (command.startswith("M83") != relative_e):
                    raise PreviewUnavailable("Conditional extrusion mode is not supported")
                if re.match(r"^G92(?:\s|$)", command) and re.search(r"E[-+0-9.]", command) and not relative_e:
                    raise PreviewUnavailable("Conditional absolute extrusion reset is not supported")
                if re.match(r"^G[0123](?:\s|$)", command) and re.search(r"E[-+0-9.]", command):
                    raise PreviewUnavailable("Conditional extrusion is not supported")
                continue
            if line.startswith(";"):
                key = line[1:].strip().split(":", 1)[0].strip().upper()
                if key in ("LAYER_CHANGE", "CHANGE_LAYER"):
                    if len(layers) >= MAX_LAYERS:
                        raise PreviewUnavailable("G-code has too many layers")
                    layer = array("f")
                    layers.append(layer)
                continue
            line = line.split(";", 1)[0].strip()
            match = COMMAND.match(line)
            if not match:
                if layer is not None and re.match(r"^[Mm]\s*622\.1(?:\s|$)", line):
                    raise PreviewUnavailable("Unsupported conditional G-code")
                if layer is not None and re.match(r"^[Gg]\s*\d", line):
                    raise PreviewUnavailable("Unsupported G-code command format")
                continue
            family, number = match.group(1).upper(), int(match.group(2))
            rest = line[match.end() :]
            if re.search(r"\d[eE][+-]\d", rest):
                raise PreviewUnavailable("Ambiguous compact G-code number")
            words = WORD.findall(rest)
            args = {key.upper(): float(value) for key, value in words}
            if len(args) != len(words):
                raise PreviewUnavailable("Duplicate G-code axis")
            if not all(math.isfinite(v) for v in args.values()):
                raise PreviewUnavailable("Non-finite G-code coordinate")
            if family == "T":
                if 0 <= number <= 15:
                    tool = number
            elif family == "M" and number in (82, 83):
                relative_e = number == 83
            elif family == "G" and number in (90, 91):
                relative_xyz = number == 91
            elif family == "G" and number == 92:
                if "X" in args or "Y" in args:
                    raise PreviewUnavailable("G-code XY coordinate reset is not supported")
                if "E" in args:
                    e = args["E"]
            elif family == "G" and number == 28:
                known_x = known_y = False
            elif family == "G" and number in (5, 10, 11, 18, 19, 20, 53, 54, 55, 56, 57, 58, 59):
                raise PreviewUnavailable(f"Unsupported G-code motion G{number}")
            elif family == "G" and number in (0, 1, 2, 3):
                if relative_xyz and not (known_x and known_y) and ("X" in args or "Y" in args):
                    raise PreviewUnavailable("Relative move from unknown position")
                if any(axis in args for axis in ("A", "B", "C", "U", "V", "W")):
                    raise PreviewUnavailable("Unsupported G-code axis")
                nx = (x + args["X"] if relative_xyz else args["X"]) if "X" in args else x
                ny = (y + args["Y"] if relative_xyz else args["Y"]) if "Y" in args else y
                ne = (e + args["E"] if relative_e else args["E"]) if "E" in args else e
                delta = ne - e
                if delta < 0:
                    retracted -= delta
                elif delta > 0:
                    restored = min(delta, retracted)
                    retracted -= restored
                    delta -= restored
                if layer is not None and delta > 1e-7 and (nx != x or ny != y) and not (known_x and known_y):
                    raise PreviewUnavailable("Extrusion starts at an unknown position")
                if layer is not None and known_x and known_y and delta > 1e-7:
                    if number in (2, 3):
                        if "R" in args or ("I" not in args and "J" not in args) or "P" in args:
                            raise PreviewUnavailable("Unsupported G-code arc")
                        cx, cy = x + args.get("I", 0), y + args.get("J", 0)
                        radius = math.hypot(x - cx, y - cy)
                        if radius < 1e-7 or abs(math.hypot(nx - cx, ny - cy) - radius) > max(0.05, radius * 0.001):
                            raise PreviewUnavailable("Invalid G-code arc")
                        start, end = math.atan2(y - cy, x - cx), math.atan2(ny - cy, nx - cx)
                        sweep = (end - start) % (2 * math.pi)
                        if number == 2:
                            sweep = sweep - 2 * math.pi if sweep > 0 else -2 * math.pi
                        elif sweep == 0:
                            sweep = 2 * math.pi
                        steps = max(1, math.ceil(abs(sweep) * radius / 0.5))
                        if steps > 256:
                            raise PreviewUnavailable("G-code arc exceeds preview limit")
                        px, py = x, y
                        for step in range(1, steps + 1):
                            angle = start + sweep * step / steps
                            qx, qy = (
                                (nx, ny)
                                if step == steps
                                else (cx + radius * math.cos(angle), cy + radius * math.sin(angle))
                            )
                            layer.extend((px, py, qx, qy, tool))
                            px, py = qx, qy
                        count += steps
                    elif nx != x or ny != y:
                        layer.extend((x, y, nx, ny, tool))
                        count += 1
                    if count > MAX_SEGMENTS:
                        raise PreviewUnavailable("G-code has too many extrusion segments")
                x, y, e = nx, ny, ne
                known_x = known_x or "X" in args
                known_y = known_y or "Y" in args
            elif layer is not None and (
                (family == "G" and number not in (4, 17, 21)) or (family == "M" and number in (98, 99, 622, 623))
            ):
                raise PreviewUnavailable(f"Unsupported G-code command {family}{number}")
    except PreviewUnavailable as exc:
        # Only whole layers before an unsupported/limited section are usable.
        if len(layers) > 1:
            return PlannedLayers(tuple(layers[:-1]), str(exc))
        raise
    if skipping:
        raise PreviewUnavailable("Unclosed conditional G-code block")
    if not layers:
        raise PreviewUnavailable("Explicit layer markers are unavailable")
    return PlannedLayers(tuple(layers))


# Run the parser in its own low-priority process so a large job does not block
# the telemetry event loop. A single in-flight job is shared across clients.
_cache_key = None
_cache_value = None
_cache_error = None
_cache_until = 0.0
_job_key = None
_job_task = None
_job_waiters = 0
_job_lock = None


def _lock():
    global _job_lock
    if _job_lock is None:
        _job_lock = asyncio.Lock()
    return _job_lock


async def _run_parser(path: Path, plate: int | None):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "backend.app.services.bmcu_gcode_preview",
        str(path),
        "" if plate is None else str(plate),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=MAX_SECONDS + 20)
    except BaseException as exc:
        if process.returncode is None:
            process.kill()
            await process.wait()
        if isinstance(exc, TimeoutError):
            raise PreviewUnavailable("G-code preview worker timed out") from exc
        raise
    try:
        result = json.loads(stdout)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise PreviewUnavailable("G-code preview worker failed") from exc
    if process.returncode or "error" in result:
        raise PreviewUnavailable(result.get("error", "G-code preview worker failed"))
    return result


async def preview_layers(path: Path, plate: int | None, request, is_current=None):
    """Share one bounded parse, and stop it when its final viewer disconnects."""
    global _cache_key, _cache_value, _cache_error, _cache_until, _job_key, _job_task, _job_waiters
    try:
        stat = path.stat()
    except OSError as exc:
        raise PreviewUnavailable("G-code archive is unavailable") from exc
    key = (str(path), stat.st_size, stat.st_mtime_ns, plate, 2, stat.st_ino, stat.st_ctime_ns)
    async with _lock():
        if _cache_key == key and time.monotonic() < _cache_until:
            if _cache_error:
                raise PreviewUnavailable(_cache_error)
            return _cache_value
        if _job_task is not None and _job_task.done() and _job_waiters == 0:
            _job_task = None
            _job_key = None
        if _job_task is not None and (_job_key != key or _job_task.cancelling()):
            raise PreviewUnavailable("Another G-code preview is being prepared")
        if _job_task is None:
            _cache_key = _cache_value = _cache_error = None
            _job_key = key
            _job_task = asyncio.create_task(_run_parser(path, plate))
        task = _job_task
        _job_waiters += 1
    try:
        while True:
            try:
                if is_current is not None and not is_current():
                    raise PreviewContextChanged("The print changed while preparing the preview")
                result = await asyncio.wait_for(asyncio.shield(task), timeout=1)
                try:
                    current = path.stat()
                except OSError as exc:
                    raise PreviewUnavailable("G-code archive is unavailable") from exc
                if (
                    current.st_size != key[1]
                    or current.st_mtime_ns != key[2]
                    or current.st_ino != key[5]
                    or current.st_ctime_ns != key[6]
                ):
                    raise PreviewUnavailable("G-code archive changed during parsing")
                async with _lock():
                    if _job_task is task:
                        _cache_key, _cache_value, _cache_until = key, result, time.monotonic() + 600
                return result
            except TimeoutError:
                if await request.is_disconnected():
                    raise asyncio.CancelledError
            except PreviewContextChanged:
                raise
            except PreviewUnavailable as exc:
                async with _lock():
                    if _job_task is task:
                        _cache_key, _cache_value, _cache_error, _cache_until = (
                            key,
                            None,
                            str(exc),
                            time.monotonic() + 600,
                        )
                raise
    finally:
        async with _lock():
            _job_waiters -= 1
            if _job_task is task and _job_waiters == 0:
                if not task.done():
                    # Keep the job registered until the child is reaped so a
                    # new viewer cannot start a second parser in parallel.
                    task.cancel()
                else:
                    _job_task = _job_key = None


def _worker_main():
    try:
        if hasattr(os, "nice"):
            os.nice(10)
        plate, result = parse_plate(Path(sys.argv[1]), int(sys.argv[2]) if sys.argv[2] else None)
        if sys.byteorder != "little":
            for layer in result.layers:
                layer.byteswap()
        print(
            json.dumps(
                {
                    "plate": plate,
                    "layers": [base64.b64encode(layer.tobytes()).decode("ascii") for layer in result.layers],
                    "truncatedReason": result.truncated_reason,
                },
                separators=(",", ":"),
            )
        )
    except (PreviewUnavailable, ValueError, IndexError) as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)


if __name__ == "__main__":
    _worker_main()
