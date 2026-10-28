# Samples for the compress-image tests

The repo is public: no sample may carry personal data.

- `s23_p3_seft.jpg`: real photo from a Galaxy S23, without anyone on it. ICC profile "DCI-P3 D65 Gamut with sRGB Transfer", Samsung end-of-file trailer (`SEFT`) without `MotionPhoto_Data`. Its GPS was added with exiftool and is fake (12.3456 N, 65.4321 E, 42 m); it carries no serial number and no owner.
- `screenshot.png`: fabricated by `make_screenshot_png.py` (a real capture would show personal content), with the `Software` text block of gnome-screenshot plus a `Comment` block, and no date.
