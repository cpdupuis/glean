# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""
The main Glean general API.
"""


import atexit
import logging
from pathlib import Path
import shutil
import tempfile
import threading
from typing import Dict, Optional, Set, TYPE_CHECKING


from .config import Configuration
from . import _uniffi
from . import _ffi
from .net import PingUploadWorker
from ._process_dispatcher import ProcessDispatcher
from . import _util


# To avoid cyclical imports, but still make mypy type-checking work.
# See https://mypy.readthedocs.io/en/latest/common_issues.html#import-cycles
if TYPE_CHECKING:
    from .metrics import PingType, RecordedExperiment


log = logging.getLogger("glean")


_ffi.setup_logging()


def _rmtree(path) -> bool:
    """
    A small wrapper around shutil.rmtree to make it runnable on the
    ProcessDispatcher.
    """
    shutil.rmtree(path)
    return True


class OnGleanEventsImpl(_uniffi.OnGleanEvents):
    def __init__(self, glean):
        self.glean = glean

    def on_initialize_finished(self):
        log.debug("OnGleanEventsImpl.on_initialize_finished")
        self.glean._init_finished = True

    def trigger_upload(self):
        log.debug("OnGleanEventsImpl.trigger_upload")
        PingUploadWorker.process(Glean._testing_mode)

    def start_metrics_ping_scheduler(self):
        log.debug("OnGleanEventsImpl.start_metrics_ping_scheduler")

    def cancel_uploads(self):
        log.debug("OnGleanEventsImpl.cancel_uploads")


class Glean:
    """
    The main Glean API.

    Before any data collection can take place, the Glean SDK **must** be
    initialized from the application.

    >>> Glean.initialize(
    ...     application_id="my-app",
    ...     application_version="0.0.0",
    ...     upload_enabled=True,
    ...     data_dir=Path.home() / ".glean",
    ... )
    """

    # Whether Glean was initialized
    _initialized: bool = False
    # Set when `initialize()` returns.
    # This allows to detect calls that happen before `Glean.initialize()` was called.
    # Note: The initialization might still be in progress, as it runs in a separate thread.
    _init_finished: bool = False

    # Are we in testing mode?
    _testing_mode: bool = False

    # The Configuration that was passed to `initialize`
    _configuration: Configuration

    # The directory that Glean stores data in
    _data_dir: Path = Path()

    # Whether Glean "owns" the data directory and should destroy it upon reset.
    _destroy_data_dir: bool = False

    # Keep track of this setting before Glean is initialized
    _upload_enabled: bool = True

    # The ping types, so they can be registered prior to Glean initialization,
    # and saved between test runs.
    _ping_type_queue: Set["PingType"] = set()

    # The application id to send in the ping.
    _application_id: str

    # The version of the application sending Glean data.
    _application_version: str

    # The build identifier generated by the CI system.
    _application_build_id: str

    # A thread lock for Glean operations that need to be synchronized
    _thread_lock = threading.RLock()

    # Simple logging API log level
    _simple_log_level: Optional[int] = None

    @classmethod
    def initialize(
        cls,
        application_id: str,
        application_version: str,
        upload_enabled: bool,
        configuration: Optional[Configuration] = None,
        data_dir: Optional[Path] = None,
        application_build_id: Optional[str] = None,
        log_level: Optional[int] = None,
    ) -> None:
        """
        Initialize the Glean SDK.

        This should only be initialized once by the application, and not by
        libraries using the Glean SDK. A message is logged to error and no
        changes are made to the state if initialize is called a more than
        once.

        Args:
            application_id (str): The application id to use when sending pings.
            application_version (str): The version of the application sending
                Glean data. The meaning of this field is application-specific,
                but it is highly recommended to set this to something
                meaningful.
            upload_enabled (bool): Controls whether telemetry is enabled. If
                disabled, all persisted metrics, events and queued pings
                (except first_run_date) are cleared.
            configuration (glean.config.Configuration): (optional) An object with
                global settings.
            data_dir (pathlib.Path): The path to the Glean data directory.
            application_build_id (str): (optional) The build identifier generated
                by the CI system (e.g. "1234/A").
            log_level (int): (optional) The level of log messages that Glean
                will emit. One of the constants in the Python `logging` module:
                `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. If you need a
                specialized logging configuration, such as to redirecting,
                filtering or reformatting, you should use the Python `logging`
                module's API directly, but that will not affect logging any of
                Glean's networking operations which happen in a subprocess.
                Details in the "Debugging Python applications with the Glean
                SDK" chapter in the docs.
        """
        if log_level is not None:
            cls._simple_log_level = log_level
            logging.basicConfig(level=log_level)

        with cls._thread_lock:
            if cls.is_initialized():
                return

            atexit.register(Glean._reset)

            if configuration is None:
                configuration = Configuration()

            if data_dir is None:
                raise TypeError("data_dir must be provided")
            cls._data_dir = data_dir
            cls._destroy_data_dir = False

            cls._configuration = configuration
            cls._application_id = application_id

            if application_version is None:
                cls._application_version = "Unknown"
            else:
                cls._application_version = application_version

            if application_build_id is None:
                cls._application_build_id = "Unknown"
            else:
                cls._application_build_id = application_build_id

        # FIXME: Require user to pass in build-date
        dt = _uniffi.Datetime(
            year=1970,
            month=1,
            day=1,
            hour=0,
            minute=0,
            second=0,
            nanosecond=0,
            offset_seconds=0,
        )
        client_info = _uniffi.ClientInfoMetrics(
            app_build=cls._application_build_id,
            app_display_version=cls._application_version,
            app_build_date=dt,
            channel=configuration.channel,
            architecture="Unknown",
            os_version="Unknown",
            locale=None,
            device_manufacturer=None,
            device_model=None,
            android_sdk_version=None,
        )
        callbacks = OnGleanEventsImpl(cls)
        cfg = _uniffi.InternalConfiguration(
            data_path=str(cls._data_dir),
            application_id=application_id,
            language_binding_name="Python",
            upload_enabled=upload_enabled,
            max_events=configuration.max_events,
            delay_ping_lifetime_io=False,
            use_core_mps=False,
            app_build=cls._application_build_id,
        )

        _uniffi.glean_initialize(cfg, client_info, callbacks)
        cls._initialized = True

    @classmethod
    def _initialize_with_tempdir_for_testing(
        cls,
        application_id: str,
        application_version: str,
        upload_enabled: bool,
        configuration: Optional[Configuration] = None,
        application_build_id: Optional[str] = None,
    ) -> None:
        """
        Initialize Glean to use a temporary data directory. Use for internal
        unit testing only.

        The temporary directory will be destroyed when Glean is initialized
        again or at process shutdown.
        """

        actual_data_dir = Path(tempfile.TemporaryDirectory().name)
        cls.initialize(
            application_id,
            application_version,
            upload_enabled,
            configuration=configuration,
            data_dir=actual_data_dir,
            application_build_id=application_build_id,
        )
        cls._destroy_data_dir = True

    @_util.classproperty
    def configuration(cls) -> Configuration:
        """
        Access the configuration object to change dynamic parameters.
        """
        return cls._configuration

    @classmethod
    def _reset(cls) -> None:
        """
        Resets the Glean singleton.
        """
        # TODO: 1594184 Send the metrics ping
        log.debug("Resetting Glean")

        # Wait for the subprocess to complete.  We only need to do this if
        # we know we are going to be deleting the data directory.
        if cls._destroy_data_dir and cls._data_dir.exists():
            ProcessDispatcher._wait_for_last_process()

        # Destroy the Glean object.
        # Importantly on Windows, this closes the handle to the database so
        # that the data directory can be deleted without a multiple access
        # violation.
        _uniffi.glean_test_destroy_glean(False)

        _uniffi.glean_set_test_mode(False)
        cls._init_finished = False
        cls._initialized = False
        cls._testing_mode = False

        # Remove the atexit handler or it will get called multiple times at
        # exit.
        atexit.unregister(cls._reset)

        if cls._destroy_data_dir and cls._data_dir.exists():
            # This needs to be run in the same one-at-a-time process as the
            # PingUploadWorker to avoid a race condition. This will block the
            # main thread waiting for all pending uploads to complete, but this
            # only happens during testing when the data directory is a
            # temporary directory, so there is no concern about delaying
            # application shutdown here.
            p = ProcessDispatcher.dispatch(_rmtree, (str(cls._data_dir),))
            p.wait()

    @classmethod
    def is_initialized(cls) -> bool:
        """
        Returns True if the Glean SDK has been initialized.
        """
        return cls._initialized

    @classmethod
    def set_upload_enabled(cls, enabled: bool) -> None:
        """
        Enable or disable Glean collection and upload.

        Metric collection is enabled by default.

        When uploading is disabled, metrics aren't recorded at all and no data
        is uploaded.

        When disabling, all pending metrics, events and queued pings are cleared.

        When enabling, the core Glean metrics are recreated.

        Args:
            enabled (bool): When True, enable metric collection.
        """
        # Changing upload enabled always happens asynchronous.
        # That way it follows what a user expect when calling it inbetween other calls:
        # It executes in the right order.
        #
        # Because the dispatch queue is halted until Glean is fully initialized
        # we can safely enqueue here and it will execute after initialization.
        _uniffi.glean_set_upload_enabled(enabled)

    @classmethod
    def set_experiment_active(
        cls, experiment_id: str, branch: str, extra: Optional[Dict[str, str]] = None
    ) -> None:
        """
        Indicate that an experiment is running. Glean will then add an
        experiment annotation to the environment which is sent with pings. This
        information is not persisted between runs.

        Args:
            experiment_id (str): The id of the active experiment (maximum 100
                bytes)
            branch (str): The experiment branch (maximum 100 bytes)
            extra (dict of str -> str): Optional metadata to output with the
                ping
        """
        map = {} if extra is None else extra
        _uniffi.glean_set_experiment_active(experiment_id, branch, map)

    @classmethod
    def set_experiment_inactive(cls, experiment_id: str) -> None:
        """
        Indicate that the experiment is no longer running.

        Args:
            experiment_id (str): The id of the experiment to deactivate.
        """
        _uniffi.glean_set_experiment_inactive(experiment_id)

    @classmethod
    def test_is_experiment_active(cls, experiment_id: str) -> bool:
        """
        Tests whether an experiment is active, for testing purposes only.

        Args:
            experiment_id (str): The id of the experiment to look for.

        Returns:
            is_active (bool): If the experiement is active and reported in
                pings.
        """
        return _uniffi.glean_test_get_experiment_data(experiment_id) is not None

    @classmethod
    def test_get_experiment_data(cls, experiment_id: str) -> "RecordedExperiment":
        """
        Returns the stored data for the requested active experiment, for testing purposes only.

        Args:
            experiment_id (str): The id of the experiment to look for.

        Returns:
            experiment_data (RecordedExperiment): The data associated with
                the experiment.
        """
        data = _uniffi.glean_test_get_experiment_data(experiment_id)
        if data is not None:
            return data
        else:
            raise RuntimeError("Experiment data is not set")

    @classmethod
    def handle_client_active(cls):
        """
        Performs the collection/cleanup operations required by becoming active.

        This functions generates a baseline ping with reason `active`
        and then sets the dirty bit.
        This should be called whenever the consuming product becomes active (e.g.
        getting to foreground).
        """
        _uniffi.glean_handle_client_active()

    @classmethod
    def handle_client_inactive(cls):
        """
        Performs the collection/cleanup operations required by becoming inactive.

        This functions generates a baseline and an events ping with reason
        `inactive` and then clears the dirty bit.
        This should be called whenever the consuming product becomes inactive (e.g.
        getting to background).
        """
        _uniffi.glean_handle_client_inactive()


__all__ = ["Glean"]
