"""Unit tests for the pure, deterministic logic in eval/run_eval.py -- no real repo, no
worktree, no subprocess, no API calls. These exist specifically so CI can verify the
SWE-bench-style scoring rule itself hasn't regressed, without needing the marshmallow
clone or any Gemini quota that the real agent phases require.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.run_eval import extract_fail_to_pass_targets, resolve_target_nodeids, score


# --- extract_fail_to_pass_targets -------------------------------------------------

def test_extract_single_added_test():
    diff = "+def test_new_thing():\n+    assert True\n"
    assert extract_fail_to_pass_targets(diff) == {"test_new_thing"}


def test_extract_multiple_added_tests():
    diff = (
        "+def test_first():\n"
        "+    assert True\n"
        "+def test_second():\n"
        "+    assert True\n"
    )
    assert extract_fail_to_pass_targets(diff) == {"test_first", "test_second"}


def test_extract_ignores_modified_test():
    # Appears on both a '+def' and a '-def' line -- an existing test being edited,
    # not a new one, so it can't be used as a FAIL_TO_PASS target.
    diff = "-def test_existing(old_arg):\n+def test_existing(new_arg):\n"
    assert extract_fail_to_pass_targets(diff) == set()


def test_extract_no_added_tests():
    diff = "-def test_removed():\n-    assert True\n"
    assert extract_fail_to_pass_targets(diff) == set()


def test_extract_matches_indented_test_in_class():
    diff = "+class TestFoo:\n+    def test_indented(self):\n+        assert True\n"
    assert extract_fail_to_pass_targets(diff) == {"test_indented"}


# --- resolve_target_nodeids --------------------------------------------------------

def test_resolve_bare_name_match():
    pass_map = {"tests/test_foo.py::test_bar": "passed"}
    assert resolve_target_nodeids(pass_map, {"test_bar"}) == {"tests/test_foo.py::test_bar"}


def test_resolve_strips_parametrize_suffix():
    pass_map = {"tests/test_foo.py::test_bar[value1]": "passed", "tests/test_foo.py::test_bar[value2]": "failed"}
    result = resolve_target_nodeids(pass_map, {"test_bar"})
    assert result == {"tests/test_foo.py::test_bar[value1]", "tests/test_foo.py::test_bar[value2]"}


def test_resolve_matches_inside_class():
    pass_map = {"tests/test_foo.py::TestClass::test_bar": "passed"}
    assert resolve_target_nodeids(pass_map, {"test_bar"}) == {"tests/test_foo.py::TestClass::test_bar"}


def test_resolve_no_match_returns_empty():
    pass_map = {"tests/test_foo.py::test_unrelated": "passed"}
    assert resolve_target_nodeids(pass_map, {"test_bar"}) == set()


# --- score (the actual SWE-bench-style resolved/regression rule) -------------------

def test_score_clean_resolve():
    result = score(
        fail_to_pass_ids={"tests/test_foo.py::test_bug"},
        baseline_pass_set={"tests/test_foo.py::test_other"},
        after_pass_set={"tests/test_foo.py::test_other", "tests/test_foo.py::test_bug"},
    )
    assert result["resolved"] is True
    assert result["regressions"] == []
    assert result["targets_passing"] == ["tests/test_foo.py::test_bug"]


def test_score_target_still_failing_is_not_resolved():
    result = score(
        fail_to_pass_ids={"tests/test_foo.py::test_bug"},
        baseline_pass_set=set(),
        after_pass_set=set(),  # the fix didn't make the target test pass
    )
    assert result["resolved"] is False
    assert result["targets_passing"] == []


def test_score_regression_blocks_resolution():
    # The target now passes, but something that was passing before no longer does --
    # this is the case SCOPE.md says matters more than the target test alone.
    result = score(
        fail_to_pass_ids={"tests/test_foo.py::test_bug"},
        baseline_pass_set={"tests/test_foo.py::test_other", "tests/test_foo.py::test_bug"},
        after_pass_set={"tests/test_foo.py::test_bug"},  # test_other vanished
    )
    assert result["resolved"] is False
    assert result["regressions"] == ["tests/test_foo.py::test_other"]


def test_score_timestamp_flaky_nodeid_is_not_a_false_regression():
    # A parametrized test's exact nodeid can differ between baseline and after runs
    # (e.g. parametrized on the current timestamp) even though it's the same logical
    # test. If another instance sharing the same base name still passes, the vanished
    # exact nodeid must NOT be counted as a real regression.
    result = score(
        fail_to_pass_ids={"tests/test_foo.py::test_bug"},
        baseline_pass_set={"tests/test_foo.py::test_bug", "tests/test_foo.py::test_flaky[2026-01-01]"},
        after_pass_set={"tests/test_foo.py::test_bug", "tests/test_foo.py::test_flaky[2026-01-02]"},
    )
    assert result["resolved"] is True
    assert result["regressions"] == []


def test_score_genuine_disappearance_of_flaky_family_is_a_regression():
    # Unlike the case above, if NO instance of the same base name survives, it's a
    # real regression, not just a parametrize-suffix rename.
    result = score(
        fail_to_pass_ids={"tests/test_foo.py::test_bug"},
        baseline_pass_set={"tests/test_foo.py::test_bug", "tests/test_foo.py::test_flaky[2026-01-01]"},
        after_pass_set={"tests/test_foo.py::test_bug"},
    )
    assert result["resolved"] is False
    assert result["regressions"] == ["tests/test_foo.py::test_flaky[2026-01-01]"]
