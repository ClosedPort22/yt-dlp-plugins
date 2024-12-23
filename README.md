# yt-dlp-plugins

Plugins for yt-dlp

## Extractor arguments

#### applemusic, applemusicalbum

- `max_thumbnail_width`: Defaults to `12000`
- `max_thumbnail_height`: Defaults to `12000`
- `thumbnail_extension`: Defaults to `jpg`
- `thumbnail_quality`: Must be an integer between `0` and `999`. Higher is
  better. Defaults to `999`

Note: Lyrics extraction is only possible if you have an active subscription and
pass the `Media-User-Token` header. You can find this token by opening Developer
Tools in your browser and looking for API calls to `amp-api.music.apple.com`.
Keep in mind that lyrics extraction will silently fail if the region code in the
provided URL differs from your account's region setting.

#### disneyplus

- `region`: ISO 3166-2 country code to pass to the API when extracting metadata
- `language`: ISO 639-1 language code to pass to the API when extracting
  metadata
- `playback_scenario`: Specifies the API endpoint to use when extracting formats

## Postprocessor arguments

#### MP4Box

This postprocessor is intended to be used alongside the Apple Music extractors.

- `embed_metadata`: Whether to embed metadata in the file. Specify `mutagen` to
  embed extended metadata using `mutagen`. Embedding is done using `mp4box`
  where possible, so the majority of the metadata fields can still be embedded
  even if `mutagen` is unavailable (see `mp4box.py` for details on which fields
  are embedded and when).
- `embed_thumbnail`: Whether to embed thumbnail in the file. Specify `delete` to
  delete the thumbnail after embedding. You can combine this option with
  `max_thumbnail_*` to control the size of the thumbnail to be embedded.
- `embed_credits`: Whether to embed credits in the MP4 file. This options has no
  effect if `mutagen` is not enabled. The exact names of keys (e.g.
  `Mastering Engineer`, `Songwriter`) depend on the language code you pass via
  the URL (e.g. `?l=en-GB`) and the region's default language, since Apple
  returns localized names. Important note: if a localized key contains non-Latin
  characters, it will be silently ignored.
- `path`: Path to the executable. Defaults to `mp4box`, which will only work if
  `mp4box` is in your `PATH`.

`embed_metadata` and `embed_thumbnail` will be done in the same step if both are
specified.

## Note

Some of the websites listed are known to use DRM. Extractors for these sites are
provided for informational/demonstration purposes only and **will not enable you
to decrypt DRM-protected content**. This project is not affiliated with or
endorsed by the yt-dlp project.

## License

The Unlicense, see `LICENSE.txt` for details
