# Sentry Rewind

A desktop app for viewing Tesla dashcam footage with synchronized multi-camera playback, a calendar view, and a map view.

Works with any Tesla USB drive, just plug it in and open the app.

<img width="1462" height="912" alt="rewind light" src="https://github.com/user-attachments/assets/25c13ef8-ae91-4744-807e-9e68c39c762b" />

## Download

Grab the latest build from [Releases](../../releases). Available for macOS and Windows.

## Features

- 6-camera synchronized playback (front, back, left/right repeater, left/right pillar)
- Click any camera to maximize it
- Sentry clips, saved clips, and drives
- Calendar view to browse by date
- Map view to browse by location
- Event marker showing where the trigger occurred
- Keyboard shortcuts (Space to play/pause, arrow keys to seek, < > to step frames)

## Building from Source

Requires Python 3.10+ and ffmpeg/ffprobe.

```bash
pip install flask pywebview
```

### Run in development

```bash
# Start the Flask server directly
python app.py

# Or use the desktop window
python main.py
```

### Build the .app / .exe

You'll need static ffmpeg and ffprobe binaries in the project root. See [FFMPEG_LICENSE.txt](FFMPEG_LICENSE.txt) for the build configuration used in official releases.

On macOS, build a minimal LGPL ffmpeg from source:

```bash
git clone --depth 1 https://git.ffmpeg.org/ffmpeg.git /tmp/ffmpeg-src
cd /tmp/ffmpeg-src
./configure --enable-static --disable-shared --disable-gpl --disable-nonfree \
  --enable-videotoolbox --enable-audiotoolbox --disable-doc --disable-ffplay \
  --disable-network --disable-encoders --disable-decoders \
  --enable-decoder=h264 --enable-decoder=hevc --enable-decoder=av1 \
  --disable-filters --disable-indevs --disable-outdevs \
  --disable-libxcb --disable-xlib --arch=arm64 --cc=clang
make -j$(sysctl -n hw.ncpu)
cp ffmpeg ffprobe /path/to/sentry-rewind/
```

On Windows, download LGPL static builds from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/).

Then package with PyInstaller:

```bash
pip install pyinstaller

# macOS
pyinstaller --windowed --name "Sentry Rewind" \
  --add-data "static:static" --add-data "FFMPEG_LICENSE.txt:." \
  --add-binary "ffmpeg:." --add-binary "ffprobe:." \
  --hidden-import webview --hidden-import pkg_resources \
  --osx-bundle-identifier com.sentryrewind.app main.py

# Windows
pyinstaller --windowed --name "Sentry Rewind" \
  --add-data "static;static" --add-data "FFMPEG_LICENSE.txt;." \
  --add-binary "ffmpeg.exe;." --add-binary "ffprobe.exe;." \
  --hidden-import webview --hidden-import pkg_resources main.py
```

The output will be in `dist/`.

## License

This project is MIT licensed. See [LICENSE](LICENSE) for details.

FFmpeg is bundled under the LGPL 2.1. See [FFMPEG_LICENSE.txt](FFMPEG_LICENSE.txt).
