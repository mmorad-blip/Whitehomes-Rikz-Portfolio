class Rejected(Exception):
    """A source file (or batch) that cannot be imported.

    The message is a plain-English reason meant for the person who uploaded
    the file. Nothing from a rejected file or batch is imported.
    """

    def __init__(self, reason: str, *, file: str | None = None):
        self.reason = reason
        self.file = file
        super().__init__(f"{file}: {reason}" if file else reason)
