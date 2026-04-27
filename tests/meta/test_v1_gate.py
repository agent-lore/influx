"""Final v1 gate tests -- AC-X-1, AC-X-2, AC-X-6 (US-011, PRD 10).

Asserts:
- AC-X-1 (part 1): every tunable value is config-driven, picks up new
  values on restart.
- AC-X-1 (part 2): no hardcoded tunable constants in filter.py, enrich.py,
  storage.py, resilience modules, or extraction code paths.
- AC-X-2: [models.extract] provider swap works without code change.
- AC-X-6: pure-module coverage >= 80% and every Lithos tool called by
  Influx has happy-path + error-envelope contract tests.
- validate-config against influx.example.toml succeeds.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest

from influx.config import load_config
from influx.http_client import FetchResult

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "influx"


# ======================================================================
# AC-X-1 Part 1: Config-driven tunability
# ======================================================================


class TestACX1ConfigDriven:
    """Every tunable from the master PRD is config-driven."""

    def test_example_toml_covers_all_config_sections(self) -> None:
        """influx.example.toml includes all tunable sections."""
        with (_ROOT / "influx.example.toml").open("rb") as f:
            raw = tomllib.load(f)

        expected_sections = {
            "influx",
            "schedule",
            "storage",
            "notifications",
            "security",
            "profiles",
            "providers",
            "models",
            "prompts",
            "filter",
            "extraction",
            "resilience",
            "feedback",
            "repair",
            "telemetry",
        }
        assert expected_sections <= set(raw.keys()), (
            f"Missing sections: {expected_sections - set(raw.keys())}"
        )

    def test_config_roundtrip_picks_up_modified_tunables(self, tmp_path: Path) -> None:
        """Modified tunables in TOML are reflected in loaded config."""
        toml_text = """\
[influx]
note_schema_version = 2

[schedule]
cron = "0 12 * * *"
misfire_grace_seconds = 7200
shutdown_grace_seconds = 60

[storage]
archive_dir = "/custom-archive"
max_download_bytes = 1000000
download_timeout_seconds = 60

[notifications]
timeout_seconds = 10

[filter]
batch_size = 50
min_score_in_results = 3
negative_example_max_title_chars = 100

[extraction]
min_html_chars = 2000
min_web_chars = 800

[resilience]
max_retries = 5
backoff_base_seconds = 2
arxiv_request_min_interval_seconds = 5
arxiv_429_backoff_seconds = 20
lithos_write_conflict_max_retries = 3

[feedback]
negative_examples_per_profile = 30
recalibrate_after_runs = 10

[repair]
max_items_per_run = 50

[telemetry]
enabled = false
service_name = "test-influx"
export_interval_ms = 60000

[prompts.filter]
text = "f {profile_description} {negative_examples} {min_score_in_results}"
[prompts.tier1_enrich]
text = "e {title} {abstract} {profile_summary}"
[prompts.tier3_extract]
text = "x {title} {full_text}"
"""
        cfg_path = tmp_path / "influx.toml"
        cfg_path.write_text(toml_text)

        config = load_config(cfg_path, check_api_keys=False)

        assert config.influx.note_schema_version == 2
        assert config.schedule.cron == "0 12 * * *"
        assert config.schedule.misfire_grace_seconds == 7200
        assert config.schedule.shutdown_grace_seconds == 60
        assert config.storage.archive_dir == "/custom-archive"
        assert config.storage.max_download_bytes == 1000000
        assert config.storage.download_timeout_seconds == 60
        assert config.notifications.timeout_seconds == 10
        assert config.filter.batch_size == 50
        assert config.filter.min_score_in_results == 3
        assert config.filter.negative_example_max_title_chars == 100
        assert config.extraction.min_html_chars == 2000
        assert config.extraction.min_web_chars == 800
        assert config.resilience.max_retries == 5
        assert config.resilience.backoff_base_seconds == 2
        assert config.resilience.arxiv_request_min_interval_seconds == 5
        assert config.resilience.arxiv_429_backoff_seconds == 20
        assert config.resilience.lithos_write_conflict_max_retries == 3
        assert config.feedback.negative_examples_per_profile == 30
        assert config.feedback.recalibrate_after_runs == 10
        assert config.repair.max_items_per_run == 50
        assert config.telemetry.service_name == "test-influx"
        assert config.telemetry.export_interval_ms == 60000


# ======================================================================
# AC-X-1 Part 2: No stray constants in business-logic modules
# ======================================================================

# Config tunable field names that must NOT appear as module-level constant
# assignments in the checked business-logic files.
_TUNABLE_NAMES: frozenset[str] = frozenset(
    {
        "batch_size",
        "min_score_in_results",
        "negative_example_max_title_chars",
        "min_html_chars",
        "min_web_chars",
        "max_retries",
        "backoff_base_seconds",
        "arxiv_request_min_interval_seconds",
        "arxiv_429_backoff_seconds",
        "lithos_write_conflict_max_retries",
        "max_download_bytes",
        "download_timeout_seconds",
        "timeout_seconds",
        "request_timeout",
        "negative_examples_per_profile",
        "recalibrate_after_runs",
        "max_items_per_run",
        "export_interval_ms",
    }
)

_CHECKED_MODULES: list[str] = [
    "filter.py",
    "enrich.py",
    "storage.py",
    "extraction/pipeline.py",
    "extraction/html.py",
    "extraction/pdf.py",
    "extraction/article.py",
    # Helper / business modules that thread tunables through HTTP /
    # notification call sites: hardcoded numeric defaults here would
    # silently override the loaded config and bypass AC-X-1 (review
    # finding 2 / US-011).
    "http_client.py",
    "notifications.py",
    "service.py",
    "sources/arxiv.py",
    "sources/rss.py",
]


def _iter_function_defaults(
    tree: ast.AST,
) -> list[tuple[int, str, ast.expr]]:
    """Yield ``(lineno, arg_name, default_node)`` for every function default.

    Walks every ``FunctionDef`` / ``AsyncFunctionDef`` and pairs each
    default expression with the parameter it applies to (positional,
    positional-only, and keyword-only).  Used by AC-X-1 part 2 to catch
    hardcoded tunable defaults that live in function signatures rather
    than module-level assignments.
    """
    pairs: list[tuple[int, str, ast.expr]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        args = node.args
        positional = list(args.posonlyargs) + list(args.args)
        if args.defaults:
            offset = len(positional) - len(args.defaults)
            for arg, default in zip(positional[offset:], args.defaults, strict=True):
                pairs.append((default.lineno, arg.arg, default))
        for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
            if default is not None:
                pairs.append((default.lineno, arg.arg, default))
    return pairs


class TestACX1NoStrayConstants:
    """No hardcoded tunable constants in business-logic modules."""

    @pytest.mark.parametrize("module", _CHECKED_MODULES)
    def test_no_tunable_module_level_constants(self, module: str) -> None:
        """Module-level code does not assign a config-tunable name to a literal."""
        path = _SRC / module
        if not path.exists():
            pytest.skip(f"{module} does not exist")

        source = path.read_text()
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.lstrip()
            # Skip comments and blank lines
            if not stripped or stripped.startswith("#"):
                continue
            # Only check module-level assignments (no leading whitespace for
            # assignments, or a single _UPPER prefix)
            if not re.match(r"^[_A-Z]", line):
                continue
            for tunable in _TUNABLE_NAMES:
                pattern = re.compile(rf"\b{re.escape(tunable)}\s*=\s*\d", re.IGNORECASE)
                if pattern.search(stripped):
                    pytest.fail(
                        f"{module}:{i}: hardcoded tunable "
                        f"'{tunable}' found: {line.strip()}"
                    )

    @pytest.mark.parametrize("module", _CHECKED_MODULES)
    def test_no_tunable_function_default_constants(self, module: str) -> None:
        """Tunable params do not carry hardcoded numeric defaults.

        Inspects the AST of each business-logic module and asserts that
        no ``FunctionDef`` / ``AsyncFunctionDef`` carries a numeric
        literal default for any parameter whose name matches a
        ``_TUNABLE_NAMES`` entry. ``None`` defaults are permitted — the
        function body resolves them from the pydantic config defaults
        in ``influx.config`` at call time, so the only place a tunable
        default lives is config-parsing code (AC-X-1).
        """
        path = _SRC / module
        if not path.exists():
            pytest.skip(f"{module} does not exist")

        tree = ast.parse(path.read_text(), filename=str(path))
        violations: list[str] = []
        for lineno, arg_name, default in _iter_function_defaults(tree):
            if arg_name not in _TUNABLE_NAMES:
                continue
            if isinstance(default, ast.Constant) and isinstance(
                default.value, (int, float)
            ):
                violations.append(
                    f"{module}:{lineno}: parameter "
                    f"{arg_name!r} has hardcoded numeric default "
                    f"{default.value!r}"
                )
        if violations:
            pytest.fail(
                "Hardcoded tunable defaults in function signatures "
                "(AC-X-1):\n  " + "\n  ".join(violations)
            )


# ======================================================================
# AC-X-2: Provider swap works without code change
# ======================================================================


class TestACX2ProviderSwap:
    """Provider swap works without code change via config."""

    def test_extract_slot_resolves_to_configured_provider(self, tmp_path: Path) -> None:
        """Changing [models.extract].provider switches the resolved provider."""
        base_toml = """\
[providers.provider_a]
base_url = "https://a.example.com/v1"
api_key_env = ""

[providers.provider_b]
base_url = "https://b.example.com/v1"
api_key_env = ""

[models.extract]
provider = "{provider}"
model = "test-model"
json_mode = true

[models.filter]
provider = "provider_a"
model = "test-model"

[models.enrich]
provider = "provider_a"
model = "test-model"
json_mode = true

[prompts.filter]
text = "f {profile_description} {negative_examples} {min_score_in_results}"
[prompts.tier1_enrich]
text = "e {title} {abstract} {profile_summary}"
[prompts.tier3_extract]
text = "x {title} {full_text}"
"""
        cfg_path = tmp_path / "influx.toml"

        # Load with provider_a
        cfg_path.write_text(base_toml.replace("{provider}", "provider_a"))
        config_a = load_config(cfg_path, check_api_keys=False)
        slot_a = config_a.models["extract"]
        assert slot_a.provider == "provider_a"
        assert config_a.providers[slot_a.provider].base_url == (
            "https://a.example.com/v1"
        )

        # Swap to provider_b -- no code change, only config
        cfg_path.write_text(base_toml.replace("{provider}", "provider_b"))
        config_b = load_config(cfg_path, check_api_keys=False)
        slot_b = config_b.models["extract"]
        assert slot_b.provider == "provider_b"
        assert config_b.providers[slot_b.provider].base_url == (
            "https://b.example.com/v1"
        )

    def test_call_json_model_resolves_provider_at_runtime(self, tmp_path: Path) -> None:
        """_call_json_model resolves the provider from config at call time."""
        from influx.enrich import _call_json_model
        from influx.errors import LCMAError

        toml_text = """\
[providers.alt]
base_url = "https://alt.example.com/v1"
api_key_env = ""

[models.extract]
provider = "alt"
model = "alt-model"
json_mode = true

[prompts.filter]
text = "f {profile_description} {negative_examples} {min_score_in_results}"
[prompts.tier1_enrich]
text = "e {title} {abstract} {profile_summary}"
[prompts.tier3_extract]
text = "x {title} {full_text}"
"""
        cfg_path = tmp_path / "influx.toml"
        cfg_path.write_text(toml_text)
        config = load_config(cfg_path, check_api_keys=False)

        # Verify the slot resolves to the alternate provider
        slot = config.models["extract"]
        assert slot.provider == "alt"
        assert config.providers[slot.provider].base_url == (
            "https://alt.example.com/v1"
        )

        # _call_json_model raises LCMAError for a missing slot, confirming
        # it resolves at runtime rather than at import time
        with pytest.raises(LCMAError, match="not configured"):
            _call_json_model(config, "nonexistent_slot", "test prompt")


# ======================================================================
# AC-X-6: Every Lithos tool has happy-path + error-envelope contract tests
# ======================================================================

# Explicit checklist of Lithos tools called by Influx and their contract
# test locations.  Each tool must have at least one happy-path test and
# at least one error-envelope test.

_LITHOS_TOOLS: dict[str, dict[str, str]] = {
    "lithos_agent_register": {
        "happy": "TestAgentRegister::test_register_on_first_tool_call",
        "error": "TestConstructionValidation::test_rejects_non_sse_transport",
    },
    "lithos_cache_lookup": {
        "happy": "TestCacheLookupChokepoint::test_happy_path_reaches_server",
        "error": "TestCacheLookupChokepoint::test_empty_query_raises_before_rpc",
    },
    "lithos_read": {
        "happy": "TestWriteEnvelopeVersionConflict::test_version_conflict_reread",
        "error": "TestFeedbackFetch::test_fetch_titles_fallback_to_read",
    },
    "lithos_write": {
        "happy": "TestWriteNote::test_happy_path_arxiv_item",
        "error": "TestWriteEnvelopeDuplicate + InvalidInput + SlugCollision + "
        "VersionConflict + ContentTooLarge",
    },
    "lithos_list": {
        "happy": "TestListNotes::test_happy_path_with_tags",
        "error": "TestListNotes::test_empty_result_propagated",
    },
    "lithos_retrieve": {
        "happy": "TestRetrieveHappyPath::test_retrieve_reaches_server",
        "error": "TestRetrieveUnknownTool::test_retrieve_unknown_tool",
    },
    "lithos_edge_upsert": {
        "happy": "TestEdgeUpsertHappyPath::test_edge_upsert_reaches_server",
        "error": "TestEdgeUpsertUnknownTool::test_edge_upsert_unknown_tool",
    },
    "lithos_task_create": {
        "happy": "TestTaskCreateHappyPath::test_task_create_reaches_server",
        "error": "TestTaskCreateUnknownTool::test_task_create_unknown_tool",
    },
    "lithos_task_complete": {
        "happy": "TestTaskCompleteHappyPath::test_task_complete_reaches_server",
        "error": "TestTaskCompleteUnknownTool::test_task_complete_unknown_tool",
    },
}


class TestACX6LithosContractTests:
    """Every Lithos tool called by Influx has contract tests."""

    def test_contract_test_files_exist(self) -> None:
        """Contract test files exist and are non-trivial."""
        contract_dir = _ROOT / "tests" / "contract"
        assert contract_dir.exists(), "tests/contract/ directory missing"
        for filename in ["test_lithos_client.py", "test_lcma_calls.py"]:
            path = contract_dir / filename
            assert path.exists(), f"{filename} missing"
            assert path.stat().st_size > 100, f"{filename} is nearly empty"

    def test_all_lithos_tools_have_contract_tests(self) -> None:
        """All 9 Lithos tools appear in contract test code."""
        contract_dir = _ROOT / "tests" / "contract"
        lithos_tests = (contract_dir / "test_lithos_client.py").read_text()
        lcma_tests = (contract_dir / "test_lcma_calls.py").read_text()
        all_code = lithos_tests + lcma_tests

        for tool_name in _LITHOS_TOOLS:
            assert tool_name in all_code, f"No contract test found for {tool_name}"

    def test_lithos_client_happy_path_tests_exist(self) -> None:
        """test_lithos_client.py contains happy-path test classes."""
        code = (_ROOT / "tests" / "contract" / "test_lithos_client.py").read_text()
        for cls in [
            "TestWriteNote",
            "TestListNotes",
            "TestCacheLookupChokepoint",
            "TestAgentRegister",
        ]:
            assert cls in code, f"Missing happy-path test class {cls}"

    def test_lithos_client_error_envelope_tests_exist(self) -> None:
        """test_lithos_client.py contains error-envelope test classes."""
        code = (_ROOT / "tests" / "contract" / "test_lithos_client.py").read_text()
        for cls in [
            "TestWriteEnvelopeDuplicate",
            "TestWriteEnvelopeInvalidInput",
            "TestWriteEnvelopeSlugCollision",
            "TestWriteEnvelopeVersionConflict",
            "TestWriteEnvelopeContentTooLarge",
        ]:
            assert cls in code, f"Missing error-envelope test class {cls}"

    def test_lcma_happy_path_tests_exist(self) -> None:
        """test_lcma_calls.py contains happy-path test classes."""
        code = (_ROOT / "tests" / "contract" / "test_lcma_calls.py").read_text()
        for cls in [
            "TestRetrieveHappyPath",
            "TestEdgeUpsertHappyPath",
            "TestTaskCreateHappyPath",
            "TestTaskCompleteHappyPath",
        ]:
            assert cls in code, f"Missing LCMA happy-path test class {cls}"

    def test_lcma_error_tests_exist(self) -> None:
        """test_lcma_calls.py contains error-handling test classes."""
        code = (_ROOT / "tests" / "contract" / "test_lcma_calls.py").read_text()
        for cls in [
            "TestRetrieveUnknownTool",
            "TestEdgeUpsertUnknownTool",
            "TestTaskCreateUnknownTool",
            "TestTaskCompleteUnknownTool",
        ]:
            assert cls in code, f"Missing LCMA error test class {cls}"


# ======================================================================
# AC-X-6: Pure-module coverage >= 80%
# ======================================================================

# Pure modules per master PRD section 18.2: config, URL, path, schemas,
# prompts, slug.  Mapped to one or more corresponding unit test files.
# The "path" pure module is the archive-path logic in ``storage.py``
# (``build_archive_path`` plus path-safety helpers); ``storage.py`` also
# carries the thin ``download_archive`` IO wrapper, so coverage for that
# module is driven by ``test_archive_path.py`` and
# ``test_archive_download.py`` together.
_PURE_MODULE_TESTS: dict[str, tuple[str, ...]] = {
    "config.py": ("test_config.py",),
    "urls.py": ("test_url_normalisation.py",),
    "storage.py": ("test_archive_path.py", "test_archive_download.py"),
    "slugs.py": ("test_slugs.py",),
    "schemas.py": ("test_schemas.py",),
    "prompts.py": ("test_prompts.py",),
}


class TestACX6PureModuleCoverage:
    """Pure-module coverage >= 80% (master PRD section 18.2).

    Each pure module must have a corresponding test file *and* its
    line-coverage must reach the 80% threshold. We measure coverage via
    a sub-pytest invocation that runs ``pytest --cov`` against the
    listed pure modules and parses the resulting JSON report.
    """

    @pytest.mark.parametrize(
        "module,test_files",
        list(_PURE_MODULE_TESTS.items()),
        ids=list(_PURE_MODULE_TESTS.keys()),
    )
    def test_pure_module_has_unit_tests(
        self, module: str, test_files: tuple[str, ...]
    ) -> None:
        """Each pure module has at least one corresponding unit test file."""
        total_tests = 0
        for test_file in test_files:
            test_path = _ROOT / "tests" / "unit" / test_file
            assert test_path.exists(), (
                f"Missing unit test file for pure module {module}: "
                f"expected tests/unit/{test_file}"
            )
            content = test_path.read_text()
            total_tests += len(re.findall(r"\bdef test_", content))
        assert total_tests >= 2, (
            f"{test_files!r} has only {total_tests} test(s); "
            "expected >= 2 for meaningful pure-module coverage"
        )

    def test_pure_modules_meet_80_percent_coverage(self, tmp_path: Path) -> None:
        """Pure-module coverage actually measures >= 80% per module.

        Spawns a clean child process (``python -m coverage run -m
        pytest``) so ``coverage`` traces from import time, then parses
        the resulting JSON report. Each pure module's covered-line
        ratio must be >= 0.80 (master PRD §18.2 / AC-X-6).
        """
        import json
        import os
        import subprocess
        import sys

        # Avoid recursive coverage measurement.
        if os.environ.get("INFLUX_PURE_COVERAGE_RUNNING") == "1":
            pytest.skip("inner coverage run; outer test is the gate")

        data_file = tmp_path / ".coverage"
        json_report = tmp_path / "coverage.json"
        cov_cfg = tmp_path / ".coveragerc"
        # ``coverage`` accepts dotted module names in ``source`` and
        # uses Python's import machinery to discover them — using file
        # paths (e.g. ``src/influx/config.py``) silently produces no
        # data with this layout, so dotted names are required.
        module_names = [
            "influx." + Path(m).with_suffix("").as_posix().replace("/", ".")
            for m in _PURE_MODULE_TESTS
        ]
        sources = "\n    ".join(module_names)
        cov_cfg.write_text(
            f"[run]\ndata_file = {data_file}\nbranch = False\nsource =\n    {sources}\n"
        )

        test_paths = [
            str(_ROOT / "tests" / "unit" / t)
            for files in _PURE_MODULE_TESTS.values()
            for t in files
        ]
        env = {
            **os.environ,
            "INFLUX_PURE_COVERAGE_RUNNING": "1",
            "COVERAGE_RCFILE": str(cov_cfg),
        }
        run_cmd = [
            sys.executable,
            "-m",
            "coverage",
            "run",
            f"--rcfile={cov_cfg}",
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--no-cov",
            *test_paths,
        ]
        result = subprocess.run(
            run_cmd, cwd=_ROOT, env=env, capture_output=True, text=True, timeout=180
        )
        assert result.returncode == 0, (
            "pure-module coverage subrun failed:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert data_file.exists(), (
            "coverage data file was not produced — "
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        json_cmd = [
            sys.executable,
            "-m",
            "coverage",
            "json",
            f"--rcfile={cov_cfg}",
            "-o",
            str(json_report),
        ]
        json_result = subprocess.run(
            json_cmd, cwd=_ROOT, env=env, capture_output=True, text=True, timeout=60
        )
        assert json_result.returncode == 0, (
            f"coverage json failed:\nstdout:\n{json_result.stdout}\n"
            f"stderr:\n{json_result.stderr}"
        )

        data = json.loads(json_report.read_text())
        files = data.get("files", {})
        normalised = {
            Path(p).resolve().as_posix(): v["summary"]["percent_covered"]
            for p, v in files.items()
        }

        below: list[str] = []
        for module in _PURE_MODULE_TESTS:
            module_path = (_SRC / module).resolve().as_posix()
            pct = normalised.get(module_path)
            assert pct is not None, (
                f"No coverage entry for pure module {module}; "
                f"keys: {sorted(normalised)}"
            )
            if pct < 80.0:
                below.append(f"{module}: {pct:.1f}%")
        assert not below, (
            "Pure-module coverage below 80% threshold (AC-X-6): " + ", ".join(below)
        )


# ======================================================================
# validate-config against influx.example.toml
# ======================================================================


class TestValidateConfig:
    """validate-config against influx.example.toml succeeds."""

    def test_example_toml_loads_and_validates(self) -> None:
        """influx.example.toml loads successfully via the full v0.7 pipeline."""
        config = load_config(
            _ROOT / "influx.example.toml",
            check_api_keys=False,
        )
        assert len(config.profiles) >= 1
        assert "filter" in config.models
        assert "enrich" in config.models
        assert "extract" in config.models
        assert config.prompts.filter is not None
        assert config.prompts.tier1_enrich is not None
        assert config.prompts.tier3_extract is not None

    def test_example_toml_prompt_variables_valid(self) -> None:
        """Prompt templates in example config use exactly the required vars."""
        from influx.prompts import load_prompt, validate_prompt_variables

        config = load_config(
            _ROOT / "influx.example.toml",
            check_api_keys=False,
        )
        for key in ["filter", "tier1_enrich", "tier3_extract"]:
            prompt_cfg = getattr(config.prompts, key)
            text = load_prompt(text=prompt_cfg.text, path=prompt_cfg.path)
            # Should not raise
            validate_prompt_variables(key, text)

    def test_all_model_slots_reference_defined_providers(self) -> None:
        """Every [models.*].provider references a defined [providers.*] block."""
        config = load_config(
            _ROOT / "influx.example.toml",
            check_api_keys=False,
        )
        for slot_name, slot in config.models.items():
            assert slot.provider in config.providers, (
                f"Model slot {slot_name!r} references provider "
                f"{slot.provider!r} which is not defined in [providers]"
            )

    def test_validate_config_cli_full_pipeline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``python -m influx validate-config`` exercises the full v0.7
        validation pipeline: config schema + prompt vars + JSON-mode
        dry-call + SSE dry-connect — verified against
        ``influx.example.toml`` with mocked HTTP/LithosClient endpoints
        so the test stays deterministic and offline (US-011 Definition
        of Done item)."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from influx.main import _cmd_validate_config

        # Point at a copy of the example config so the load path matches
        # the user-facing CLI surface exactly.
        cfg_src = (_ROOT / "influx.example.toml").read_text()
        cfg_path = tmp_path / "influx.toml"
        cfg_path.write_text(cfg_src)
        monkeypatch.setenv("INFLUX_CONFIG", str(cfg_path))

        # Provide test-only API keys so config loading + JSON-mode dry
        # call have credentials they can route into the auth header.
        monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")

        # Stub the JSON-mode dry-call: any [models.*] slot with
        # ``json_mode=true`` will POST to /chat/completions during
        # validation. Return 200 OK so the CLI accepts the slot.
        ok_response = FetchResult(
            body=b'{"choices":[]}',
            status_code=200,
            content_type="application/json",
            final_url="https://api.openai.com/v1/chat/completions",
        )

        # Stub the LithosClient SSE dry-connect: ``_ensure_connected``
        # is what the CLI invokes; mocking it sidesteps a real MCP
        # handshake while still exercising the validate-config branch.
        fake_client = MagicMock()
        fake_client._ensure_connected = AsyncMock(return_value=None)
        fake_client.close = AsyncMock(return_value=None)

        with (
            patch(
                "influx.http_client.guarded_post_json_fetch",
                return_value=ok_response,
            ),
            patch("influx.lithos_client.LithosClient", return_value=fake_client),
        ):
            # The CLI calls ``sys.exit`` only on failure paths; success
            # falls through. Any non-zero exit indicates a regression in
            # the validate-config pipeline.
            _cmd_validate_config()
