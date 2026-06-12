import hashlib
import ipaddress
import socket
import struct

from src.config import get_settings

settings = get_settings()


def _encode_text_attr(attr_type: int, value: str) -> bytes:
    value_bytes = value.encode("utf-8")
    return struct.pack("!BB", attr_type, len(value_bytes) + 2) + value_bytes


def _encode_ipv4_attr(attr_type: int, value: str) -> bytes:
    ip_value = ipaddress.ip_address(value)
    if ip_value.version != 4:
        raise ValueError(f"RADIUS attribute {attr_type} requires an IPv4 address")
    return struct.pack("!BB", attr_type, 6) + ip_value.packed


def _build_disconnect_packet(identifier: int, router_secret: str, attributes: dict[str, str]) -> bytes:
    attrs = b""
    attrs += _encode_text_attr(1, attributes["User-Name"])
    attrs += _encode_ipv4_attr(8, attributes["Framed-IP-Address"])

    packet_code = bytes([40])
    packet_id = bytes([identifier])
    packet_length = struct.pack("!H", 20 + len(attrs))
    zero_authenticator = b"\x00" * 16
    authenticator = hashlib.md5(
        packet_code + packet_id + packet_length + zero_authenticator + attrs + router_secret.encode()
    ).digest()

    return packet_code + packet_id + packet_length + authenticator + attrs


def send_disconnect_request(router_ip: str, router_secret: str, attributes: dict[str, str]) -> dict:
    """
    Send a RADIUS Disconnect-Request to terminate a user's session.

    MikroTik accepts User-Name + Framed-IP-Address and rejects Acct-Session-Id
    in this flow, so no Acct-Session-Id or Message-Authenticator is emitted.
    """
    import os

    if not router_ip or str(router_ip) == "None":
        return {"status": "error", "message": "Router has no reachable IP address"}

    identifier = os.urandom(1)[0]
    final_packet = _build_disconnect_packet(identifier, router_secret, attributes)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3)
    try:
        sock.sendto(final_packet, (router_ip, 3799))
        response, _ = sock.recvfrom(4096)
        response_code = response[0]
        if response_code == 41:
            return {"status": "success", "message": "Session terminated", "response_code": response_code}
        if response_code == 42:
            return {
                "status": "error",
                "message": f"Router rejected disconnect (NAK), code={response_code}",
                "response_code": response_code,
            }
        return {"status": "unknown", "message": f"Unexpected response code: {response_code}", "response_code": response_code}
    except socket.timeout:
        return {"status": "timeout", "message": "No response from router (may be offline)"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        sock.close()


def send_coa_disconnect(ip_address: str, nas_secret: str, username: str, framed_ip_address: str) -> dict:
    return send_disconnect_request(
        router_ip=ip_address,
        router_secret=nas_secret,
        attributes={
            "User-Name": username,
            "Framed-IP-Address": framed_ip_address,
        },
    )


def send_coa_reconnect(ip_address: str, nas_secret: str, username: str) -> dict:
    """
    Send a Change of Authorization (CoA) request to force re-authentication.
    Code type=43 for CoA-Request.
    """
    import os

    identifier = os.urandom(1)[0]
    authenticator = os.urandom(16)

    attrs = b""
    attrs += _encode_text_attr(1, username)

    attrs += struct.pack("!BB", 80, 18) + b"\x00" * 16

    packet_code = bytes([43])
    packet_id = bytes([identifier])
    packet_length = struct.pack("!H", 20 + len(attrs))

    hmac_data = packet_code + packet_id + packet_length + authenticator + attrs
    import hmac

    msg_authenticator = hmac.new(nas_secret.encode(), hmac_data, hashlib.md5).digest()

    full_attrs_list = bytearray(attrs)
    idx = 0
    while idx < len(full_attrs_list):
        attr_type = full_attrs_list[idx]
        attr_len = full_attrs_list[idx + 1]
        if attr_type == 80:
            for i in range(16):
                full_attrs_list[idx + 2 + i] = msg_authenticator[i]
            break
        idx += attr_len

    final_packet = packet_code + packet_id + packet_length + authenticator + bytes(full_attrs_list)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3)
    try:
        sock.sendto(final_packet, (ip_address, 3799))
        response, _ = sock.recvfrom(4096)
        response_code = response[0]
        if response_code == 44:
            return {"status": "success", "message": "CoA accepted", "response_code": response_code}
        if response_code == 45:
            return {"status": "error", "message": f"Router rejected CoA (NAK), code={response_code}", "response_code": response_code}
        return {"status": "unknown", "message": f"Unexpected response code: {response_code}", "response_code": response_code}
    except socket.timeout:
        return {"status": "timeout", "message": "No response from router"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        sock.close()
