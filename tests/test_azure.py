import pytest

from cloudmap.ingest import azure


def test_resolve_secret_handles_failures(monkeypatch):
    def fake_az(*args, **kwargs):
        raise RuntimeError("az command failed")
    monkeypatch.setattr(azure, "_az", fake_az)
    
    assert azure._resolve_secret("@Microsoft.KeyVault(SecretUri=https://myvault.vault.azure.net/secrets/mysecret/)") == ""

def test_guard_raises_system_exit_on_mismatch(monkeypatch):
    monkeypatch.setenv("CLOUDMAP_ALLOW_SUBSCRIPTION", "sub-2")
    def fake_az(*args, **kwargs):
        return '{"id": "sub-1", "tenantId": "t-1"}'
    monkeypatch.setattr(azure, "_az", fake_az)
    
    with pytest.raises(SystemExit, match="Refusing the mismatch"):
        azure._guard()


def test_gap_classification_names_the_category_not_the_incident():
    cases = {
        "az webapp show failed: AuthorizationFailed: client 'x' with object id "
        "'11111111-2222-3333-4444-555555555555' does not have authorization":
            "authorization denied (RBAC)",
        "ERROR: (ResourceNotFound) the resource was not found": "not found",
        "az aks command invoke timed out after 120s": "timeout",
        "AADSTS50076: MFA required": "Entra token rejected",
        "Failed to resolve 'management.azure.com'": "network (DNS)",
    }
    for raw, expected in cases.items():
        assert azure.classify_gap(raw) == expected, raw


def test_an_unknown_gap_is_kept_but_every_guid_is_masked():
    short = azure.classify_gap(
        "some novel failure, correlation id 12345678-abcd-ef01-2345-6789abcdef01\n"
        "second line with more detail")

    assert "12345678-abcd" not in short          # nothing identifying rides along
    assert "<id>" in short
    assert "second line" not in short            # first line only
    assert len(short) <= 120
