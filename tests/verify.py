"""Verify DLNA browsing, a seekable stream and HEVC/AAC decode on a running server."""

import argparse
import json
import socket
import subprocess
import time
import urllib.request
from xml.etree import ElementTree as ET

SERVICE = "urn:schemas-upnp-org:service:ContentDirectory:1"
PACKET_SIZE = 188


def discover(ip, url):
    target = "urn:schemas-upnp-org:device:MediaServer:1"
    group = "239.255.255.250"
    inquiry = (f"M-SEARCH * HTTP/1.1\r\nHOST: {group}:1900\r\nMAN: \"ssdp:discover\"\r\n"
               f"MX: 1\r\nST: {target}\r\n\r\n").encode()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.bind((ip, 0))
        sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
        sender.sendto(inquiry, (group, 1900))
        deadline = time.monotonic() + 4
        while deadline > time.monotonic():
            sender.settimeout(max(0.1, deadline - time.monotonic()))
            reply = sender.recv(4096).decode("utf-8", errors="replace").lower()
            if f"location: {url}/description.xml" in reply:
                return
    raise RuntimeError("Target did not respond to LAN SSDP M-SEARCH")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--directory", action="append", default=[])
    parser.add_argument("--video", required=True, help="Substring of the video title")
    parser.add_argument("--slots", type=int, default=1)
    parser.add_argument("--start-slot", type=int, default=0)
    parser.add_argument("--fps", type=float, default=25)
    parser.add_argument("--ssdp-ip", help="Local IPv4 interface to send M-SEARCH through")
    parser.add_argument("--subtitle", action="store_true")
    args = parser.parse_args()
    url = args.url.rstrip("/")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(path, headers=None, method=None):
        full = path if path.startswith(("http:", "https:")) else url + path
        return opener.open(urllib.request.Request(full, headers=headers or {}, method=method), timeout=45)

    with get("/health") as response:
        health = json.load(response)
    assert health["model"] == "oVDA c16" and health["status"] == "ready", health
    with get("/") as response:
        assert b'id="entries"' in response.read()
    with get("/description.xml") as response:
        description = ET.fromstring(response.read())
    assert description.findtext(".//{*}friendlyName") == health["name"], health
    if args.ssdp_ip:
        discover(args.ssdp_ip, url)

    def browse(object_id, flag="BrowseDirectChildren"):
        body = (f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
                f'<s:Body><u:Browse xmlns:u="{SERVICE}"><ObjectID>{object_id}</ObjectID>'
                f'<BrowseFlag>{flag}</BrowseFlag><Filter>*</Filter><StartingIndex>0</StartingIndex>'
                '<RequestedCount>0</RequestedCount><SortCriteria/></u:Browse></s:Body></s:Envelope>')
        request = urllib.request.Request(url + "/control/cds", body.encode(),
                                         {"SOAPAction": f'"{SERVICE}#Browse"', "Content-Type": "text/xml"})
        with opener.open(request, timeout=10) as response:
            return list(ET.fromstring(ET.fromstring(response.read()).findtext(".//{*}Result")))

    folder = "0"
    for name in args.directory:
        folder = next(item.attrib["id"] for item in browse(folder) if item.findtext("{*}title") == name)
    items = [item for item in browse(folder) if args.video in item.findtext("{*}title", "") and item.tag.endswith("}item")]
    assert len(items) == 1
    item = items[0]
    media_url = item.findtext("{*}res")
    assert "_Half_TB.ts" in media_url and "上下 3D" in item.findtext("{*}title", "")
    assert browse(item.attrib["id"], "BrowseMetadata")[0].findtext("{*}res") == media_url
    if args.subtitle:
        subtitle_url = item.findtext("{*}CaptionInfoEx")
        with get(subtitle_url) as response:
            assert b" --> " in response.read()
    # Run this receiver on the chosen host even if a local container advertises a different LAN address.
    from urllib.parse import urlsplit
    media_path = urlsplit(media_url).path
    with get(media_path, method="HEAD") as response:
        assert response.headers["Accept-Ranges"] == "bytes"
        size = int(response.headers["Content-Length"])
        assert size > 0 and size % PACKET_SIZE == 0
    slot_frames = round(args.fps * 4)
    slot_size = round(32_000_000 * slot_frames / (8 * PACKET_SIZE * args.fps)) * PACKET_SIZE
    start = args.start_slot * slot_size
    stop = (args.start_slot + args.slots) * slot_size - 1
    assert 0 <= start <= stop < size, (start, stop, size)
    started = time.perf_counter()
    with get(media_path, {"Range": f"bytes={start}-{stop}"}) as response:
        assert response.status == 206 and response.headers["Content-Range"] == f"bytes {start}-{stop}/{size}"
        data = response.read()
    elapsed = time.perf_counter() - started
    assert len(data) == stop - start + 1 and data[0] == 0x47
    inspection = subprocess.run(
        ["ffprobe", "-v", "error", "-f", "mpegts", "-count_frames",
         "-show_entries", "stream=codec_type,codec_name,width,height,nb_read_frames",
         "-of", "json", "-i", "pipe:0"], input=data, capture_output=True, check=True, timeout=30)
    streams = json.loads(inspection.stdout)["streams"]
    video = next(stream for stream in streams if stream["codec_type"] == "video")
    assert (video["codec_name"], video["width"], video["height"]) == ("hevc", 1920, 1080), video
    assert any(stream["codec_name"] == "aac" for stream in streams), streams
    frames = int(video["nb_read_frames"])
    assert frames == args.slots * slot_frames, video
    print(json.dumps({"name": health["name"], "model": health["model"], "frames": frames,
                      "seconds": round(elapsed, 2), "throughput_fps": round(frames / elapsed, 2),
                      "ssdp": bool(args.ssdp_ip), "subtitle": args.subtitle}))


if __name__ == "__main__":
    main()
