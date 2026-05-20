import pytest

from mathematica_kernel_mcp.server import _validate_kernel_id


@pytest.mark.parametrize(
    "kernel_id",
    ["main", "scratch-abc123", "Probe_1", "analysis.kernel"],
)
def test_validate_kernel_id_accepts_safe_names(kernel_id):
    assert _validate_kernel_id(kernel_id) == kernel_id


@pytest.mark.parametrize(
    "kernel_id",
    ["", "1bad", "has space", "../bad", "shared", "collab", "notebook"],
)
def test_validate_kernel_id_rejects_unsafe_or_reserved_names(kernel_id):
    with pytest.raises(ValueError):
        _validate_kernel_id(kernel_id)
