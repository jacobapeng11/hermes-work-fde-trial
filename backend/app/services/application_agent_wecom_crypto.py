from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import struct
from dataclasses import dataclass
from xml.etree import ElementTree

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class WeComCryptoError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class WeComTextMessage:
    corp_id: str
    member_id: str
    agent_id: int
    message_id: str
    content: str


def verify_message_signature(
    *,
    token: str,
    timestamp: str,
    nonce: str,
    encrypted: str,
    signature: str,
) -> None:
    values = sorted((token, timestamp, nonce, encrypted))
    expected = hashlib.sha1("".join(values).encode("utf-8")).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise WeComCryptoError("wecom_signature_invalid")


def decrypt_message(*, encoding_aes_key: str, encrypted: str, receive_id: str) -> str:
    key = _aes_key(encoding_aes_key)
    try:
        ciphertext = base64.b64decode(encrypted, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise WeComCryptoError("wecom_ciphertext_invalid") from exc
    if not ciphertext or len(ciphertext) % 16:
        raise WeComCryptoError("wecom_ciphertext_invalid")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    plaintext = _pkcs7_unpad_32(padded)
    if len(plaintext) < 20:
        raise WeComCryptoError("wecom_plaintext_invalid")
    message_length = struct.unpack("!I", plaintext[16:20])[0]
    message_end = 20 + message_length
    if message_end > len(plaintext):
        raise WeComCryptoError("wecom_plaintext_invalid")
    message = plaintext[20:message_end]
    embedded_receive_id = plaintext[message_end:]
    try:
        embedded = embedded_receive_id.decode("utf-8")
        decoded_message = message.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WeComCryptoError("wecom_plaintext_invalid") from exc
    if not hmac.compare_digest(embedded, receive_id):
        raise WeComCryptoError("wecom_receive_id_mismatch")
    return decoded_message


def encrypt_message(*, encoding_aes_key: str, plaintext: str, receive_id: str) -> str:
    key = _aes_key(encoding_aes_key)
    message = plaintext.encode("utf-8")
    payload = (
        os.urandom(16)
        + struct.pack("!I", len(message))
        + message
        + receive_id.encode("utf-8")
    )
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    ciphertext = encryptor.update(_pkcs7_pad_32(payload)) + encryptor.finalize()
    return base64.b64encode(ciphertext).decode("ascii")


def encrypted_xml(*, encrypted: str, signature: str, timestamp: str, nonce: str) -> str:
    root = ElementTree.Element("xml")
    ElementTree.SubElement(root, "Encrypt").text = encrypted
    ElementTree.SubElement(root, "MsgSignature").text = signature
    ElementTree.SubElement(root, "TimeStamp").text = timestamp
    ElementTree.SubElement(root, "Nonce").text = nonce
    return ElementTree.tostring(root, encoding="unicode", short_empty_elements=False)


def extract_encrypted_xml(raw_xml: str) -> str:
    root = _safe_xml_root(raw_xml)
    encrypted = _required_text(root, "Encrypt")
    if len(encrypted) > 1_000_000:
        raise WeComCryptoError("wecom_payload_too_large")
    return encrypted


def parse_text_message(raw_xml: str) -> WeComTextMessage:
    root = _safe_xml_root(raw_xml)
    msg_type = _required_text(root, "MsgType")
    if msg_type != "text":
        raise WeComCryptoError("wecom_message_type_unsupported")
    try:
        agent_id = int(_required_text(root, "AgentID"))
    except ValueError as exc:
        raise WeComCryptoError("wecom_agent_id_invalid") from exc
    content = _required_text(root, "Content").strip()
    if not content or len(content) > 10_000:
        raise WeComCryptoError("wecom_message_content_invalid")
    return WeComTextMessage(
        corp_id=_required_text(root, "ToUserName"),
        member_id=_required_text(root, "FromUserName"),
        agent_id=agent_id,
        message_id=_required_text(root, "MsgId"),
        content=content,
    )


def text_reply_xml(
    *,
    corp_id: str,
    member_id: str,
    content: str,
    timestamp: int,
) -> str:
    root = ElementTree.Element("xml")
    ElementTree.SubElement(root, "ToUserName").text = member_id
    ElementTree.SubElement(root, "FromUserName").text = corp_id
    ElementTree.SubElement(root, "CreateTime").text = str(timestamp)
    ElementTree.SubElement(root, "MsgType").text = "text"
    ElementTree.SubElement(root, "Content").text = content
    return ElementTree.tostring(root, encoding="unicode", short_empty_elements=False)


def message_signature(*, token: str, timestamp: str, nonce: str, encrypted: str) -> str:
    return hashlib.sha1(
        "".join(sorted((token, timestamp, nonce, encrypted))).encode("utf-8")
    ).hexdigest()


def _aes_key(value: str) -> bytes:
    if len(value) != 43:
        raise WeComCryptoError("wecom_encoding_aes_key_invalid")
    try:
        key = base64.b64decode(f"{value}=", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise WeComCryptoError("wecom_encoding_aes_key_invalid") from exc
    if len(key) != 32:
        raise WeComCryptoError("wecom_encoding_aes_key_invalid")
    return key


def _pkcs7_pad_32(payload: bytes) -> bytes:
    padding_length = 32 - (len(payload) % 32)
    return payload + bytes((padding_length,)) * padding_length


def _pkcs7_unpad_32(payload: bytes) -> bytes:
    if not payload:
        raise WeComCryptoError("wecom_padding_invalid")
    padding_length = payload[-1]
    if padding_length < 1 or padding_length > 32:
        raise WeComCryptoError("wecom_padding_invalid")
    if payload[-padding_length:] != bytes((padding_length,)) * padding_length:
        raise WeComCryptoError("wecom_padding_invalid")
    return payload[:-padding_length]


def _safe_xml_root(raw_xml: str) -> ElementTree.Element:
    if not raw_xml or len(raw_xml) > 1_100_000:
        raise WeComCryptoError("wecom_payload_invalid")
    upper = raw_xml.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise WeComCryptoError("wecom_payload_invalid")
    try:
        root = ElementTree.fromstring(raw_xml)
    except ElementTree.ParseError as exc:
        raise WeComCryptoError("wecom_payload_invalid") from exc
    if root.tag != "xml":
        raise WeComCryptoError("wecom_payload_invalid")
    return root


def _required_text(root: ElementTree.Element, name: str) -> str:
    value = root.findtext(name)
    if not isinstance(value, str) or not value.strip():
        raise WeComCryptoError("wecom_payload_invalid")
    return value.strip()
