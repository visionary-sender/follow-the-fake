# Video features (`ftf.features.video`)

Turns ad videos, screen recordings and saved HLS video segments into keyframes. The still-image
families then read those keyframes, so a video is linked through the same image, text and face
indicators as a screenshot. Speech-to-text is optional.

Run: `ftf features --families video --dataset <dataset.json> --out <dir>` (or as part of all
families). Extractor name: `video@0.1`.

## Method

1. **Which artefacts.** Every artefact of type `video`, or with a video suffix (`.mp4 .mov .webm
   .mkv .avi`). `.ts` files count only when they start with MPEG-TS sync bytes, because
   `.ts` is also TypeScript. Captures marked `duplicate_of` are skipped.
2. **Probe.** `ffprobe` reads duration, resolution and whether there is an audio stream. A file
   with no readable video stream gets no indicators.
3. **Keyframes.** A single ffmpeg pass over the video resampled to 2 fps keeps:
   - the first frame,
   - every scene cut (`scene > 0.3`),
   - a uniform grid of frames, at least 2 s apart, sized so a video gives about 40 grid frames.
     Slow scrolls in screen recordings never score as cuts, so without the grid a 166 s
     recording (case 47) gave a single keyframe.

   Frames whose 64×64 greyscale thumbnail differs from an already kept frame by less than 2 grey
   levels on average are dropped as duplicates. pHash is not used for this: on mostly white pages
   it barely changes while the content does. At most 40 keyframes remain (an evenly spread
   subset). The longest side is scaled down to 1600 px, enough to OCR phone recordings. Keyframes
   are JPEGs in `out_dir/keyframes/<artefact-slug>/t<milliseconds>.jpg`, never next to the raw
   files. They are derived organiser data and never go into git.
4. **Features per keyframe.** Each installed still-image family (`text`, `image`, `faces`) that
   exposes `features_for_image(path)` runs on each keyframe. The indicators are attached to the
   video's capture with `provenance.artefact_id` = the video artefact and
   `provenance.locator = "t=<s>s"`. When the still family set its own locator (for example
   `face=0`), it becomes `t=12.50s;face=0`. A family that is missing or fails on one frame is
   skipped with a note on stderr.
   **Stopgap:** until the image family is installed, the video family emits its own
   64-bit pHash per keyframe (`image_phash`, ImageHash `phash`, hex). It switches itself off as
   soon as `ftf.features.image` imports.
5. **Video metadata (`video_meta`).** For ad videos and HLS segments: `resolution=<w>x<h>`, and
   for whole videos `duration_s=<seconds>`. An ad's length and size are properties of the
   creative and help match re-uploads. Screen recordings (detected by the usual English and Polish screen-recording file-name
   patterns, see `_SCREEN_RECORDING`) describe the analyst's phone, not the scam. Their resolution is
   emitted as the collector kind `screen_size` in the `collector` layer (never linkable) and
   their duration is not emitted. Capture times and recording dates are never emitted.
6. **Speech (`speech_text`, opt-in).** With `FTF_VIDEO_SPEECH=1`, the audio is decoded by our own
   sandboxed ffmpeg call to 16 kHz mono and transcribed with faster-whisper on CPU (int8,
   Polish, VAD filter). The model is `FTF_VIDEO_SPEECH_MODEL` (default
   `Systran/faster-whisper-small`). One indicator per video holds the full transcript, with
   locator `t=0.00-<s>s`. Feeding the transcript into the text family's masking and embeddings
   is left to that family (it can read `speech_text` indicators).

Similarity is exported only as raw inputs (hashes, vectors, texts). The only threshold here
(`DEDUP_DIFF`) decides which frames are worth extracting, not whether two items are linked.

### Hostile input

Videos are untrusted. Every ffmpeg/ffprobe call runs with:
- `stdin` closed (`-nostdin` / DEVNULL),
- a timeout (60 s for a probe, 900 s for an extraction),
- `-protocol_whitelist file` and a container whitelist (`-format_whitelist
  mov,mp4,…,mpegts`), so a playlist renamed to `.mp4` cannot pull in other files or URLs,
- the explicit `file:` input prefix,
- at most 1800 s decoded.

There is a test for the renamed-playlist case.

## Models and dependencies (the `features` extra)

| Package / model | Licence | Source | Download size | Used for |
|---|---|---|---|---|
| ffmpeg / ffprobe (system) | LGPL-2.1+ (Debian build is GPL) | distro package, run as a separate program | ~ | decoding |
| Pillow | MIT-CMU (HPND) | PyPI | ~4 MB | thumbnails |
| ImageHash | BSD-2-Clause | PyPI | small | stopgap `image_phash` |
| faster-whisper 1.2.1 | MIT | PyPI | small; pulls ctranslate2 (MIT), av (BSD-3-Clause), onnxruntime (MIT), tokenizers (Apache-2.0), huggingface_hub (Apache-2.0) | speech |
| `Systran/faster-whisper-base` | MIT (OpenAI Whisper weights) | Hugging Face | 148 MB | speech, faster |
| `Systran/faster-whisper-small` | MIT (OpenAI Whisper weights) | Hugging Face | 486 MB | speech, default |

Whisper weights download on first use into `ftf.features.common.model_dir()/faster-whisper`
(`FTF_MODEL_DIR`). `ftf features prefetch --speech` fetches them ahead of time; with `FTF_OFFLINE=1`
the loader reads only that cache. ffmpeg is called as an external program, not linked, so the copyleft
of the Debian build does not reach our code. A container that wants a permissive build can
use an LGPL-only ffmpeg.

**Container gap:** the current `Dockerfile` installs neither ffmpeg nor the `features` extra.
Without ffmpeg the family logs a note and skips every video, so the pipeline still runs.
Adding both is the demo's job.

## CPU time (cloud VM, e2-standard-8, 4 threads)

| Video | Length, size | Keyframes | Wall | CPU (user+sys) |
|---|---|---|---|---|
| case 5 ad video | 198 s, 500×500 h264 30 fps | 40 | 2.7 s | 7.1 s |
| case 47 screen recording | 166 s, 512×698 h264 30 fps | 33 | 2.6 s | 7.4 s |
| case 14 iPhone screen recording | 249 s, 1170×2532 HEVC 60 fps | 40 | 55 s | 149 s |

Keyframe extraction costs about 2–4 CPU s per video minute for typical ad resolutions. The
outlier is 60 fps HEVC phone recordings, at about 36 CPU s per minute, because every frame has
to be decoded. Still-family costs per keyframe add on top, from their own docs.

Speech (CPU int8, 4 threads, audio of case 5, 198 s):

| Model | CPU s | CPU s per audio minute |
|---|---|---|
| base | 116 | 35 |
| small | 264 | 80 |

Small transcribes Polish visibly better than base (fewer broken words) at 2.3× the CPU.
At about 80 CPU s per audio minute plus a 486 MB download, speech is **opt-in**
(`FTF_VIDEO_SPEECH=1`): the default container run does keyframes only. With `base` it is
35 CPU s per minute and 148 MB. In the coverage run (other workers sharing the VM) the
same 198 s video took 315 CPU s with small.

## Results against the acceptance criteria

Coverage run: the ingestion module's organiser dataset (`outputs/ingest/organiser/dataset.json`,
25 incidents, 31 captures) with `--families video` and speech on (small). Exit 0, 234 s wall,
730 s CPU on <VM>. Outputs (dataset, keyframes, log): `gs://<DATA_BUCKET>/outputs/visual/<run>/coverage/`.

| Case | Video artefacts | Keyframes (`image_phash`) | `video_meta` | `screen_size` (collector) | `speech_text` |
|---|---|---|---|---|---|
| 5 | 1 ad video | 40 | 2 | – | 1 (2,859 chars) |
| 14 | 1 screen recording + 2 HLS segments | 40 + 6 + 6 | 2 (segment resolutions) | 1 | 3 |
| 47 | 1 screen recording | 33 | – | 1 | – (silent track) |

- **The organiser videos produce keyframe features in the contract format:** yes. All five video
  artefacts gave keyframe indicators with `artefact_id` and `t=<s>s` locators, and the dataset
  validates. With text and image installed (rerun over the ingested organiser dataset, output
  in `gs://<DATA_BUCKET>/outputs/visual/<run>/organiser-v2/`), the 5 videos give 820
  indicators: 125 keyframes with `image_phash`, `image_dhash`, `image_pdq` and
  `image_embedding` each, plus 107 `ocr_text`, 96 `text_embedding`, 70 `text_template` and
  41 `keyword`, 4 `video_meta`, and 2 `screen_size` in the collector layer. Faces join when
  the faces work merges.
- **Everything runs on CPU in the container, or is clearly optional:** keyframes are CPU-only
  ffmpeg + Pillow (seconds per video). Speech is opt-in by flag. ffmpeg still has to be added
  to the Dockerfile (see the container gap above).

## Known limits

- Screen recordings are detected by file name. A renamed recording is treated as an ad video,
  which would emit its duration and screen resolution as `video_meta`.
- Keyframes of a screen recording include the browser and OS chrome of the analyst's phone.
  The still families see that too; masking it is left to the image features.
- The `.m3u8` playlists of saved HLS videos are not followed (on purpose). Each `.ts` segment is
  handled as its own short video.
- The scene threshold and grid are tuned on the three organiser videos only.
- Speech: one transcript per video, no per-segment timestamps. Music-only or silent audio
  (case 47 has a silent track, −91 dB) gives no indicator.

## Review notes

- Screen-recording keyframes lose their top 8 % (status bar: clock, date, notifications) before
  the still-image families run, so collector data does not become creative indicators. Browser
  chrome lower down can still be read by OCR.
- Known limits: candidate frames are capped at 400 in time order before deduplication (all
  organiser videos are under 5 minutes, so nothing is cut today); rotation side data is not
  applied to `resolution` / `screen_size`.
- Container: Debian's ffmpeg package is a GPL build. It is called as a separate program, not
  linked, but the demo has two options: ship it, or build an LGPL-only variant.
