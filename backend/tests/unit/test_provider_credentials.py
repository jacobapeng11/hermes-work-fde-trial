import pytest

from app.services.provider_credentials import ProviderCredentialCodec


def test_provider_credential_codec_round_trips_and_does_not_store_plaintext() -> None:
    codec = ProviderCredentialCodec("test-secret")
    encrypted = codec.encrypt("provider-api-key")

    assert encrypted.startswith("provider-v1:")
    assert "provider-api-key" not in encrypted
    assert codec.decrypt(encrypted) == "provider-api-key"


def test_provider_credential_codec_reads_legacy_unprefixed_values() -> None:
    assert ProviderCredentialCodec("test-secret").decrypt("legacy-value") == "legacy-value"


def test_provider_credential_codec_rejects_wrong_key() -> None:
    encrypted = ProviderCredentialCodec("test-secret").encrypt("provider-api-key")

    with pytest.raises(ValueError, match="cannot be decrypted"):
        ProviderCredentialCodec("other-secret").decrypt(encrypted)
