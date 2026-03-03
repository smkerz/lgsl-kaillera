#!/usr/bin/env python3
"""
Kaillera v086 Server Status Poller for LGSL

Connects to an EmuLinker/Kaillera server using the full v086 binary UDP
protocol, retrieves the server status (connected users + active games),
and writes the result to a JSON file for LGSL to consume.

Usage:
    python3 kaillera_poll.py [--output /path/to/status.json]

Protocol reference:
    https://kaillerareborn.github.io/resources/kailleraprotocol.txt
"""

import socket
import struct
import json
import os
import sys
import time
import tempfile
import logging
import argparse

# ============ CONFIGURATION ============
SERVER_IP       = "127.0.0.1"
SERVER_PORT     = 27888
BOT_USERNAME    = "lgsl_poll"
CLIENT_TYPE     = "LGSL/1.0"
CONNECTION_TYPE = 1              # 1=LAN, 2=Excellent, 3=Good, 4=Average, 5=Low, 6=Bad
TIMEOUT         = 5              # Socket timeout in seconds
MAX_RECV_LOOPS  = 15             # Max receive attempts waiting for ServerStatus
OUTPUT_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "lgsl_kaillera_status.json")

# Message types
MSG_QUIT            = 0x01
MSG_USER_JOINED     = 0x02
MSG_USER_INFO       = 0x03
MSG_SERVER_STATUS   = 0x04
MSG_SERVER_ACK      = 0x05
MSG_CLIENT_ACK      = 0x06

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("kaillera_poll")


# ============ PROTOCOL HELPERS ============

def build_bundle(messages):
    """Build a Kaillera v086 UDP bundle.

    Args:
        messages: list of (msg_number, msg_type, body_bytes) tuples

    Returns:
        bytes: the complete UDP datagram to send
    """
    data = struct.pack("B", len(messages))
    for msg_num, msg_type, body in messages:
        msg_len = len(body) + 1  # +1 for the type byte
        data += struct.pack("<HHB", msg_num, msg_len, msg_type)
        data += body
    return data


def parse_bundle(data):
    """Parse a received Kaillera v086 UDP bundle.

    Returns:
        list of (msg_number, msg_type, body_bytes) tuples
    """
    messages = []
    offset = 0
    if len(data) < 1:
        return messages

    count = data[offset]
    offset += 1

    for _ in range(count):
        if offset + 5 > len(data):
            break
        msg_num, msg_len, msg_type = struct.unpack_from("<HHB", data, offset)
        offset += 5
        body_len = max(0, msg_len - 1)
        body = data[offset:offset + body_len]
        offset += body_len
        messages.append((msg_num, msg_type, body))

    return messages


def read_cstring(data, offset):
    """Read a null-terminated string from binary data.

    Returns:
        (string, new_offset)
    """
    try:
        end = data.index(0x00, offset)
    except ValueError:
        # No null terminator found, take rest of data
        return data[offset:].decode("latin-1", errors="replace"), len(data)
    return data[offset:end].decode("latin-1", errors="replace"), end + 1


def parse_server_status(body):
    """Parse a ServerStatus (0x04) message body.

    Format:
        \x00 (padding)
        uint32 LE numUsers
        uint32 LE numGames
        [per user: name\x00, uint32 ping, uint8 status, uint16 userID, uint8 connType]
        [per game: romName\x00, int32 gameID, clientType\x00, owner\x00, players\x00, uint8 status]

    Returns:
        dict with 'users' and 'games' lists
    """
    offset = 0

    # Skip padding byte
    if len(body) < 9:
        return {"users": [], "games": []}
    offset += 1  # \x00

    num_users = struct.unpack_from("<I", body, offset)[0]
    offset += 4
    num_games = struct.unpack_from("<I", body, offset)[0]
    offset += 4

    users = []
    for _ in range(num_users):
        if offset >= len(body):
            break

        name, offset = read_cstring(body, offset)

        if offset + 8 > len(body):
            break

        ping = struct.unpack_from("<I", body, offset)[0]
        offset += 4
        status = body[offset]
        offset += 1
        user_id = struct.unpack_from("<H", body, offset)[0]
        offset += 2
        conn_type = body[offset]
        offset += 1

        users.append({
            "name": name,
            "ping": ping,
            "status": status,
            "id": user_id,
            "connection": conn_type
        })

    games = []
    for _ in range(num_games):
        if offset >= len(body):
            break

        rom_name, offset = read_cstring(body, offset)

        if offset + 4 > len(body):
            break

        game_id = struct.unpack_from("<i", body, offset)[0]
        offset += 4

        client_type, offset = read_cstring(body, offset)
        owner, offset = read_cstring(body, offset)
        players_str, offset = read_cstring(body, offset)

        if offset > len(body):
            break

        game_status = body[offset] if offset < len(body) else 0
        offset += 1

        games.append({
            "name": rom_name,
            "id": game_id,
            "emulator": client_type,
            "owner": owner,
            "players": players_str,
            "status": game_status
        })

    return {"users": users, "games": games}


# ============ PROTOCOL PHASES ============

def phase1_hello(server_ip, server_port):
    """Phase 1: HELLO beacon on main port.

    Sends HELLO0.83, expects HELLOD00D{port}.
    Returns the assigned private port number.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(TIMEOUT)

    try:
        sock.sendto(b"HELLO0.83\x00", (server_ip, server_port))
        data, addr = sock.recvfrom(256)
    finally:
        sock.close()

    response = data.decode("latin-1", errors="replace")

    if not response.startswith("HELLOD00D"):
        if response.startswith("TOO"):
            raise ConnectionError("Server is full (TOO response)")
        raise ConnectionError(f"Unexpected response: {response!r}")

    # Extract port number: HELLOD00D{port}\x00
    port_str = response[9:].rstrip("\x00")
    try:
        assigned_port = int(port_str)
    except ValueError:
        raise ConnectionError(f"Invalid port in HELLOD00D response: {port_str!r}")

    log.info("Phase 1 OK: assigned port %d", assigned_port)
    return assigned_port


def phase2_get_status(server_ip, assigned_port, username=None):
    """Phase 2: v086 binary protocol on assigned port.

    Sends UserInfo, exchanges ACKs, receives ServerStatus, sends Quit.
    Returns parsed status dict with 'users' and 'games'.
    """
    bot_name = username or BOT_USERNAME
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(TIMEOUT)
    msg_num = 0

    try:
        # Step 1: Send UserInformation (type 0x03)
        body = (bot_name.encode("latin-1") + b"\x00"
                + CLIENT_TYPE.encode("latin-1") + b"\x00"
                + struct.pack("B", CONNECTION_TYPE))
        bundle = build_bundle([(msg_num, MSG_USER_INFO, body)])
        sock.sendto(bundle, (server_ip, assigned_port))
        msg_num += 1

        # Step 2: Receive ServerACK (type 0x05) and exchange ACKs
        # ACK body: \x00 + 4x uint32 LE (0,1,2,3) = 17 bytes
        ack_body = b"\x00" + struct.pack("<IIII", 0, 1, 2, 3)
        server_status = None
        ack_sent = 0

        for loop in range(MAX_RECV_LOOPS):
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                if server_status:
                    break
                if ack_sent >= 3:
                    break
                continue

            messages = parse_bundle(data)

            for m_num, m_type, m_body in messages:
                log.debug("Received msg #%d type=0x%02X len=%d",
                          m_num, m_type, len(m_body))

                if m_type == MSG_SERVER_ACK and ack_sent < 6:
                    # Send ClientACK (type 0x06) 3 times
                    ack_messages = []
                    for i in range(3):
                        ack_messages.append((msg_num, MSG_CLIENT_ACK, ack_body))
                        msg_num += 1
                    bundle = build_bundle(ack_messages)
                    sock.sendto(bundle, (server_ip, assigned_port))
                    ack_sent += 3
                    log.info("Sent %d ClientACKs", ack_sent)

                elif m_type == MSG_SERVER_STATUS:
                    server_status = parse_server_status(m_body)
                    log.info("Received ServerStatus: %d users, %d games",
                             len(server_status["users"]),
                             len(server_status["games"]))

            # If we have the status, we can stop
            if server_status:
                break

        # Step 3: Send Quit (type 0x01)
        quit_body = (b"\x00"
                     + struct.pack("<H", 0xFFFF)
                     + bot_name.encode("latin-1") + b"\x00")
        bundle = build_bundle([(msg_num, MSG_QUIT, quit_body)])
        sock.sendto(bundle, (server_ip, assigned_port))
        log.info("Sent Quit message")

    finally:
        sock.close()

    if not server_status:
        raise TimeoutError("Never received ServerStatus (type 0x04)")

    return server_status


# ============ OUTPUT ============

def write_json_atomic(data, output_file):
    """Write JSON data atomically using tempfile + os.replace."""
    dir_name = os.path.dirname(output_file) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, output_file)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ============ MAIN ============

def main():
    parser = argparse.ArgumentParser(description="Kaillera v086 status poller for LGSL")
    parser.add_argument("--ip", default=SERVER_IP, help="EmuLinker server IP")
    parser.add_argument("--port", type=int, default=SERVER_PORT, help="EmuLinker main port")
    parser.add_argument("--output", default=OUTPUT_FILE, help="Output JSON file path")
    parser.add_argument("--username", default=BOT_USERNAME, help="Bot username")
    args = parser.parse_args()

    try:
        # Phase 1: HELLO beacon
        assigned_port = phase1_hello(args.ip, args.port)

        # Phase 2: v086 protocol — get status
        status = phase2_get_status(args.ip, assigned_port, username=args.username)

        # Filter out our own bot from the user list
        real_users = [u for u in status["users"] if u["name"] != args.username]

        output = {
            "last_updated": int(time.time()),
            "online": True,
            "server_name": "Kaillera / EmuLinker",
            "players": len(real_users),
            "max_players": 100,
            "games_count": len(status["games"]),
            "users": real_users,
            "games": status["games"]
        }

        log.info("Success: %d players, %d games", output["players"], output["games_count"])

    except Exception as e:
        log.error("Poll failed: %s", e)
        output = {
            "last_updated": int(time.time()),
            "online": False,
            "server_name": "Kaillera / EmuLinker",
            "players": 0,
            "max_players": 100,
            "games_count": 0,
            "users": [],
            "games": []
        }

    write_json_atomic(output, args.output)
    log.info("Written to %s", args.output)


if __name__ == "__main__":
    main()
