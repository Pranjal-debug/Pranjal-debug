#!/usr/bin/env python3
"""Generate a safe, self-hosted GitHub activity graphic for the profile README.

The source is limited to public repository metadata and the public event feed.
The event feed is intentionally described as a recent API window, not as a
complete contribution history.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


API_ROOT = "https://api.github.com"
USER_AGENT = "Pranjal-debug-profile-activity/1.0"
MAX_EVENT_PAGES = 3
MAX_REPOSITORY_PAGES = 10

EVENT_LABELS = {
    "CreateEvent": "Created a branch or tag",
    "DeleteEvent": "Deleted a branch or tag",
    "ForkEvent": "Forked a repository",
    "GollumEvent": "Updated repository wiki pages",
    "IssueCommentEvent": "Commented on an issue or pull request",
    "IssuesEvent": "Updated an issue",
    "MemberEvent": "Updated repository membership",
    "PublicEvent": "Made a repository public",
    "PullRequestEvent": "Updated a pull request",
    "PullRequestReviewEvent": "Reviewed a pull request",
    "PullRequestReviewCommentEvent": "Commented on a pull request review",
    "PushEvent": "Pushed code",
    "ReleaseEvent": "Published a release",
    "WatchEvent": "Starred a repository",
    "WorkflowRunEvent": "Ran a GitHub Actions workflow",
}


class ApiError(RuntimeError):
    """An expected GitHub API or response-shape failure."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default="Pranjal-debug")
    parser.add_argument("--output", type=Path, default=Path("assets/live-activity.svg"))
    parser.add_argument(
        "--fallback",
        type=Path,
        default=Path("assets/live-activity-fallback.svg"),
    )
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""))
    parser.add_argument("--max-event-pages", type=int, default=MAX_EVENT_PAGES)
    parser.add_argument("--max-repository-pages", type=int, default=MAX_REPOSITORY_PAGES)
    return parser.parse_args()


def escape(value: Any) -> str:
    """Escape API-derived text before placing it in SVG markup."""

    return html.escape(str(value), quote=True)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso_timestamp(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def compact_date(value: str | None) -> str:
    parsed = iso_timestamp(value)
    return parsed.strftime("%Y-%m-%d") if parsed else "unknown date"


def api_request(url: str, token: str) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status != 200:
                    raise ApiError(f"GitHub API returned HTTP {response.status} for {url}")
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            remaining = error.headers.get("X-RateLimit-Remaining")
            retry_after = error.headers.get("Retry-After")
            if error.code in {403, 429}:
                detail = f"rate limit or access response (remaining={remaining or 'unknown'})"
                if retry_after:
                    detail += f", retry-after={retry_after}s"
                raise ApiError(f"GitHub API {detail}") from error
            if error.code >= 500 and attempt < 2:
                time.sleep(2**attempt)
                continue
            raise ApiError(f"GitHub API returned HTTP {error.code} for {url}") from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            if attempt < 2:
                time.sleep(2**attempt)
                continue
            raise ApiError(f"GitHub API request failed for {url}: {error}") from error
    raise ApiError(f"GitHub API request exhausted retries for {url}")


def fetch_pages(url: str, token: str, max_pages: int) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        query = urllib.parse.urlencode({"per_page": 100, "page": page})
        separator = "&" if "?" in url else "?"
        payload = api_request(f"{url}{separator}{query}", token)
        if not isinstance(payload, list):
            raise ApiError(f"Expected a JSON list from {url}")
        if not payload:
            break
        collected.extend(item for item in payload if isinstance(item, dict))
        if len(payload) < 100:
            break
    return collected


def event_label(event_type: str) -> str:
    return EVENT_LABELS.get(event_type, event_type.removesuffix("Event") or "Public activity")


def normalize_events(events: list[dict[str, Any]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for event in events:
        repo = event.get("repo") or {}
        repo_name = repo.get("name")
        created_at = event.get("created_at")
        event_type = event.get("type")
        if not isinstance(repo_name, str) or not isinstance(created_at, str):
            continue
        if not isinstance(event_type, str):
            event_type = "Public activity"
        normalized.append(
            {
                "type": event_type,
                "label": event_label(event_type),
                "repo": repo_name,
                "date": compact_date(created_at),
                "created_at": created_at,
            }
        )
    normalized.sort(key=lambda item: item["created_at"], reverse=True)
    return normalized


def build_snapshot(
    username: str,
    repositories: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    public_repositories = [repo for repo in repositories if repo.get("private") is not True]
    normalized_events = normalize_events(events)
    event_counts = Counter(event["type"] for event in normalized_events)
    touched_repositories = sorted({event["repo"] for event in normalized_events})
    repository_names = sorted(
        str(repo["full_name"])
        for repo in public_repositories
        if isinstance(repo.get("full_name"), str)
    )
    timeline = normalized_events[:4]
    latest_activity = normalized_events[0]["created_at"] if normalized_events else None

    snapshot = {
        "username": username,
        "public_repository_count": len(public_repositories),
        "event_count": len(normalized_events),
        "touched_repository_count": len(touched_repositories),
        "repository_names": repository_names,
        "event_counts": dict(sorted(event_counts.items())),
        "latest_activity": latest_activity,
        "timeline": timeline,
        "coverage_note": "Recent public GitHub Events API window; not full contribution history.",
    }
    fingerprint_payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    snapshot["fingerprint"] = hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest()[:16]
    return snapshot


def read_previous_metadata(path: Path) -> tuple[str | None, str | None]:
    if not path.exists():
        return None, None
    text = path.read_text(encoding="utf-8")
    fingerprint = re.search(r'data-fingerprint="([0-9a-f]+)"', text)
    updated = re.search(r'data-updated="([^"]+)"', text)
    return (fingerprint.group(1) if fingerprint else None, updated.group(1) if updated else None)


def render_svg(snapshot: dict[str, Any], updated_at: str) -> str:
    username = escape(snapshot["username"])
    fingerprint = escape(snapshot["fingerprint"])
    last_activity = escape(compact_date(snapshot.get("latest_activity")))
    timeline = snapshot["timeline"]
    event_count = snapshot["event_count"]
    repo_count = snapshot["public_repository_count"]
    touched_count = snapshot["touched_repository_count"]

    timeline_markup: list[str] = []
    for index, event in enumerate(timeline):
        y = 390 + index * 27
        color = "#d9ff3f" if event["type"] in {"PushEvent", "ReleaseEvent"} else "#a78bfa"
        label = escape(event["label"])
        repo = escape(event["repo"])
        date = escape(event["date"])
        timeline_markup.append(
            f'''<g transform="translate(790 {y})">
  <circle cx="0" cy="0" r="5" fill="{color}"/>
  <text x="22" y="-2" fill="#f8fafc" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="12">{label}</text>
  <text x="22" y="14" fill="#94a3b8" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="10">{repo} · {date}</text>
</g>'''
        )
    if not timeline_markup:
        timeline_markup.append(
            '''<g transform="translate(790 392)">
  <circle cx="0" cy="0" r="5" fill="#64748b"/>
  <text x="22" y="-2" fill="#f8fafc" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="13">No recent public events returned</text>
  <text x="22" y="14" fill="#94a3b8" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="10">The API window is empty; no activity is being inferred.</text>
</g>'''
        )

    cards = [
        ("PUBLIC REPOSITORIES", str(repo_count), "owned public repositories", "#d9ff3f"),
        ("EVENTS SEEN", str(event_count), "recent public API events", "#a78bfa"),
        ("REPOS TOUCHED", str(touched_count), "repositories in event window", "#22d3ee"),
    ]
    cards_markup: list[str] = []
    for index, (label, value, note, color) in enumerate(cards):
        x = 72 + index * 220
        cards_markup.append(
            f'''<g transform="translate({x} 178)">
  <rect width="190" height="108" rx="14" fill="#111827" stroke="#334155"/>
  <text x="18" y="27" fill="#94a3b8" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="11" letter-spacing="1">{label}</text>
  <text x="18" y="70" fill="{color}" font-family="Inter, ui-sans-serif, system-ui, sans-serif" font-size="36" font-weight="800">{value}</text>
  <text x="18" y="92" fill="#64748b" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="10">{note}</text>
</g>'''
        )

    return f'''<!-- profile-activity-data fingerprint={fingerprint} last-successful-update={escape(updated_at)} source=GitHub-public-REST-API -->
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1400 560" role="img" aria-labelledby="title desc" data-fingerprint="{fingerprint}" data-updated="{escape(updated_at)}">
  <title id="title">Recent public GitHub activity for {username}</title>
  <desc id="desc">A self-hosted activity visualization showing public repository count, recent public events, repositories touched, and a recent event timeline.</desc>
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#070712"/><stop offset="0.55" stop-color="#17102d"/><stop offset="1" stop-color="#0b1020"/></linearGradient>
    <linearGradient id="lime" x1="0" y1="0" x2="1" y2="0"><stop stop-color="#d9ff3f"/><stop offset="1" stop-color="#86efac"/></linearGradient>
    <pattern id="grid" width="42" height="42" patternUnits="userSpaceOnUse"><path d="M42 0H0V42" fill="none" stroke="#c4b5fd" stroke-opacity="0.08"/></pattern>
    <filter id="glow" x="-60%" y="-60%" width="220%" height="220%"><feGaussianBlur stdDeviation="7" result="blur"/><feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
  </defs>
  <rect width="1400" height="560" rx="28" fill="url(#bg)"/>
  <rect width="1400" height="560" rx="28" fill="url(#grid)"/>
  <circle cx="1240" cy="92" r="150" fill="#7c3aed" fill-opacity="0.12" filter="url(#glow)"/>
  <text x="72" y="72" fill="#d9ff3f" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="17" letter-spacing="4">LIVE ACTIVITY / PUBLIC SIGNAL</text>
  <text x="72" y="116" fill="#f8fafc" font-family="Inter, ui-sans-serif, system-ui, sans-serif" font-size="36" font-weight="800">{username} · what the public API can see</text>
  <text x="72" y="145" fill="#94a3b8" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="13">RECENT EVENTS WINDOW · LATEST ACTIVITY {last_activity} · VERIFIED {escape(updated_at)}</text>
  {''.join(cards_markup)}
  <rect x="72" y="324" width="1256" height="166" rx="18" fill="#0b1220" fill-opacity="0.92" stroke="#334155"/>
  <text x="790" y="360" fill="#c4b5fd" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="14" letter-spacing="2">RECENT PUBLIC EVENTS</text>
  <path d="M765 374V478" stroke="#334155"/>
  {''.join(timeline_markup)}
  <text x="104" y="416" fill="#64748b" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="13">SOURCE</text>
  <text x="104" y="443" fill="#e2e8f0" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="16">GitHub REST API · public repositories + public events</text>
  <text x="104" y="470" fill="#64748b" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="12">Coverage is limited to the API event window; it is not a complete contribution history.</text>
  <text x="72" y="532" fill="#64748b" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="12" letter-spacing="1">PRANJAL-DEBUG  ·  DATA FINGERPRINT {fingerprint}</text>
</svg>
'''


def validate_svg(text: str) -> None:
    if any(token in text.lower() for token in ("<script", "<foreignobject", "<iframe", "javascript:", "<image")):
        raise ApiError("Generated SVG contains a forbidden or external markup element")
    root = ET.fromstring(text)
    if root.tag != "{http://www.w3.org/2000/svg}svg":
        raise ApiError("Generated file is not an SVG root")
    if root.attrib.get("viewBox") != "0 0 1400 560":
        raise ApiError("Generated SVG has an unexpected viewBox")
    if root.find("{http://www.w3.org/2000/svg}title") is None:
        raise ApiError("Generated SVG is missing a title")
    if root.find("{http://www.w3.org/2000/svg}desc") is None:
        raise ApiError("Generated SVG is missing a description")


def write_fallback_if_missing(output: Path, fallback: Path) -> int:
    if output.exists():
        print(f"warning: preserving last known-good asset at {output}")
        return 0
    if not fallback.exists():
        print("error: no existing activity asset or static fallback is available", file=sys.stderr)
        return 1
    fallback_text = fallback.read_text(encoding="utf-8")
    validate_svg(fallback_text)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(fallback_text, encoding="utf-8")
    print(f"wrote static fallback to {output}")
    return 0


def main() -> int:
    args = parse_args()
    try:
        repositories = fetch_pages(
            f"{API_ROOT}/users/{urllib.parse.quote(args.username, safe='')}/repos?type=owner&sort=updated",
            args.token,
            max(1, args.max_repository_pages),
        )
        events = fetch_pages(
            f"{API_ROOT}/users/{urllib.parse.quote(args.username, safe='')}/events/public",
            args.token,
            max(1, args.max_event_pages),
        )
        snapshot = build_snapshot(args.username, repositories, events)
        previous_fingerprint, previous_updated = read_previous_metadata(args.output)
        updated_at = previous_updated or utc_now().isoformat().replace("+00:00", "Z")
        if previous_fingerprint != snapshot["fingerprint"]:
            updated_at = utc_now().isoformat().replace("+00:00", "Z")
        output_text = render_svg(snapshot, updated_at)
        validate_svg(output_text)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        current_text = args.output.read_text(encoding="utf-8") if args.output.exists() else None
        if current_text == output_text:
            print(f"unchanged: {args.output} (fingerprint {snapshot['fingerprint']})")
            return 0
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(output_text, encoding="utf-8")
        validate_svg(temporary.read_text(encoding="utf-8"))
        os.replace(temporary, args.output)
        print(
            f"updated: {args.output} | repositories={snapshot['public_repository_count']} "
            f"events={snapshot['event_count']} fingerprint={snapshot['fingerprint']}"
        )
        return 0
    except (ApiError, OSError) as error:
        print(f"warning: activity refresh skipped: {error}", file=sys.stderr)
        return write_fallback_if_missing(args.output, args.fallback)


if __name__ == "__main__":
    raise SystemExit(main())
