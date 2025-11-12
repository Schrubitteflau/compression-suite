# Compression Suite

Automatically chooses the best compression and processing pipeline based on the content: video, audio, slides, or images. No expertise required!

-   Free software: MIT License

The main idea is to build a content-aware multimedia compression and processing toolbox, fully configurable through human-readable workflows and pipelines defined in formats like YAML.

# Core concepts

There are two core concepts:

-   Workflows, which describe when and in what order different modules should be executed
-   Modules, which are small units of logic with a single purpose:
    -   detect the type of content
    -   extract images from a video,
    -   find the best lossy compression settings for an image while ensuring that no visible differences remain

Each module can rely on external tools or existing software instead of reinventing the wheel. The goal is to abstract away the manual chaining of commands that users would normally do by hand:

-   `ffmpeg` (video/audio processing)
-   `imagemagick` (image conversion)
-   `sox` / `pydub` (audio)
-   custom scripts

Not clear? Let's see an example.

# Slides presentation

Imagine you've attended an online presentation on a topic that you're really interested in. After the session, you receive a replay/download link so you can watch it again later, in case you missed something. Actually the presentation was sooo interesting that you plan on keeping the file locally. Great, until you realize the video file is 3 GB for only 1 hour and a half. When you think about it, the file mostly consists of a few dozen static images (the slides), and the audio track. So why store a massive video when the content could be represented much more efficiently?

A possible workflow for this use case might look like this.

## Video track pipeline:

1. Extract every distinct frame, optionally cropping parts of the image depending on how the video was recorded (for example, removing the UI of the video-conferencing tool)

2. For each extracted frame, run a module that removes the laser pointer/mouse cursor (using AI, of course!!), but stores its position separately so it can be reconstructed later if needed

3. Detect the webcam area showing the presenter (if not already cropped out). Freeze that section so it becomes a static image across the entire presentation

4. Identify which frames are actually unique slides and annotate them with their timestamps

5. Optionally compress the resulting images for even more gain

6. Rebuild the video track using only the unique slides and the recorded timestamps, drastically reducing the number of frames and therefore the file size :D

### Room for optimizations

There are many opportunities to make each process more efficient. Take the frame-extraction stage, for example.

If the content type is known to be a slide presentation, it may not be necessary to extract every single frame. Instead of processing each of the 60 frames per second, we could sample every 12th frame (or any other ratio). If the user accepts a small loss of synchronization accuracy, the slide transition might occur within at most 11 frames of the detected moment. For a presenter advancing slides manually, that trade-off can drastically improve performance.

Another idea would be to use a dichotomy strategy to detect slide changes. As long as the presenter does not go backward to a previous slide, this would efficiently pinpoint change moments with very few frame reads. The downside is determining the exact timestamps of each slide, which may make this approach less accurate for reconstruction. However, if the goal is simply to extract all unique slides as images, without caring about the timeline, this method could be extremely fast.

To sum up, the more the workflow understands the expected content, the more opportunities exist to optimize it :D

## And what about the audio track?

Since presentations typically involve speech only, encoding the audio using Opus (around 40 kbps) can produce a huge reduction in size with virtually no audible loss in quality.

All of these steps, and their order, can be described in a workflow.

# Wait, that's ambitous

Build a modular toolbox that is flexible enough to cover the majority of real-world use cases, while also supporting lossless compression workflows and algorithms. Quality verification is central: users should not have to manually check whether compression introduced perceptible degradation. Instead, the system should automatically measure quality and ensure any loss stays within a user-authorized margin.

With this approach, a near-optimal workflow could exist for every use case. With additional engineering, the system could even automatically detect the media type and apply the most appropriate workflow without human intervention.

## Features

As of now, this project is more an idea than anything else. No workflows, no smart adaptation based on the file content, no parameterization, only a single script that currently aims to perform that Slides presentation example, in a simpler way: extract frames, deduplicate, rebuild the video track, and compress the audio in Opus.

Voilà 😉

## Credits

This package was created with [Cookiecutter](https://github.com/audreyfeldroy/cookiecutter) and the [audreyfeldroy/cookiecutter-pypackage](https://github.com/audreyfeldroy/cookiecutter-pypackage) project template.
