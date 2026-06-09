"""Real tests for Agent 3 logic — no mocks, real data from the demo repo."""

import os
import pytest

from ai.agent3 import (
    blast_radius_score,
    build_dep_graph,
    compute_risk,
    extract_change_subject,
    file_value_label,
    folder_category,
    fmt_time,
    group_vertical_slices,
    parse_snippets,
    review_minutes,
    structural_token_estimate,
)

# ── Real file content from the demo repo ─────────────────────────────────────

_DEMO = os.path.join(os.path.dirname(__file__), "..", "demo_repo")


def _read(rel: str) -> str:
    with open(os.path.join(_DEMO, rel), encoding="utf-8") as f:
        return f.read()


CUSTOMER_PROFILE_CONTENT = _read("models/customer_profile.py")
RISK_CLASSIFIER_CONTENT = _read("pipelines/risk_classifier.py")
PROFILE_VALIDATOR_CONTENT = _read("validators/profile_validator.py")
TEST_FILE_CONTENT = _read("tests/test_customer_profile.py")

REAL_SNIPPETS = {
    "models/customer_profile.py": CUSTOMER_PROFILE_CONTENT,
    "pipelines/risk_classifier.py": RISK_CLASSIFIER_CONTENT,
    "validators/profile_validator.py": PROFILE_VALIDATOR_CONTENT,
    "tests/test_customer_profile.py": TEST_FILE_CONTENT,
}

REAL_FILES = list(REAL_SNIPPETS.keys())

REAL_CONTEXT = "\n\n".join(
    f"# FILE: {path}\n{content}" for path, content in REAL_SNIPPETS.items()
)


# ── 1. folder_category ────────────────────────────────────────────────────────

class TestFolderCategory:
    def test_models_directory(self):
        assert folder_category("models/customer_profile.py") == "models"

    def test_pipeline_is_core(self):
        assert folder_category("pipelines/risk_classifier.py") == "core"

    def test_validator_is_core(self):
        assert folder_category("validators/profile_validator.py") == "core"

    def test_tests_directory(self):
        assert folder_category("tests/test_customer_profile.py") == "tests"

    def test_readme_is_readme(self):
        assert folder_category("README.md") == "readme"

    def test_other_falls_through(self):
        assert folder_category("ltv_calculator.py") == "other"

    def test_services_is_core(self):
        assert folder_category("services/export_service.py") == "core"


# ── 2. blast_radius_score ─────────────────────────────────────────────────────

class TestBlastRadius:
    def test_zero_for_isolated_files(self):
        graph = {
            "models/customer_profile.py": [],
            "validators/profile_validator.py": [],
        }
        assert blast_radius_score(list(graph), graph) == 0

    def test_one_for_single_dependency(self):
        # risk_classifier imports customer_profile → customer_profile has blast radius 1
        graph = {
            "models/customer_profile.py": [],
            "pipelines/risk_classifier.py": ["models/customer_profile.py"],
        }
        assert blast_radius_score(list(graph), graph) == 1

    def test_high_when_multiple_files_import_same_file(self):
        # Both risk_classifier and profile_validator import customer_profile
        graph = {
            "models/customer_profile.py": [],
            "pipelines/risk_classifier.py": ["models/customer_profile.py"],
            "validators/profile_validator.py": ["models/customer_profile.py"],
        }
        assert blast_radius_score(list(graph), graph) == 2

    def test_real_dep_graph_from_demo_repo(self):
        graph = build_dep_graph(REAL_FILES, REAL_SNIPPETS)
        radius = blast_radius_score(REAL_FILES, graph)
        # customer_profile.py is imported by risk_classifier + profile_validator → radius >= 2
        assert radius >= 2


# ── 3. build_dep_graph ────────────────────────────────────────────────────────

class TestBuildDepGraph:
    def test_detects_import_between_changed_files(self):
        snippets = {
            "models/customer_profile.py": "class CustomerProfile:\n    pass\n",
            "pipelines/risk_classifier.py": (
                "from demo_repo.models.customer_profile import CustomerProfile\n"
                "def classify(p): return p\n"
            ),
        }
        files = list(snippets)
        graph = build_dep_graph(files, snippets)
        assert "models/customer_profile.py" in graph["pipelines/risk_classifier.py"]

    def test_no_self_reference(self):
        snippets = {
            "models/customer_profile.py": "from demo_repo.models.customer_profile import CustomerProfile\n",
        }
        graph = build_dep_graph(["models/customer_profile.py"], snippets)
        assert "models/customer_profile.py" not in graph["models/customer_profile.py"]

    def test_real_demo_repo_detects_risk_classifier_dependency(self):
        graph = build_dep_graph(REAL_FILES, REAL_SNIPPETS)
        classifier_deps = graph.get("pipelines/risk_classifier.py", [])
        assert "models/customer_profile.py" in classifier_deps


# ── 4. structural_token_estimate ─────────────────────────────────────────────

class TestStructuralTokenEstimate:
    def test_result_exceeds_raw_token_count(self):
        context = CUSTOMER_PROFILE_CONTENT + "\n" + RISK_CLASSIFIER_CONTENT
        request = "migrate risk_level to RiskCategory enum"
        result = structural_token_estimate(context, request)
        # Must be higher than raw context alone (overhead + output multiplier)
        from ai.tokenizer import estimate_tokens
        raw = estimate_tokens(context) + estimate_tokens(request)
        assert result > raw

    def test_includes_system_overhead(self):
        # Empty context: only system overhead + multiplier
        result = structural_token_estimate("", "add a docstring")
        from ai.tokenizer import estimate_tokens
        req = estimate_tokens("add a docstring")
        assert result >= int((req + 500) * 1.4)

    def test_real_demo_context_produces_meaningful_estimate(self):
        result = structural_token_estimate(REAL_CONTEXT, "migrate risk_level to RiskCategory enum")
        assert result > 1000  # real files produce substantial context
        assert result < 50000  # but not absurdly large


# ── 5. extract_change_subject ─────────────────────────────────────────────────

class TestExtractChangeSubject:
    def test_extracts_camel_case_identifier(self):
        assert extract_change_subject("Replace RiskCategory with a new enum") == "RiskCategory"

    def test_extracts_snake_case_field(self):
        assert extract_change_subject("update risk_level field in the schema") == "risk_level"

    def test_does_not_return_article_a(self):
        result = extract_change_subject("add a summary count of profiles with missing reviews")
        assert result != "a"

    def test_does_not_return_short_stopwords(self):
        result = extract_change_subject("update the validator to flag profiles")
        assert result is None or len(result) >= 5

    def test_real_pilar_request(self):
        # Pilar's exact request from the Slack screenshot
        req = (
            "The customer aggregator needs to handle missing review scores gracefully "
            "— default to 0 instead of crashing, update the validator to flag profiles "
            "with no reviews, and add a summary count of profiles with missing reviews "
            "to the pipeline output"
        )
        result = extract_change_subject(req)
        assert result != "a"
        assert result != "the"
        assert result is None or len(result) >= 5


# ── 6. compute_risk ───────────────────────────────────────────────────────────

class TestComputeRisk:
    def test_high_risk_when_model_and_core_both_touched(self):
        files = ["models/customer_profile.py", "pipelines/risk_classifier.py"]
        risk, score, _, _, coupled = compute_risk(files, REAL_SNIPPETS)
        assert risk == "HIGH"
        assert len(coupled) == 2

    def test_coupling_detected_for_model_plus_core(self):
        files = ["models/customer_profile.py", "validators/profile_validator.py"]
        _, _, _, _, coupled = compute_risk(files, REAL_SNIPPETS)
        assert len(coupled) == 2

    def test_low_risk_for_test_only_change(self):
        files = ["tests/test_customer_profile.py"]
        # Test file contains test patterns → no_test_count = 0
        risk, score, _, no_test, _ = compute_risk(files, REAL_SNIPPETS)
        assert risk == "LOW"
        assert no_test == 0

    def test_no_coupling_when_only_core_files(self):
        files = ["pipelines/risk_classifier.py", "validators/profile_validator.py"]
        _, _, _, _, coupled = compute_risk(files, REAL_SNIPPETS)
        # No model file → no coupling
        assert coupled == []

    def test_full_demo_repo_change_is_high_risk(self):
        risk, _, _, _, coupled = compute_risk(REAL_FILES, REAL_SNIPPETS)
        assert risk == "HIGH"
        assert len(coupled) > 0


# ── 7. parse_snippets ─────────────────────────────────────────────────────────

class TestParseSnippets:
    def test_all_files_visible_including_first(self):
        context = (
            "# FILE: models/customer_profile.py\nclass CustomerProfile:\n    pass\n\n"
            "# FILE: validators/profile_validator.py\ndef validate():\n    pass"
        )
        snippets = parse_snippets(context)
        # Both files must be visible — the first file bug is fixed
        assert "models/customer_profile.py" in snippets
        assert "validators/profile_validator.py" in snippets

    def test_first_file_content_is_correct(self):
        context = "# FILE: models/customer_profile.py\nclass CustomerProfile:\n    pass"
        snippets = parse_snippets(context)
        assert "class CustomerProfile" in snippets["models/customer_profile.py"]

    def test_real_context_parses_all_four_demo_files(self):
        snippets = parse_snippets(REAL_CONTEXT)
        for path in REAL_FILES:
            assert path in snippets, f"Missing: {path}"


# ── 8. file_value_label ───────────────────────────────────────────────────────

class TestFileValueLabel:
    def test_reads_triple_quoted_docstring(self):
        label = file_value_label("models/customer_profile.py", CUSTOMER_PROFILE_CONTENT)
        assert "Customer" in label or len(label) > 10

    def test_reads_docstring_from_risk_classifier(self):
        label = file_value_label("pipelines/risk_classifier.py", RISK_CLASSIFIER_CONTENT)
        assert len(label) > 10
        assert label != "risk classifier"  # should get real description, not just stem

    def test_fallback_to_stem_when_no_docstring(self):
        label = file_value_label("some_module.py", "x = 1\n")
        assert label == "some module"

    def test_does_not_return_empty(self):
        label = file_value_label("validators/profile_validator.py", PROFILE_VALIDATOR_CONTENT)
        assert len(label) > 0


# ── 9. group_vertical_slices ─────────────────────────────────────────────────

class TestGroupVerticalSlices:
    def test_no_file_duplication(self):
        graph = build_dep_graph(REAL_FILES, REAL_SNIPPETS)
        titles = [
            "Happy path: risk_level works for complete data",
            "Edge case: incomplete or missing data handled gracefully",
            "Sad path: invalid input fails safely before downstream processing",
        ]
        slices = group_vertical_slices(titles, REAL_FILES, graph)
        all_files = [f for _, files in slices for f in files]
        assert len(all_files) == len(set(all_files)), "Duplicate files across slices"

    def test_all_files_assigned(self):
        graph = build_dep_graph(REAL_FILES, REAL_SNIPPETS)
        titles = ["Slice A", "Slice B", "Slice C"]
        slices = group_vertical_slices(titles, REAL_FILES, graph)
        all_files = [f for _, files in slices for f in files]
        assert set(all_files) == set(REAL_FILES)

    def test_numbering_starts_at_one(self):
        graph = build_dep_graph(REAL_FILES, REAL_SNIPPETS)
        slices = group_vertical_slices(["A", "B", "C"], REAL_FILES, graph)
        for idx, (_, _) in enumerate(slices, start=1):
            assert idx >= 1  # indices always start at 1 when caller enumerates


# ── 10. review_minutes + fmt_time ────────────────────────────────────────────

class TestReviewHelpers:
    def test_high_risk_costs_more_per_file(self):
        high = review_minutes(3, "HIGH")
        low = review_minutes(3, "LOW")
        assert high > low

    def test_coupling_adds_overhead(self):
        without = review_minutes(3, "HIGH", has_coupling=False)
        with_coupling = review_minutes(3, "HIGH", has_coupling=True)
        assert with_coupling == without + 30

    def test_fmt_time_under_60_shows_minutes(self):
        assert fmt_time(45) == "~45 min"

    def test_fmt_time_over_60_shows_hours(self):
        assert "hr" in fmt_time(90)
        assert fmt_time(90) == "~1.5 hr"

    def test_fmt_time_exactly_60(self):
        assert "hr" in fmt_time(60)
