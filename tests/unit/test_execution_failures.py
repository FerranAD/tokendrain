from tokendrain.orchestration.failures import execution_failure


def test_guest_disconnect_does_not_claim_oom() -> None:
    detail = execution_failure(ConnectionResetError(), stage="guest", memory_mib=4096)
    assert "may have" in detail["explanation"]
    assert "does not confirm" in detail["recovery"]
    assert "Check the VM logs" in detail["recovery"]


def test_helper_disconnect_does_not_blame_guest_memory() -> None:
    detail = execution_failure(ConnectionRefusedError(), stage="vm_start", memory_mib=512)
    assert "doctor" in detail["recovery"]
    assert "RAM" not in detail["recovery"]
