"""
Code quality tests for BamBuddy backend.

These tests check for common anti-patterns and code quality issues
that could cause runtime errors but aren't caught by normal tests.
"""

import ast
from pathlib import Path

import pytest

# Get the backend source directory
BACKEND_DIR = Path(__file__).parent.parent.parent / "app"


# Safe imports that are commonly re-imported in functions without issues
# These are typically imported at the START of a function, not midway through
SAFE_REIMPORT_NAMES = {
    "logging",
    "re",
    "os",
    "sys",
    "json",
    "Path",
    "datetime",
    "timedelta",
    "asyncio",
    "time",
    "typing",
    "Optional",
    "List",
    "Dict",
    "Any",
    "Union",
}


class DangerousImportVisitor(ast.NodeVisitor):
    """AST visitor that detects dangerous import patterns.

    Specifically looks for cases where:
    1. A name is imported at module level
    2. The same name is imported locally in a function
    3. The name is USED before the local import in that function

    This pattern causes 'cannot access local variable' errors.
    """

    def __init__(self):
        self.module_imports: set[str] = set()
        self.dangerous_imports: list[tuple[str, int, str, int]] = []  # (name, import_line, function, first_use_line)
        self.current_function: str | None = None
        self.function_start_line: int = 0
        self.in_function = False

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            name = alias.asname or alias.name
            if not self.in_function:
                self.module_imports.add(name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        for alias in node.names:
            name = alias.asname or alias.name
            if not self.in_function:
                self.module_imports.add(name)
        self.generic_visit(node)

    def _check_function(self, node):
        """Check a function for dangerous import patterns."""
        if not self.in_function:
            return

        # Skip safe reimports
        # Collect all local imports in this function
        local_imports: dict[str, int] = {}  # name -> line number
        name_uses: dict[str, int] = {}  # name -> first use line number

        for child in ast.walk(node):
            # Find local imports
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                for alias in child.names:
                    name = alias.asname or alias.name
                    if name in self.module_imports and name not in SAFE_REIMPORT_NAMES:
                        local_imports[name] = child.lineno

            # Find name uses
            if isinstance(child, ast.Name):
                if child.id not in name_uses:
                    name_uses[child.id] = child.lineno

        # Check for dangerous pattern: use before import
        for name, import_line in local_imports.items():
            if name in name_uses:
                first_use = name_uses[name]
                if first_use < import_line:
                    self.dangerous_imports.append((name, import_line, self.current_function, first_use))

    def visit_FunctionDef(self, node: ast.FunctionDef):
        old_function = self.current_function
        old_in_function = self.in_function
        old_start_line = self.function_start_line

        self.current_function = node.name
        self.in_function = True
        self.function_start_line = node.lineno

        self._check_function(node)
        self.generic_visit(node)

        self.current_function = old_function
        self.in_function = old_in_function
        self.function_start_line = old_start_line

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        old_function = self.current_function
        old_in_function = self.in_function
        old_start_line = self.function_start_line

        self.current_function = node.name
        self.in_function = True
        self.function_start_line = node.lineno

        self._check_function(node)
        self.generic_visit(node)

        self.current_function = old_function
        self.in_function = old_in_function
        self.function_start_line = old_start_line


def find_import_shadowing(file_path: Path) -> list[tuple[str, int, str]]:
    """Find cases where local imports shadow module-level imports AND are used before import.

    Returns list of (name, line_number, function_name) tuples.
    """
    try:
        with open(file_path, encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
        visitor = DangerousImportVisitor()
        visitor.visit(tree)
        # Convert (name, import_line, function, first_use_line) to (name, import_line, function)
        return [(name, import_line, func) for name, import_line, func, _ in visitor.dangerous_imports]
    except SyntaxError:
        return []  # Skip files with syntax errors


def get_python_files(directory: Path) -> list[Path]:
    """Get all Python files in a directory recursively."""
    return list(directory.rglob("*.py"))


class TestImportShadowing:
    """Tests for import shadowing anti-pattern."""

    def test_no_import_shadowing_in_main(self):
        """Check main.py has no import shadowing issues.

        This test would have caught the ArchiveService scoping bug.
        """
        main_file = BACKEND_DIR / "main.py"
        if not main_file.exists():
            pytest.skip("main.py not found")

        shadows = find_import_shadowing(main_file)

        if shadows:
            error_msg = "Import shadowing detected in main.py:\n"
            for name, line, func in shadows:
                error_msg += f"  - '{name}' at line {line} in function '{func}' shadows module-level import\n"
            error_msg += "\nThis can cause 'cannot access local variable' errors."
            pytest.fail(error_msg)

    def test_no_import_shadowing_in_services(self):
        """Check service files have no import shadowing issues."""
        services_dir = BACKEND_DIR / "services"
        if not services_dir.exists():
            pytest.skip("services directory not found")

        all_shadows = []
        for py_file in get_python_files(services_dir):
            shadows = find_import_shadowing(py_file)
            for name, line, func in shadows:
                all_shadows.append((py_file.name, name, line, func))

        if all_shadows:
            error_msg = "Import shadowing detected in services:\n"
            for filename, name, line, func in all_shadows:
                error_msg += f"  - {filename}: '{name}' at line {line} in function '{func}'\n"
            pytest.fail(error_msg)

    def test_no_import_shadowing_in_routes(self):
        """Check route files have no import shadowing issues."""
        routes_dir = BACKEND_DIR / "api" / "routes"
        if not routes_dir.exists():
            pytest.skip("routes directory not found")

        all_shadows = []
        for py_file in get_python_files(routes_dir):
            shadows = find_import_shadowing(py_file)
            for name, line, func in shadows:
                all_shadows.append((py_file.name, name, line, func))

        if all_shadows:
            error_msg = "Import shadowing detected in routes:\n"
            for filename, name, line, func in all_shadows:
                error_msg += f"  - {filename}: '{name}' at line {line} in function '{func}'\n"
            pytest.fail(error_msg)


class TestModuleImports:
    """Tests for module import health."""

    def test_all_modules_importable(self):
        """Verify all Python modules can be imported without errors.

        This catches syntax errors and missing dependencies.

        IMPORTANT: We must NOT ``del sys.modules[name]`` to force a fresh
        import here. ``backend.app.main`` is a stateful module — re-importing
        it builds NEW module-level dicts (_timelapse_baselines,
        _expected_prints, _active_prints, …) and re-runs ``root_logger.
        addHandler(console_handler)``. Any test that already bound those
        names via ``from backend.app.main import _timelapse_baselines`` now
        holds a stale reference, while production code resolves the symbol
        through the new module instance — they're two different dicts. CI
        under -n 2 puts test_code_quality.py on the same worker as
        test_print_start_assigns_printer_id_to_vp_archive.py and
        test_timelapse_baseline_restart_recovery.py, and those tests see
        their mock_archive un-mutated / their baseline dict empty even
        though production logged the mutations went through. Local -n 30
        spreads the tests across workers and the collision never happens.

        ``importlib.import_module`` already covers the "is this importable"
        check — it returns the cached module if cached, or runs the import
        machinery if not. Either way, an import-time error surfaces here.
        """
        import importlib

        # Modules to test importing
        modules = [
            "backend.app.main",
            "backend.app.services.bambu_mqtt",
            "backend.app.services.printer_manager",
            "backend.app.services.archive",
            "backend.app.services.notification_service",
            "backend.app.services.smart_plug_manager",
        ]

        errors = []
        for module_name in modules:
            try:
                importlib.import_module(module_name)
            except Exception as e:
                errors.append(f"{module_name}: {type(e).__name__}: {e}")

        if errors:
            pytest.fail("Failed to import modules:\n" + "\n".join(errors))


# ---------------------------------------------------------------------------
# print_queue.status write fence (lifecycle refactor, 2026-07-12)
#
# Motivated by the BMCU double-feed incident: a helper force-wrote a live
# "printing" job back to "pending" and the printer received the project twice.
# Every write site to PrintQueueItem.status must be enumerated here with its
# concurrency polarity:
#   CAS   — guarded by a current-status check (explicit compare, status-scoped
#           SELECT, or UPDATE ... WHERE status IN (...) + rowcount).
#   FORCE — unconditional write (error paths, intentional overrides).
# New writes smuggled in (e.g. via upstream merges) fail this test until they
# are reviewed and added here with an explicit polarity. Removed sites also
# fail (delete the stale entry) — the shrinking allowlist is the migration
# progress meter for backend/app/services/printer_lifecycle.py.
# ---------------------------------------------------------------------------

PQ_MODEL_NAME = "PrintQueueItem"

# (path relative to backend/app, function, kind, value) -> (count, polarity)
ALLOWED_PQ_STATUS_WRITES = {
    # --- services/print_scheduler.py ---
    # stale pending-SELECT with awaits before the write: effectively FORCE (TOCTOU)
    ("services/print_scheduler.py", "check_queue", "attr", "'skipped'"): (2, "FORCE(stale-pending-select)"),
    # error paths in _start_print; no current-status guard, may clobber a concurrent cancel
    ("services/print_scheduler.py", "_start_print", "attr", "'failed'"): (10, "FORCE(error-path)"),
    # canonical CAS: UPDATE ... WHERE status=='pending' + rowcount check (#1853)
    ("services/print_scheduler.py", "_start_print", "bulk", "'printing'"): (1, "CAS(update-where-rowcount)"),
    # in-memory mirror executed only after the bulk CAS above succeeded
    ("services/print_scheduler.py", "_start_print", "attr", "'printing'"): (1, "CAS(mirror-of-bulk-cas)"),
    # _do_revert: explicit `item.status != "printing"` recheck before write
    ("services/print_scheduler.py", "_watchdog_print_start", "attr", "'pending'"): (1, "CAS(status-recheck)"),
    # --- main.py ---
    # HMS auto-clear requeue: SELECT scoped to status=='printing' (fw-truth gated, d9e81190)
    ("main.py", "_requeue_print_rejected_by_hms", "attr", "'pending'"): (1, "CAS(select-scoped)"),
    # print-complete handler: SELECT scoped 'printing', but an await sits before commit (TOCTOU window)
    ("main.py", "on_print_complete", "attr", "<dynamic>"): (1, "CAS(select-scoped,pre-commit-await)"),
    # startup data migration aborted->cancelled
    ("main.py", "lifespan", "attr", "'cancelled'"): (1, "CAS(startup-migration)"),
    # --- api/routes/print_queue.py ---
    ("api/routes/print_queue.py", "add_to_queue", "ctor", "'pending'"): (1, "CREATE"),
    ("api/routes/print_queue.py", "cancel_batch", "attr", "'cancelled'"): (1, "CAS(select-scoped)"),
    ("api/routes/print_queue.py", "resume_queue_after_failure", "attr", "'pending'"): (1, "CAS(select-scoped)"),
    ("api/routes/print_queue.py", "cancel_queue_item", "attr", "'cancelled'"): (1, "CAS(explicit-guard)"),
    # /stop: CAS on status (raises unless 'printing') but intentionally cancels
    # even when the printer is offline / stop command failed — do NOT weaken.
    ("api/routes/print_queue.py", "stop_queue_item", "attr", "'cancelled'"): (1, "CAS(status)+FORCE(connectivity)"),
    # --- api/routes/pipeline_runs.py ---
    # python-level guard `status in ("pending","queued")`, commit later in loop (small TOCTOU)
    ("api/routes/pipeline_runs.py", "_make_orchestration_callable", "attr", "'cancelled'"): (1, "CAS(python-guard,pre-commit-await)"),
    ("api/routes/pipeline_runs.py", "_make_orchestration_callable", "ctor", "'pending'"): (1, "CREATE"),
    ("api/routes/pipeline_runs.py", "cancel_run", "attr", "'cancelled'"): (1, "CAS(python-guard,pre-commit-await)"),
    # --- other creators ---
    ("api/routes/library.py", "add_files_to_queue", "ctor", "'pending'"): (1, "CREATE"),
    ("services/virtual_printer/manager.py", "_add_to_print_queue", "ctor", "'pending'"): (1, "CREATE"),
    # --- core/database.py ---
    # startup migration #1667: raw SQL scoped by WHERE pq.status='skipped'
    ("core/database.py", "run_migrations", "rawsql", "<sql>"): (1, "CAS(sql-scoped)"),
}


def _pq_subtree_names(node) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


class PrintQueueStatusWriteScanner:
    """Find PrintQueueItem.status writes in one function via light taint tracking.

    A variable is 'tainted' (treated as a PrintQueueItem) when bound from an
    expression mentioning PrintQueueItem, or from another tainted name.
    Binding from an expression that mentions a DIFFERENT class-like name
    clears the taint (e.g. `batch = db.get(PrintBatch, ...)` reusing a name).
    Attribute/subscript writes never rebind, so they never change taint.
    """

    def __init__(self):
        self.tainted: set[str] = set()
        self.findings: list[tuple[int, str, str]] = []  # (lineno, kind, value_repr)

    @staticmethod
    def _class_like(names: set[str]) -> set[str]:
        return {n for n in names if n[:1].isupper() and n != PQ_MODEL_NAME}

    def _apply_taint(self, rhs_names: set[str], target_names: set[str]):
        if PQ_MODEL_NAME in rhs_names:
            self.tainted |= target_names
        elif self._class_like(rhs_names):
            self.tainted -= target_names
        elif rhs_names & self.tainted:
            self.tainted |= target_names

    def _taint_pass(self, nodes):
        for _ in range(2):  # simple fixpoint
            for node in nodes:
                if isinstance(node, ast.Assign) and node.value is not None:
                    tgts = set()
                    for tgt in node.targets:
                        if isinstance(tgt, ast.Name):
                            tgts.add(tgt.id)
                        elif isinstance(tgt, (ast.Tuple, ast.List)):
                            tgts |= {e.id for e in tgt.elts if isinstance(e, ast.Name)}
                    if tgts:
                        self._apply_taint(_pq_subtree_names(node.value), tgts)
                elif isinstance(node, (ast.For, ast.AsyncFor)):
                    tgts = {n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)}
                    self._apply_taint(_pq_subtree_names(node.iter), tgts)

    @staticmethod
    def _value_repr(node) -> str:
        if isinstance(node, ast.Constant):
            return repr(node.value)
        return "<dynamic>"

    def scan(self, func_node):
        nodes = list(ast.walk(func_node))
        self._taint_pass(nodes)
        for node in nodes:
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if (
                        isinstance(tgt, ast.Attribute)
                        and tgt.attr == "status"
                        and isinstance(tgt.value, ast.Name)
                        and tgt.value.id in self.tainted
                    ):
                        self.findings.append((node.lineno, "attr", self._value_repr(node.value)))
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id == PQ_MODEL_NAME:
                    for kw in node.keywords:
                        if kw.arg == "status":
                            self.findings.append((node.lineno, "ctor", self._value_repr(kw.value)))
                if isinstance(node.func, ast.Attribute) and node.func.attr == "values":
                    for kw in node.keywords:
                        if kw.arg == "status" and PQ_MODEL_NAME in _pq_subtree_names(node):
                            self.findings.append((node.lineno, "bulk", self._value_repr(kw.value)))
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                lowered = node.value.lower()
                if "update print_queue" in lowered and "status" in lowered:
                    self.findings.append((node.lineno, "rawsql", "<sql>"))


def find_pq_status_writes(file_path: Path) -> list[tuple[str, int, str, str]]:
    """Return (function, lineno, kind, value_repr) for PrintQueueItem.status writes."""
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    results = []
    seen = set()
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        scanner = PrintQueueStatusWriteScanner()
        scanner.scan(func)
        for lineno, kind, value in scanner.findings:
            # nested functions are walked by both the outer and inner pass;
            # keep the first (outermost) attribution only
            if (lineno, kind) not in seen:
                seen.add((lineno, kind))
                results.append((func.name, lineno, kind, value))
    return results


class TestPrintQueueStatusFence:
    """Fence: every PrintQueueItem.status write must be allowlisted with polarity."""

    def test_all_status_writes_are_allowlisted(self):
        found: dict[tuple[str, str, str, str], list[int]] = {}
        for py_file in get_python_files(BACKEND_DIR):
            rel = str(py_file.relative_to(BACKEND_DIR))
            for func, lineno, kind, value in find_pq_status_writes(py_file):
                found.setdefault((rel, func, kind, value), []).append(lineno)

        errors = []
        for key, linenos in sorted(found.items()):
            allowed = ALLOWED_PQ_STATUS_WRITES.get(key)
            if allowed is None:
                errors.append(
                    f"UNREVIEWED write site: {key[0]}:{sorted(linenos)} in {key[1]}() "
                    f"[{key[2]} -> {key[3]}] — review its CAS/FORCE polarity and add "
                    f"it to ALLOWED_PQ_STATUS_WRITES (or route it through printer_lifecycle)."
                )
            elif len(linenos) > allowed[0]:
                errors.append(
                    f"NEW write site(s): {key[0]} in {key[1]}() [{key[2]} -> {key[3]}] "
                    f"found {len(linenos)} at lines {sorted(linenos)}, allowlist permits {allowed[0]}."
                )
        for key, (count, polarity) in sorted(ALLOWED_PQ_STATUS_WRITES.items()):
            n_found = len(found.get(key, []))
            if n_found < count:
                errors.append(
                    f"STALE allowlist entry: {key} [{polarity}] expects {count} site(s), found {n_found} "
                    f"— if the write moved into printer_lifecycle, shrink/remove the entry."
                )

        if errors:
            pytest.fail(
                "print_queue.status write fence violations:\n  " + "\n  ".join(errors)
            )


class TestLogErrorPatterns:
    """Tests that use log capture to detect runtime errors."""

    def test_mqtt_message_processing_no_errors(self, capture_logs):
        """Test that MQTT message processing doesn't log errors."""
        from backend.app.services.bambu_mqtt import BambuMQTTClient

        client = BambuMQTTClient(
            ip_address="192.168.1.100",
            serial_number="TEST123",
            access_code="12345678",
        )
        client.on_print_start = lambda data: None
        client.on_print_complete = lambda data: None

        # Process a realistic print lifecycle
        messages = [
            {"print": {"gcode_state": "RUNNING", "gcode_file": "/test.gcode", "subtask_name": "Test"}},
            {"print": {"gcode_state": "RUNNING", "gcode_file": "/test.gcode", "mc_percent": 50}},
            {"print": {"gcode_state": "FINISH", "gcode_file": "/test.gcode", "subtask_name": "Test"}},
        ]

        for msg in messages:
            client._process_message(msg)

        assert not capture_logs.has_errors(), f"Errors during MQTT processing:\n{capture_logs.format_errors()}"
