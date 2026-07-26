import socket
import sys


SOCKET = "/tmp/wayocr.sock"


def main():
    print("WayOCR status")

    # 检查 socket
    sock = socket.socket(
        socket.AF_UNIX,
        socket.SOCK_STREAM
    )

    try:
        sock.connect(SOCKET)
        print("Server: running")
        print("Socket: OK")

    except Exception:
        print("Server: stopped")
        print("Socket: unavailable")
        sys.exit(1)

    finally:
        sock.close()


if __name__ == "__main__":
    main()