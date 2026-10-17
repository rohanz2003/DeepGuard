"""Request class that keeps bounded multipart uploads in memory."""

import io

from flask import Request as FlaskRequest, current_app
from werkzeug.exceptions import RequestEntityTooLarge


class BoundedMemoryFile(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > current_app.config["MAX_UPLOAD_BYTES"]:
            raise RequestEntityTooLarge()
        return super().write(data)


class InMemoryRequest(FlaskRequest):
    def _get_file_stream(
        self, total_content_length, content_type, filename=None, content_length=None
    ):
        # MAX_CONTENT_LENGTH bounds the whole multipart body before parsing.
        # Returning BytesIO avoids Werkzeug's default temporary-file spool.
        return BoundedMemoryFile()
