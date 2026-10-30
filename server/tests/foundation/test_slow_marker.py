import pytest


@pytest.mark.slow
def test_slow_marker_is_registered_and_skipped_by_default():
    """scripts/test-all.sh deselects this unless --slow is given."""
