import socket
import json


SOCKET = "/tmp/wayocr.sock"


def recognize(image):

    sock = socket.socket(
        socket.AF_UNIX,
        socket.SOCK_STREAM
    )

    try:
        sock.connect(SOCKET)

        request = {
            "image": image
        }

        sock.sendall(
            json.dumps(
                request
            ).encode("utf-8")
        )

        data = sock.recv(4096)

        response = json.loads(
            data.decode("utf-8")
        )

        if response.get("error"):
            raise RuntimeError(
                response["error"]
            )

        return response.get(
            "text",
            ""
        )

    finally:
        sock.close()