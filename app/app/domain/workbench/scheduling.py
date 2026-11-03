"""Project lanes are selected from the trusted internal profile, never client hints."""

from typing import Literal

ProjectLane = Literal["execution", "lookup"]
LOOKUP_PROFILE = "FIND_EXPERT"


def lane_of(profile_id: str) -> ProjectLane:
    return "lookup" if profile_id == LOOKUP_PROFILE else "execution"
