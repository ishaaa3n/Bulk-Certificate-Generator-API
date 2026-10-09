"""Domain errors. The service layer raises these; the API layer maps them to HTTP (see main.py).

Keeping HTTP out of the service layer means the same logic can be reused by a CLI, a worker or a different transport.
"""


class DomainError(Exception):
    status_code = 400

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class NotFound(DomainError):
    status_code = 404


class Conflict(DomainError):
    status_code = 409


class Gone(DomainError):
    status_code = 410


class PayloadTooLarge(DomainError):
    status_code = 413
