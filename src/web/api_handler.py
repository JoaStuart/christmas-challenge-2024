import hashlib
import json
import mimetypes
import os
import re
from typing import Any, Optional

import constants
from log import LOG
from proj_types.webmethod import WebMethod
from storage.datadb import DataDB
from web.handler import WebHandler
from web.response import WebResponse
from web.session import Session, SessionStorage
from web.socket_data import DataReceiver, DataSender


class APIHandler(WebHandler):
    API_PREFIX = "/a/v1/"

    def can_handle(self) -> bool:
        """
        Returns:
            bool: Whether this handler can handle the request
        """

        if self._request.path is None:
            return False

        return self._request.path.startswith(self.API_PREFIX)

    def _get_body(self) -> dict[str, Any]:
        """Tries to retrieve the body in JSON format

        Returns:
            dict[str, Any]: The retrieved body
        """

        if self._request.body is None:
            return {}

        contype = self._request.headers.get("Content-Type", "")
        if contype != "application/json":
            return {}

        if not isinstance(self._request.body, bytes):
            return {}

        try:
            return json.loads(self._request.body)
        except json.JSONDecodeError:
            LOG.debug("Could not decode JSON %s", self._request.body)
            return {}

    def handle(self, response: WebResponse) -> None:
        """Handles the request

        Args:
            response (WebResponse): The response to this request
        """

        if self._request.path is None:
            return

        # Load path and json body
        path = self._request.path[len(self.API_PREFIX) :].split("/")
        body: dict[str, Any] = self._get_body()

        # Tell the browser API responses should not be cached
        response.headers["Cache-Control"] = "no-store"

        match path[0]:
            case "register":
                self._register(body, response)

            case "login":
                self._login(body, response)

            case "user":
                self._user(response)

            case "upload":
                self._upload(path, self._request.body, response)

            case "rename":
                self._rename(body, response)

            case "move":
                self._move(body, response)

            case "delete":
                self._delete(body, response)

            case "folder":
                self._folder(body, response)

            case "listall":
                self._list_all(response)

            case "preview":
                self._preview(path, response)

            case "share":
                self._share(body, response)

            case "sharedetails":
                self._share_details(body, response)

            case _:
                if DataDB().files().check_file_id(path[0]):
                    if self._request.method == WebMethod.GET:

                        # User requests contents of a file
                        self._download(path, response)
                    elif self._request.method == WebMethod.POST:

                        # User overwrites a file
                        self._update(path, self._request.body, response)

                elif DataDB().shares().check_share_id(path[0]):

                    # The user downloads a shared file
                    self._download_share(path, body, response)

    def _check_email(self, email: str) -> bool:
        """Checks the provided Email address

        Args:
            email (str): The Email address to check

        Returns:
            bool: Whether this address is valid
        """

        return re.search(r"^\S+@\S+\.\S+$", email) is not None

    def _check_login(self, response: WebResponse) -> Optional[Session]:
        """Checks if the user is logged in

        Args:
            response (WebResponse): The response to send errors to

        Returns:
            Optional[Session]: The session if the user is logged in
        """

        session = self.get_session()
        if session is None:
            self._response_invalid_data(response, "You need to login.")
            return None

        return session

    def _response_invalid_data(self, response: WebResponse, message: str) -> None:
        """Modifies the response for invalid data

        Args:
            response (WebResponse): The response to modify
            message (str): The message to show to the user
        """

        response.code, response.msg = 500, "Invalid Form Data"
        response.json_body({"message": message})

    def _register(self, body: dict[str, Any], response: WebResponse) -> None:
        """Registers the user with the provided data

        Args:
            body (dict[str, Any]): The body containing user data
            response (WebResponse): The response to this request
        """

        userdb = DataDB().users()

        # Read user data
        userid = body.get("userid", None)
        email = body.get("email", None)
        password = body.get("passwd", None)

        # Check user data
        if userid is None or len(userid) < 3:
            self._response_invalid_data(
                response, "The User ID has to be at least 3 characters long!"
            )
            return

        # Check if the user ID is already taken
        if userdb.id_exists(userid):
            self._response_invalid_data(response, "This User ID is already taken!")
            return

        # Check if the email is valid
        if email is None or not self._check_email(email):
            self._response_invalid_data(response, "Invalid Email address!")
            return

        # Check if the email is already taken
        if userdb.email_exists(email):
            self._response_invalid_data(
                response, "This Email address is already taken!"
            )
            return

        # Check if the password is valid
        if password is None:
            self._response_invalid_data(response, "Failed to transmit password!")
            return

        # Register user using data
        userdb.register(userid, email, hashlib.sha512(password.encode()).hexdigest())

        response.json_body({"location": "/login"})

    def _login(self, body: dict[str, Any], response: WebResponse) -> None:
        """Logs the user in using the provided user data

        Args:
            body (dict[str, Any]): The body containing user data
            response (WebResponse): The response to this request
        """

        # Read user data
        userid = body.get("userid", None)
        password = body.get("passwd", None)

        # Check user data
        if userid is None:
            self._response_invalid_data(response, "Please provide a User ID!")
            return

        if password is None:
            self._response_invalid_data(response, "Failed to transmit password!")
            return

        # Try to log in
        session = SessionStorage().create_session(
            self._request.ip, userid, hashlib.sha512(password.encode()).hexdigest()
        )

        # Check if login was successful
        if session is None:
            self._response_invalid_data(
                response, "Could not login with these credentials!"
            )
            return

        # Set the session cookie and redirect to the user's home
        response.headers["Set-Cookie"] = (
            f"session={session.session_id}; SameSite=Lax; HttpOnly; Path=/"
        )
        response.json_body({"location": f"/~{userid}/"})

    def _user(self, response: WebResponse) -> None:
        """Queries user information

        Args:
            response (WebResponse): The response to this request
        """

        # Check if the user is logged in
        if not (session := self._check_login(response)):
            return

        response.json_body({"user_name": session.userid})

    def _upload(
        self,
        path: list[str],
        body: Optional[bytes | DataReceiver],
        response: WebResponse,
    ) -> None:
        """Performs a file upload

        Args:
            path (list[str]): The path the user requested
            body (bytes): The raw file (body) in bytes
            response (WebResponse): The response to this request
        """

        if body is None:
            self._response_invalid_data(response, "No data provided!")
            return

        # Check if the user is logged in
        if not (session := self._check_login(response)):
            return

        if len(path) == 2:
            # We do not have a parent dir
            parent_dir: str = ""
            file_name: str = path[1]

        elif len(path) > 2:
            # We have a parent dir
            parent_dir: str = path[1]
            file_name: str = path[2]

        else:
            # We do not even have a file
            self._response_invalid_data(response, "Invalid Data.")
            return

        file_db = DataDB().files()

        # Check if a file with this name already existssc
        if not file_db.name_check(session, parent_dir, file_name):
            self._response_invalid_data(
                response, "A file with this name already exists!"
            )
            return

        # Enter file into database
        file_id = file_db.make_file(session, parent_dir, file_name)

        # Write file to disk
        with open(os.path.join(constants.FILES, file_id), "wb") as file:
            if isinstance(body, DataReceiver):
                body.receive_into(file)
            else:
                file.write(body)

        # Respond with the file_id for JS
        response.json_body({"file_id": file_id})

    def _update(
        self,
        path: list[str],
        body: Optional[bytes | DataReceiver],
        response: WebResponse,
    ) -> None:
        """Updates the contents of an already existing file

        Args:
            path (list[str]): The path containing the file id
            body (bytes): The data to save into the file
            response (WebResponse): The response to this request
        """

        if body is None:
            self._response_invalid_data(response, "No data provided!")
            return

        # Check if the user is logged in
        if not (session := self._check_login(response)):
            return

        # Check if the file_id is passed into the
        if len(path) < 1:
            self._response_invalid_data(response, "No file selected!")
            return

        file_db = DataDB().files()
        file_id = path[0]

        # Check if the file exists and the user has access to it
        if not file_db.check_file_id(file_id) or not file_db.can_download(
            session, file_id
        ):
            self._response_invalid_data(
                response,
                "The file does not exist or you do not have permissions for it.",
            )
            return

            # Modify the contents of the file
        with open(os.path.join(constants.FILES, file_id), "wb") as wf:
            if isinstance(body, DataReceiver):
                body.receive_into(wf)
            else:
                wf.write(body)

    def _download(self, path: list[str], response: WebResponse) -> None:
        """Performs a file download

        Args:
            path (list[str]): The path containing the file the user wants
            response (WebResponse): The response to download with
        """

        # Check if the user is logged in
        if not (session := self._check_login(response)):
            return

        file_db = DataDB().files()
        file_id = path[0]

        # Check if the file should be downloaded
        do_download = path[1] == "download" if len(path) > 1 else False

        # Check if user has permissions to download file
        if not file_db.can_download(session, file_id):
            self._response_invalid_data(response, "You cannot download this file!")
            return

        # Check if the file is a folder
        if file_db.check_folder_id(file_id):
            self._response_invalid_data(response, "You cannot download a folder!")
            return

        # Download file
        response.body = DataSender(os.path.join(constants.FILES, file_id))

        # Guess MIME type for browser
        response.headers["Content-Type"] = (
            mimetypes.guess_type(file_db.get_name(file_id))[0]
            or constants.MIME_FALLBACK
        )

        # Add content disposition for download
        if do_download:
            response.headers["Content-Disposition"] = (
                f'attachment; filename="{file_db.get_name(file_id)}"'
            )

    def _rename(self, body: dict[str, Any], response: WebResponse) -> None:
        """Renames a file selected in the body to a new name

        Args:
            body (dict[str, Any]): The data for renaming
            response (WebResponse): The response to this request
        """

        # Check if the user is logged in
        if not (session := self._check_login(response)):
            return

        file_db = DataDB().files()

        file_id = body.get("file_id", None)
        new_name = body.get("new_name", None)

        # Check if the file exists
        if file_id is None or not file_db.check_file_id(file_id):
            self._response_invalid_data(response, "File does not exist.")
            return

        # Check if the user has access to the file
        if not file_db.can_download(session, file_id):
            self._response_invalid_data(response, "You can't do that!")
            return

        # Check if the new name is valid
        if new_name is None or len(new_name) == 0:
            self._response_invalid_data(response, "No new name specified!")
            return

        # Rename the file
        file_db.rename(file_id, new_name)

    def _move(self, body: dict[str, Any], response: WebResponse) -> None:
        """Moves a file into a new folder

        Args:
            body (dict[str, Any]): The file and folder data
            response (WebResponse): The response to this request
        """

        # Check if the user is logged in
        if not (session := self._check_login(response)):
            return

        file_db = DataDB().files()

        file_id = body.get("file_id", None)
        new_path = body.get("folder_id", None)

        # Check if file exists
        if file_id is None or not file_db.check_file_id(file_id):
            self._response_invalid_data(response, "File does not exist.")
            return

        # Check if user has access to file
        if not file_db.can_download(session, file_id):
            self._response_invalid_data(response, "You can't do that!")

        # Check if folder exists
        if new_path is None or not (
            file_db.check_folder_id(new_path) or len(new_path) == 0
        ):
            self._response_invalid_data(response, "The target path does not exist.")
            return

        # Move the file
        file_db.move(file_id, new_path)

    def _delete(self, body: dict[str, Any], response: WebResponse) -> None:
        """Deletes a file

        Args:
            body (dict[str, Any]): The data of the file
            response (WebResponse): The response to this request
        """

        # Check if the user is logged in
        if not (session := self._check_login(response)):
            return

        file_db = DataDB().files()
        file_id = body.get("file_id", None)

        # Check if file exists
        if file_id is None or not file_db.check_file_id(file_id):
            self._response_invalid_data(response, "File does not exist.")
            return

        # Check if user has access to file
        if not file_db.can_download(session, file_id):
            self._response_invalid_data(response, "You can't do that!")

        # Delete the file
        file_db.delete_file(file_id)

    def _folder(self, body: dict[str, Any], response: WebResponse) -> None:

        # Check if the user is logged in
        if not (session := self._check_login(response)):
            return

        file_db = DataDB().files()
        parent_id = body.get("parent_id", None)
        folder_name = body.get("folder_name", None)

        # Check if parent_id and folder_name are sent
        if parent_id is None or folder_name is None:
            self._response_invalid_data(response, "No data sent!")
            return

        # Check if the folder name is valid
        if len(parent_id) > 0 and not file_db.check_folder_id(parent_id):
            self._response_invalid_data(response, "The parent folder does not exist!")
            return

        # Create the folder
        folder_id = file_db.make_folder(session, parent_id, folder_name)

        response.json_body({"folder_id": folder_id})

    def _list_all(self, response: WebResponse) -> None:
        """Lists all directories and files belonging to the user

        Args:
            response (WebResponse): The response to this request
        """

        # Check if the user is logged in
        if not (session := self._check_login(response)):
            return

        # Respond with the list of files
        response.json_body(DataDB().files().list_all(session))

    def _send_file(self, web_file: str, response: WebResponse) -> None:
        """Sends a file inside the /web/ directory

        Args:
            web_file (str): The file to send
            response (WebResponse): The response to send to
        """

        # Send the file
        path = os.path.join(constants.WEB, web_file)
        response.body = DataSender(path)

        # Guess MIME type for browser
        response.headers["Content-Type"] = (
            mimetypes.guess_type(web_file)[0] or constants.MIME_FALLBACK
        )

    def _preview(self, path: list[str], response: WebResponse) -> None:
        """Searches for the preview type a file needs

        Args:
            path (list[str]): The ID of the file inside the path
            response (WebResponse): The response to this request
        """

        # Check if the user is logged in
        if not (session := self._check_login(response)):
            return

        file_db = DataDB().files()
        file_id = path[1] if len(path) > 1 else None

        # Check if file id was sent
        if file_id is None:
            self._response_invalid_data(response, "No file ID specified!")
            return

        # Check if user has access to file
        if not file_db.can_download(session, file_id):
            self._response_invalid_data(response, "You can't do that")
            return

        # Get MIME type of file
        mime = mimetypes.guess_type(file_db.get_name(file_id))[0]

        if mime is None:
            # If the MIME type is unknown, send a default preview
            self._send_file("no_preview.html", response)

        elif mime.startswith("text/"):
            # If the MIME type is text, send a text preview
            self._send_file("text_preview.html", response)

        elif mime.startswith("image/"):
            # If the MIME type is an image, send an image preview
            self._send_file("img_preview.html", response)

        elif mime.startswith("video/"):
            # If the MIME type is a video, send a video preview
            self._send_file("vid_preview.html", response)

        else:
            # If the MIME type is unknown, send a default preview
            self._send_file("no_preview.html", response)

    def _share(self, body: dict[str, Any], response: WebResponse) -> None:
        """Creates a share link for the specified file

        Args:
            body (dict[str, Any]): The body containing the file and password
            response (WebResponse): The response to this request
        """

        # Check if the user is logged in
        if not (session := self._check_login(response)):
            return

        data_db = DataDB()
        file_db = data_db.files()
        share_db = data_db.shares()

        file_id = body.get("file_id", None)
        password = body.get("password", None)

        # Check if file_id was sent
        if file_id is None:
            self._response_invalid_data(response, "You didn't provide a file id.")
            return

        # Check if the user has access to the file
        if not file_db.can_download(session, file_id):
            self._response_invalid_data(response, "You cannot do that!")
            return

        # Create a share link
        share_id = share_db.create_share(
            session,
            file_id,
            (
                hashlib.sha512(password.encode()).hexdigest()
                if password is not None
                else None
            ),
        )

        # Respond with the share_id
        response.json_body({"share_id": share_id})

    def _share_details(self, body: dict[str, Any], response: WebResponse) -> None:
        """Retrieves the details of a share link

        Args:
            body (dict[str, Any]): The body containing the share ID
            response (WebResponse): The response to this request
        """

        data_db = DataDB()
        share_db = data_db.shares()
        file_db = data_db.files()

        share_id = body.get("share_id", None)

        file_id = share_db.get_file_id(share_id)

        # Send the name of the file and whether it has a password
        response.json_body(
            {
                "name": file_db.get_name(file_id),
                "password": share_db.has_password(share_id),
            }
        )

    def _download_share(
        self, path: list[str], body: dict[str, Any], response: WebResponse
    ) -> None:
        """Downloads the file contained in a share

        Args:
            path (list[str]): The path containing the share ID
            body (dict[str, Any]): The body containing the potential password
            response (WebResponse): The response to this request
        """

        data_db = DataDB()
        share_db = data_db.shares()
        file_db = data_db.files()

        share_id = path[0]
        password = body.get("password", None)
        do_download = path[1] == "download" if len(path) > 1 else False

        # Check if the user can download the share with the provided password
        if not share_db.can_download(
            share_id,
            (
                hashlib.sha512(password.encode()).hexdigest()
                if password is not None
                else None
            ),
        ):
            self._response_invalid_data(response, "You cannot do that!")
            return

        # Get the file ID of the share
        file_id = share_db.get_file_id(share_id)

        # Send the file and guess its MIME type for the browser
        response.body = DataSender(os.path.join(constants.FILES, file_id))
        response.headers["Content-Type"] = (
            mimetypes.guess_type(file_db.get_name(file_id))[0]
            or constants.MIME_FALLBACK
        )

        # Add content disposition for download
        if do_download:
            response.headers["Content-Disposition"] = (
                f'attachment; filename="{file_db.get_name(file_id)}"'
            )
