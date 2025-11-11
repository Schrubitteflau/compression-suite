"""Console script for compression_suite."""

import typer
from rich.console import Console

from compression_suite import utils

app = typer.Typer()
console = Console()


@app.command()
def main():
    """Console script for compression_suite."""
    console.print("Replace this message by putting your code into "
               "compression_suite.cli.main")
    console.print("See Typer documentation at https://typer.tiangolo.com/")
    utils.do_something_useful()


if __name__ == "__main__":
    app()
