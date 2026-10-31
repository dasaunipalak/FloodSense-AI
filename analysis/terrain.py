"""Descriptive elevation differences; no flood-risk classification."""

import math


def summarize_elevations(points):
    """Summarize validated center-first points; preserve missing elevations.

    Each point contains label and elevation_m. Pair differences use neighbor
    minus center; summaries require the center and all four neighbors.
    """
    center = points[0]["elevation_m"]
    differences = []
    for point in points[1:]:
        elevation = point["elevation_m"]
        differences.append({"label": point["label"], "neighbor_minus_center_m":
                            None if center is None or elevation is None else elevation - center})
    missing = sum(p["elevation_m"] is None for p in points)
    neighbors = [p["elevation_m"] for p in points[1:]]
    return {
        "relative_elevation": differences,
        "missing_points": missing,
        "center_minus_neighbor_mean_m": None if missing else center - math.fsum(neighbors) / 4,
        "sample_elevation_range_m": None if missing else
            max(p["elevation_m"] for p in points) - min(p["elevation_m"] for p in points),
    }
