"""One error type for every way a supplier or datasheet call can fail. The message is safe to show: it never carries a key, a token, a URL with a key or a response body."""


class PartsError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


NOT_CONFIGURED = 'not_configured'
CREDENTIALS_REFUSED = 'credentials_refused'
RATE_LIMITED = 'rate_limited'
TRANSPORT = 'transport_error'
UNREADABLE = 'unreadable_response'
REQUEST_FAILED = 'request_failed'
