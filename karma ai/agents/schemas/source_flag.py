from enum import Enum


class SourceFlag(str, Enum):
    user_stated = "user_stated"
    inferred = "inferred"
    default_applied = "default_applied"
    skipped_by_user = "skipped_by_user"
