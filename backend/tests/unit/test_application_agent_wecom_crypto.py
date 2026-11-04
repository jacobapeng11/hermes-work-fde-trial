from __future__ import annotations

import base64

import pytest

from app.services.application_agent_wecom_crypto import (
    WeComCryptoError,
    decrypt_message,
    encrypt_message,
    extract_encrypted_xml,
    message_signature,
    parse_text_message,
    verify_message_signature,
)
from app.services.managed_secret import ManagedSecretCodec, ManagedSecretError


AES_KEY = base64.b64encode(b"k" * 32).decode("ascii").rstrip("=")


def test_wecom_crypto_round_trip_and_signature() -> None:
    plaintext = (
        "<xml><ToUserName>ww-corp</ToUserName>"
        "<FromUserName>zhangsan</FromUserName><CreateTime>1</CreateTime>"
        "<MsgType>text</MsgType><Content>继续分析工单</Content>"
        "<MsgId>msg-1</MsgId><AgentID>1000002</AgentID></xml>"
    )
    encrypted = encrypt_message(
        encoding_aes_key=AES_KEY,
        plaintext=plaintext,
        receive_id="ww-corp",
    )
    signature = message_signature(
        token="callback-token",
        timestamp="1720000000",
        nonce="nonce-1",
        encrypted=encrypted,
    )
    verify_message_signature(
        token="callback-token",
        timestamp="1720000000",
        nonce="nonce-1",
        encrypted=encrypted,
        signature=signature,
    )
    decoded = decrypt_message(
        encoding_aes_key=AES_KEY,
        encrypted=encrypted,
        receive_id="ww-corp",
    )
    message = parse_text_message(decoded)
    assert message.member_id == "zhangsan"
    assert message.agent_id == 1000002
    assert message.content == "继续分析工单"


def test_wecom_crypto_rejects_wrong_signature_receive_id_and_entities() -> None:
    encrypted = encrypt_message(
        encoding_aes_key=AES_KEY,
        plaintext="<xml><MsgType>text</MsgType></xml>",
        receive_id="ww-corp",
    )
    with pytest.raises(WeComCryptoError, match="wecom_signature_invalid"):
        verify_message_signature(
            token="callback-token",
            timestamp="1",
            nonce="nonce",
            encrypted=encrypted,
            signature="bad",
        )
    with pytest.raises(WeComCryptoError, match="wecom_receive_id_mismatch"):
        decrypt_message(
            encoding_aes_key=AES_KEY,
            encrypted=encrypted,
            receive_id="other-corp",
        )
    with pytest.raises(WeComCryptoError, match="wecom_payload_invalid"):
        extract_encrypted_xml(
            '<!DOCTYPE xml [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            "<xml><Encrypt>&xxe;</Encrypt></xml>"
        )


def test_managed_secret_codec_encrypts_and_detects_wrong_key() -> None:
    codec = ManagedSecretCodec("test-key")
    encrypted = codec.encrypt("never-return-this-secret")
    assert "never-return-this-secret" not in encrypted
    assert codec.decrypt(encrypted) == "never-return-this-secret"
    with pytest.raises(ManagedSecretError):
        ManagedSecretCodec("other-key").decrypt(encrypted)
