from rich.align import Align
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import (BarColumn, Progress, SpinnerColumn, TextColumn,
                           TimeElapsedColumn)
from rich.table import Table

console = Console()


def make_progress(description=""):
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.percentage:>3.0f}%"),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
    )


class PipelineProgress:
    def __init__(self, total_revs, do_processing=True, do_mosaicing=True):
        self.status_messages = {}

        self.do_processing = do_processing
        self.do_mosaicing = do_mosaicing

        self.rev_progress = make_progress()
        self.process_progress = make_progress()
        self.mosaic_progress = make_progress()

        self.status_messages["initialisation"] = "Starting Pipeline..."

        self.rev_task = self.rev_progress.add_task("Generating Revolutions", total=total_revs)

        self.live = Live(self.render(), refresh_per_second=5)

    def start(self):
        self.live.start()

    def stop(self):
        self.live.stop()

    def render(self):
        table = Table.grid(expand=True)

        table.add_row(Panel(Align.center(self.rev_progress), title="Summary"))

        if self.do_processing:
            table.add_row(Panel(Align.center(self.process_progress), title="Processing"))

        if self.do_mosaicing:
            table.add_row(Panel(Align.center(self.mosaic_progress), title="Mosaicking"))

        if self.status_messages:
            status = "\n".join(self.status_messages.values())

            table.add_row(Panel(status, title="Status"))

        return table

    def refresh(self):
        self.live.update(self.render())

    def rev_done(self):
        self.rev_progress.advance(self.rev_task)

    def update_status(self, key, message):
        self.status_messages[key] = message
        self.refresh()

    def clear_status(self, key):
        self.status_messages.pop(key, None)
        self.refresh()

    def wipe_status(self):
        self.status_messages = {}
        self.refresh()

    def reset_processing(self, total):
        if not self.do_processing:
            return

        for task in list(self.process_progress.tasks):
            self.process_progress.remove_task(task.id)

        self.process_progress.add_task("Processing Observations", total=total)

    def reset_mosaic(self, total):
        if not self.do_mosaicing:
            return

        for task in list(self.mosaic_progress.tasks):
            self.mosaic_progress.remove_task(task.id)

        self.mosaic_progress.add_task("Mosaicking Sky Images", total=total)

    def advance_processing(self):
        if not self.do_processing:
            return

        for task in self.process_progress.tasks:
            self.process_progress.advance(task.id)

    def advance_mosaic(self):
        if not self.do_mosaicing:
            return

        for task in self.mosaic_progress.tasks:
            self.mosaic_progress.advance(task.id)
