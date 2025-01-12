# importer.py
#
# Copyright 2022-2023 kramo
# Copyright 2023 Geoffrey Coulaud
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

import logging

from gi.repository import Adw, GLib, Gtk

from src import shared
from src.errors.error_producer import ErrorProducer
from src.errors.friendly_error import FriendlyError
from src.game import Game
from src.importer.sources.location import UnresolvableLocationError
from src.importer.sources.source import Source
from src.store.managers.async_manager import AsyncManager
from src.store.pipeline import Pipeline
from src.utils.task import Task


# pylint: disable=too-many-instance-attributes
class Importer(ErrorProducer):
    """A class in charge of scanning sources for games"""

    progressbar = None
    import_statuspage = None
    import_dialog = None
    summary_toast = None

    sources: set[Source] = None

    n_source_tasks_created: int = 0
    n_source_tasks_done: int = 0
    n_pipelines_done: int = 0
    game_pipelines: set[Pipeline] = None

    def __init__(self):
        super().__init__()
        self.game_pipelines = set()
        self.sources = set()

    @property
    def n_games_added(self):
        return sum(
            1 if not (pipeline.game.blacklisted or pipeline.game.removed) else 0
            for pipeline in self.game_pipelines
        )

    @property
    def pipelines_progress(self):
        progress = sum(pipeline.progress for pipeline in self.game_pipelines)
        try:
            progress = progress / len(self.game_pipelines)
        except ZeroDivisionError:
            progress = 0
        return progress

    @property
    def sources_progress(self):
        try:
            progress = self.n_source_tasks_done / self.n_source_tasks_created
        except ZeroDivisionError:
            progress = 0
        return progress

    @property
    def finished(self):
        return (
            self.n_source_tasks_created == self.n_source_tasks_done
            and len(self.game_pipelines) == self.n_pipelines_done
        )

    def add_source(self, source):
        self.sources.add(source)

    def run(self):
        """Use several Gio.Task to import games from added sources"""

        shared.win.get_application().lookup_action("import").set_enabled(False)

        self.create_dialog()

        # Collect all errors and reset the cancellables for the managers
        # - Only one importer exists at any given time
        # - Every import starts fresh
        self.collect_errors()
        for manager in shared.store.managers.values():
            manager.collect_errors()
            if isinstance(manager, AsyncManager):
                manager.reset_cancellable()

        for source in self.sources:
            logging.debug("Importing games from source %s", source.source_id)
            task = Task.new(None, None, self.source_callback, (source,))
            self.n_source_tasks_created += 1
            task.set_task_data((source,))
            task.run_in_thread(self.source_task_thread_func)

        self.progress_changed_callback()

    def create_dialog(self):
        """Create the import dialog"""
        self.progressbar = Gtk.ProgressBar(margin_start=12, margin_end=12)
        self.import_statuspage = Adw.StatusPage(
            title=_("Importing Games…"),
            child=self.progressbar,
        )
        self.import_dialog = Adw.Window(
            content=self.import_statuspage,
            modal=True,
            default_width=350,
            default_height=-1,
            transient_for=shared.win,
            deletable=False,
        )
        self.import_dialog.present()

    def source_task_thread_func(self, _task, _obj, data, _cancellable):
        """Source import task code"""

        source: Source
        source, *_rest = data

        # Early exit if not available or not installed
        if not source.is_available:
            logging.info("Source %s skipped, not available", source.source_id)
            return
        try:
            iterator = iter(source)
        except UnresolvableLocationError:
            logging.info("Source %s skipped, bad location", source.source_id)
            return

        # Get games from source
        logging.info("Scanning source %s", source.source_id)
        while True:
            # Handle exceptions raised when iterating
            try:
                iteration_result = next(iterator)
            except StopIteration:
                break
            except Exception as error:  # pylint: disable=broad-exception-caught
                logging.exception("%s in %s", type(error).__name__, source.source_id)
                self.report_error(error)
                continue

            # Handle the result depending on its type
            if isinstance(iteration_result, Game):
                game = iteration_result
                additional_data = {}
            elif isinstance(iteration_result, tuple):
                game, additional_data = iteration_result
            elif iteration_result is None:
                continue
            else:
                # Warn source implementers that an invalid type was produced
                # Should not happen on production code
                logging.warning(
                    "%s produced an invalid iteration return type %s",
                    source.source_id,
                    type(iteration_result),
                )
                continue

            # Register game
            pipeline: Pipeline = shared.store.add_game(game, additional_data)
            if pipeline is not None:
                logging.info("Imported %s (%s)", game.name, game.game_id)
                pipeline.connect("advanced", self.pipeline_advanced_callback)
                self.game_pipelines.add(pipeline)

    def update_progressbar(self):
        """Update the progressbar to show the overall import progress"""
        # Reserve 10% for the sources discovery, the rest is the pipelines
        self.progressbar.set_fraction(
            (0.1 * self.sources_progress) + (0.9 * self.pipelines_progress)
        )

    def source_callback(self, _obj, _result, data):
        """Callback executed when a source is fully scanned"""
        source, *_rest = data
        logging.debug("Import done for source %s", source.source_id)
        self.n_source_tasks_done += 1
        self.progress_changed_callback()

    def pipeline_advanced_callback(self, pipeline: Pipeline):
        """Callback called when a pipeline for a game has advanced"""
        if pipeline.is_done:
            self.n_pipelines_done += 1
            self.progress_changed_callback()

    def progress_changed_callback(self):
        """
        Callback called when the import process has progressed

        Triggered when:
        * All sources have been started
        * A source finishes
        * A pipeline finishes
        """
        self.update_progressbar()
        if self.finished:
            self.import_callback()

    def import_callback(self):
        """Callback called when importing has finished"""
        logging.info("Import done")
        self.import_dialog.close()
        self.summary_toast = self.create_summary_toast()
        self.create_error_dialog()
        shared.win.get_application().lookup_action("import").set_enabled(True)

    def create_error_dialog(self):
        """Dialog containing all errors raised by importers"""

        # Collect all errors that happened in the importer and the managers
        errors: list[Exception] = []
        errors.extend(self.collect_errors())
        for manager in shared.store.managers.values():
            errors.extend(manager.collect_errors())

        # Filter out non friendly errors
        errors = set(
            tuple(
                (error.title, error.subtitle)
                for error in (
                    filter(lambda error: isinstance(error, FriendlyError), errors)
                )
            )
        )

        # No error to display
        if not errors:
            self.timeout_toast()
            return

        # Create error dialog
        dialog = Adw.MessageDialog()
        dialog.set_heading(_("Warning"))
        dialog.add_response("close", _("Dismiss"))
        dialog.add_response("open_preferences_import", _("Preferences"))
        dialog.set_default_response("open_preferences_import")
        dialog.connect("response", self.dialog_response_callback)
        dialog.set_transient_for(shared.win)

        if len(errors) == 1:
            dialog.set_heading((error := next(iter(errors)))[0])
            dialog.set_body(error[1])
        else:
            # Display the errors in a list
            list_box = Gtk.ListBox()
            list_box.set_selection_mode(Gtk.SelectionMode.NONE)
            list_box.set_css_classes(["boxed-list"])
            list_box.set_margin_top(9)
            for error in errors:
                row = Adw.ActionRow.new()
                row.set_title(error[0])
                row.set_subtitle(error[1])
                list_box.append(row)
            dialog.set_body(_("The following errors occured during import:"))
            dialog.set_extra_child(list_box)

        dialog.present()

    def create_summary_toast(self):
        """N games imported toast"""

        toast = Adw.Toast(timeout=0, priority=Adw.ToastPriority.HIGH)

        if self.n_games_added == 0:
            toast.set_title(_("No new games found"))
            toast.set_button_label(_("Preferences"))
            toast.connect(
                "button-clicked",
                self.dialog_response_callback,
                "open_preferences",
                "import",
            )

        elif self.n_games_added == 1:
            toast.set_title(_("1 game imported"))

        elif self.n_games_added > 1:
            # The variable is the number of games
            toast.set_title(_("{} games imported").format(self.n_games_added))

        shared.win.toast_overlay.add_toast(toast)
        return toast

    def open_preferences(self, page=None, expander_row=None):
        return shared.win.get_application().on_preferences_action(
            page_name=page, expander_row=expander_row
        )

    def timeout_toast(self, *_args):
        """Manually timeout the toast after the user has dismissed all warnings"""
        GLib.timeout_add_seconds(5, self.summary_toast.dismiss)

    def dialog_response_callback(self, _widget, response, *args):
        """Handle after-import dialogs callback"""
        logging.debug("After-import dialog response: %s (%s)", response, str(args))
        if response == "open_preferences":
            self.open_preferences(*args)
        elif response == "open_preferences_import":
            self.open_preferences(*args).connect("close-request", self.timeout_toast)
        else:
            self.timeout_toast()
