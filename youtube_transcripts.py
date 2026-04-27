import argparse
import json
import re
import sys
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests


_VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
_WATCH_ID_PATTERN = re.compile(r"watch\?v=([A-Za-z0-9_-]{11})")
_INNERTUBE_CLIENT_VERSION_PATTERN = re.compile(
    r'"INNERTUBE_CONTEXT_CLIENT_VERSION":"([^"]+)"'
)
_VISITOR_DATA_PATTERN = re.compile(r'"VISITOR_DATA":"([^"]+)"')
_VALID_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "www.youtu.be",
}


def _validate_youtube_host(parsed_url):
    if parsed_url.netloc.lower() not in _VALID_HOSTS:
        raise ValueError("URL is not a supported YouTube link")


def extract_video_id(url):
    parsed_url = urlparse(url)
    _validate_youtube_host(parsed_url)

    host = parsed_url.netloc.lower()
    query = parse_qs(parsed_url.query)

    if host in {"youtu.be", "www.youtu.be"}:
        candidate = parsed_url.path.strip("/").split("/")[0]
        if _VIDEO_ID_PATTERN.fullmatch(candidate):
            return candidate

    if "v" in query:
        candidate = query["v"][0]
        if _VIDEO_ID_PATTERN.fullmatch(candidate):
            return candidate

    path_parts = [part for part in parsed_url.path.split("/") if part]
    if len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed"}:
        candidate = path_parts[1]
        if _VIDEO_ID_PATTERN.fullmatch(candidate):
            return candidate

    raise ValueError("Could not determine a YouTube video ID from the URL")


def extract_playlist_id(url):
    parsed_url = urlparse(url)
    _validate_youtube_host(parsed_url)

    query = parse_qs(parsed_url.query)
    playlist_ids = query.get("list", [])
    if not playlist_ids:
        raise ValueError("Could not determine a YouTube playlist ID from the URL")
    return playlist_ids[0]


def _extract_json_after_marker(text, marker):
    marker_index = text.find(marker)
    if marker_index == -1:
        return None

    json_start = text.find("{", marker_index + len(marker))
    if json_start == -1:
        return None

    depth = 0
    in_string = False
    escape = False

    for index in range(json_start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[json_start : index + 1]

    return None


def _extract_yt_initial_data(html):
    markers = (
        "var ytInitialData = ",
        'window["ytInitialData"] = ',
        "ytInitialData = ",
    )

    for marker in markers:
        json_text = _extract_json_after_marker(html, marker)
        if not json_text:
            continue
        try:
            return json.loads(json_text)
        except json.JSONDecodeError:
            continue

    return None


def _iter_json_nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_json_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_json_nodes(child)


def _append_unique_video_id(candidate, ordered_ids, seen_ids):
    if (
        isinstance(candidate, str)
        and _VIDEO_ID_PATTERN.fullmatch(candidate)
        and candidate not in seen_ids
    ):
        seen_ids.add(candidate)
        ordered_ids.append(candidate)


def _extract_video_ids_from_data(data):
    ordered_ids = []
    seen_ids = set()

    for node in _iter_json_nodes(data):
        for renderer_key in (
            "playlistVideoRenderer",
            "playlistPanelVideoRenderer",
            "reelItemRenderer",
        ):
            renderer = node.get(renderer_key)
            if isinstance(renderer, dict):
                _append_unique_video_id(
                    renderer.get("videoId"), ordered_ids, seen_ids
                )

    return ordered_ids


def _extract_continuation_tokens(data):
    ordered_tokens = []
    seen_tokens = set()

    for node in _iter_json_nodes(data):
        token = None

        continuation_command = node.get("continuationCommand")
        if isinstance(continuation_command, dict):
            token = continuation_command.get("token")

        if token is None:
            next_continuation = node.get("nextContinuationData")
            if isinstance(next_continuation, dict):
                token = next_continuation.get("continuation")

        if token is None:
            reload_continuation = node.get("reloadContinuationData")
            if isinstance(reload_continuation, dict):
                token = reload_continuation.get("continuation")

        if isinstance(token, str) and token not in seen_tokens:
            seen_tokens.add(token)
            ordered_tokens.append(token)

    return ordered_tokens


def _extract_innertube_context(html):
    client_version_match = _INNERTUBE_CLIENT_VERSION_PATTERN.search(html)
    if not client_version_match:
        return None

    client = {
        "clientName": "WEB",
        "clientVersion": client_version_match.group(1),
        "hl": "en",
        "gl": "US",
    }

    visitor_data_match = _VISITOR_DATA_PATTERN.search(html)
    if visitor_data_match:
        client["visitorData"] = visitor_data_match.group(1)

    return {"client": client}


def _fetch_playlist_continuation(session, playlist_url, context, continuation_token):
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://www.youtube.com",
        "Referer": playlist_url,
        "X-YouTube-Client-Name": "1",
        "X-YouTube-Client-Version": context["client"]["clientVersion"],
    }
    response = session.post(
        "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false",
        json={"context": context, "continuation": continuation_token},
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def get_playlist_video_ids(playlist_url, session=None):
    parsed_url = urlparse(playlist_url)
    _validate_youtube_host(parsed_url)

    session = session or requests
    response = session.get(playlist_url, timeout=30)
    response.raise_for_status()

    initial_data = _extract_yt_initial_data(response.text)
    if initial_data is None:
        ordered_ids = []
        seen_ids = set()
        for video_id in _WATCH_ID_PATTERN.findall(response.text):
            if video_id not in seen_ids:
                seen_ids.add(video_id)
                ordered_ids.append(video_id)
        return ordered_ids

    ordered_ids = _extract_video_ids_from_data(initial_data)
    context = _extract_innertube_context(response.text)
    if context is None:
        return ordered_ids

    continuation_tokens = deque(_extract_continuation_tokens(initial_data))
    seen_tokens = set(continuation_tokens)
    seen_ids = set(ordered_ids)

    while continuation_tokens:
        continuation_token = continuation_tokens.popleft()
        try:
            continuation_data = _fetch_playlist_continuation(
                session, playlist_url, context, continuation_token
            )
        except Exception:
            break

        for video_id in _extract_video_ids_from_data(continuation_data):
            if video_id not in seen_ids:
                seen_ids.add(video_id)
                ordered_ids.append(video_id)

        for token in _extract_continuation_tokens(continuation_data):
            if token not in seen_tokens:
                seen_tokens.add(token)
                continuation_tokens.append(token)

    return ordered_ids


def fetch_transcript(video_id, languages=None):
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError as exc:
        raise ImportError(
            "youtube_transcript_api is required. Install it with "
            "'pip install youtube-transcript-api'."
        ) from exc

    languages = languages or ["en"]
    return YouTubeTranscriptApi().fetch(video_id, languages=languages)


def normalize_output_dir(output_dir):
    return Path(output_dir).expanduser().resolve(strict=False)


def is_ip_block_error(exc):
    error_name = exc.__class__.__name__
    if error_name in {"RequestBlocked", "IpBlocked"}:
        return True

    message = str(exc)
    return "YouTube is blocking requests from your IP" in message


def transcript_file_path(video_id, output_dir):
    return normalize_output_dir(output_dir) / f"{video_id}.txt"


def write_transcript_file(video_id, transcript, output_dir):
    output_path = normalize_output_dir(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    file_path = transcript_file_path(video_id, output_dir)
    lines = []
    for entry in transcript:
        text = entry["text"] if isinstance(entry, dict) else entry.text
        lines.append(str(text).strip())

    file_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return str(file_path)


def download_transcripts(url, output_dir="transcripts", languages=None, session=None):
    output_dir = normalize_output_dir(output_dir)

    try:
        extract_playlist_id(url)
        video_ids = get_playlist_video_ids(url, session=session)
        if not video_ids:
            raise ValueError("No videos found in playlist")
    except ValueError:
        video_ids = [extract_video_id(url)]

    written_files = []
    skipped_files = []
    failed_videos = []
    aborted_videos = []

    for index, video_id in enumerate(video_ids):
        file_path = transcript_file_path(video_id, output_dir)
        if file_path.exists():
            skipped_files.append(str(file_path))
            continue

        try:
            transcript = fetch_transcript(video_id, languages=languages)
            written_files.append(
                write_transcript_file(video_id, transcript, output_dir=output_dir)
            )
        except Exception as exc:
            failed_videos.append({"video_id": video_id, "error": str(exc)})
            if is_ip_block_error(exc):
                aborted_videos = video_ids[index + 1 :]
                break

    return {
        "processed_videos": video_ids,
        "written_files": written_files,
        "skipped_files": skipped_files,
        "failed_videos": failed_videos,
        "aborted_videos": aborted_videos,
        "output_dir": str(output_dir),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Download transcripts from YouTube videos or playlists."
    )
    parser.add_argument("url", help="YouTube video or playlist URL")
    parser.add_argument(
        "-o",
        "--output-dir",
        default="transcripts",
        help="Directory where transcript files will be written",
    )
    parser.add_argument(
        "-l",
        "--language",
        action="append",
        dest="languages",
        help="Preferred transcript language. Repeat to provide fallbacks.",
    )
    args = parser.parse_args(argv)

    result = download_transcripts(
        args.url,
        output_dir=args.output_dir,
        languages=args.languages,
    )

    summary = (
        f"Processed {len(result['processed_videos'])} videos. "
        f"Wrote {len(result['written_files'])} files. "
        f"Skipped {len(result['skipped_files'])} existing files. "
        f"Failed: {len(result['failed_videos'])}. "
        f"Aborted: {len(result['aborted_videos'])}"
    )
    print(summary)
    print(f"Output directory: {result['output_dir']}")
    for written_file in result["written_files"]:
        print(written_file)
    for skipped_file in result["skipped_files"]:
        print(f"Skipped existing: {skipped_file}")
    if result["aborted_videos"]:
        print(
            "Stopped after YouTube started blocking transcript requests. "
            f"Did not attempt {len(result['aborted_videos'])} remaining videos."
        )

    if result["failed_videos"] or result["aborted_videos"]:
        print(summary, file=sys.stderr)
        print(f"Output directory: {result['output_dir']}", file=sys.stderr)
        if result["aborted_videos"]:
            print(
                "Stopped after YouTube started blocking transcript requests. "
                f"Did not attempt {len(result['aborted_videos'])} remaining videos.",
                file=sys.stderr,
            )
        for failure in result["failed_videos"]:
            print(f"{failure['video_id']}: {failure['error']}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
