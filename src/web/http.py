import socket

import time
import urllib.parse
import constants
from proj_types.case_insensitive_dict import CaseInsensitiveDict
from proj_types.proto_error import ProtocolError
from proj_types.webmethod import WebMethod
from web.encoding import Encoding
from web.request import WebRequest
from web.response import WebResponse
from web.socket_data import DataReceiver, DataSender


class HttpRequest(WebRequest):
    def __init__(self, sock: socket.socket, addr: tuple[str, int]) -> None:
        super().__init__(sock, addr)

    def _read_line(self) -> str:
        """Reads one line from the request socket

        Returns:
            str: The line read, not including any padding like `\r\n`
        """

        buffer: list[bytes] = []

        # Read data into buffer until encountering `\n`
        while (c := self._socket.recv(1)) != b"\n":
            buffer.append(c)

        # Joining and stripping the buffer
        return b"".join(buffer).decode().strip()

    def handle(self) -> None:
        """Read in the request"""

        self._read_status()
        self._read_headers()
        self._read_body()

    def _read_status(self) -> None:
        """Reads the status line and parses it into their corresponding variables

        Raises:
            ProtocolError: Upon encountering any protocol related error
        """

        # Read status
        status = self._read_line().split(" ", 2)

        # Check for 3 status arguments
        if len(status) != 3:
            raise ProtocolError(f"The status line {str(status)} needs three arguments!")

        # Check for the appropriate HTTP version
        if status[2].upper() != constants.HTTP_VERSION:
            raise ProtocolError(f"Unknown version {status[2]} could not be recognized!")

        # Parse status into variables
        self._method = WebMethod(status[0])
        path = status[1].split("?")
        self._path = urllib.parse.unquote(path[0])

    def _read_headers(self) -> None:
        """Read all provided headers

        Raises:
            ProtocolError: Upon encountering any protocol related error
        """

        while True:
            # Read header line
            l = self._read_line()

            # Check if end of headers is reached (empty line)
            if len(l) == 0:
                return

            # Split header into key and value
            header = l.split(": ", 1)
            if len(header) != 2:
                self._headers[header[0]] = ""
                continue

            # Store header into CaseInsensitiveDict
            self._headers[header[0]] = header[1]

    def _read_body(self) -> None:
        """Read the body if the request has one

        Raises:
            ProtocolError: Upon encountering any protocol related error
        """

        # Check if there is a body
        if "Content-Length" not in self._headers:
            return

        # Get the length of the body
        try:
            content_length = int(self._headers["Content-Length"])
        except ValueError:
            raise ProtocolError("Content-Length must be a number!")

        # Check if the body is too large to be buffered instantly
        if content_length >= constants.BUFFERED_CHUNK_SIZE:
            self._body = DataReceiver(self._socket, content_length)
            return

        # Receive the body
        self._body = self._socket.recv(content_length)

    def response(self) -> WebResponse:
        """
        Returns:
            WebResponse: The HTTP response based on this request
        """

        return HttpResponse(self._socket, self.headers)

    def _decompress_body(self) -> None:
        """Decompresses the body using the provided encodings"""

        if "Content-Encoding" not in self._headers or self._body is None:
            return

        encodings = self._headers["Content-Encoding"].split(", ")

        for encoding in encodings:
            encoding = Encoding.get_encoding(encoding)
            if encoding is None:
                continue

            if isinstance(self._body, DataReceiver):
                decomp = encoding.chunked_decompression()
                if decomp is None:
                    continue

                self._body.decompress(decomp)
            else:
                self._body = encoding.decompress(self._body)


class HttpResponse(WebResponse):
    def __init__(
        self, sock: socket.socket, recv_headers: CaseInsensitiveDict[str]
    ) -> None:
        super().__init__(sock, recv_headers)

    def _send_line(self, line: str) -> None:
        """Sends the specified line plus line terminators `\r\n`

        Args:
            line (str): The line to be sent
        """

        self._socket.sendall(line.encode() + b"\r\n")

    def send(self) -> None:
        """Sends the response to the requesting socket"""

        # Append `Content-Length` if necessary
        if self.body is not None:
            self._compress_body()
            self.headers["Content-Length"] = str(len(self.body))

        self._send_status()
        self._send_headers()
        self._send_body()

        self._socket.close()

    def _compress_sender(self, encoding: Encoding) -> bool:
        """Appends a chunked compression algorithm to the sender

        Args:
            encoding (Encoding): The encoding to use

        Returns:
            bool: Whether this encoding supports chunked compression
        """

        if not isinstance(self.body, DataSender):
            return False

        compressor = encoding.chunked_compression()

        if compressor is None:
            return False

        self.body.compress(compressor)
        return True

    def _compress_body(self) -> None:
        """Tries to compress the body using the provided encodings"""

        # Check if there are supported encodings and a body
        if "Accept-Encoding" not in self._recv_headers or not self.body:
            return

        accept_encoding = self._recv_headers["Accept-Encoding"]

        used_encodings: list[str] = []

        # Go through all our encodings and apply them to the body
        for encoding in Encoding.supported_encodings():
            # Check if the client accepts the encoding
            if encoding.name() not in accept_encoding:
                continue

            if isinstance(self.body, DataSender):
                if self._compress_sender(encoding):
                    used_encodings.append(encoding.name())

            else:
                # Check if the encoded body is smaller than original
                tested_encoding = encoding.compress(self.body)
                if len(tested_encoding) > len(self.body):
                    continue

                # Apply encoding to the body and header list
                self.body = tested_encoding
                used_encodings.append(encoding.name())

        # Create the Content-Encoding header and set the new body
        self.headers["Content-Encoding"] = ", ".join(used_encodings)

    def _send_status(self) -> None:
        """Sends the status line"""

        self._send_line(f"{constants.HTTP_VERSION} {self.code} {self.msg}")

    def _send_headers(self) -> None:
        """Sends all headers plus the empty line denoting the end of headers"""

        headers = self._default_headers() | self.headers

        for key, val in headers.items():
            self._send_line(f"{key}: {val}")

        self._send_line("")

    def _send_body(self) -> None:
        """Sends the response body if specified"""

        if self.body is None:
            return

        if isinstance(self.body, DataSender):
            self.body.send_to(self._socket)
        else:
            self._socket.sendall(self.body)

    def _default_headers(self) -> dict[str, str]:
        """
        Returns:
            dict[str, str]: The default headers for the response
        """

        return {
            "Date": time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime()),
            "Server": constants.SERVER_NAME,
        }
