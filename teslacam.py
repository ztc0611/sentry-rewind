"""TeslaCam scanner."""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

CAMERAS = ["front", "back", "left_repeater", "right_repeater", "left_pillar", "right_pillar"]
CLIP_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-(front|back|left_repeater|right_repeater|left_pillar|right_pillar)\.mp4$")


@dataclass
class Segment:
    timestamp: str
    cameras: dict[str, Path] = field(default_factory=dict)


@dataclass
class Event:
    folder: Path
    source: str  # "SentryClips", "SavedClips", or "RecentClips"
    folder_timestamp: str
    segments: list[Segment] = field(default_factory=list)
    metadata: dict | None = None
    thumb: Path | None = None
    preview: Path | None = None

    @property
    def display_name(self) -> str:
        reason = ""
        if self.metadata:
            reason = self.metadata.get("reason", "")
        city = ""
        if self.metadata:
            city = self.metadata.get("city", "")
        parts = [self.folder_timestamp]
        if city:
            parts.append(city)
        if reason:
            parts.append(reason.replace("_", " "))
        return " - ".join(parts)

    @property
    def duration_seconds(self) -> float:
        return len(self.segments) * 60.0

    @property
    def trigger_offset_seconds(self) -> float | None:
        """Seconds from the start of the first segment to the event trigger."""
        if not self.metadata or not self.segments:
            return None
        # Manual saves have the trigger at the very end — no useful marker
        reason = self.metadata.get("reason", "")
        if reason in ("user_interaction_dashcam_launcher_action_tapped",
                       "user_interaction_dashcam_icon_tapped"):
            return None
        try:
            trigger_ts = self.metadata["timestamp"]  # e.g. "2026-04-01T09:44:33"
            trigger_dt = datetime.fromisoformat(trigger_ts)
            # First segment timestamp format: "2026-04-01_09-35-23"
            first_seg = self.segments[0].timestamp
            first_dt = datetime.strptime(first_seg, "%Y-%m-%d_%H-%M-%S")
            offset = (trigger_dt - first_dt).total_seconds()
            if offset >= 0:
                return offset
        except (KeyError, ValueError):
            pass
        return None


def scan_event_folder(folder: Path, source: str) -> Event:
    folder_timestamp = folder.name
    event = Event(folder=folder, source=source, folder_timestamp=folder_timestamp)

    # Parse event.json if present
    event_json = folder / "event.json"
    if event_json.exists():
        with open(event_json) as f:
            event.metadata = json.load(f)

    # Thumbnail and preview
    thumb = folder / "thumb.png"
    if thumb.exists():
        event.thumb = thumb
    preview = folder / "event.mp4"
    if preview.exists():
        event.preview = preview

    # Parse clip files into segments
    segments_dict: dict[str, Segment] = {}
    for file in folder.iterdir():
        match = CLIP_PATTERN.match(file.name)
        if match:
            ts, camera = match.groups()
            if ts not in segments_dict:
                segments_dict[ts] = Segment(timestamp=ts)
            segments_dict[ts].cameras[camera] = file

    event.segments = sorted(segments_dict.values(), key=lambda s: s.timestamp)
    return event


def _parse_segment_dt(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%d_%H-%M-%S")


# Gap threshold: if two consecutive segments are more than this apart, it's a new drive
_DRIVE_GAP_SECONDS = 90


def scan_recent_clips(folder: Path) -> list[Event]:
    """Scan RecentClips and group into continuous drives based on timestamp gaps."""
    if not folder.exists():
        return []

    segments_dict: dict[str, Segment] = {}
    for file in folder.iterdir():
        match = CLIP_PATTERN.match(file.name)
        if match:
            ts, camera = match.groups()
            if ts not in segments_dict:
                segments_dict[ts] = Segment(timestamp=ts)
            segments_dict[ts].cameras[camera] = file

    if not segments_dict:
        return []

    all_segments = sorted(segments_dict.values(), key=lambda s: s.timestamp)

    # Group into continuous drives
    drives: list[list[Segment]] = [[all_segments[0]]]
    for i in range(1, len(all_segments)):
        prev_dt = _parse_segment_dt(all_segments[i - 1].timestamp)
        curr_dt = _parse_segment_dt(all_segments[i].timestamp)
        gap = (curr_dt - prev_dt).total_seconds()
        if gap > _DRIVE_GAP_SECONDS:
            drives.append([all_segments[i]])
        else:
            drives[-1].append(all_segments[i])

    events = []
    for drive_segments in drives:
        event = Event(
            folder=folder,
            source="RecentClips",
            folder_timestamp=drive_segments[0].timestamp,
            segments=drive_segments,
        )
        events.append(event)

    return events


def scan_teslacam(teslacam_path: Path) -> list[Event]:
    events = []

    for source in ["SentryClips", "SavedClips"]:
        source_dir = teslacam_path / source
        if not source_dir.exists():
            continue
        for folder in sorted(source_dir.iterdir()):
            if folder.is_dir():
                event = scan_event_folder(folder, source)
                if event.segments:
                    events.append(event)

    # RecentClips grouped into drives
    events.extend(scan_recent_clips(teslacam_path / "RecentClips"))

    return events
