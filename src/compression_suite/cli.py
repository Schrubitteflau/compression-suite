"""Console script for compression_suite."""

import typer

from compression_suite.compress_image.cli import compress_image
from compression_suite.doctor.cli import doctor
from compression_suite.extract_unique_frames.cli import extract_unique_frames
from compression_suite.inspect_file.cli import inspect
from compression_suite.preview.cli import preview
from compression_suite.reassemble_video.cli import reassemble_video
from compression_suite.reduce_jpeg_size.cli import reduce_jpeg_size
from compression_suite.utils.contract import JsonUsageErrorCommand

app = typer.Typer()


@app.command()
def version():
    """Display version information."""
    typer.echo("Compression Suite v0.1.0")
    raise typer.Exit()


app.command(cls=JsonUsageErrorCommand)(compress_image)
app.command(cls=JsonUsageErrorCommand)(doctor)
app.command()(extract_unique_frames)
app.command(cls=JsonUsageErrorCommand)(inspect)
app.command(cls=JsonUsageErrorCommand)(preview)
app.command()(reassemble_video)
app.command()(reduce_jpeg_size)


if __name__ == "__main__":
    app()
