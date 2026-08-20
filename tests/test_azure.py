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
