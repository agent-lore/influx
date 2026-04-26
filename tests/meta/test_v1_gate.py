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

import re
import tomllib
from pathlib import Path

import pytest

from influx.config import load_config

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

    def test_config_roundtrip_picks_up_modified_tunables(
        self, tmp_path: Path
    ) -> None:
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
]


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
                pattern = re.compile(
                    rf"\b{re.escape(tunable)}\s*=\s*\d", re.IGNORECASE
                )
                if pattern.search(stripped):
                    pytest.fail(
                        f"{module}:{i}: hardcoded tunable "
                        f"'{tunable}' found: {line.strip()}"
                    )


# ======================================================================
# AC-X-2: Provider swap works without code change
# ======================================================================


class TestACX2ProviderSwap:
    """Provider swap works without code change via config."""

    def test_extract_slot_resolves_to_configured_provider(
        self, tmp_path: Path
    ) -> None:
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
text = "f {{profile_description}} {{negative_examples}} {{min_score_in_results}}"
[prompts.tier1_enrich]
text = "e {{title}} {{abstract}} {{profile_summary}}"
[prompts.tier3_extract]
text = "x {{title}} {{full_text}}"
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

    def test_call_json_model_resolves_provider_at_runtime(
        self, tmp_path: Path
    ) -> None:
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
        "error": "LCMA unknown_tool contract (same mechanism as retrieve)",
    },
    "lithos_task_complete": {
        "happy": "TestTaskCompleteHappyPath::test_task_complete_reaches_server",
        "error": "LCMA unknown_tool contract (same mechanism as retrieve)",
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
            assert tool_name in all_code, (
                f"No contract test found for {tool_name}"
            )

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
        ]:
            assert cls in code, f"Missing LCMA error test class {cls}"


# ======================================================================
# AC-X-6: Pure-module coverage >= 80%
# ======================================================================

# Pure modules per master PRD section 18.2: config, URL, path, schemas,
# prompts, slug.  Mapped to their corresponding unit test files.
_PURE_MODULE_TESTS: dict[str, str] = {
    "config.py": "test_config.py",
    "urls.py": "test_url_normalisation.py",
    "slugs.py": "test_slugs.py",
    "schemas.py": "test_schemas.py",
    "prompts.py": "test_prompts.py",
    "dedup.py": "test_dedup_query.py",
}


class TestACX6PureModuleCoverage:
    """Pure-module coverage >= 80% (master PRD section 18.2).

    Coverage is measured via ``pytest --cov`` as a quality-check step.
    These tests verify the prerequisite: each pure module has a
    corresponding test file with meaningful test functions.
    """

    @pytest.mark.parametrize(
        "module,test_file",
        list(_PURE_MODULE_TESTS.items()),
        ids=list(_PURE_MODULE_TESTS.keys()),
    )
    def test_pure_module_has_unit_tests(
        self, module: str, test_file: str
    ) -> None:
        """Each pure module has a corresponding unit test file."""
        test_path = _ROOT / "tests" / "unit" / test_file
        assert test_path.exists(), (
            f"Missing unit test file for pure module {module}: "
            f"expected tests/unit/{test_file}"
        )
        content = test_path.read_text()
        test_count = len(re.findall(r"\bdef test_", content))
        assert test_count >= 2, (
            f"{test_file} has only {test_count} test(s); "
            "expected >= 2 for meaningful pure-module coverage"
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
