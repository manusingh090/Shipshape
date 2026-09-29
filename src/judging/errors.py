class JudgingError(Exception):
    """A rule said no. The message is written for the person who hit it."""

    status = 400
    code = "judging_error"

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class NotAssigned(JudgingError):
    """Deliberately indistinguishable from "no such project"."""

    status = 404
    code = "not_found"

    def __init__(self):
        super().__init__("No such project in your judging queue.")


class RateLimited(JudgingError):
    status = 429
    code = "slow_down"
