"""Associate scoring by accuracy and speed.

Not yet used for routing (v1 routes by load and round-robin). The score
feeds /stats now and becomes a routing weight once there is enough data.
"""
import asyncpg

# A response this fast or faster gets the full speed score.
FAST_MS = 10_000
# A response this slow or slower gets zero speed score.
SLOW_MS = 300_000


def speed_score(avg_response_ms: float | None) -> float:
    if avg_response_ms is None:
        return 0.5
    if avg_response_ms <= FAST_MS:
        return 1.0
    if avg_response_ms >= SLOW_MS:
        return 0.0
    return 1.0 - (avg_response_ms - FAST_MS) / (SLOW_MS - FAST_MS)


def combined_score(accuracy: float, avg_response_ms: float | None) -> float:
    """70 percent accuracy, 30 percent speed."""
    return round(0.7 * accuracy + 0.3 * speed_score(avg_response_ms), 3)


async def associate_scores(conn: asyncpg.Connection) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT a.id, a.name, a.accuracy,
               avg(v.response_time_ms) AS avg_ms,
               count(v.id) FILTER (WHERE v.status <> 'pending') AS answered
        FROM associates a
        LEFT JOIN verifies v ON v.associate_id = a.id
        WHERE a.status = 'active'
        GROUP BY a.id, a.name, a.accuracy
        ORDER BY a.id
        """
    )
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "answered": r["answered"],
            "avg_ms": float(r["avg_ms"]) if r["avg_ms"] is not None else None,
            "score": combined_score(float(r["accuracy"]), float(r["avg_ms"]) if r["avg_ms"] is not None else None),
        }
        for r in rows
    ]
