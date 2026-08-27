# VLoader

A local web interface for downloading videos from pages supported by
[yt-dlp](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md).

## Features

- **Auto quality** downloads yt-dlp's best available video and audio streams.
- **Manual quality** inspects the pasted page and lists only the resolutions it actually offers.
- **Output format** can be MP4, MOV, WebM, or MKV, with compatible codecs enforced.
- Separate video/audio streams are merged with FFmpeg.
- Real-time progress, speed, ETA, and an in-app download history.
- Works with supported video pages and direct media URLs—not only YouTube.

VLoader cannot bypass DRM, paywalls, account permissions, or unsupported site
protections. Only download media you own or have permission to save.

## Requirements

- Python 3.10 or newer
- FFmpeg and ffprobe on `PATH`
- A supported JavaScript runtime (Deno is recommended by yt-dlp; Node.js also works)

Keeping yt-dlp current matters because video sites change frequently.

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
python -m pip install -U pip
python -m pip install -r requirements.txt
```

## Usage

```bash
python app.py
```

Open <http://localhost:5001>, paste a video page URL, and either:

1. Leave **Quality** on **Auto** and download the best available version; or
2. Select **Choose a quality**, fetch the page's available resolutions, and pick one.

MP4 is the recommended default and is encoded as H.264 video with AAC audio for
broad playback support. Choosing another format requires FFmpeg conversion and can
take longer. MKV is intended for players such as VLC or IINA; QuickTime does not
natively support the MKV container.

## Troubleshooting

If a previously supported page stops working, update yt-dlp first:

```bash
python -m pip install -U --pre "yt-dlp[default,curl-cffi]"
```

Some sites also require browser cookies or authentication. This app does not import
cookies automatically.
