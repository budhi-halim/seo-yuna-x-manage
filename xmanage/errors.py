"""Concise errors: never include raw HTTP bodies or credential-bearing URLs."""


class ManagerError(Exception):
    pass


class ApiError(ManagerError):
    def __init__(self, message, status=None, *, ambiguous=False):
        super().__init__(message)
        self.status = status
        self.ambiguous = ambiguous


class IncompleteTimeline(ManagerError):
    pass


class UncertainReply(ManagerError):
    pass
