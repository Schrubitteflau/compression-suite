"""Console script for compression_suite."""

import typer

from compression_suite.compress_image.cli import compress_image
from compression_suite.extract_unique_frames.cli import extract_unique_frames
from compression_suite.reassemble_video.cli import reassemble_video
from compression_suite.reduce_jpeg_size.cli import reduce_jpeg_size

app = typer.Typer()


@app.command()
def version():
    """Display version information."""
    typer.echo("Compression Suite v0.1.0")
    raise typer.Exit()


app.command()(compress_image)
app.command()(extract_unique_frames)
app.command()(reassemble_video)
app.command()(reduce_jpeg_size)


if __name__ == "__main__":
    app()
