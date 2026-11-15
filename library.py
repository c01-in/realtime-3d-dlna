"""Read-only media catalogue for the HTTP UI and UPnP ContentDirectory."""

import base64
import binascii
import html
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree as ET

from utils.subtitles import find_external_subtitles
from video import CONTENT_FEATURES, MIME

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".m4v", ".avi", ".webm", ".ts",
                    ".m2ts", ".mpg", ".mpeg", ".wmv", ".flv"}
CDS = "urn:schemas-upnp-org:service:ContentDirectory:1"
SOAP = "http://schemas.xmlsoap.org/soap/envelope/"


class Library:
    def __init__(self, roots, address):
        self.roots = roots
        self.address = address

    @staticmethod
    def identifier(root, parts=()):
        raw = "/".join((str(root), *parts))
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")

    def resolve(self, object_id):
        try:
            raw = base64.b64decode(object_id + "=" * (-len(object_id) % 4),
                                   altchars=b"-_", validate=True).decode("utf-8")
            index, *parts = raw.split("/")
            root_index = int(index)
            if not 0 <= root_index < len(self.roots):
                raise ValueError
            if self.identifier(root_index, parts) != object_id or any(
                part in {"", ".", ".."} or "\\" in part or ":" in part for part in parts
            ):
                raise ValueError
            root = self.roots[root_index]
            path = root.joinpath(*parts).resolve()
            if path.is_relative_to(root):
                return root_index, tuple(parts), path
        except (OSError, RuntimeError, ValueError, binascii.Error):
            pass
        raise FileNotFoundError(object_id)

    def previous_id(self, token):
        """Translate a cached Thru3D NAS identifier to the configured root."""
        try:
            raw = base64.b64decode(token + "=" * (-len(token) % 4),
                                   altchars=b"-_", validate=True).decode("utf-8")
            label, *parts = raw.split("/")
            index = next(i for i, root in enumerate(self.roots) if root.name.casefold() == label.casefold())
        except (UnicodeError, ValueError, binascii.Error, StopIteration) as exc:
            raise FileNotFoundError(token) from exc
        return self.identifier(index, parts)

    def entry(self, root, parts, path, *, subtitles=False):
        title = path.name or path.anchor
        object_id = self.identifier(root, parts)
        parent = self.identifier(root, parts[:-1]) if parts else "0"
        if path.is_dir():
            return {"id": object_id, "parent": parent, "title": title, "folder": True}
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
            raise FileNotFoundError(object_id)
        suffix = quote(path.stem, safe="") + "_Half_TB.ts"
        return {"id": object_id, "parent": parent, "title": path.stem + " - 上下 3D",
                "folder": False, "url": f"{self.address}/media/{object_id}/{suffix}",
                "subtitles": [
                    {"url": f"{self.address}/subtitles/{object_id}/{quote(track.path.name, safe='')}",
                     "format": track.kind, "lang": track.lang}
                    for track in find_external_subtitles(path)
                    if track.path.resolve().is_relative_to(path.parent.resolve())
                ] if subtitles else []}

    def browse(self, object_id, metadata=False):
        if object_id == "nas-media:root":
            object_id = "0"
        elif object_id.startswith("nas-media:"):
            object_id = self.previous_id(object_id.removeprefix("nas-media:").split(":", 1)[0])
        if object_id == "0":
            if metadata:
                return [{"id": "0", "parent": "-1", "title": "Media library", "folder": True}]
            return [self.entry(index, (), root) for index, root in enumerate(self.roots)]
        root, parts, path = self.resolve(object_id)
        if metadata:
            return [self.entry(root, parts, path, subtitles=True)]
        items = []
        for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold())):
            child_parts = (*parts, child.name)
            try:
                _, _, resolved = self.resolve(self.identifier(root, child_parts))
                items.append(self.entry(root, child_parts, resolved, subtitles=True))
            except (OSError, ValueError):
                continue
        return items


def didl(entries):
    items = []
    for entry in entries:
        key = html.escape(entry["id"], quote=True)
        parent = html.escape(entry["parent"], quote=True)
        title = html.escape(entry["title"])
        if entry["folder"]:
            items.append(f'<container id="{key}" parentID="{parent}" restricted="1">'
                         f'<dc:title>{title}</dc:title><upnp:class>object.container.storageFolder'
                         f'</upnp:class></container>')
            continue
        url = html.escape(entry["url"])
        subtitles = "".join(
            f'<sec:CaptionInfoEx sec:type="{html.escape(track["format"], quote=True)}">'
            f'{html.escape(track["url"])}</sec:CaptionInfoEx>' for track in entry["subtitles"]
        )
        items.append(f'<item id="{key}" parentID="{parent}" restricted="1"><dc:title>{title}</dc:title>'
                     f'<upnp:class>object.item.videoItem.movie</upnp:class>'
                     f'<res protocolInfo="http-get:*:{MIME}:{CONTENT_FEATURES}" resolution="1920x1080">'
                     f'{url}</res>{subtitles}</item>')
    return ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns:sec="http://www.sec.co.kr/">') + "".join(items) + "</DIDL-Lite>"


def envelope(payload):
    return f'<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="{SOAP}"><s:Body>{payload}</s:Body></s:Envelope>'


def directory_response(headers, xml, library):
    action = headers.get("SOAPAction", "").strip('"').rsplit("#", 1)[-1]
    capabilities = {
        "GetSearchCapabilities": "<SearchCaps></SearchCaps>",
        "GetSortCapabilities": "<SortCaps></SortCaps>",
        "GetSystemUpdateID": "<Id>0</Id>",
    }
    try:
        if action == "Browse":
            node = next(iter(ET.fromstring(xml).find(f"{{{SOAP}}}Body")))
            arguments = {child.tag.rsplit("}", 1)[-1]: child.text or "" for child in node}
            first = max(0, int(arguments.get("StartingIndex", "0")))
            count = max(0, int(arguments.get("RequestedCount", "0")))
            flag = arguments.get("BrowseFlag", "BrowseDirectChildren")
            if flag not in {"BrowseDirectChildren", "BrowseMetadata"}:
                raise ValueError
            entries = library.browse(arguments.get("ObjectID", "0"), metadata=flag == "BrowseMetadata")
            page = entries[first:first + count] if count else entries[first:]
            payload = (f"<Result>{html.escape(didl(page))}</Result><NumberReturned>{len(page)}</NumberReturned>"
                       f"<TotalMatches>{len(entries)}</TotalMatches><UpdateID>0</UpdateID>")
        else:
            payload = capabilities[action]
        return envelope(f'<u:{action}Response xmlns:u="{CDS}">{payload}</u:{action}Response>'), 200
    except (ET.ParseError, OSError, KeyError, ValueError, StopIteration, TypeError):
        return envelope('<s:Fault><faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring>'
                        '<detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
                        '<errorCode>701</errorCode><errorDescription>No such object or invalid request</errorDescription>'
                        '</UPnPError></detail></s:Fault>'), 500
