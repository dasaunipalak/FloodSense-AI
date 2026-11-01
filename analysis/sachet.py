"""Pure CAP parsing, lifecycle reconciliation, and conservative point matching."""

from datetime import datetime, timezone
import math
import re
import xml.etree.ElementTree as ET

CAP = "{urn:oasis:names:tc:emergency:cap:1.2}"


class AlertDataError(ValueError):
    """Untrusted feed data cannot support a verified alert result."""


def xml_root(body):
    """Accept bounded UTF-8 XML only; reject DTD/entities before parsing.

    Custom entities and external DTDs are never expanded. The transport also
    enforces a byte limit. Predefined XML escapes (e.g. &amp;) remain supported.
    """
    try:
        text = body.decode("utf-8-sig")
        if "\x00" in text or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I):
            raise AlertDataError("DTD and entity declarations are forbidden")
        root = ET.fromstring(text)
        if sum(1 for _ in root.iter()) > 20000:
            raise AlertDataError("XML element limit exceeded")
        return root
    except (UnicodeError, ET.ParseError) as exc:
        raise AlertDataError("Invalid UTF-8 XML") from exc


def timestamp(value, *, required=True):
    """Normalize ISO 8601 CAP timestamps to UTC; never guess a timezone."""
    if not value and not required:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone missing")
        return parsed.astimezone(timezone.utc).isoformat()
    except (AttributeError, TypeError, ValueError) as exc:
        raise AlertDataError("Missing or invalid timezone-aware CAP timestamp") from exc


def _text(node, name, *, required=False):
    value = (node.findtext(CAP + name) or "").strip()
    if required and not value:
        raise AlertDataError(f"Missing CAP {name}")
    return value or None


def _polygon(value):
    """Only simple closed non-dateline polygons are supported; otherwise unknown."""
    try:
        points = [tuple(map(float, pair.split(","))) for pair in value.split()]
        if not 4 <= len(points) <= 10000 or points[0] != points[-1]:
            return None
        if any(len(p) != 2 or not all(math.isfinite(v) for v in p)
               or not -90 < p[0] < 90 or not -180 <= p[1] <= 180 for p in points):
            return None
        if max(p[1] for p in points) - min(p[1] for p in points) >= 180:
            return None
        # Reject zero-area and self-intersecting rings, including touching edges.
        if len(set(points[:-1])) != len(points) - 1:
            return None
        area = sum(a[1] * b[0] - b[1] * a[0] for a, b in zip(points, points[1:]))
        if abs(area) < 1e-12:
            return None
        def cross(a, b, c):
            return (b[1]-a[1])*(c[0]-a[0]) - (b[0]-a[0])*(c[1]-a[1])
        edges = list(zip(points, points[1:]))
        # Bound quadratic validation work on untrusted rings.
        if len(edges) > 256:
            return None
        for i, (a, b) in enumerate(edges):
            for j in range(i + 1, len(edges)):
                if j == i + 1 or (i == 0 and j == len(edges) - 1):
                    continue
                c, d = edges[j]
                if cross(a,b,c)*cross(a,b,d) <= 0 and cross(c,d,a)*cross(c,d,b) <= 0:
                    if (max(min(a[0],b[0]),min(c[0],d[0])) <= min(max(a[0],b[0]),max(c[0],d[0]))
                            and max(min(a[1],b[1]),min(c[1],d[1])) <= min(max(a[1],b[1]),max(c[1],d[1]))):
                        return None
        return points
    except (TypeError, ValueError, IndexError):
        return None


def parse_cap(body, source_url):
    """Parse official CAP 1.2 preserving every language/area and original times."""
    root = xml_root(body)
    if root.tag != CAP + "alert":
        raise AlertDataError("Expected CAP 1.2 alert")
    result = {name: _text(root, name, required=True)
              for name in ("identifier", "sender", "status", "msgType", "scope")}
    if result["status"] not in {"Actual", "Exercise", "System", "Test", "Draft"}:
        raise AlertDataError("Unknown CAP status")
    if result["msgType"] not in {"Alert", "Update", "Cancel", "Ack", "Error"}:
        raise AlertDataError("Unknown CAP message type")
    if result["scope"] not in {"Public", "Restricted", "Private"}:
        raise AlertDataError("Unknown CAP scope")
    result["sent_original"] = _text(root, "sent", required=True)
    result["sent"] = timestamp(result["sent_original"])
    result["source_url"] = source_url
    result["references"] = []
    result["references_original"] = _text(root, "references")
    for reference in (result["references_original"] or "").split():
        parts = reference.split(",")
        if len(parts) != 3 or not parts[0] or not parts[1]:
            raise AlertDataError("Invalid CAP reference")
        result["references"].append({"sender": parts[0], "identifier": parts[1], "sent": timestamp(parts[2])})
    if result["msgType"] in {"Update", "Cancel"} and not result["references"]:
        raise AlertDataError("Update/cancellation without references")
    result["info"] = []
    for node in root.findall(CAP + "info"):
        info = {name: _text(node, name, required=name in {"event", "severity", "urgency", "certainty"}) for name in
                ("language", "senderName", "event", "severity", "urgency", "certainty", "headline", "description", "instruction")}
        for name in ("effective", "onset", "expires"):
            info[name + "_original"] = _text(node, name)
            info[name] = timestamp(info[name + "_original"], required=False)
        info["areas"] = []
        for area in node.findall(CAP + "area"):
            codes = []
            for code in area.findall(CAP + "geocode"):
                if _text(code, "valueName") == "LGD District Code":
                    value = _text(code, "value", required=True)
                    if not value.isascii() or not value.isdigit():
                        raise AlertDataError("Invalid LGD district code")
                    codes.append(value)
            info["areas"].append({
                "description": _text(area, "areaDesc"), "lgd_district_codes": sorted(set(codes)),
                "polygons": [p.text or "" for p in area.findall(CAP + "polygon")],
                "unsupported_circle": bool(area.findall(CAP + "circle")),
            })
        result["info"].append(info)
    if not result["info"] and result["msgType"] in {"Alert", "Update"}:
        raise AlertDataError("Alert/update has no information sections")
    return result


def _contains(points, latitude, longitude):
    inside = False
    for a, b in zip(points, points[1:]):
        ay, ax = a
        by, bx = b
        cross = (longitude-ax)*(by-ay) - (latitude-ay)*(bx-ax)
        if abs(cross) < 1e-12 and min(ax,bx) <= longitude <= max(ax,bx) and min(ay,by) <= latitude <= max(ay,by):
            return True
        if (ay > latitude) != (by > latitude):
            if longitude < ax + (latitude-ay)*(bx-ax)/(by-ay):
                inside = not inside
    return inside


def _area_match(areas, latitude, longitude):
    unresolved = not areas
    for area in areas:
        if not area["polygons"] or area["unsupported_circle"]:
            unresolved = True
        for raw in area["polygons"]:
            polygon = _polygon(raw)
            if polygon is None:
                unresolved = True
            elif _contains(polygon, latitude, longitude):
                return "matched"
    return "unresolved" if unresolved else "not_matched"


def reconcile(messages, latitude, longitude, now):
    """Reconcile references without turning one uncertain edge into global loss.

    All languages are retained; section activity/matching are evaluated
    separately. Missing expiry/invalid time ordering prevents an active claim.
    No matching is inferred from district names, proximity, or unmapped LGD IDs.
    Cross-sender references are valid CAP syntax but require authority knowledge
    this client does not have. Quarantine connected messages rather than trusting
    the transfer or discarding unrelated messages. Compare instants, not strings.
    """
    by_key = {}
    for message in messages:
        key = (message["sender"], message["identifier"], message["sent"])
        if key in by_key and ({k:v for k,v in by_key[key].items() if k != "source_url"}
                              != {k:v for k,v in message.items() if k != "source_url"}):
            raise AlertDataError("Conflicting CAP messages share an identity")
        by_key[key] = message
    removed, uncertain, relationships = set(), set(), []
    issues = {key: [] for key in by_key}
    for key, message in by_key.items():
        if (message["status"] != "Actual" or message["scope"] != "Public"
                or datetime.fromisoformat(message["sent"]) > datetime.fromisoformat(now)):
            continue
        if message["msgType"] in {"Cancel", "Update"}:
            for ref in message["references"]:
                target = (ref["sender"], ref["identifier"], ref["sent"])
                relationships.append((key, target))
                candidates = {candidate for candidate in by_key if candidate[:2] == target[:2]}
                code = None
                if ref["sender"] != message["sender"]:
                    code = "cross_sender_authority_unresolved"
                elif datetime.fromisoformat(ref["sent"]) >= datetime.fromisoformat(message["sent"]):
                    code = "non_chronological_reference"
                elif target not in by_key and candidates:
                    code = "reference_identity_mismatch"
                if code:
                    issues[key].append({"code": code, "reference": dict(ref)})
                    uncertain.update((key, target))
                    if target not in by_key:
                        # A timestamp mismatch cannot leave a potential target active.
                        uncertain.update(candidates)
                        relationships.extend((key, candidate) for candidate in candidates)
                else:
                    removed.add(target)
                    if target not in by_key:
                        issues[key].append({"code": "referenced_message_not_in_snapshot", "reference": dict(ref)})
    # Uncertainty follows the reference chain in either direction. In particular,
    # a later same-sender update cannot silently clear an unresolved transfer.
    changed = True
    while changed:
        changed = False
        for source, target in relationships:
            if (source in uncertain or target in uncertain) and not {source, target} <= uncertain:
                uncertain.update((source, target))
                changed = True
    alerts = []
    for key, message in sorted(by_key.items(), key=lambda pair: pair[0][2], reverse=True):
        message = dict(message)
        eligible = (message["status"] == "Actual" and message["scope"] == "Public"
                    and message["msgType"] in {"Alert", "Update"} and message["sent"] <= now
                    and key not in removed and key not in uncertain)
        sections = []
        for original in message["info"]:
            info = dict(original)
            start = info["effective"] or message["sent"]
            end = info["expires"]
            info["active"] = bool(eligible and end and start <= now < end and start < end)
            info["temporal_status"] = (
                "ineligible" if not eligible else "unresolved" if not end or start >= end else
                "expired" if now >= end else "pending" if now < start else "active")
            info["location_match"] = _area_match(info["areas"], latitude, longitude)
            sections.append(info)
        message["info"] = sections
        active = [section for section in sections if section["active"]]
        message["active"] = bool(active)
        matches = {section["location_match"] for section in active}
        message["location_match"] = ("matched" if "matched" in matches else
                                     "unresolved" if not active or "unresolved" in matches else "not_matched")
        message["lifecycle"] = ("unresolved" if key in uncertain else "superseded" if key in removed else
                                "active" if active else "inactive_or_unverifiable")
        message["lifecycle_issues"] = list(issues[key])
        if key in uncertain and not issues[key]:
            message["lifecycle_issues"].append({"code": "linked_lifecycle_uncertainty", "reference": None})
        alerts.append(message)
    return alerts
