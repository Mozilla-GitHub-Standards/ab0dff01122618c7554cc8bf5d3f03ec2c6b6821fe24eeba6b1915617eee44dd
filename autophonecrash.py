# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# http://dxr.mozilla.org/mozilla-central/source/testing/mozbase/mozcrash/mozcrash/mozcrash.py
# http://dxr.mozilla.org/mozilla-central/source/build/automation.py.in
# http://dxr.mozilla.org/mozilla-central/source/build/mobile/remoteautomation.py
# http://developer.android.com/training/articles/perf-anr.html

import glob
import logging
import os
import subprocess
import re
import shutil
import sys
import tempfile
from collections import namedtuple

from adb import ADBError
from phonestatus import TestStatus

# Set the logger globally in the file, but this must be reset when
# used in a child process.
LOGGER = logging.getLogger()

TRACES = "/data/anr/traces.txt"
TOMBSTONES = "/data/tombstones"

StackInfo = namedtuple("StackInfo",
                       ["minidump_path",
                        "signature",
                        "stackwalk_stdout",
                        "stackwalk_stderr",
                        "stackwalk_retcode",
                        "stackwalk_errors",
                        "extra"])


class AutophoneCrashProcessor(object):
    def __init__(self, adbdevice, remote_profile_dir, upload_dir, app_name):
        """Initialize an AutophoneCrashProcessor object.

        AutophoneCrashProcessor re-implements several features from
        mozcrash.

        :param adbdevice: instance of ADBDevice used to manage the device.
        :param remote_profile_dir: path on device to the Firefox
            profile.
        :param upload_dir: path to a host directory to be used to contain
            ANR traces, tombstones uploaded from the device.
        :param app_name: name of the application package,
            e.g. org.mozilla.fennec.
        """
        self.adb = adbdevice
        self.remote_profile_dir = remote_profile_dir
        self.upload_dir = upload_dir
        self._dump_files = None
        self.app_name = app_name

    @property
    def remote_dump_dir(self):
        """Minidump directory in Firefox profile."""
        if not self.remote_profile_dir:
            return None
        return os.path.join(self.remote_profile_dir, 'minidumps')

    @property
    def remote_pending_crashreports_dir(self):
        """Pending Crash Reports in the application directory.."""
        return '/data/data/%s/files/mozilla/Crash\\ Reports/pending/' % self.app_name

    def delete_anr_traces(self, root=True):
        """Empty ANR traces.txt file."""
        try:
            self.adb.rm(TRACES, root=root)
            self.adb.shell_output('echo > %s' % TRACES, root=root)
            self.adb.chmod(TRACES, mask='666', root=root)
        except ADBError, e:
            LOGGER.warning("Could not initialize ANR traces %s, %s", TRACES, e)

    def check_for_anr_traces(self, root=True):
        """Reports the ANR traces file from the device.

        Outputs the contents of the ANR traces file to the log and
        creates a copy of the traces file in the upload_dir on the
        host before truncating the contents of the ANR traces file on
        the device.
        """
        if self.adb.exists(TRACES, root=root):
            try:
                t = self.adb.shell_output("cat %s" % TRACES, root=root)
                LOGGER.info("Contents of %s:", TRACES)
                LOGGER.info(t)
                f = open(os.path.join(self.upload_dir, 'traces.txt', 'wb'))
                f.write(t)
                f.close()
                # Once reported, delete traces
                self.delete_anr_traces()
            except ADBError, e:
                LOGGER.warning("Error %s pulling %s", e, TRACES)
            except IOError, e:
                LOGGER.warning("Error %s pulling %s", e, TRACES)
        else:
            LOGGER.info("%s not found", TRACES)

    def delete_tombstones(self, root=True):
        """Deletes any existing tombstone files from device."""
        self.adb.rm(TOMBSTONES, force=True, recursive=True, root=root)

    def delete_crash_dumps(self, root=True):
        """Deletes any existing crash dumps in the Firefox profile."""
        self.adb.rm(os.path.join(self.remote_profile_dir, 'minidumps', '*'),
                    force=True, recursive=True, root=root)

    def clear(self):
        """Delete any existing ANRs, tombstones and crash dumps on the device."""
        self.delete_anr_traces()
        self.delete_tombstones()
        self.delete_crash_dumps()

    def check_for_tombstones(self, root=True):
        """Copies tombstones from the device to the upload_dir before deleting
        them from the device.

        Each copied tombstone filename will be renamed to have a
        unique integer suffix with a .txt extension.
        """
        if self.adb.exists(TOMBSTONES, root=root):
            self.adb.chmod(TOMBSTONES, root=root)
            self.adb.chmod(os.path.join(TOMBSTONES, '*'), mask='666', root=root)
            self.adb.pull(TOMBSTONES, self.upload_dir)
            self.delete_tombstones()
            for f in glob.glob(os.path.join(self.upload_dir, "tombstone_??")):
                for i in xrange(1, sys.maxint):
                    newname = "%s.%d.txt" % (f, i)
                    if not os.path.exists(newname):
                        os.rename(f, newname)
                        LOGGER.debug('AutophoneCrashProcessor.'
                                     'check_for_tombstones: %s', newname)
                        break
        else:
            LOGGER.warning("%s does not exist; tombstone check skipped", TOMBSTONES)

    def get_java_exception(self):
        """Returns a summary of the first fatal Java exception found in
        logcat output.

        Example:
        {
          'reason': 'java-exception',
          'signature': 'java.lang.NullPointerException at org.mozilla.gecko.GeckoApp$21.run(GeckoApp.java:1833)'
        }
        """
        logre = re.compile(r".*\): \t?(.*)")
        exception = None

        logcat = self.adb.get_logcat()

        for i, line in enumerate(logcat):
            # Logs will be of form:
            #
            # 01-30 20:15:41.937 E/GeckoAppShell( 1703): >>> REPORTING UNCAUGHT EXCEPTION FROM THREAD 9 ("GeckoBackgroundThread")
            # 01-30 20:15:41.937 E/GeckoAppShell( 1703): java.lang.NullPointerException
            # 01-30 20:15:41.937 E/GeckoAppShell( 1703): 	at org.mozilla.gecko.GeckoApp$21.run(GeckoApp.java:1833)
            # 01-30 20:15:41.937 E/GeckoAppShell( 1703): 	at android.os.Handler.handleCallback(Handler.java:587)
            if "REPORTING UNCAUGHT EXCEPTION" in line or "FATAL EXCEPTION" in line:
                # Strip away the date, time, logcat tag and pid from the next two lines and
                # concatenate the remainder to form a concise summary of the exception.
                if len(logcat) >= i + 3:
                    exception_type = ''
                    exception_location = ''
                    m = logre.search(logcat[i+1])
                    if m and m.group(1):
                        exception_type = m.group(1)
                    m = logre.search(logcat[i+2])
                    if m and m.group(1):
                        exception_location = m.group(1)
                    if exception_type:
                        exception = {'reason': 'java-exception',
                                     'signature': "%s %s" % (
                                         exception_type, exception_location)}
                else:
                    LOGGER.warning("Automation Error: check_for_java_exceptions: Logcat is truncated!")
                break
        return exception

    def _process_dump_file(self, path, extra, symbols_path, stackwalk_binary):
        """Process a single dump file using stackwalk_binary, and return a
        tuple containing properties of the crash dump.

        :param path: Path to the minidump file to analyse
        :param extra: Path to the extra file to analyse.
        :param symbols_path: Path to the directory containing symbols.
        :param stackwalk_binary: Path to the minidump_stackwalk binary.
        :return: A StackInfo tuple with the fields::
                   minidump_path: Path of the dump file
                   signature: The top frame of the stack trace, or None if it
                              could not be determined.
                   stackwalk_stdout: String of stdout data from stackwalk
                   stackwalk_stderr: String of stderr data from stackwalk or
                                     None if it succeeded
                   stackwalk_retcode: Return code from stackwalk
                   stackwalk_errors: List of errors in human-readable form that prevented
                                     stackwalk being launched.
                   extra: Path of the extra file.
        """
        LOGGER.debug('AutophoneCrashProcessor.'
                     '_process_dump_file: %s %s %s %s',
                     path, extra, symbols_path, stackwalk_binary)
        errors = []
        signature = None
        include_stderr = False
        out = None
        err = None
        retcode = None
        if symbols_path and stackwalk_binary and os.path.exists(stackwalk_binary):
            # run minidump_stackwalk
            p = subprocess.Popen([stackwalk_binary, path, symbols_path],
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
            (out, err) = p.communicate()
            retcode = p.returncode
            if len(out) > 3:
                # minidump_stackwalk is chatty,
                # so ignore stderr when it succeeds.
                # The top frame of the crash is always the line after "Thread N (crashed)"
                # Examples:
                #  0  libc.so + 0xa888
                #  0  libnss3.so!nssCertificate_Destroy [certificate.c : 102 + 0x0]
                #  0  mozjs.dll!js::GlobalObject::getDebuggers() [GlobalObject.cpp:89df18f9b6da : 580 + 0x0]
                #  0  libxul.so!void js::gc::MarkInternal<JSObject>(JSTracer*, JSObject**) [Marking.cpp : 92 + 0x28]
                lines = out.splitlines()
                for i, line in enumerate(lines):
                    if "(crashed)" in line:
                        match = re.search(r"^ 0  (?:.*!)?(?:void )?([^\[]+)", lines[i+1])
                        if match:
                            signature = "@ %s" % match.group(1).strip()
                        break
            else:
                include_stderr = True
        else:
            if not symbols_path:
                errors.append("No symbols path given, can't process dump.")
            if not stackwalk_binary:
                errors.append("MINIDUMP_STACKWALK not set, can't process dump.")
            elif stackwalk_binary and not os.path.exists(stackwalk_binary):
                errors.append("MINIDUMP_STACKWALK binary not found: %s" % stackwalk_binary)

        LOGGER.debug('AutophoneCrashProcessor.'
                     '_process_dump_file: %s %s signature: %s '
                     'stdout: %s stderr: %s return code: %s errors: %s',
                     path, extra, signature, out, err, retcode, errors)

        return StackInfo(path,
                         signature,
                         out,
                         err if include_stderr else None,
                         retcode,
                         errors,
                         extra)

    def get_crashes(self, symbols_path, stackwalk_binary, clean=True, root=True):
        """Returns a list of crash summaries for any crash dumps found on the device.

        Note that the crash dumps are deleted as a side effect.

        :param symbols_path: path on host to the directory
            containing the symbols for the Firefox build being tested.
        :param stackwalk_binary: path on host to the
            minidump_stackwalk binary to be used to parse the dump files.
        :param clean: If True, remove dump files from the device after processing.

        Example:
        [
          {
            'reason': TestStatus.PROCESS_CRASH,
            'signature': 'libmm-color-convertor.so + 0x1232',
            'stackwalk_output': '...',
            'stackwalk_errors': '...'
          },
        ]
        """
        self.check_for_anr_traces()
        self.check_for_tombstones()

        crashes = []
        if not self.remote_dump_dir or \
           not self.adb.is_dir(self.remote_dump_dir, root=root):
            # If crash reporting is enabled (MOZ_CRASHREPORTER=1), the
            # minidumps directory is automatically created when Fennec
            # (first) starts, so its lack of presence is a hint that
            # something went wrong.
            LOGGER.warning("No crash directory (%s) "
                           "found on remote device", self.remote_dump_dir)
            return crashes
        # Create a temporary directory to hold the dump files from the
        # device.  This will allow us to accumulate a number of
        # crashes into the upload directory while ensuring that we
        # only process them once.
        temp_upload_dir = tempfile.mkdtemp()
        self.adb.chmod(self.remote_dump_dir, recursive=True, root=root)
        self.adb.pull(self.remote_dump_dir, temp_upload_dir)
        if clean:
            self.adb.rm(self.remote_dump_dir + "/*", force=True, root=True)
        if self.adb.is_dir(self.remote_pending_crashreports_dir, root=root):
            self.adb.chmod(self.remote_pending_crashreports_dir, recursive=True,
                           root=root)
            self.adb.pull(self.remote_pending_crashreports_dir, temp_upload_dir)
            if clean:
                self.adb.rm(self.remote_pending_crashreports_dir + "/*", force=True, root=True)
        dump_files = [(path, os.path.splitext(path)[0] + '.extra') for path in
                      glob.glob(os.path.join(temp_upload_dir, '*.dmp'))]
        max_dumps = 10
        if len(dump_files) > max_dumps:
            LOGGER.warning("Found %d dump files -- limited to %d!", len(dump_files), max_dumps)
            del dump_files[max_dumps:]
        LOGGER.debug('AutophoneCrashProcessor.dump_files: %s', dump_files)
        for path, extra in dump_files:
            try:
                if os.path.exists(path):
                    shutil.copy(path, self.upload_dir)
            except:
                LOGGER.exception('Attempting to copy %s to upload directory %s',
                                 path, self.upload_dir)
            try:
                if os.path.exists(extra):
                    shutil.copy(extra, self.upload_dir)
            except:
                LOGGER.exception('Attempting to copy %s to upload directory %s',
                                 extra, self.upload_dir)
            info = self._process_dump_file(path, extra, symbols_path, stackwalk_binary)
            stackwalk_output = ["Crash dump filename: %s" % info.minidump_path]
            if info.stackwalk_stderr:
                stackwalk_output.append("stderr from minidump_stackwalk:")
                stackwalk_output.append(info.stackwalk_stderr)
            elif info.stackwalk_stdout is not None:
                stackwalk_output.append(info.stackwalk_stdout)
            if info.stackwalk_retcode is not None and info.stackwalk_retcode != 0:
                stackwalk_output.append("minidump_stackwalk exited with return code %d" %
                                        info.stackwalk_retcode)
            signature = info.signature if info.signature else "unknown top frame"
            LOGGER.info("application crashed [%s]", signature)
            crashes.append(
                {'reason': TestStatus.PROCESS_CRASH,
                 'signature': signature,
                 'stackwalk_output': '\n'.join(stackwalk_output),
                 'stackwalk_errors': '\n'.join(info.stackwalk_errors)})
        try:
            shutil.rmtree(temp_upload_dir)
        except:
            LOGGER.exception('Attempting to remove upload directory %s',
                             temp_upload_dir)
        return crashes

    def get_errors(self, symbols_path, stackwalk_binary, clean=True):
        """Processes ANRs, tombstones and crash dumps on the device and
        returns a list of errors.

        The ANR trace and tombstones are copied from the device to the
        upload_dir before being deleted from the device.

        :param symbols_path: path on host to the directory
            containing the symbols for the Firefox build being tested.
        :param stackwalk_binary: path on host to the
            minidump_stackwalk binary to be used to parse the dump files.
        :param clean: If True, remove dump files from the device after processing.

        :returns: list of error objects. Error object can be of the
        following types:

           Java Exception:
           {
             'reason': 'java-exception',
             'signature': '...'
           }

           Profile Error:
           {
             'reason': TestStatus.TEST_UNEXPECTED_FAIL,
             'signature': 'No crash directory (...) found on remote device'
           }

           Crash:
           {
             'reason': TestStatus.PROCESS_CRASH,
             'signature': signature,
             'stackwalk_output': '...',
             'stackwalk_errors': '...'
           }
        """
        errors = []
        java_exception = self.get_java_exception()
        if java_exception:
            errors.append(java_exception)
        errors.extend(self.get_crashes(symbols_path, stackwalk_binary, clean=clean))
        return errors
